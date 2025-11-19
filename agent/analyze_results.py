#!/usr/bin/env python3
# agent/analyze_results.py
# Post-process xv6 scheduler logs and LLM advice to visualize scheduling behavior.

from collections import defaultdict
from pathlib import Path
import sys

import matplotlib.pyplot as plt

#### Paths (absolute, CWD-agnostic) ####
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT       = SCRIPT_DIR.parent
SHARED     = ROOT / "shared"

SCHED_LOG_PATH  = SHARED / "sched_log.txt"
ADVICE_LOG_PATH = SHARED / "llm_advice.txt"
OUTPUT_FIG_PATH = SHARED / "scheduling_analysis.png"

#### Parsing helpers ####
def parse_sched_log(path: Path):
    """
    Parse sched_log.txt and build per-PID time series.

    Returns:
        tuple:
            dict[int, dict[str, list]]: metrics[pid] -> {
                "ts":        [timestamp, ...],
                "cpu_ticks": [int, ...],
                "wait_ticks":[int, ...],
                "io_count":  [int, ...],
                "recent_cpu":[int, ...],
            }
            list[int]: sorted list of all timestamps seen in the log.
    """
    if not path.exists():
        print(f"[analyze] Scheduler log not found: {path}")
        return {}, []

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # Per-PID metric arrays, filled block by block
    metrics = defaultdict(lambda: {
        "ts": [],
        "cpu_ticks": [],
        "wait_ticks": [],
        "io_count": [],
        "recent_cpu": [],
    })

    all_timestamps = set()

    # Each block is framed by SCHED_LOG_START / SCHED_LOG_END
    for block in content.split("SCHED_LOG_START"):
        block = block.strip()
        if not block:
            continue
        if "SCHED_LOG_END" not in block:
            continue

        # Only keep text up to the END marker
        block = block.split("SCHED_LOG_END")[0]

        log_ts = None
        lines = block.splitlines()

        # First, find the TIMESTAMP line
        for line in lines:
            line = line.strip()
            if line.startswith("TIMESTAMP:"):
                try:
                    log_ts = int(line.split(":", 1)[1].strip())
                except ValueError:
                    log_ts = None
                break

        # If timestamp is missing or invalid, skip this block
        if log_ts is None:
            continue

        all_timestamps.add(log_ts)

        # Now parse PROC lines for this snapshot
        for line in lines:
            line = line.strip()
            if not line.startswith("PROC:"):
                continue
            parts = line[5:].split(",")  # drop "PROC:" prefix
            if len(parts) != 6:
                continue

            try:
                pid        = int(parts[0])
                state      = int(parts[1])   # currently unused for plotting
                cpu_ticks  = int(parts[2])
                wait_ticks = int(parts[3])
                io_count   = int(parts[4])
                recent_cpu = int(parts[5])
            except ValueError:
                continue

            # Skip the xv6 init / shell processes (pid 1–2)
            if pid <= 2:
                continue

            rec = metrics[pid]
            rec["ts"].append(log_ts)
            rec["cpu_ticks"].append(cpu_ticks)
            rec["wait_ticks"].append(wait_ticks)
            rec["io_count"].append(io_count)
            rec["recent_cpu"].append(recent_cpu)

    sorted_ts = sorted(all_timestamps)
    print(f"[analyze] Parsed {len(sorted_ts)} snapshots and {len(metrics)} PIDs from {path}")
    return metrics, sorted_ts

def parse_llm_advice(path: Path):
    """
    Parse llm_advice.txt into a mapping of timestamp -> chosen PID.

    Returns:
        dict[int, int]: advice[ts] = pid
    """
    advice = {}

    if not path.exists():
        print(f"[analyze] Advice log not found: {path} (continuing without LLM timeline)")
        return advice

    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("ADVICE:"):
                continue

            # Expected format: ADVICE:PID=5 TS=123456789 V=1
            tokens = line.split()
            pid_val = None
            ts_val = None

            for token in tokens:
                if token.startswith("PID="):
                    try:
                        pid_val = int(token.split("=", 1)[1])
                    except ValueError:
                        pid_val = None
                elif token.startswith("TS="):
                    try:
                        ts_val = int(token.split("=", 1)[1])
                    except ValueError:
                        ts_val = None

            if pid_val is not None and ts_val is not None:
                advice[ts_val] = pid_val

    print(f"[analyze] Parsed {len(advice)} LLM advice entries from {path}")
    return advice


