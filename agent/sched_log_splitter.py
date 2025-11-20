# sched_log_splitter.py
# Host-side helper that splits QEMU output into:
#   - Human-facing console output (stdout)
#   - Structured scheduler logs (shared/sched_log.txt)
#
# It reads all QEMU output from stdin, extracts blocks between
#   SCHED_LOG_START
#   ...
#   SCHED_LOG_END
# and appends them to shared/sched_log.txt, while *not* printing those
# blocks to stdout. Everything else is forwarded to stdout unchanged.

import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT       = SCRIPT_DIR.parent
SHARED     = ROOT / "shared"

LOG_PATH = SHARED / "sched_log.txt"

def main() -> None:
  SHARED.mkdir(exist_ok=True)
  # Ensure the log file exists so other tools can tail it.
  LOG_PATH.touch(exist_ok=True)

  in_block = False
  block_lines = []

  # Read QEMU output line by line.
  for line in sys.stdin:
    if not in_block:
      if line.startswith("SCHED_LOG_START"):
        # Start of a new scheduler log block; begin buffering.
        in_block = True
        block_lines = [line]
      else:
        # Normal console output; pass it through.
        sys.stdout.write(line)
        sys.stdout.flush()
    else:
      # Currently inside a SCHED_LOG block; buffer until SCHED_LOG_END.
      block_lines.append(line)
      if line.startswith("SCHED_LOG_END"):
        # Block complete; append it to the scheduler log file.
        try:
          with LOG_PATH.open("a", encoding="utf-8") as f:
            for l in block_lines:
              f.write(l)
            f.flush()
            os.fsync(f.fileno())
        except Exception as e:
          print(f"[splitter] Failed writing sched_log.txt: {e}", file=sys.stderr)
        # Reset for the next block.
        in_block = False
        block_lines = []

  # If stdin closes, just exit.
  sys.stdout.flush()

if __name__ == "__main__":
  main()
