#!/usr/bin/env python3
# testing/test_scheduling.py
# LLM-only scheduler simulator:
# - emits SCHED_LOG blocks at each decision boundary
# - waits for matching ADVICE:PID=<n> TS=<ts> V=1 from agent_bridge.py
# - applies the advised PID for the next quantum and advances per-tick stats

import os
import re
import sys
import time
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

#### Shared Paths ####
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT       = SCRIPT_DIR.parent
SHARED     = ROOT / "shared"
LOG_FILE    = SHARED / "sched_log.txt"
ADVICE_FILE = SHARED / "llm_advice.txt"

SHARED.mkdir(exist_ok=True)
LOG_FILE.touch(exist_ok=True)
ADVICE_FILE.touch(exist_ok=True)

#### Weights (match agent defaults for RECENT effect on stats only) ####
W_WAIT   = float(os.getenv("LLM_AGENT_W_WAIT",   "1.0"))
W_IO     = float(os.getenv("LLM_AGENT_W_IO",     "1.0"))
W_RECENT = float(os.getenv("LLM_AGENT_W_RECENT", "1.2"))

RUNNABLE = 3

# Adaptive timeout to account for agent retries
_RETRIES = int(os.getenv("LLM_AGENT_RETRIES", "2"))
_RETRY_SLEEP_MS = int(os.getenv("LLM_AGENT_RETRY_SLEEP_MS", "150"))
_default_timeout = 2.0 + (_RETRIES + 1) * (1.0 + _RETRY_SLEEP_MS / 1000.0)
DEFAULT_TIMEOUT = float(os.getenv("TEST_HARNESS_TIMEOUT", str(max(8.0, _default_timeout))))

@dataclass
class Proc:
    """
    Simulated process state for the test scheduler.

    Fields track:
      - total CPU required (total_required)
      - CPU time used so far (cpu_ticks)
      - accumulated wait time (wait_ticks)
      - simple I/O event count (io_count)
      - recent CPU usage (recent_cpu)
      - completion markers (done, start_tick, finish_tick)
    """
    pid: int
    total_required: int
    io_bias: float = 0.2
    cpu_ticks: int = 0
    wait_ticks: int = 0
    io_count: int = 0
    recent_cpu: int = 0
    done: bool = False
    start_tick: Optional[int] = None
    finish_tick: Optional[int] = None

@dataclass
class SimStats:
    """
    Aggregate statistics for a simulation run.

    Fields:
      - ticks: total simulated ticks executed
      - decisions: number of scheduling decisions made
      - context_switches: number of times the chosen PID changed
      - history: list of PIDs chosen at each decision
      - avg_wait: average total wait time per process
      - avg_turnaround: average turnaround time from 0 to finish
    """
    ticks: int = 0
    decisions: int = 0
    context_switches: int = 0
    history: List[int] = field(default_factory=list)
    avg_wait: float = 0.0
    avg_turnaround: float = 0.0

#### Helpers ####
def _file_size(path: Path) -> int:
    """
    Return file size in bytes, or 0 if the file does not exist.
    """
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0

def _append_sched_block(ts: int, procs: List[Proc]) -> None:
    """
    Append a SCHED_LOG block for the current snapshot of processes.

    Each non-completed process is written as:
        PROC:<pid>,3,<cpu_ticks>,<wait_ticks>,<io_count>,<recent_cpu>
    where state=3 marks the process as RUNNABLE for the agent.
    """
    lines = ["SCHED_LOG_START", f"TIMESTAMP:{ts}"]
    live = 0
    for p in procs:
        if p.done:
            continue
        live += 1
        lines.append(f"PROC:{p.pid},{RUNNABLE},{p.cpu_ticks},{p.wait_ticks},{p.io_count},{p.recent_cpu}")
    lines.append("SCHED_LOG_END\n")
    data = "\n".join(lines)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    print(f"[sim] Wrote SCHED_LOG block TS={ts} with {live} procs")

