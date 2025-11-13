#!/usr/bin/env python3
# runner.py
#
# Drives xv6 + QEMU and acts as the glue between:
#   * xv6’s scheduler logs (SCHED_LOG blocks over serial)
#   * llmhelper running inside xv6
#   * an external LLM agent writing advice into shared/llm_advice.txt
#
# Core responsibilities:
#   - Ensure xv6 is built and up-to-date
#   - Launch QEMU with two serial channels:
#         serial0 → PTY (bi-directional xv6 console)
#         serial1 → QEMU monitor on host stdio
#   - Auto-discover the PTY used by serial0
#   - Reader thread:
#         streams console output to host,
#         detects the shell prompt,
#         extracts *only* complete SCHED_LOG blocks,
#         dedupes them by TIMESTAMP,
#         and appends them to shared/sched_log.txt
#   - Boot thread:
#         waits for the xv6 shell to appear, then runs llmhelper
#   - Relay thread:
#         tails llm_advice.txt from the host side,
#         filters & dedupes ADVICE:PID=... lines,
#         and injects them into xv6 via the PTY
#
# Environment overrides:
#   QEMU=…, SMP=…
#
# Expected project structure:
#   repo/
#     runner.py
#     shared/{sched_log.txt, llm_advice.txt}
#     xv6/{kernel/kernel, fs.img, ...}

import os
import sys
import time
import re
import select
import subprocess
import threading
import shutil
from pathlib import Path
from collections import deque
from typing import Generator, Optional

#### Paths and Environment ####
# Resolve important paths once, relative to repo root
ROOT   = Path(__file__).resolve().parent
XV6    = ROOT / "xv6"
SHARED = ROOT / "shared"

# xv6 build outputs
KERNEL = XV6 / "kernel" / "kernel"
FSIMG  = XV6 / "fs.img"

# Files used for host↔agent communication
LOG_FILE        = SHARED / "sched_log.txt"   # appended with SCHED_LOG blocks
ADVICE_FILE     = SHARED / "llm_advice.txt"  # tailed by relay thread
SERIAL_TTY_FILE = SHARED / "serial_tty"      # last PTY path for debugging/tools

# QEMU configuration (allow environment override for CI/dev)
QEMU_EXE = os.environ.get("QEMU", "qemu-system-riscv64")
SMP      = os.environ.get("SMP", "1")

# QEMU command line: RISCV virt machine, no BIOS, xv6 kernel+fs.img, 2 serial ports
QEMU_CMD = [
    QEMU_EXE,
    "-machine", "virt",
    "-bios", "none",
    "-kernel", str(KERNEL),
    "-m", "128M",
    "-smp", str(SMP),
    "-nographic",
    "-global", "virtio-mmio.force-legacy=false",
    "-drive", f"file={FSIMG},if=none,format=raw,id=x0",
    "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0",
    "-serial", "pty",          # serial0 → PTY for xv6 console I/O
    "-serial", "mon:stdio",    # serial1 → QEMU monitor on host stdin/out
]

# Patterns for detecting shell readiness and SCHED_LOG timestamps
PROMPTS    = [re.compile(r"\binit: starting sh\b"), re.compile(r"(^|\n)\$ $")]
PTY_PATTERN = re.compile(r"(/dev/pts/\d+)")
TS_PATTERN  = re.compile(r"^\s*TIMESTAMP:(\d+)\s*$", re.M | re.S)

#### Build xv6 if Needed ####
def run_make(*targets: str) -> int:
    return subprocess.call(["make", "-C", str(XV6), *targets])

