#!/usr/bin/env python3
# agent/agent_bridge.py
# Reads xv6 scheduler logs, asks Ollama for scheduling advice, writes guidance to shared file.
import os
import re
import time
import signal
import requests
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple
from pathlib import Path

#### Paths (absolute, CWD-agnostic) ####
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
SHARED = ROOT / "shared"
LOG_FILE = str(SHARED / "sched_log.txt")
ADVICE_FILE = str(SHARED / "llm_advice.txt")
SHARED.mkdir(exist_ok=True)
Path(ADVICE_FILE).touch(exist_ok=True)  # so runner can tail it

# Retry policy for invalid/unparsable/non-runnable LLM replies
RETRIES = int(os.getenv("LLM_AGENT_RETRIES", "2"))  # extra attempts (tries = RETRIES+1)
RETRY_SLEEP_MS = int(os.getenv("LLM_AGENT_RETRY_SLEEP_MS", "150"))

# LLM generation knobs
LLM_TEMP = float(os.getenv("LLM_AGENT_TEMPERATURE", "0.0"))
LLM_NUM_PRED = int(os.getenv("LLM_AGENT_NUM_PREDICT", "16"))

#### Auto-Configuration ####
def _detect_gateway() -> str:
    """Detect Windows host gateway IP for WSL (e.g., 172.x.x.x)."""
    try:
        out = subprocess.check_output(
            "ip route show | awk '/default/ {print $3}'",
            shell=True, text=True
        ).strip()
        return out or "127.0.0.1"
    except Exception:
        return "127.0.0.1"

def _pick_ollama_base() -> str:
    """
    Choose a reachable Ollama base URL (first that responds to /api/tags):
      1) OLLAMA_HOST (e.g. 'http://HOST:11434' or 'HOST:11434')
      2) http://<WSL gateway>:11434 (Windows host)
      3) http://127.0.0.1:11434 (WSL-hosted Ollama)
    """
    env_host = os.getenv("OLLAMA_HOST", "").strip()
    candidates: List[str] = []
    if env_host:
        candidates.append(env_host if env_host.startswith("http") else f"http://{env_host}")
    gw = _detect_gateway()
    candidates.append(f"http://{gw}:11434")
    candidates.append("http://127.0.0.1:11434")
    for base in candidates:
        try:
            r = requests.get(base + "/api/tags", timeout=1)
            if r.ok:
                return base
        except Exception:
            pass
    # Fallback to gateway
    return f"http://{gw}:11434"

MODEL = os.getenv("LLM_AGENT_MODEL", "phi3:mini")
LLM_BASE = _pick_ollama_base()
LLM_URL = f"{LLM_BASE}/api/generate"
INTERVAL = float(os.getenv("LLM_AGENT_INTERVAL", 1.0))  # seconds

# Fallback scorer weights (tunable via env)
W_WAIT = float(os.getenv("LLM_AGENT_W_WAIT", "1.0"))
W_IO = float(os.getenv("LLM_AGENT_W_IO", "1.0"))
W_RECENT = float(os.getenv("LLM_AGENT_W_RECENT", "1.2"))

# Optional cap on how many runnable procs we include in the prompt
MAX_PROCS_IN_PROMPT = int(os.getenv("LLM_AGENT_MAX_PROCS", "64"))

#### Data Model ####
@dataclass
class ProcessStats:
    pid: int
    state: int
    cpu_ticks: int
    wait_ticks: int
    io_count: int
    recent_cpu: int