def _wait_for_advice(ts: int, start_offset: int, timeout: float) -> Optional[int]:
    """
    Tail llm_advice.txt until a line with the matching timestamp is found.

    Searches for:
        ADVICE:PID=<n> TS=<ts> V=1

    Parameters:
        ts           (int): Log timestamp to match.
        start_offset (int): Byte offset to start reading from (previous file end).
        timeout    (float): Maximum time to wait in seconds.

    Returns:
        int or None: Advised PID on success, or None on timeout / parse failure.
    """
    deadline = time.time() + timeout
    pos = start_offset
    while time.time() < deadline:
        size = _file_size(ADVICE_FILE)
        if size < pos:
            # Advice file was truncated; restart from beginning
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
                pid_s, ts_s = m.groups()
                try:
                    pid_val = int(pid_s)
                    ts_val = int(ts_s)
                except ValueError:
                    continue
                if ts_val == ts:
                    print(f"[sim] Advice for TS={ts}: PID={pid_val}")
                    return pid_val
        time.sleep(0.02)
    print(f"[sim] Advice wait timed out for TS={ts}")
    return None

def _summarize(procs: List[Proc], stats: SimStats) -> None:
    """
    Compute and print summary statistics for the simulation.

    Calculates:
      - average wait time across processes
      - average turnaround time (finish_tick - arrival_time)
      - execution order and counts of ticks / decisions / context switches
    """
    waits = [p.wait_ticks for p in procs]
    turns = []
    for p in procs:
        if p.finish_tick is not None:
            # In this simulator, all processes "arrive" at time 0.
            turns.append(p.finish_tick - 0)
    stats.avg_wait = (sum(waits) / len(waits)) if waits else 0.0
    stats.avg_turnaround = (sum(turns) / len(turns)) if turns else 0.0

    print("\n===== Simulation Results =====")
    for p in sorted(procs, key=lambda x: x.pid):
        print(
            f"PID={p.pid:2d} CPU={p.cpu_ticks:3d} WAIT={p.wait_ticks:3d} "
            f"IO={p.io_count:2d} RECENT={p.recent_cpu:3d} "
            f"DONE={str(p.done):5s} START={p.start_tick} FINISH={p.finish_tick}"
        )
    print(f"\nTotal ticks: {stats.ticks}")
    print(f"Decisions: {stats.decisions}")
    print(f"Context switches: {stats.context_switches}")
    print(f"Avg wait: {stats.avg_wait:.2f}")
    print(f"Avg turnaround: {stats.avg_turnaround:.2f}")
    print(f"Execution order (first 40 decisions): {stats.history[:40]}\n")

#### Simulation ####
def simulate_llm(
    procs: List[Proc],
    total_ticks: int,
    quantum: int,
    base_ts: Optional[int],
    advice_timeout: float,
    truncate_shared: bool,
    slow_after_emit_ms: int
) -> SimStats:
    """
    Run an LLM-driven scheduling simulation.

    At each decision boundary, the simulator:
      1) Emits a SCHED_LOG block with TIMESTAMP=ts.
      2) Waits for a matching ADVICE line for that timestamp.
      3) Runs the advised PID for one quantum (or until completion).
      4) Updates per-process CPU, wait, I/O, and recent CPU stats.

    Parameters:
        procs             (list[Proc]): Workload to schedule.
        total_ticks              (int): Global limit on simulated ticks.
        quantum                  (int): Quantum length in ticks.
        base_ts             (int|None): Optional base timestamp, or time-based if None.
        advice_timeout         (float): Max seconds to wait for each ADVICE line.
        truncate_shared          (bool): If True, clears shared log/advice files at start.
        slow_after_emit_ms        (int): Extra delay after writing each snapshot (ms).

    Returns:
        SimStats: Aggregated metrics for this run.
    """
    if truncate_shared:
        LOG_FILE.write_text("")
        ADVICE_FILE.write_text("")
        print("[sim] Truncated shared files at start.")

    stats = SimStats()
    current: Optional[Proc] = None
    decision_idx = 0

    if base_ts is None:
        base_ts = int(time.time())

    tick = 0
    while tick < total_ticks:
        # Decision boundary (beginning and every quantum, or if current is missing/done)
        if (stats.decisions == 0) or (tick % quantum == 0) or (current is None) or (current.done):
            # Emit snapshot and wait for advice
            ts = base_ts + decision_idx
            decision_idx += 1
            stats.decisions += 1

            # Capture starting offset so we only read new advice
            start_off = _file_size(ADVICE_FILE)
            _append_sched_block(ts, procs)

            # Small delay helps agent notice the new block (WSL + fsync already helps)
            if slow_after_emit_ms > 0:
                time.sleep(slow_after_emit_ms / 1000.0)

            advised_pid = _wait_for_advice(ts, start_off, advice_timeout)
            if advised_pid is None:
                print("[sim] No advice received — stopping with error.")
                break

            # Choose process based on advised PID
            ready = [p for p in procs if (not p.done)]
            next_proc = next((p for p in ready if p.pid == advised_pid), None)
            if next_proc is None:
                print(f"[sim] Advised PID {advised_pid} not runnable — stopping with error.")
                break

            # Context switch if PID changes
            if (current is None) or (current.pid != next_proc.pid):
                stats.context_switches += 1
            current = next_proc
            if current.start_tick is None:
                current.start_tick = tick
            stats.history.append(current.pid)
            print(f"[tick {tick}] Switch → PID {current.pid} (llm)")

        # Run one tick on the current process
        stats.ticks += 1
        if current and (not current.done):
            current.cpu_ticks += 1
            current.recent_cpu += 1
            # Cheap stochastic I/O event model
            if current.io_bias > 0.0 and (hash((current.pid, tick)) % 100) < int(current.io_bias * 100):
                current.io_count += 1

        # Everyone else accumulates wait time
        for p in procs:
            if p is current or p.done:
                continue
            p.wait_ticks += 1

        # Check for completion
        if current and (not current.done) and current.cpu_ticks >= current.total_required:
            current.done = True
            current.finish_tick = tick

        tick += 1

        # If all processes are done, stop early
        if all(p.done for p in procs):
            break

    _summarize(procs, stats)
    return stats