def ensure_build():
    """Make sure kernel and fs.img exist and rebuild if stale."""
    if not (KERNEL.exists() and FSIMG.exists()):
        print("[runner] Building xv6 (first build or missing outputs)...")
        if run_make("kernel", "fs.img") != 0:
            sys.exit("[runner] Build failed")

    # make -q returns 1 if target is out of date
    stale_kernel = subprocess.call(["make", "-q", "-C", str(XV6), "kernel"])
    stale_fs     = subprocess.call(["make", "-q", "-C", str(XV6), "fs.img"])
    if stale_kernel == 1 or stale_fs == 1:
        print("[runner] Rebuilding stale artifacts...")
        if run_make("kernel", "fs.img") != 0:
            sys.exit("[runner] Build failed")

#### Prepare shared files/dirs ####
def ensure_files():
    """Ensure QEMU exists, create shared/, and reset log file."""
    if not shutil.which(QEMU_EXE):
        sys.exit(f"[runner] QEMU not found: {QEMU_EXE}")
    SHARED.mkdir(exist_ok=True)

    # sched_log.txt is rewritten every run; the LLM agent reads it fresh
    LOG_FILE.write_text("", encoding="utf-8")

    # llm_advice.txt must exist for the relay thread but is not truncated
    ADVICE_FILE.touch()

#### Tail a file (like tail -f) ####
def tail_file(path: Path, stop: threading.Event, start_at_end: bool) -> Generator[str, None, None]:
    """
    Yield ONLY new lines appended to a file.
    start_at_end=True means “don’t replay existing content”.
    """
    size = path.stat().st_size if (start_at_end and path.exists()) else 0
    while not stop.is_set():
        try:
            cur = path.stat().st_size
            if cur < size:
                # File truncated; restart from 0
                size = 0
            if cur > size:
                with path.open("r", encoding="utf-8", errors="ignore") as f:
                    f.seek(size)
                    for line in f:
                        yield line.rstrip("\n")
                    size = f.tell()
        except FileNotFoundError:
            pass
        time.sleep(0.2)

