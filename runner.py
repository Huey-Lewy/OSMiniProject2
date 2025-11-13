#!/usr/bin/env python3
# runner.py
#
# Drives xv6 + QEMU and acts as the glue between:
# * xv6’s scheduler logs (SCHED_LOG blocks over console)
# * llmhelper running inside xv6
# * an external LLM agent writing advice into shared/llm_advice.txt
#
# Core responsibilities:
# - Ensure xv6 is built and up-to-date
# - Launch QEMU with console on stdio (-nographic)
# - Reader thread:
#   streams console output to host,
#   detects the shell prompt,
#   detects an already-running llmhelper,
#   extracts only complete SCHED_LOG blocks,
#   dedupes them by TIMESTAMP,
#   and appends them to shared/sched_log.txt
# - Boot thread:
#   if llmhelper is not already running, starts it once the shell appears
# - Relay thread:
#   tails llm_advice.txt from the host side,
#   filters & dedupes ADVICE:PID=... lines,
#   and injects them into xv6 via QEMU stdin
import os
import sys
import time
import re
import subprocess
import threading
import shutil
from pathlib import Path
from collections import deque
from typing import Generator

#### Paths and Environment ####
ROOT = Path(__file__).resolve().parent
XV6 = ROOT / "xv6"
SHARED = ROOT / "shared"
KERNEL = XV6 / "kernel" / "kernel"
FSIMG = XV6 / "fs.img"
LOG_FILE = SHARED / "sched_log.txt"  # appended with SCHED_LOG blocks
ADVICE_FILE = SHARED / "llm_advice.txt"  # tailed by relay thread
QEMU_EXE = os.environ.get("QEMU", "qemu-system-riscv64")
SMP = os.environ.get("SMP", "1")

# QEMU command line: all args must be strings; use -nographic for console on stdio; no PTY juggling
QEMU_CMD = [
    str(QEMU_EXE),
    "-machine", "virt",
    "-bios", "none",
    "-kernel", str(KERNEL),
    "-m", "128M",
    "-smp", str(SMP),
    "-nographic",
    "-global", "virtio-mmio.force-legacy=false",
    "-drive", f"file={str(FSIMG)},if=none,format=raw,id=x0",
    "-device", "virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0",
]

# Patterns
PROMPTS = [re.compile(r"\binit: starting sh\b"), re.compile(r"(^|\n)\$ $")]
HELPER_PATTERNS = [re.compile(r"\bllmhelper:.*listening on stdin", re.I),
                   re.compile(r"\bllmhelper:\s*ready\b", re.I)]
TS_PATTERN = re.compile(r"^\s*TIMESTAMP:(\d+)\s*$", re.M | re.S)

#### Build xv6 if Needed ####
def run_make(*targets: str) -> int:
    return subprocess.call(["make", "-C", str(XV6), *targets])

def ensure_build():
    """Make sure kernel and fs.img exist and rebuild if stale."""
    if not (KERNEL.exists() and FSIMG.exists()):
        print("[runner] Building xv6 (first build or missing outputs)...")
        if run_make("kernel/kernel", "fs.img") != 0:
            sys.exit("[runner] Build failed")
    # make -q returns 1 if target is out of date
    stale_kernel = subprocess.call(["make", "-q", "-C", str(XV6), "kernel/kernel"])
    stale_fs = subprocess.call(["make", "-q", "-C", str(XV6), "fs.img"])
    if stale_kernel == 1 or stale_fs == 1:
        print("[runner] Rebuilding stale artifacts...")
        if run_make("kernel/kernel", "fs.img") != 0:
            sys.exit("[runner] Build failed")

#### Prepare shared files/dirs ####
def ensure_files():
    """Ensure QEMU exists, create shared/, and reset log file."""
    if not shutil.which(QEMU_EXE):
        sys.exit(f"[runner] QEMU not found: {QEMU_EXE}")
    SHARED.mkdir(exist_ok=True)
    # Fresh log each run; the agent consumes from beginning
    LOG_FILE.write_text("", encoding="utf-8")
    # Advice file must exist for the relay thread; don't truncate (agent may persist state)
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

def drain_stderr(proc: subprocess.Popen, stop: threading.Event):
    """Continuously drain QEMU stderr so the pipe can't fill."""
    if not proc.stderr:
        return
    while not stop.is_set():
        chunk = proc.stderr.read(4096)
        if not chunk:
            break
        try:
            sys.stderr.write(chunk.decode("utf-8", "ignore"))
        except Exception:
            pass

