# agent/agent_bridge.py
# Runs the LLM Scheduler Agent that connects xv6 logs to a local Ollama model.

import os
import re
import time
import signal
import requests
import subprocess
from dataclasses import dataclass
from typing import List, Optional


#### Auto-Configuration ####
def _detect_gateway():
    """
    Detect Windows host gateway IP for WSL (e.g., 172.x.x.x).
    Fallback to localhost if detection fails.
    """
    try:
        output = subprocess.check_output(
            "ip route show | grep -i default | awk '{print $3}'",
            shell=True,
            text=True
        ).strip()
        return output if output else "127.0.0.1"
    except Exception:
        return "127.0.0.1"


#### Constants ####
GATEWAY = _detect_gateway()
MODEL = "phi3:mini"
LLM_URL = f"http://{GATEWAY}:11434/api/generate"
LOG_FILE = "../shared/sched_log.txt"
ADVICE_FILE = "../shared/llm_advice.txt"
INTERVAL = float(os.getenv("LLM_AGENT_INTERVAL", 1.5))  # seconds


#### Data Structures ####
@dataclass
class ProcessStats:
    """Single process statistics parsed from xv6 scheduler logs."""
    pid: int
    state: int
    cpu_ticks: int
    wait_ticks: int
    io_count: int
    recent_cpu: int


#### Agent Logic ####
class LLMSchedulerAgent:
    """Bridge between xv6 logs and local Ollama LLM."""

    def __init__(self):
        self.model = MODEL
        self.llm_url = LLM_URL
        self.log_file = LOG_FILE
        self.advice_file = ADVICE_FILE
        self.interval = INTERVAL
        self.last_log_size = 0
        self._running = True

        print("====== LLM Scheduler Agent ======")
        print(f"[agent] Model: {self.model}")
        print(f"[agent] Gateway: {GATEWAY}")
        print(f"[agent] Log: {self.log_file}")
        print(f"[agent] Advice: {self.advice_file}")
        print(f"[agent] Poll interval: {self.interval}s\n")

        # Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)

    def _handle_exit(self, *_):
        """Handle Ctrl+C or SIGTERM cleanly."""
        print("\n[agent] Termination signal received. Shutting down.\n")
        self._running = False

    #### Log Reading ####
    def _read_latest_log(self) -> Optional[List[ProcessStats]]:
        """Read latest xv6 scheduler log block."""
        try:
            with open(self.log_file, "r") as f:
                f.seek(self.last_log_size)
                new_data = f.read()
                self.last_log_size = f.tell()

            if not new_data or "SCHED_LOG_START" not in new_data:
                return None

            blocks = new_data.split("SCHED_LOG_START")
            last_block = blocks[-1]
            if "SCHED_LOG_END" not in last_block:
                return None

            processes = []
            for line in last_block.splitlines():
                if line.startswith("PROC:"):
                    parts = line[5:].split(",")
                    if len(parts) == 6:
                        processes.append(ProcessStats(
                            pid=int(parts[0]),
                            state=int(parts[1]),
                            cpu_ticks=int(parts[2]),
                            wait_ticks=int(parts[3]),
                            io_count=int(parts[4]),
                            recent_cpu=int(parts[5])
                        ))

            return processes or None

        except FileNotFoundError:
            print(f"[agent] Waiting for log file: {self.log_file}")
            return None
        except Exception as e:
            print(f"[x] Error reading log: {e}")
            return None

    #### Prompt Builder ####
    def _build_prompt(self, processes: List[ProcessStats]) -> str:
        """Build scheduling prompt for the LLM."""
        runnable = [p for p in processes if p.state == 2 and p.pid > 2]
        if not runnable:
            return "No runnable processes found."

        prompt = (
            "You are a scheduling advisor for an operating system.\n"
            "Each process line shows: PID, CPU time, Wait time, I/O count, and Recent CPU usage.\n\n"
        )
        for p in runnable:
            prompt += f"PID {p.pid}: CPU={p.cpu_ticks}, WAIT={p.wait_ticks}, IO={p.io_count}, RECENT={p.recent_cpu}\n"

        prompt += (
            "\nGoals:\n"
            "1. Minimize wait time.\n"
            "2. Prioritize I/O-bound tasks.\n"
            "3. Avoid starvation.\n"
            "4. Balance CPU load.\n\n"
            "Respond only with: PID:<number>\n"
        )
        return prompt

    #### LLM Query ####
    def _query_llm(self, prompt: str, retries: int = 1) -> Optional[int]:
        """Query Ollama model. Retry once if transient failure occurs."""
        try:
            response = requests.post(
                self.llm_url,
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=8
            )

            if response.status_code != 200:
                print(f"[x] Ollama returned HTTP {response.status_code}. Is it running?")
                return None

            text = response.json().get("response", "").strip()
            match = re.search(r"PID[:\s]+(\d+)", text)
            if match:
                pid = int(match.group(1))
                print(f"[✓] LLM advised PID={pid}")
                return pid

            print(f"[!] No valid PID found in LLM output: {text[:80]}")
            return None

        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            if retries > 0:
                print(f"[!] Connection issue ({e}); retrying once...")
                time.sleep(0.5)
                return self._query_llm(prompt, retries=retries - 1)
            print("[x] Cannot reach Ollama after retry. Check gateway or Ollama service.")
            return None
        except Exception as e:
            print(f"[x] LLM query failed: {e}")
            return None

    #### Write Advice ####
    def _write_advice(self, pid: int):
        """Write selected PID to advice file for xv6 scheduler."""
        try:
            with open(self.advice_file, "w") as f:
                f.write(f"ADVICE:PID={pid}\n")
            print(f"[log] Advice file updated with PID={pid}\n")
        except Exception as e:
            print(f"[x] Error writing advice file: {e}")

    #### Main Loop ####
    def run(self):
        """Main runtime loop."""
        print("[agent] Scheduler Agent started.\n")

        while self._running:
            processes = self._read_latest_log()
            if processes:
                prompt = self._build_prompt(processes)
                pid = self._query_llm(prompt)
                if pid:
                    self._write_advice(pid)
            time.sleep(self.interval)

        print("[agent] Agent stopped.\n")


#### Run as Script ####
if __name__ == "__main__":
    agent = LLMSchedulerAgent()
    agent.run()
