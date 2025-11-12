#!/usr/bin/env python3
# Simple tester for the LLM Scheduler Agent.
# Runs local tests for log parsing, prompt formatting, and LLM connectivity.

import os
import re
import time
import tempfile
from agent_bridge import LLMSchedulerAgent, ProcessStats


#### Sample Log Generator ####
def _write_sample_log(path: str):
    """
    Create a fake scheduler log for testing.
    """
    sample_log = """SCHED_LOG_START
TIMESTAMP:100
PROC:1,3,50,10,5,30
PROC:2,3,25,15,8,12
PROC:3,2,5,20,12,2
PROC:4,2,8,30,20,5
SCHED_LOG_END
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(sample_log)
    print(f"[log] Sample log written to {path}")


#### Helpers ####
def _extract_pids_from_prompt(prompt: str) -> set[int]:
    """
    Return the set of PIDs mentioned in a prompt produced by the agent.
    Supports both legacy 'Process 3:' and new 'PID=3' / 'PID: 3' formats.
    """
    ids = set(int(x) for x in re.findall(r"\bPID\s*[=:]\s*(\d+)", prompt))
    ids |= set(int(x) for x in re.findall(r"\bProcess\s+(\d+)\s*:", prompt))
    return ids


#### Parser Test ####
def test_log_parsing(agent: LLMSchedulerAgent) -> bool:
    """Test that the agent reads and parses process entries correctly."""
    print("==== Test: Log Parsing ====")
    with tempfile.TemporaryDirectory() as td:
        fake_log = os.path.join(td, "test_sched_log.txt")
        _write_sample_log(fake_log)

        # Point the agent at our temp file and reset its cursor
        agent.log_file = fake_log
        agent.last_log_size = 0  # property backed by agent.last_size

        processes = agent.read_scheduling_log()  # public API
        if not processes:
            print("[x] Failed to parse scheduler log (no processes returned).")
            return False

        print(f"[✓] Parsed {len(processes)} processes:")
        for p in processes:
            print(
                f"  [proc] PID={p.pid} STATE={p.state} "
                f"CPU={p.cpu_ticks} WAIT={p.wait_ticks} IO={p.io_count} RECENT={p.recent_cpu}"
            )
        print("[log] Parsed successfully.\n")
        return True


#### Prompt Builder Test ####
def test_prompt(agent: LLMSchedulerAgent) -> bool:
    """Test that the scheduling prompt is formatted correctly."""
    print("==== Test: Prompt Generation ====")
    sample_data = [
        ProcessStats(pid=3, state=2, cpu_ticks=5,  wait_ticks=20, io_count=12, recent_cpu=2),  # RUNNABLE
        ProcessStats(pid=4, state=2, cpu_ticks=8,  wait_ticks=30, io_count=20, recent_cpu=5),  # RUNNABLE
        ProcessStats(pid=2, state=3, cpu_ticks=25, wait_ticks=15, io_count=8,  recent_cpu=12), # NOT RUNNABLE
    ]
    prompt = agent.format_prompt_for_llm(sample_data)  # public API
    if not prompt:
        print("[x] Prompt generation returned empty/None.")
        return False

    print("[log] Generated scheduling prompt:\n")
    print(prompt)
    print("[log] End of prompt.\n")

    # Only RUNNABLE (state==2) PIDs should appear.
    pids_in_prompt = _extract_pids_from_prompt(prompt)
    ok = (3 in pids_in_prompt) and (4 in pids_in_prompt) and (2 not in pids_in_prompt)
    if not ok:
        print(f"[x] Unexpected PIDs in prompt. Found: {sorted(pids_in_prompt)}; expected to include 3,4 and exclude 2.")
    else:
        print("[✓] Prompt includes only RUNNABLE PIDs and expected fields.\n")
    return ok


#### Connectivity Test ####
def test_llm_connection(agent: LLMSchedulerAgent) -> bool:
    """
    Connectivity & parsing test against Ollama.
    Set SKIP_OLLAMA_TEST=1 to skip this test.
    """
    if os.getenv("SKIP_OLLAMA_TEST") == "1":
        print("==== Test: Ollama Connectivity — SKIPPED (SKIP_OLLAMA_TEST=1) ====\n")
        return True

    print("==== Test: Ollama Connectivity (Multi-Step) ====")

    expected_pids = [3, 1, 2]
    success_count = 0

    for pid_expected in expected_pids:
        test_prompt_text = (
            "You are a scheduling advisor.\n"
            f"Respond with exactly: PID: {pid_expected}\n"
            "No extra words."
        )

        print(f"[log] Sending prompt expecting PID:{pid_expected} ...")
        start_time = time.time()
        pid = agent.query_llm(test_prompt_text)  # public API
        elapsed = time.time() - start_time
        print(f"[log] Query completed in {elapsed:.2f}s")

        if pid == pid_expected:
            print(f"[✓] Correct — got PID={pid}\n")
            success_count += 1
        elif pid is None:
            print(f"[x] No valid PID parsed (expected {pid_expected}).\n")
        else:
            print(f"[!] Unexpected PID={pid} (expected {pid_expected}).\n")

        time.sleep(0.3)

    print(f"[log] Completed {success_count}/{len(expected_pids)} PID checks.\n")
    return success_count == len(expected_pids)


#### Run All Tests ####
def main():
    print("====== LLM Agent Test Suite ======\n")
    agent = LLMSchedulerAgent()  # uses your defaults (Ollama URL/model, shared paths)

    tests = [
        test_log_parsing,
        test_prompt,
        test_llm_connection,
    ]

    passed = 0
    for test_func in tests:
        print("----------------------------------------")
        try:
            ok = test_func(agent)
            if ok:
                passed += 1
        except Exception as e:
            print(f"[x] Test {test_func.__name__} failed: {e}\n")
        print("----------------------------------------\n")

    print(f"Completed {passed}/{len(tests)} tests.\n")


if __name__ == "__main__":
    main()
