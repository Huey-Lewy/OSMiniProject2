#!/usr/bin/env python3
# runner.py
# Build and launch xv6 under QEMU with a duplex serial PTY.
# - Discovers the PTY reliably (stdout/stderr scan + /proc fallback)
# - Streams console from the PTY and tees to shared/sched_log.txt
# - Boots llmhelper automatically once the shell is ready
# - Forwards lines appended to shared/llm_advice.txt into the PTY (stdin of xv6)

import os
import sys
import time
import re
import select
import subprocess
import threading
import shutil
from pathlib import Path
from typing import Generator, Optional

#### Paths and Environment ####
ROOT   = os.path.dirname(os.path.abspath(__file__))
XV6    = os.path.join(ROOT, "xv6")
SHARED = os.path.join(ROOT, "shared")

KERNEL = os.path.join(XV6, "kernel", "kernel")
FSIMG  = os.path.join(XV6, "fs.img")

LOG_FILE         = os.path.join(SHARED, "sched_log.txt")
ADVICE_FILE      = os.path.join(SHARED, "llm_advice.txt")
SERIAL_TTY_FILE  = os.path.join(SHARED, "serial_tty")

QEMU_EXE = os.environ.get("QEMU", "qemu-system-riscv64")
SMP      = os.environ.get("SMP", "1")

QEMU_CMD = [
    QEMU_EXE,
    "-machine", "virt",
    "-bios", "none",
    "-kernel", KERNEL,
    "-m", "128M",
    "-smp", SMP,
    "-nographic",                              # use serial console
    "-global", "virtio-mmio.force-legacy=false",
    "-drive", f"file={FSIMG},if=none,format=raw,id=x0",
    "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0",
    "-serial", "pty",                          # serial0 -> PTY (xv6 console, duplex)
    "-serial", "mon:stdio",                    # serial1 -> QEMU monitor on stdio (ensures discoverable output)
]

# Markers that indicate the shell is up
PROMPTS = [
    re.compile(r"\binit: starting sh\b"),
    re.compile(r"(^|\n)\$ $"),
]


#### Build xv6 if Needed ####
def ensure_build():
    """Build kernel/fs if missing or stale."""
    if not (os.path.exists(KERNEL) and os.path.exists(FSIMG)):
        print("[runner] Building xv6...")
        if subprocess.call(["make", "-C", XV6, "kernel", "fs.img"]) != 0:
            sys.exit("[runner] Build failed")

    # Rebuild if stale
    stale_kernel = subprocess.call(["make", "-q", "-C", XV6, "kernel"])
    stale_fs     = subprocess.call(["make", "-q", "-C", XV6, "fs.img"])
    if stale_kernel or stale_fs:
        print("[runner] Rebuilding stale artifacts...")
        if subprocess.call(["make", "-C", XV6, "kernel", "fs.img"]) != 0:
            sys.exit("[runner] Build failed")


#### Prepare Shared Paths ####
def ensure_files():
    """Ensure shared folder and log files exist."""
    if not shutil.which(QEMU_EXE):
        sys.exit(f"[runner] QEMU not found: {QEMU_EXE}")

    os.makedirs(SHARED, exist_ok=True)
    # Fresh log (tee will append)
    open(LOG_FILE, "w").close()
    # Advice file may be tailed by tooling; ensure it exists
    open(ADVICE_FILE, "a").close()


#### Tail a File (like `tail -f`) ####
def tail_file(path: str, stop: threading.Event) -> Generator[str, None, None]:
    """Yield new lines appended to a file."""
    size = 0
    while not stop.is_set():
        try:
            cur = os.path.getsize(path)
            if cur < size:
                size = 0  # log was truncated
            if cur > size:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    f.seek(size)
                    for line in f:
                        yield line.rstrip("\n")
                    size = f.tell()
        except FileNotFoundError:
            pass
        time.sleep(0.2)


#### Read QEMU stdio to discover the PTY path ####
PTY_PATTERN = re.compile(r"(/dev/pts/\d+)")

