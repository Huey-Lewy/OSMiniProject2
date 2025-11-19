#!/usr/bin/env python3
# agent/test_xv6.py
# Validates scheduling-advisor logic before integrating with xv6.
# Appends well-formed SCHED_LOG blocks to shared/sched_log.txt,
# waits for agent_bridge.py to write advice lines to shared/llm_advice.txt,
# and compares the agent’s decision (PID) against expected results.

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

#### Shared Paths ####
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT       = SCRIPT_DIR.parent
SHARED     = ROOT / "shared"
LOG_FILE    = SHARED / "sched_log.txt"
ADVICE_FILE = SHARED / "llm_advice.txt"

SHARED.mkdir(exist_ok=True)
LOG_FILE.touch(exist_ok=True)
ADVICE_FILE.touch(exist_ok=True)

#### Weights (match agent defaults) ####
W_WAIT   = float(os.getenv("LLM_AGENT_W_WAIT",   "1.0"))
W_IO     = float(os.getenv("LLM_AGENT_W_IO",     "1.0"))
W_RECENT = float(os.getenv("LLM_AGENT_W_RECENT", "1.2"))

# Adaptive timeout to account for agent retries
_RETRIES = int(os.getenv("LLM_AGENT_RETRIES", "2"))
_RETRY_SLEEP_MS = int(os.getenv("LLM_AGENT_RETRY_SLEEP_MS", "150"))
# Assume ~1s budget per LLM attempt + retry sleep; floor at 8s
_default_timeout = 2.0 + (_RETRIES + 1) * (1.0 + _RETRY_SLEEP_MS / 1000.0)
TIMEOUT  = float(os.getenv("TEST_HARNESS_TIMEOUT", str(max(8.0, _default_timeout))))

RUNNABLE = 3  # xv6 RUNNABLE state

#### Data Models ####
@dataclass
class Proc:
    """
    Snapshot of a process state used in a test case.

    Fields:
      - pid: process ID
      - cpu: total CPU ticks accumulated so far
      - wait: total wait ticks accumulated so far
      - io: I/O events observed
      - recent: recent CPU usage (e.g., last window)
      - state: scheduler state (default RUNNABLE)
    """
    pid: int
    cpu: int
    wait: int
    io: int
    recent: int
    state: int = RUNNABLE


@dataclass
class TestCase:
    """
    Single scheduling test scenario.

    Fields:
      - name: human-readable description
      - ts: timestamp used for the SCHED_LOG block
      - procs: list of Proc entries in that snapshot
      - expected_pid: PID the agent is expected to choose
    """
    name: str
    ts: int
    procs: List[Proc]
    expected_pid: int


