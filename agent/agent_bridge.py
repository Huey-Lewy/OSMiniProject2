#!/usr/bin/env python3
# agent/agent_bridge.py
# Reads xv6 scheduler logs, asks an Ollama model for scheduling advice,
# and writes the chosen PID to shared logs and a FIFO for live injection.
#
# Expected scheduler log format (produced by the modified xv6 kernel):
#
#   SCHED_LOG_START
#   TIMESTAMP:<ticks>
#   PROC:<pid>,<state>,<cpu_ticks>,<wait_ticks>,<io_count>,<recent_cpu>
#   PROC:...
#   ...
#   SCHED_LOG_END
#
# where `ticks` is the kernel's global timer tick counter and `state`
# is the enum value from xv6:
#   SLEEPING == 2
#   RUNNABLE == 3
#   RUNNING  == 4
#
# Advice format (written by this agent):
#
#   ADVICE:PID=<pid> TS=<ticks> V=1
#
# These lines are appended to llm_advice.txt and also written to
# llm_advice.fifo so that console_mux.py can inject them into QEMU's stdin.

import os
import re
import time
import signal
import errno
import requests
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path

#### Paths (absolute, CWD-agnostic) ####
# Resolve everything relative to this script so it works no matter where the process is started from.
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SHARED = ROOT / "shared"

# Shared files used for IPC with xv6 / external tools:
LOG_FILE = str(SHARED / "sched_log.txt")      # scheduler logs (written by sched_log_splitter.py)
ADVICE_FILE = str(SHARED / "llm_advice.txt")  # append-only record of advice lines
ADVICE_FIFO = SHARED / "llm_advice.fifo"      # named pipe used for live advice injection

# Make sure the shared folder and advice log file exist so other tools can tail them.
SHARED.mkdir(exist_ok=True)
Path(ADVICE_FILE).touch(exist_ok=True)

#### LLM and retry configuration ####
# RETRIES is "extra" attempts; total tries = RETRIES + 1.
RETRIES = int(os.getenv("LLM_AGENT_RETRIES", "3"))
RETRY_SLEEP_MS = int(os.getenv("LLM_AGENT_RETRY_SLEEP_MS", "150"))

# LLM generation knobs (passed directly to Ollama).
LLM_TEMP = float(os.getenv("LLM_AGENT_TEMPERATURE", "0.0"))
LLM_NUM_PRED = int(os.getenv("LLM_AGENT_NUM_PREDICT", "16"))


#### Ollama connection (WSL → Windows host) ####
def _get_wsl_gateway_ip() -> str:
    """
    Return the default gateway IP address seen from WSL.

    In a typical WSL setup, the default gateway corresponds to the
    Windows host, which is where Ollama is running. If detection
    fails for any reason, fall back to 127.0.0.1 so we still try.
    """
    try:
        out = subprocess.check_output(
            "ip route show | awk '/default/ {print $3}'",
            shell=True,
            text=True,
        ).strip()
        return out or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


# Base model name (can be overridden via env).
MODEL = os.getenv("LLM_AGENT_MODEL", "phi3:mini")

# Determine base URL for Ollama:
#   - If OLLAMA_HOST is set (e.g., 'http://HOST:11434' or 'HOST:11434'), use that.
#   - Otherwise, assume Ollama is on the Windows host reachable via the WSL gateway.
_env_host = os.getenv("OLLAMA_HOST", "").strip()
if _env_host:
    LLM_BASE = _env_host if _env_host.startswith("http") else f"http://{_env_host}"
else:
    gateway_ip = _get_wsl_gateway_ip()
    LLM_BASE = f"http://{gateway_ip}:11434"

# Final endpoint used for all LLM calls.
LLM_URL = f"{LLM_BASE}/api/generate"

# Main polling interval for reading the scheduler log (in seconds).
INTERVAL = float(os.getenv("LLM_AGENT_INTERVAL", 1.0))

#### Fallback scoring knobs ####
# Weights used by the deterministic fallback scorer when the LLM fails.
W_WAIT = float(os.getenv("LLM_AGENT_W_WAIT", "1.0"))
W_IO = float(os.getenv("LLM_AGENT_W_IO", "1.0"))
W_RECENT = float(os.getenv("LLM_AGENT_W_RECENT", "1.2"))

# Optional cap on how many runnable processes we include in the LLM prompt.
MAX_PROCS_IN_PROMPT = int(os.getenv("LLM_AGENT_MAX_PROCS", "64"))


