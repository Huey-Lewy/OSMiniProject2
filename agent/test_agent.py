# agent/test_agent.py
# Simple tester for the LLM Scheduler Agent.
# Runs local tests for log parsing, prompt formatting, and LLM connectivity.

import os
import time
from agent_bridge import LLMSchedulerAgent, ProcessStats


#### Sample Log Generator ####
def _write_sample_log(path):
    """
    Create a fake scheduler log for testing. (runs in temp file)
    """
    sample_log = """SCHED_LOG_START
TIMESTAMP:100
PROC:1,3,50,10,5,30
PROC:2,3,25,15,8,12
PROC:3,2,5,20,12,2
PROC:4,2,8,30,20,5
SCHED_LOG_END
"""
    with open(path, "w") as f:
        f.write(sample_log)
    print(f"[log] Sample log written to {path}")


#### Parser Test ####
def test_log_parsing(agent):
    """Test that the agent reads and parses process entries correctly."""
    print("==== Test: Log Parsing ====")
    fake_log = "test_sched_log.txt"
    _write_sample_log(fake_log)

    agent.log_file = fake_log
    processes = agent._read_latest_log()
    if not processes:
        print("[x] Failed to parse scheduler log.")
        return False

    print(f"[✓] Parsed {len(processes)} processes:")
    for p in processes:
        print(f"  [proc] PID={p.pid} STATE={p.state} CPU={p.cpu_ticks} WAIT={p.wait_ticks} IO={p.io_count} RECENT={p.recent_cpu}")
    os.remove(fake_log)
    print("[log] Temporary log file removed.\n")
    return True


#### Prompt Builder Test ####
def test_prompt(agent):
    """Test that the scheduling prompt is formatted correctly."""
    print("==== Test: Prompt Generation ====")
    sample_data = [
        ProcessStats(pid=3, state=2, cpu_ticks=5, wait_ticks=20, io_count=12, recent_cpu=2),
        ProcessStats(pid=4, state=2, cpu_ticks=8, wait_ticks=30, io_count=20, recent_cpu=5),
    ]
    prompt = agent._build_prompt(sample_data)

    print("[log] Generated scheduling prompt:\n")
    print(prompt)
    print("[log] End of prompt.\n")
    return True


#### Connectivity Test ####
def test_llm_connection(agent):
    """Run a multi-step connectivity test with sequential PID checks."""
    print("==== Test: Ollama Connectivity (Multi-Step) ====")

    expected_pids = [3, 1, 2]  # mix up the order to avoid pattern assumptions
    success_count = 0

    for pid_expected in expected_pids:
        test_prompt_text = (
            f"You are a scheduling advisor.\n"
            f"This is step expecting PID:{pid_expected}.\n"
            f"Respond *only* with: PID:{pid_expected}\n"
        )

        print(f"[log] Sending prompt expecting PID:{pid_expected} ...")
        start_time = time.time()
        pid = agent._query_llm(test_prompt_text)
        elapsed = time.time() - start_time
        print(f"[log] Query completed in {elapsed:.2f}s")

        if pid == pid_expected:
            print(f"[✓] Correct — got PID={pid}\n")
            success_count += 1
        elif pid:
            print(f"[!] Unexpected PID={pid} (expected {pid_expected})\n")
        else:
            print(f"[x] No valid PID response for step expecting {pid_expected}\n")

        # small pause to simulate scheduler pacing
        time.sleep(0.5)

    print(f"[log] Completed {success_count}/{len(expected_pids)} PID checks.\n")
    return success_count == len(expected_pids)


#### Run All Tests ####
def main():
    """Main entry point for test runner."""
    print("====== LLM Agent Test Suite ======\n")
    agent = LLMSchedulerAgent()

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


#### Run as Script ####
if __name__ == "__main__":
    main()