#### Agent ####
class LLMSchedulerAgent:
    def __init__(self):
        self.model = MODEL
        self.llm_url = LLM_URL
        self.log_file = LOG_FILE
        self.advice_file = ADVICE_FILE
        self.interval = INTERVAL
        self.last_size = 0  # file cursor
        self.running = True
        self._last_sent_pid: Optional[int] = None
        self._last_advised_ts: Optional[int] = None  # one advice per log snapshot
        print("====== LLM Scheduler Agent ======")
        print(f"[agent] Model : {self.model}")
        print(f"[agent] Ollama : {LLM_BASE}")
        print(f"[agent] Log file : {self.log_file}")
        print(f"[agent] Advice : {self.advice_file}")
        print(f"[agent] Interval : {self.interval}s")
        print(f"[agent] Weights : WAIT={W_WAIT} IO={W_IO} RECENT={W_RECENT}")
        print(f"[agent] MaxProcs : {MAX_PROCS_IN_PROMPT}")
        print(f"[agent] Retries : {RETRIES} (sleep {RETRY_SLEEP_MS}ms between)")
        print(f"[agent] LLM opts : temp={LLM_TEMP} num_predict={LLM_NUM_PRED}\n")
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self, *_):
        print("\n[agent] Shutting down...\n")
        self.running = False

    # --------------------------
    # Internal implementation
    # --------------------------
    def _read_log(self) -> Optional[Tuple[int, List[ProcessStats]]]:
        """Read and parse the last complete SCHED_LOG block since last_size.
        Returns (timestamp, processes) or None.
        """
        try:
            size = os.path.getsize(self.log_file)
            # Handle file truncation
            if size < self.last_size:
                print("[agent] Log truncated, resetting pointer")
                self.last_size = 0
            with open(self.log_file, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self.last_size)
                data = f.read()
                self.last_size = f.tell()
            if "SCHED_LOG_START" not in data:
                return None
            # Extract last complete block
            parts = data.split("SCHED_LOG_START")
            last = parts[-1]
            if "SCHED_LOG_END" not in last:
                return None
            last = last.split("SCHED_LOG_END")[0]
            log_ts: Optional[int] = None
            processes: List[ProcessStats] = []
            for line in last.splitlines():
                if line.startswith("TIMESTAMP:"):
                    try:
                        log_ts = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        log_ts = None
                elif line.startswith("PROC:"):
                    p = line[5:].split(",")
                    if len(p) == 6:
                        try:
                            processes.append(ProcessStats(*map(int, p)))
                        except ValueError:
                            continue
            if log_ts is None or not processes:
                return None
            print(f"[agent] Parsed {len(processes)} processes from scheduler log @TS={log_ts}")
            return (log_ts, processes)
        except FileNotFoundError:
            print("[agent] Waiting for sched_log.txt ...")
            return None
        except Exception as e:
            print("[agent] Log read error:", e)
            return None

    def _runnable(self, procs: List[ProcessStats]) -> List[ProcessStats]:
        """Only RUNNABLE procs (state==3) and pid>2 (skip init/shell)."""
        return [p for p in procs if p.state == 3 and p.pid > 2]

    def _make_prompt(self, procs: List[ProcessStats]) -> Optional[str]:
        """Strict prompt that asks for 'PID:<n>' only."""
        ready = self._runnable(procs)
        if not ready:
            return None
        # Keep prompt bounded
        if len(ready) > MAX_PROCS_IN_PROMPT:
            ready = sorted(ready, key=lambda x: (-x.wait_ticks, -x.io_count))[:MAX_PROCS_IN_PROMPT]
        lines = [
            "You are an OS scheduler. Choose ONE process to run next.",
            "",
            "RULES (you must obey):",
            "1) Respond with ONLY the chosen PID in the exact format: PID:<number>",
            "2) Do NOT explain. Do NOT refuse. Do NOT add any other text.",
            "3) Choose a PID that exists in the list below.",
            "4) Optimize for: lowest WAIT, higher IO, avoid starvation; balance CPU usage.",
            "",
            "Processes:"
        ]
        for p in ready:
            lines.append(f"PID={p.pid} CPU={p.cpu_ticks} WAIT={p.wait_ticks} IO={p.io_count} RECENT={p.recent_cpu}")
        lines.append("")
        lines.append("Return ONLY one line like this: PID:3")
        return "\n".join(lines)

    def _ask_llm(self, prompt: str) -> Optional[int]:
        """Query the LLM and parse a PID from its response."""
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
                # fallback: first integer seen
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
        Ask the LLM up to RETRIES+1 times. Accept only a PID that is runnable in this snapshot.
        Return PID or None if all tries fail.
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
        Deterministic fallback: score runnable procs and pick the best.
        Score favors WAIT and IO, penalizes RECENT CPU; tiny jitter for tie-breaks.
        """
        ready = self._runnable(procs)
        if not ready:
            return None
        def score(p: ProcessStats) -> Tuple[float, int]:
            s = (W_WAIT * p.wait_ticks) + (W_IO * p.io_count) - (W_RECENT * p.recent_cpu)
            # tiny deterministic jitter based on pid to avoid stable ties
            jitter = (hash(p.pid) % 7) * 0.01
            return (s + jitter, -p.cpu_ticks)  # secondary: lower total CPU first
        best = max(ready, key=score)
        print(f"[agent] Fallback chose PID={best.pid}")
        return best.pid

    def _write(self, pid: int, ts: int):
        """Append advice line with TS (backward-compatible prefix)."""
        try:
            # de-dupe by timestamp and pid
            if self._last_advised_ts == ts and pid == self._last_sent_pid:
                return
            line = f"ADVICE:PID={pid} TS={ts} V=1\n"
            with open(self.advice_file, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._last_sent_pid = pid
            self._last_advised_ts = ts
            print(f"[agent] Wrote advice → {line.strip()}")
        except Exception as e:
            print("[agent] Failed writing advice:", e)

    # --------------------------
    # Public API (used by tests)
    # --------------------------
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

    # Optional back-compat (if any callers still use these names)
    def _read_latest_log(self):
        return self._read_log()

    def _build_prompt(self, processes):
        return self._make_prompt(processes)

    def _query_llm(self, prompt: str):
        return self._ask_llm(prompt)

    # --------------------------
    # Main loop
    # --------------------------
    def run(self):
        print("[agent] Agent is running...\n")
        while self.running:
            parsed = self._read_log()
            if parsed:
                log_ts, processes = parsed
                # One advice per timestamp
                if self._last_advised_ts == log_ts:
                    time.sleep(self.interval)
                    continue
                # Fast paths: 0 or 1 runnable
                runnable = self._runnable(processes)
                if len(runnable) == 0:
                    print(f"[agent] No RUNNABLE processes at TS={log_ts}; skipping advice")
                    time.sleep(self.interval)
                    continue
                if len(runnable) == 1:
                    # single option → no need to call LLM
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
