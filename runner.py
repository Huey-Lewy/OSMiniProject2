#!/usr/bin/env python3
import os, sys, time, subprocess, threading, shutil, re

ROOT   = os.path.dirname(os.path.abspath(__file__))
XV6    = os.path.join(ROOT, "xv6")
SHARED = os.path.join(ROOT, "shared")

KERNEL = os.path.join(XV6, "kernel", "kernel")
FSIMG  = os.path.join(XV6, "fs.img")
LOG_PATH    = os.path.join(SHARED, "sched_log.txt")
ADVICE_PATH = os.path.join(SHARED, "llm_advice.txt")

QEMU_EXE = os.environ.get("QEMU", "qemu-system-riscv64")
SMP      = os.environ.get("SMP", "1")

QEMU_CMD = [
    QEMU_EXE,
    "-machine", "virt",
    "-bios", "none",
    "-kernel", KERNEL,
    "-m", "128M",
    "-smp", SMP,
    "-nographic",
    "-global", "virtio-mmio.force-legacy=false",
    "-drive", f"file={FSIMG},if=none,format=raw,id=x0",
    "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0",
]

PROMPT_PATTERNS = [
    re.compile(r"\binit: starting sh\b"),
    re.compile(r"(^|\n)\$ $"),        # xv6 sh prompt: "$ "
]

def run_make_if_needed():
    # If artifacts are missing, or make -q says stale, rebuild.
    missing = (not os.path.exists(KERNEL)) or (not os.path.exists(FSIMG))
    if missing:
        print("[runner] building xv6 artifacts (fs.img, kernel)...")
        rc = subprocess.call(["make", "-C", XV6, "fs.img", "kernel"])
        if rc != 0:
            print("[runner] build failed")
            sys.exit(1)
        return

    # Make quiet check: 0=up-to-date, 1=needs build
    q1 = subprocess.call(["make", "-q", "-C", XV6, "kernel"])
    q2 = subprocess.call(["make", "-q", "-C", XV6, "fs.img"])
    if q1 != 0 or q2 != 0:
        print("[runner] artifacts stale, rebuilding...")
        rc = subprocess.call(["make", "-C", XV6, "fs.img", "kernel"])
        if rc != 0:
            print("[runner] build failed")
            sys.exit(1)

def ensure_paths():
    if shutil.which(QEMU_EXE) is None:
        print(f"[runner] cannot find QEMU: {QEMU_EXE}")
        sys.exit(1)
    os.makedirs(SHARED, exist_ok=True)
    # Start with clean files; agent tails LOG, and appends ADVICE
    open(LOG_PATH, "w").close()
    open(ADVICE_PATH, "a").close()  # don't truncate; agent may already be writing

def follow_file(path, stop_evt, poll=0.2):
    """
    Generator that yields lines appended to 'path' (like tail -f).
    Handles truncation/rotation.
    """
    last_size = 0
    while not stop_evt.is_set():
        try:
            st = os.stat(path)
            size = st.st_size
            if size < last_size:
                # truncated/rotated
                last_size = 0
            if size > last_size:
                with open(path, "r") as f:
                    f.seek(last_size)
                    chunk = f.read()
                    last_size = f.tell()
                # splitlines(True) keeps trailing '\n' if present
                buf = ""
                for part in chunk.splitlines(True):
                    buf += part
                    if buf.endswith("\n"):
                        yield buf.rstrip("\n")
                        buf = ""
                # keep any partial line buffered until completed later
                if buf:
                    # don't emit partials; wait for newline on next pass
                    pass
        except FileNotFoundError:
            pass
        time.sleep(poll)

def main():
    run_make_if_needed()
    ensure_paths()

    print(f"[runner] kernel : {KERNEL}")
    print(f"[runner] fs.img : {FSIMG}")
    print(f"[runner] qemu   : {' '.join(QEMU_CMD)}")

    # Launch QEMU
    q = subprocess.Popen(
        QEMU_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )

    stdin_lock = threading.Lock()
    stop_evt = threading.Event()
    shell_ready_evt = threading.Event()

    def tee_stdout():
        # Single reader of q.stdout; also detect shell readiness
        with open(LOG_PATH, "a", buffering=1) as lf:
            partial = ""
            while True:
                line = q.stdout.readline()
                if not line:
                    break
                sys.stdout.write(line)
                lf.write(line)

                # detect shell readiness in the cumulative stream
                partial += line
                # check patterns
                for pat in PROMPT_PATTERNS:
                    if pat.search(partial):
                        shell_ready_evt.set()
                        break
                # keep partial buffer bounded
                if len(partial) > 4096:
                    partial = partial[-2048:]

    def send_line(s):
        with stdin_lock:
            try:
                if not s.endswith("\n"):
                    s += "\n"
                q.stdin.write(s)
                q.stdin.flush()
            except BrokenPipeError:
                pass

    def auto_start_llmhelper():
        # Wait for shell prompt, then start llmhelper (foreground)
        if not shell_ready_evt.wait(timeout=10):
            print("[runner] warning: shell prompt not detected; sending 'llmhelper' anyway.")
        print("[runner] starting llmhelper in xv6...")
        send_line("llmhelper")

    def forward_advice():
        # Tail advice file; forward each new line to QEMU stdin.
        for line in follow_file(ADVICE_PATH, stop_evt):
            line = line.strip()
            if not line:
                continue
            # Each line is expected like: ADVICE:PID=<n>
            print(f"[runner] >> advice: {line}")
            send_line(line)

    t_out  = threading.Thread(target=tee_stdout, daemon=True)
    t_boot = threading.Thread(target=auto_start_llmhelper, daemon=True)
    t_adv  = threading.Thread(target=forward_advice, daemon=True)

    t_out.start()
    t_boot.start()
    t_adv.start()

    try:
        rc = q.wait()
        stop_evt.set()
        t_out.join(timeout=1)
        t_adv.join(timeout=1)
        sys.exit(rc)
    except KeyboardInterrupt:
        print("\n[runner] Ctrl-C: terminating QEMU...")
        stop_evt.set()
        try:
            q.terminate()
        except Exception:
            pass
        try:
            q.wait(timeout=2)
        except Exception:
            q.kill()

if __name__ == "__main__":
    main()