#### Plotting ####
def plot_metrics(metrics, advice, output_path: Path):
    """
    Visualize CPU, wait, and I/O metrics per PID and overlay LLM choices over time.

    Parameters:
        metrics (dict): Output from parse_sched_log().
        advice  (dict): Mapping of timestamp -> chosen PID from parse_llm_advice().
        output_path (Path): Where to save the PNG figure.
    """
    if not metrics:
        print("[analyze] No metrics to plot.")
        return

    # Four stacked subplots:
    #  1) CPU ticks per PID
    #  2) Wait ticks per PID
    #  3) I/O count per PID
    #  4) LLM-chosen PID vs timestamp
    fig, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True)

    # --- CPU ticks ---
    for pid, data in metrics.items():
        axes[0].plot(data["ts"], data["cpu_ticks"], label=f"PID {pid}")
    axes[0].set_ylabel("CPU Ticks")
    axes[0].set_title("CPU Usage Over Time")
    axes[0].legend(loc="best")
    axes[0].grid(True)

    # --- Wait ticks ---
    for pid, data in metrics.items():
        axes[1].plot(data["ts"], data["wait_ticks"], label=f"PID {pid}")
    axes[1].set_ylabel("Wait Ticks")
    axes[1].set_title("Wait Time Over Time")
    axes[1].legend(loc="best")
    axes[1].grid(True)

    # --- I/O count ---
    for pid, data in metrics.items():
        axes[2].plot(data["ts"], data["io_count"], label=f"PID {pid}")
    axes[2].set_ylabel("I/O Count")
    axes[2].set_title("I/O Operations Over Time")
    axes[2].legend(loc="best")
    axes[2].grid(True)

    # --- LLM-chosen PID over time ---
    if advice:
        ts_sorted = sorted(advice.keys())
        chosen_pids = [advice[t] for t in ts_sorted]

        axes[3].step(ts_sorted, chosen_pids, where="post", label="LLM choice")
        axes[3].set_ylabel("PID")
        axes[3].set_xlabel("Timestamp (sched_log TS)")
        axes[3].set_title("LLM-Selected PID Over Time")
        axes[3].grid(True)

        # Optional: set yticks to the union of PIDs seen
        all_pids = sorted({pid for pid in metrics.keys()} | set(chosen_pids))
        axes[3].set_yticks(all_pids)
        axes[3].legend(loc="best")
    else:
        # Hide the fourth subplot if there is no advice data
        axes[3].set_visible(False)

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path)
    print(f"[analyze] Saved analysis figure to {output_path}")


def print_summary(metrics, advice):
    """
    Print a small text summary of per-PID stats and advice coverage.
    """
    if not metrics:
        return

    print("\n[analyze] === Summary ===")
    for pid, data in sorted(metrics.items()):
        snapshots = len(data["ts"])
        final_cpu = data["cpu_ticks"][-1] if data["cpu_ticks"] else 0
        final_wait = data["wait_ticks"][-1] if data["wait_ticks"] else 0
        final_io = data["io_count"][-1] if data["io_count"] else 0
        print(
            f"  PID {pid}: snapshots={snapshots}, "
            f"final CPU={final_cpu}, final WAIT={final_wait}, final IO={final_io}"
        )

    if advice:
        covered_ts = len(advice)
        print(f"[analyze] LLM advice entries: {covered_ts}")
    else:
        print("[analyze] No LLM advice entries found.")


#### Main entry point ####
def main():
    """
    Entry point for standalone analysis script.
    """
    # Optional overrides: analyze_results.py [sched_log] [llm_advice]
    sched_path = Path(sys.argv[1]) if len(sys.argv) >= 2 else SCHED_LOG_PATH
    advice_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else ADVICE_LOG_PATH

    metrics, _ = parse_sched_log(sched_path)
    advice = parse_llm_advice(advice_path)

    print_summary(metrics, advice)
    plot_metrics(metrics, advice, OUTPUT_FIG_PATH)


if __name__ == "__main__":
    main()