def discover_pty(proc: subprocess.Popen, timeout: float = 10.0) -> Optional[str]:
    """
    Watch QEMU stdout/stderr for a '/dev/pts/N' line.
    If not seen, fallback to scanning /proc/<pid>/fd for a pts handle.
    """
    start = time.time()

    # File descriptors to watch
    fds = []
    if proc.stdout: fds.append(proc.stdout.fileno())
    if proc.stderr: fds.append(proc.stderr.fileno())

    # Non-blocking read loop
    while time.time() - start < timeout:
        if proc.poll() is not None:
            break
        if not fds:
            break
        r, _, _ = select.select(fds, [], [], 0.2)
        for fd in r:
            try:
                chunk = os.read(fd, 4096).decode("utf-8", "ignore")
            except Exception:
                continue
            # Echo for visibility
            sys.stderr.write(chunk)
            m = PTY_PATTERN.search(chunk)
            if m:
                return m.group(1)

    # Fallback: scan /proc/<pid>/fd for a pts link
    try:
        fd_dir = f"/proc/{proc.pid}/fd"
        for name in os.listdir(fd_dir):
            try:
                target = os.readlink(os.path.join(fd_dir, name))
                m = PTY_PATTERN.search(target)
                if m:
                    return m.group(1)
            except Exception:
                continue
    except Exception:
        pass

    return None


#### Main ####
def main():
    ensure_build()
    ensure_files()

    print("====== xv6 Runner ======")
    print(f"[runner] Kernel: {KERNEL}")
    print(f"[runner]  FS  : {FSIMG}")
    print(f"[runner] QEMU : {' '.join(QEMU_CMD)}\n")

    # Launch QEMU; we do not use its stdin for console (console is PTY).
    q = subprocess.Popen(
        QEMU_CMD,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,              # capture stdout (monitor on stdio)
        stderr=subprocess.PIPE,              # capture stderr (some builds print PTY here)
        text=False,                          # raw bytes for robust decoding
        bufsize=0,
    )

    # Discover PTY path printed by QEMU (e.g., "/dev/pts/7")
    serial_tty = discover_pty(q, timeout=10.0)
    if not serial_tty:
        try:
            q.terminate()
        except Exception:
            pass
        sys.exit("[runner] Could not discover QEMU serial PTY. Aborting.")

    print(f"[runner] Serial PTY: {serial_tty}")
    Path(SERIAL_TTY_FILE).write_text(serial_tty)

    # Open the PTY for duplex I/O
    tty = open(serial_tty, "r+b", buffering=0)

    lock = threading.Lock()
    stop = threading.Event()
    shell = threading.Event()

    #### Stream console from PTY and tee to LOG_FILE ####
    def read_pty():
        buf = ""
        with open(LOG_FILE, "a", encoding="utf-8", errors="ignore") as log:
            while not stop.is_set():
                try:
                    chunk = tty.read(4096)  # bytes
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    text = chunk.decode("utf-8", errors="ignore")
                    sys.stdout.write(text)   # live console on host stdout
                    log.write(text)         # persist to shared/sched_log.txt
                    log.flush()

                    # Detect shell
                    buf += text
                    if any(p.search(buf) for p in PROMPTS):
                        shell.set()
                    buf = buf[-4000:]
                except Exception:
                    break

    #### Write to PTY (send a line to xv6 stdin) ####
    def write_line(cmd: str):
        data = (cmd.rstrip() + "\n").encode("utf-8", errors="ignore")
        with lock:
            try:
                tty.write(data)
                tty.flush()
            except Exception:
                pass

    #### Boot llmhelper once the shell is ready ####
    def boot_llmhelper():
        if not shell.wait(10):
            print("[runner] Shell prompt not detected. Boot likely failed.\n")
            return
        print("[runner] Launching llmhelper...\n")
        write_line("llmhelper")

    #### Forward lines from shared/llm_advice.txt into PTY ####
    def relay_advice():
        shell.wait()  # ensure xv6 is responsive first
        for line in tail_file(ADVICE_FILE, stop):
            s = line.strip()
            if not s:
                continue
            print(f"[runner] >> {s}")
            write_line(s)

    # Threads
    t_read  = threading.Thread(target=read_pty,       daemon=True)
    t_boot  = threading.Thread(target=boot_llmhelper, daemon=True)
    t_relay = threading.Thread(target=relay_advice,   daemon=True)

    t_read.start()
    t_boot.start()
    t_relay.start()

    # Wait for QEMU to exit; allow Ctrl-C to terminate cleanly
    try:
        q.wait()
    except KeyboardInterrupt:
        print("\n[runner] Stopping QEMU...")
        try:
            q.terminate()
        except Exception:
            pass
    finally:
        stop.set()
        try:
            tty.close()
        except Exception:
            pass
        # drain child pipes
        try:
            if q.stdout: q.stdout.close()
            if q.stderr: q.stderr.close()
        except Exception:
            pass

        rc = q.returncode
        if rc not in (0, None):
            print(f"\n[runner] QEMU exited with error code {rc}\n")


#### Run ####
if __name__ == "__main__":
    main()