#### Main ####
def main():
    ensure_build()
    ensure_files()
    print("====== xv6 Runner ======")
    print(f"[runner] Kernel: {KERNEL}")
    print(f"[runner] FS : {FSIMG}")
    print(f"[runner] QEMU : {' '.join(map(str, QEMU_CMD))}\n")
    # Launch QEMU; console is stdio, so wire up pipes directly.
    q = subprocess.Popen(
        QEMU_CMD,
        stdin=subprocess.PIPE,  # we will write advice + commands here
        stdout=subprocess.PIPE,  # xv6 console stream
        stderr=subprocess.PIPE,  # diagnostics
        text=False,
        bufsize=0,
    )
    if not q.stdin or not q.stdout:
        sys.exit("[runner] Failed to open QEMU pipes.")
    # Thread coordination
    lock = threading.Lock()  # serialize writes to stdin
    stop = threading.Event()  # graceful shutdown signal
    shell_ready = threading.Event()  # set once xv6 shell prompt detected
    helper_running = threading.Event()  # set once llmhelper banner appears
    last_ts_written = {"ts": -1}  # dedupe key for SCHED_LOG blocks
    recent_advice = deque(maxlen=64)
    io_in = q.stdin
    io_out = q.stdout
    #### reader thread: mirror console, detect logs, detect prompt/helper ####
    def read_console():
        console_buf = ""  # for prompt/helper detection
        parse_buf = ""  # for SCHED_LOG block assembly
        with LOG_FILE.open("a", encoding="utf-8", errors="ignore") as out:
            while not stop.is_set():
                try:
                    chunk = io_out.read(4096)
                    if not chunk:
                        time.sleep(0.05)
                        continue
                    text = chunk.decode("utf-8", errors="ignore")
                    # stream live console output to host stdout
                    sys.stdout.write(text)
                    # detect shell prompt and helper banner
                    console_buf += text
                    if any(p.search(console_buf) for p in PROMPTS):
                        shell_ready.set()
                    if any(p.search(console_buf) for p in HELPER_PATTERNS):
                        helper_running.set()
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
                        nl_pos = parse_buf.find("\n", end_cut)
                        block = parse_buf[start_idx:(nl_pos+1 if nl_pos != -1 else end_cut)]
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
                io_in.write(data)
                io_in.flush()
            except Exception:
                pass
    #### boot llmhelper once shell prompt appears (unless already running) ####
    def boot_llmhelper():
        # Wait for shell so we can issue commands either way
        shell_ready.wait(timeout=20)
        if helper_running.is_set():
            print("[runner] llmhelper already running in init.\n")
        else:
            print("[runner] Launching llmhelper...\n")
            write_line("llmhelper")
            time.sleep(0.2)  # let banner print
        # Start a workload to create RUNNABLE procs (override with XVSCHED_WORKLOAD=none)
        workload = os.getenv("XVSCHED_WORKLOAD", "usertests")
        if workload and workload.lower() != "none":
            print(f"[runner] Starting workload: {workload}\n")
            write_line(workload)
    #### relay: feed new ADVICE lines from host to xv6 ####
    def relay_advice():
        # Wait for either the helper to be running or the shell to be ready
        helper_running.wait(timeout=25)  # proceed anyway after timeout
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
    # Start threads
    t_read = threading.Thread(target=read_console, daemon=True, name="read-console")
    t_boot = threading.Thread(target=boot_llmhelper, daemon=True, name="boot-llmhelper")
    t_relay = threading.Thread(target=relay_advice, daemon=True, name="relay-advice")
    t_stderr = threading.Thread(target=drain_stderr, args=(q, threading.Event()), daemon=True, name="drain-stderr")
    t_read.start()
    t_boot.start()
    t_relay.start()
    t_stderr.start()
    # Wait until QEMU exits or user interrupts
    try:
        rc = q.wait()
    except KeyboardInterrupt:
        print("\n[runner] Stopping QEMU...")
        try:
            q.terminate()
        except Exception:
            pass
        try:
            q.wait(timeout=3)
        except Exception:
            pass
        rc = q.returncode
    finally:
        stop.set()
        try:
            if io_in:
                io_in.close()
        except Exception:
            pass
        try:
            if io_out:
                io_out.close()
        except Exception:
            pass
        try:
            if q.stderr:
                q.stderr.close()
        except Exception:
            pass
        if rc not in (0, None):
            print(f"\n[runner] QEMU exited with error code {rc}\n")

#### Run ####
if __name__ == "__main__":
    main()