#### File Helpers ####
def _file_size(path: Path) -> int:
    """
    Return the file size in bytes, or 0 if the file is missing.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _append_sched_block(ts: int, procs: List[Proc]) -> None:
    """
    Append a complete SCHED_LOG block to the shared log file.

    Format matches agent_bridge.py:
        SCHED_LOG_START
        TIMESTAMP:<ts>
        PROC:<pid>,<state>,<cpu>,<wait>,<io>,<recent>
        ...
        SCHED_LOG_END
    """
    lines = ["SCHED_LOG_START", f"TIMESTAMP:{ts}"]
    for p in procs:
        lines.append(f"PROC:{p.pid},{p.state},{p.cpu},{p.wait},{p.io},{p.recent}")
    lines.append("SCHED_LOG_END\n")
    block = "\n".join(lines)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(block)
        f.flush()
        os.fsync(f.fileno())
    print(f"[harness] Wrote SCHED_LOG block TS={ts} with {len(procs)} procs")


def _tail_for_advice(ts: int, start_offset: int, timeout: float) -> Optional[int]:
    """
    Tail llm_advice.txt until a matching advice line is found, or timeout.

    Looks for lines of the form:
        ADVICE:PID=<n> TS=<ts> V=1

    Parameters:
        ts           (int): Timestamp used for the log block.
        start_offset (int): Byte offset to start reading from.
        timeout    (float): Maximum time in seconds to wait.

    Returns:
        int or None: Advised PID on success, or None if timed out.
    """
    deadline = time.time() + timeout
    pos = start_offset

    while time.time() < deadline:
        size = _file_size(ADVICE_FILE)
        if size < pos:
            # Advice file truncated between polls; restart from beginning
            pos = 0

        if size > pos:
            with open(ADVICE_FILE, "r", encoding="utf-8") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()

            for line in chunk.splitlines():
                m = re.search(r"^ADVICE:PID=(\d+)\s+TS=(\d+)", line.strip())
                if not m:
                    continue
                pid_str, ts_str = m.groups()
                try:
                    pid_val = int(pid_str)
                    ts_val  = int(ts_str)
                except ValueError:
                    continue
                if ts_val == ts:
                    print(f"[harness] Saw advice for TS={ts}: PID={pid_val}")
                    return pid_val

        time.sleep(0.05)

    print(f"[harness] Timeout waiting for advice TS={ts}")
    return None


#### Fallback Policy (matches agent scorer) ####
def _runnable(procs: List[Proc]) -> List[Proc]:
    """
    Return processes that are RUNNABLE and have pid > 2.

    This mirrors the filter used by the agent (skipping init/shell).
    """
    return [p for p in procs if p.state == RUNNABLE and p.pid > 2]


def _score(p: Proc) -> Tuple[float, int]:
    """
    Score a process using the same heuristic as the agent fallback.

    Score favors:
      - higher WAIT and IO
      - lower RECENT CPU
    A tiny jitter based on PID breaks ties deterministically.
    """
    score = (W_WAIT * p.wait) + (W_IO * p.io) - (W_RECENT * p.recent)
    jitter = (hash(p.pid) % 7) * 0.01
    return (score + jitter, -p.cpu)


def _fallback_pick(procs: List[Proc]) -> Optional[int]:
    """
    Apply the fallback scorer to runnable processes and return the best PID.

    Returns:
        int or None: PID of the highest-scoring runnable process,
                     or None if no candidates exist.
    """
    ready = _runnable(procs)
    if not ready:
        return None
    best = max(ready, key=_score)
    return best.pid


#### Test Runner ####
def run_case(tc: TestCase) -> bool:
    """
    Run a single scheduling test case end-to-end.

    Steps:
      1) Append a SCHED_LOG block for the test case.
      2) Tail llm_advice.txt for an ADVICE line with the same timestamp.
      3) Compute the fallback scorer's choice for comparison.
      4) Report expected, agent, and fallback PIDs.

    Returns:
        bool: True if the agent's PID matches tc.expected_pid; False otherwise.
    """
    start_offset = _file_size(ADVICE_FILE)
    _append_sched_block(tc.ts, tc.procs)

    agent_pid = _tail_for_advice(tc.ts, start_offset, TIMEOUT)
    scorer_pid = _fallback_pick(tc.procs)

    print(f"[check] {tc.name}")
    print(f"        expected PID={tc.expected_pid}")
    print(f"        agent    PID={agent_pid}")
    print(f"        scorer   PID={scorer_pid}\n")

    return agent_pid == tc.expected_pid


def main():
    """
    Command-line test harness for the xv6 scheduling advisor.

    Reads test cases, writes synthetic SCHED_LOG blocks, waits for
    agent_bridge.py to respond in llm_advice.txt, and verifies that
    the chosen PID matches the expected one for each scenario.
    """
    print("====== xv6 Scheduling Advisor Test Harness ======\n")
    print(f"[env] Weights: WAIT={W_WAIT} IO={W_IO} RECENT={W_RECENT}")
    print(f"[env] Retries: {_RETRIES} (sleep {_RETRY_SLEEP_MS}ms)")
    print(f"[env] Files  : log={LOG_FILE} advice={ADVICE_FILE}\n")

    #### Test Cases ####
    tests = [
        TestCase(
            name="Example 1: IO-heavy + lower recent CPU",
            ts=2001,
            procs=[
                Proc(pid=3, cpu=5,  wait=20, io=12, recent=2),
                Proc(pid=4, cpu=8,  wait=30, io=20, recent=5),
            ],
            expected_pid=3,
        ),
        TestCase(
            name="Example 2: Starvation mitigation",
            ts=2002,
            procs=[
                Proc(pid=5, cpu=40, wait=55, io=1,  recent=30),
                Proc(pid=6, cpu=5,  wait=60, io=8,  recent=2),
            ],
            expected_pid=6,
        ),
        TestCase(
            name="Example 3: Avoid CPU hogging",
            ts=2003,
            procs=[
                Proc(pid=2, cpu=120, wait=10, io=2, recent=45),
                Proc(pid=7, cpu=10,  wait=8,  io=5, recent=3),
            ],
            expected_pid=7,
        ),
    ]

    passed = 0
    for tc in tests:
        ok = run_case(tc)
        if ok:
            passed += 1
        else:
            print("[warn] Mismatch (agent vs expected). Check scorer.\n")
        time.sleep(0.2)

    print(f"Done: {passed}/{len(tests)} cases passed.\n")
    if passed != len(tests):
        exit(1)


if __name__ == "__main__":
    main()
