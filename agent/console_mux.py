# console_mux.py
# Host-side helper that merges interactive keyboard input and LLM advice
# into a single stream that is fed to QEMU's stdin.
#
# Usage (from the xv6/ directory, for example):
#
#   python3 ../agent/console_mux.py ../shared/llm_advice.fifo \
#     | qemu-system-riscv64 ... \
#     | python3 ../agent/sched_log_splitter.py
#
# This script:
#   - Reads from sys.stdin (the user's keyboard), one *line* at a time.
#   - Reads from the named pipe (FIFO) where agent_bridge.py writes
#     ADVICE:PID=... lines.
#   - Writes both streams to stdout, which is then piped into QEMU's stdin.

import sys
import time
import selectors
from pathlib import Path

def main() -> None:
  if len(sys.argv) != 2:
    print(f"Usage: {sys.argv[0]} /path/to/llm_advice.fifo", file=sys.stderr)
    sys.exit(1)

  fifo_path = Path(sys.argv[1]).resolve()

  if not fifo_path.exists():
    print(f"[console_mux] FIFO does not exist: {fifo_path}", file=sys.stderr)
    sys.exit(1)

  # Open the FIFO for reading and writing in this process. Keeping a write
  # descriptor open prevents EOF on the read side when the external writer
  # (agent) closes or restarts.
  try:
    fifo_r = fifo_path.open("r")
    fifo_w = fifo_path.open("w")
  except Exception as e:
    print(f"[console_mux] Failed to open FIFO {fifo_path}: {e}", file=sys.stderr)
    sys.exit(1)

  sel = selectors.DefaultSelector()
  sel.register(sys.stdin, selectors.EVENT_READ)
  sel.register(fifo_r, selectors.EVENT_READ)

  try:
    while True:
      events = sel.select()
      for key, _ in events:
        if key.fileobj is sys.stdin:
          # Interactive input from the user: read a whole line so shell
          # commands stay intact and don't get interleaved with ADVICE lines.
          line = sys.stdin.readline()
          if line == "":
            # stdin closed; nothing more to send to QEMU.
            return
          sys.stdout.write(line)
          sys.stdout.flush()
        elif key.fileobj is fifo_r:
          # Advice lines from the agent. We read whole lines so each
          # ADVICE:PID=... arrives intact.
          line = fifo_r.readline()
          if line == "":
            # Should not happen due to the local writer, but be defensive.
            time.sleep(0.05)
            continue
          sys.stdout.write(line)
          sys.stdout.flush()
  finally:
    try:
      sel.unregister(sys.stdin)
    except Exception:
      pass
    try:
      sel.unregister(fifo_r)
    except Exception:
      pass
    fifo_r.close()
    fifo_w.close()

if __name__ == "__main__":
  main()