#### Scenarios ####
def scenario_demo() -> List[Proc]:
    """
    Demo workload with five processes of varying CPU and I/O behavior.
    """
    return [
        Proc(pid=3, total_required=50, io_bias=0.30),
        Proc(pid=4, total_required=40, io_bias=0.45),
        Proc(pid=5, total_required=70, io_bias=0.05),
        Proc(pid=6, total_required=60, io_bias=0.25),
        Proc(pid=7, total_required=30, io_bias=0.60),
    ]

def scenario_minimal() -> List[Proc]:
    """
    Minimal workload with two processes for quick sanity checks.
    """
    return [
        Proc(pid=15, total_required=40, io_bias=0.10),
        Proc(pid=16, total_required=40, io_bias=0.50),
    ]

#### CLI ####
def main():
    """
    Command-line entry point for the LLM scheduling simulator.

    Allows configuration of:
      - total ticks and quantum size
      - base timestamp and advice timeout
      - whether to truncate shared files at start
      - which workload scenario to run
    """
    parser = argparse.ArgumentParser(description="LLM-only scheduling simulator (xv6-style handshake).")
    parser.add_argument("--ticks", type=int, default=250, help="Total simulation ticks")
    parser.add_argument("--quantum", type=int, default=10, help="Quantum length in ticks")
    parser.add_argument("--base-ts", type=int, default=0, help="Base TIMESTAMP (default: time-based)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Advice wait timeout (sec)")
    parser.add_argument("--truncate", action="store_true", help="Truncate shared files at start")
    parser.add_argument("--scenario", choices=["demo", "minimal"], default="demo", help="Workload scenario")
    parser.add_argument(
        "--slow-after-emit-ms",
        type=int,
        default=50,
        help="Extra delay after writing snapshot to give agent time to react (ms)",
    )
    args = parser.parse_args()

    # Setup scenario
    if args.scenario == "demo":
        procs = scenario_demo()
    else:
        procs = scenario_minimal()

    base_ts = args.base_ts if args.base_ts > 0 else None

    print("====== LLM Scheduling Simulation ======")
    print(f"Ticks: {args.ticks} | Quantum: {args.quantum}")
    print(f"Timeout: {args.timeout}s | Slow-after-emit: {args.slow_after_emit_ms}ms")
    print(f"Files: log={LOG_FILE} | advice={ADVICE_FILE}")
    print(f"Weights: WAIT={W_WAIT} IO={W_IO} RECENT={W_RECENT}")
    print(f"Retries: {_RETRIES} (sleep {_RETRY_SLEEP_MS}ms)")
    print(f"Scenario: {args.scenario}\n")

    stats = simulate_llm(
        procs=procs,
        total_ticks=args.ticks,
        quantum=args.quantum,
        base_ts=base_ts,
        advice_timeout=args.timeout,
        truncate_shared=args.truncate,
        slow_after_emit_ms=args.slow_after_emit_ms,
    )

    # Fail the run if advice was missing and we bailed early
    if not all(p.done for p in procs):
        print("[sim] Not all processes finished — likely advice missing or too slow.")
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()