#### Data Model ####
@dataclass
class ProcessStats:
    """
    Snapshot of per-process scheduling metrics parsed from the log.

    Attributes:
        pid        (int): Process ID.
        state      (int): Scheduler state (2=SLEEPING, 3=RUNNABLE, 4=RUNNING in this xv6).
        cpu_ticks  (int): Total CPU ticks consumed.
        wait_ticks (int): Time spent waiting to be scheduled.
        io_count   (int): Count of I/O-style blocking events.
        recent_cpu (int): Recent CPU usage (e.g., ticks in the latest window).
    """
    pid: int
    state: int
    cpu_ticks: int
    wait_ticks: int
    io_count: int
    recent_cpu: int


#### Agent ####
class LLMSchedulerAgent:
    """
    Bridges between xv6 scheduler logs and an LLM:

      - Reads periodic scheduler snapshots from sched_log.txt
      - Builds a strict prompt listing only runnable processes
      - Asks an Ollama-served model to choose the next PID to run
      - Falls back to a deterministic scorer if the LLM fails
      - Appends advice lines (ADVICE:PID=...) to llm_advice.txt
      - Also mirrors advice lines into a FIFO for live injection into xv6
    """

    def __init__(self):
        # Static configuration
        self.model = MODEL
        self.llm_url = LLM_URL
        self.log_file = LOG_FILE
        self.advice_file = ADVICE_FILE
        self.advice_fifo_path = ADVICE_FIFO
        self.interval = INTERVAL

        # Log reading cursor (tracks how many bytes we've already consumed).
        self.last_size = 0

        # Lifetime flag controlled by signal handlers.
        self.running = True

        # Deduplication for advice lines: last PID and timestamp written.
        self._last_sent_pid: Optional[int] = None
        self._last_advised_ts: Optional[int] = None

        # Startup banner for debugging and configuration checks.
        print("====== LLM Scheduler Agent ======")
        print(f"[agent] Model       : {self.model}")
        print(f"[agent] Ollama      : {LLM_BASE}")
        print(f"[agent] Log file    : {self.log_file}")
        print(f"[agent] Advice log  : {self.advice_file}")
        print(f"[agent] Advice FIFO : {self.advice_fifo_path} (optional)")
        print(f"[agent] Interval    : {self.interval}s")
        print(f"[agent] Weights     : WAIT={W_WAIT} IO={W_IO} RECENT={W_RECENT}")
        print(f"[agent] MaxProcs    : {MAX_PROCS_IN_PROMPT}")
        print(f"[agent] Retries     : {RETRIES} (sleep {RETRY_SLEEP_MS}ms between)")
        print(f"[agent] LLM opts    : temp={LLM_TEMP} num_predict={LLM_NUM_PRED}\n")

        # Allow Ctrl+C or SIGTERM to cleanly stop the agent loop.
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self, *_):
        """
        Signal handler used to stop the main loop gracefully.
        """
        print("\n[agent] Shutting down...\n")
        self.running = False

    #### Internal implementation ####
    def _read_log(self) -> Optional[Tuple[int, List[ProcessStats]]]:
        """
        Read and parse the most recent complete SCHED_LOG block since last_size.

        Returns:
            tuple[int, list[ProcessStats]]: (timestamp, processes) if a complete
                block was parsed successfully. The timestamp is the kernel's
                global tick count at the time of logging.
            None: If the log doesn't contain a complete block yet or an error occurs.
        """
        try:
            size = os.path.getsize(self.log_file)

            # If the file shrank, it was truncated; reset our cursor.
            if size < self.last_size:
                print("[agent] Log truncated, resetting pointer")
                self.last_size = 0

            # Seek to the last known position and read new data.
            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_size)
                data = f.read()
                self.last_size = f.tell()

            # No new SCHED_LOG_START marker in the newly read data.
            if "SCHED_LOG_START" not in data:
                return None

            # Take the last complete block between SCHED_LOG_START and SCHED_LOG_END.
            parts = data.split("SCHED_LOG_START")
            last = parts[-1]
            if "SCHED_LOG_END" not in last:
                # A partial block is present but incomplete; wait for more data.
                return None
            last = last.split("SCHED_LOG_END")[0]

            log_ts: Optional[int] = None
            processes: List[ProcessStats] = []

            # Parse header and process lines.
            for line in last.splitlines():
                if line.startswith("TIMESTAMP:"):
                    try:
                        log_ts = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        log_ts = None
                elif line.startswith("PROC:"):
                    # Order must match the kernel's printf():
                    #   PROC:%d,%d,%d,%d,%d,%d
                    #        pid,state,cpu,wait,io,recent
                    p = line[5:].split(",")
                    if len(p) == 6:
                        try:
                            processes.append(ProcessStats(*map(int, p)))
                        except ValueError:
                            continue

            if log_ts is None or not processes:
                return None

            print(f"[agent] Parsed {len(processes)} processes from scheduler log @TS={log_ts}")
            return log_ts, processes

        except FileNotFoundError:
            print("[agent] Waiting for sched_log.txt ...")
            return None
        except Exception as e:
            print("[agent] Log read error:", e)
            return None

    def _runnable(self, procs: List[ProcessStats]) -> List[ProcessStats]:
        """
        Filter to only "runnable" processes, skipping kernel/init/system PIDs.

        For this xv6-riscv port:
            SLEEPING == 2
            RUNNABLE == 3
            RUNNING  == 4
        We also skip PIDs <= 3 to avoid init/system and the llmhelper process itself.
        """
        return [
            p
            for p in procs
            if p.state == 3   # RUNNABLE
            and p.pid > 3     # skip 1, 2, and llmhelper (pid 3)
        ]

    def _make_prompt(self, procs: List[ProcessStats]) -> Optional[str]:
        """
        Build a strict LLM prompt asking for exactly one PID in 'PID:<n>' format.

        Returns:
            str or None: Prompt text including a list of runnable processes,
            or None if there is nothing runnable.
        """
        ready = self._runnable(procs)
        if not ready:
            return None

        # Keep the prompt small: limit how many processes we show.
        if len(ready) > MAX_PROCS_IN_PROMPT:
            ready = sorted(ready, key=lambda x: (-x.wait_ticks, -x.io_count))[:MAX_PROCS_IN_PROMPT]

        lines = [
            "You are an OS scheduler. Choose ONE process to run next.",
            "",
            "RULES (you must obey):",
            "1) Respond with ONLY the chosen PID in the exact format: PID:<number>",
            "2) Do NOT explain. Do NOT refuse. Do NOT add any other text.",
            "3) Choose a PID that exists in the list below.",
            "4) Prefer processes with HIGHER WAIT (they have waited longer),",
            "   HIGHER IO (more interactive / I/O-bound),",
            "   and LOWER RECENT CPU (to avoid hogs and balance CPU).",
            "   Avoid starvation: if any process has much larger WAIT, it should be preferred.",
            "",
            "Processes:",
        ]

        for p in ready:
            lines.append(
                f"PID={p.pid} CPU={p.cpu_ticks} WAIT={p.wait_ticks} "
                f"IO={p.io_count} RECENT={p.recent_cpu}"
            )

        lines.append("")
        lines.append("Return ONLY one line like this: PID:3")

        return "\n".join(lines)

    def _ask_llm(self, prompt: str) -> Optional[int]:
        """
        Call the Ollama API with a prompt and parse a PID from its response.

        Returns:
            int or None: Parsed PID if successful, otherwise None.
        """
        try:
            r = requests.post(
                self.llm_url,
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": LLM_TEMP, "num_predict": LLM_NUM_PRED},
                },
                timeout=8,
            )

            if r.status_code != 200:
                print("[agent] Ollama HTTP error:", r.status_code)
                return None

            txt = (r.json().get("response") or "").strip()
            m = re.search(r"PID[:\s]+(\d+)", txt, flags=re.IGNORECASE)
            if not m:
                m = re.search(r"\b(\d+)\b", txt)
            if not m:
                print("[agent] LLM gave invalid response:", txt[:160])
                return None

            pid = int(m.group(1))
            print(f"[agent] LLM suggests: PID={pid}")
            return pid

        except Exception as e:
            print("[agent] LLM query failed:", e)
            return None

    def _choose_with_retry(self, prompt: str, runnable_pids: set[int]) -> Optional[int]:
        """
        Ask the LLM up to RETRIES+1 times and return a valid runnable PID.
        """
        tries = RETRIES + 1
        for attempt in range(1, tries + 1):
            pid = self._ask_llm(prompt)
            if pid is not None and pid in runnable_pids:
                return pid

            reason = "unparsable" if pid is None else f"non-runnable ({pid})"
            print(f"[agent] LLM response {reason}; retry {attempt}/{tries}")

            if attempt < tries and RETRY_SLEEP_MS > 0:
                time.sleep(RETRY_SLEEP_MS / 1000.0)

        return None

    def _fallback_choice(self, procs: List[ProcessStats]) -> Optional[int]:
        """
        Deterministic backup policy when the LLM fails.
        """
        ready = self._runnable(procs)
        if not ready:
            return None

        def score(p: ProcessStats) -> Tuple[float, int]:
            s = (W_WAIT * p.wait_ticks) + (W_IO * p.io_count) - (W_RECENT * p.recent_cpu)
            jitter = (hash(p.pid) % 7) * 0.01
            return s + jitter, -p.cpu_ticks

        best = max(ready, key=score)
        print(f"[agent] Fallback chose PID={best.pid}")
        return best.pid

    def _write_fifo(self, line: str) -> None:
        """
        Best-effort write of an advice line into the FIFO used by console_mux.py.
        """
        try:
            if not self.advice_fifo_path.exists():
                return
            fd = os.open(str(self.advice_fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            if e.errno not in (errno.ENXIO, errno.ENOENT):
                print(f"[agent] FIFO open error: {e}")
            return

        try:
            os.write(fd, line.encode("utf-8"))
        except OSError as e:
            print(f"[agent] FIFO write error: {e}")
        finally:
            os.close(fd)

    def _write(self, pid: int, ts: int):
        """
        Append an advice line to the advice file and mirror it to the FIFO.

        Format:
            ADVICE:PID=<pid> TS=<timestamp> V=1
        """
        try:
            if self._last_advised_ts == ts and pid == self._last_sent_pid:
                return

            line = f"ADVICE:PID={pid} TS={ts} V=1\n"

            # Log file (for analysis / debugging).
            with open(self.advice_file, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            # FIFO (for live advice into xv6).
            self._write_fifo(line)

            self._last_sent_pid = pid
            self._last_advised_ts = ts
            print(f"[agent] Wrote advice → {line.strip()}")
        except Exception as e:
            print("[agent] Failed writing advice:", e)

    #### Public API (used by tests or other tooling) ####
    @property
    def last_log_size(self) -> int:
        return self.last_size

    @last_log_size.setter
    def last_log_size(self, v: int) -> None:
        try:
            self.last_size = int(v or 0)
        except Exception:
            self.last_size = 0

    def read_scheduling_log(self):
        return self._read_log()

    def format_prompt_for_llm(self, processes):
        return self._make_prompt(processes)

    def query_llm(self, prompt: str):
        return self._ask_llm(prompt)

    # Back-compat aliases
    def _read_latest_log(self):
        return self._read_log()

    def _build_prompt(self, processes):
        return self._make_prompt(processes)

    def _query_llm(self, prompt: str):
        return self._ask_llm(prompt)

    #### Main loop ####
    def run(self):
        """
        Main polling loop for the scheduler agent.
        """
        print("[agent] Agent is running...\n")
        while self.running:
            parsed = self._read_log()
            if parsed:
                log_ts, processes = parsed

                # Avoid emitting duplicate advice for the same timestamp.
                if self._last_advised_ts == log_ts:
                    time.sleep(self.interval)
                    continue

                runnable = self._runnable(processes)
                if not runnable:
                    time.sleep(self.interval)
                    continue

                # If there's only one RUNNABLE candidate, just pick it.
                if len(runnable) == 1:
                    self._write(runnable[0].pid, log_ts)
                    time.sleep(self.interval)
                    continue

                prompt = self._make_prompt(processes)
                chosen_pid: Optional[int] = None

                if prompt:
                    runnable_pids = {p.pid for p in runnable}
                    pid = self._choose_with_retry(prompt, runnable_pids)
                    if pid is not None:
                        chosen_pid = pid
                    else:
                        print(f"[agent] All retries failed @TS={log_ts}; using fallback.")
                        chosen_pid = self._fallback_choice(processes)
                else:
                    chosen_pid = self._fallback_choice(processes)

                if chosen_pid is not None:
                    self._write(chosen_pid, log_ts)

            time.sleep(self.interval)

        print("[agent] Agent stopped.")

if __name__ == "__main__":
    LLMSchedulerAgent().run()