#### Discover QEMU PTY ####
def discover_pty(proc: subprocess.Popen, timeout: float = 10.0) -> Optional[str]:
    """
    QEMU prints “char device redirected to /dev/pts/X”.
    Extract that path from stdout/stderr first; fall back to scanning /proc/<pid>/fd.
    """
    start = time.time()
    fds = []
    if proc.stdout: fds.append(proc.stdout.fileno())
    if proc.stderr: fds.append(proc.stderr.fileno())

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

            # mirror monitor output to stderr for debugging
            sys.stderr.write(chunk)

            m = PTY_PATTERN.search(chunk)
            if m:
                return m.group(1)

    # Fallback: QEMU sometimes opens PTY before printing the path
    try:
        for entry in Path(f"/proc/{proc.pid}/fd").iterdir():
            try:
                target = os.readlink(str(entry))
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

    # Launch QEMU with raw stdout/stderr pipes. PTY discovery depends on this.
    q = subprocess.Popen(
        QEMU_CMD,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,   # binary mode for PTY detection
        bufsize=0,
    )

    # Resolve the PTY used by serial0
    serial_tty = discover_pty(q, timeout=10.0)
    if not serial_tty:
        try: q.terminate()
        except Exception: pass
        sys.exit("[runner] Could not discover QEMU serial PTY.")

    print(f"[runner] Serial PTY: {serial_tty}")
    SERIAL_TTY_FILE.write_text(serial_tty)

    # Open PTY for bi-directional communication
    try:
        tty = open(serial_tty, "r+b", buffering=0)
    except Exception as e:
        try: q.terminate()
        except Exception: pass
        sys.exit(f"[runner] Failed to open PTY {serial_tty}: {e}")

    # Thread coordination
    lock  = threading.Lock()   # serialize PTY writes
    stop  = threading.Event()  # graceful shutdown signal
    shell = threading.Event()  # set once xv6 shell prompt detected

    last_ts_written = {"ts": -1}  # dedupe key for SCHED_LOG blocks
    recent_advice   = deque(maxlen=64)

    #### reader thread: mirror console, detect logs, detect prompt ####
    def read_pty():
        console_buf = ""   # for prompt detection
        parse_buf   = ""   # for SCHED_LOG block assembly

        with LOG_FILE.open("a", encoding="utf-8", errors="ignore") as out:
            while not stop.is_set():
                try:
                    chunk = tty.read(4096)
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    text = chunk.decode("utf-8", errors="ignore")

                    # stream live console output
                    sys.stdout.write(text)

                    # detect shell prompt
                    console_buf += text
                    if any(p.search(console_buf) for p in PROMPTS):
                        shell.set()
                    console_buf = console_buf[-4096:]

                    # accumulate console output and extract full SCHED_LOG blocks
                    parse_buf += text
                    while True:
                        start_idx = parse_buf.find("SCHED_LOG_START")
                        if start_idx == -1:
                            parse_buf = parse_buf[-4096:]
                            break

                        end_idx = parse_buf.find("SCHED_LOG_END", start_idx)
                        if end_idx == -1:
                            parse_buf = parse_buf[start_idx:]
                            break

                        # Extract block including its trailing newline if present
                        end_cut = end_idx + len("SCHED_LOG_END")
                        nl_pos  = parse_buf.find("\n", end_cut)
                        block   = parse_buf[start_idx:(nl_pos+1 if nl_pos != -1 else end_cut)]

                        # Remove this block from the rolling buffer
                        parse_buf = parse_buf[(nl_pos+1 if nl_pos != -1 else end_cut):]

                        # Deduplicate using TIMESTAMP
                        m = TS_PATTERN.search(block)
                        ts = int(m.group(1)) if m else -1
                        if ts == -1 or ts == last_ts_written["ts"]:
                            continue

                        # Append the block to sched_log.txt
                        out.write(block if block.endswith("\n") else (block + "\n"))
                        out.flush()
                        try:
                            os.fsync(out.fileno())
                        except Exception:
                            pass

                        last_ts_written["ts"] = ts
                except Exception:
                    break

    #### write helper: send one line safely to xv6 ####
    def write_line(line: str):
        data = (line.rstrip() + "\n").encode("utf-8", errors="ignore")
        with lock:
            try:
                tty.write(data)
                tty.flush()
            except Exception:
                pass

    #### boot llmhelper once shell prompt appears ####
    def boot_llmhelper():
        if not shell.wait(15):
            print("[runner] Shell prompt not detected.")
            return
        print("[runner] Launching llmhelper...\n")
        write_line("llmhelper")

    #### relay: feed new ADVICE lines from host to xv6 ####
    def relay_advice():
        shell.wait()  # ensure xv6 console is active
        for line in tail_file(ADVICE_FILE, stop, start_at_end=True):
            s = line.strip()
            if not s:
                continue
            if not s.startswith("ADVICE:PID="):
                continue
            if s in recent_advice:
                continue

            recent_advice.append(s)
            print(f"[runner] >> {s}")
            write_line(s)

    # Start all worker threads
    t_read  = threading.Thread(target=read_pty,       daemon=True, name="read-pty")
    t_boot  = threading.Thread(target=boot_llmhelper, daemon=True, name="boot-llmhelper")
    t_relay = threading.Thread(target=relay_advice,   daemon=True, name="relay-advice")

    t_read.start()
    t_boot.start()
    t_relay.start()

    # Wait until QEMU exits or user interrupts
    try:
        rc = q.wait()
    except KeyboardInterrupt:
        print("\n[runner] Stopping QEMU...")
        try: q.terminate()
        except Exception: pass
        try: q.wait(timeout=3)
        except Exception: pass
        rc = q.returncode
    finally:
        stop.set()
        try: tty.close()
        except Exception: pass
        try:
            if q.stdout: q.stdout.close()
            if q.stderr: q.stderr.close()
        except Exception:
            pass

        if rc not in (0, None):
            print(f"\n[runner] QEMU exited with error code {rc}\n")

#### Run ####
if __name__ == "__main__":
    main()
