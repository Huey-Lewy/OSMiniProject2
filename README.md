# About OSMiniProject2

**OSMiniProject2** is an experimental extension of the **xv6 operating system** that incorporates an **external LLM agent** to give the kernel **real-time scheduling advice**.

## Repository Layout

```
├── agent/
│   ├── agent_bridge.py      # Reads scheduler logs, queries LLM, writes ADVICE lines
│   ├── test_agent.py        # Tests: log parsing, prompt generation, LLM connectivity
│   ├── test_xv6.py          # Synthetic SCHED_LOG feed → verifies PID choices
│   └── test_scheduling.py   # Full simulated scheduler using agent advice
│
├── shared/
│   ├── sched_log.txt        # Produced by xv6 (via runner.py)
│   └── llm_advice.txt       # Written by agent_bridge.py, consumed by xv6
│
├── xv6/
│   ├── Makefile             # Adds llmhelper and workloads to UPROGS
│   ├── kernel/
│   │   ├── defs.h           # Prototypes for scheduling stat helpers + set_llm_advice
│   │   ├── proc.h           # Extended struct proc: cpu_ticks, wait_ticks, io_count, recent_cpu
│   │   ├── proc.c           # Tick accounting, state logging, scheduler uses advice
│   │   ├── sysproc.c        # sys_set_llm_advice, increments io_count via sys_sleep
│   │   ├── syscall.c        # Adds SYS_set_llm_advice to dispatch table
│   │   ├── syscall.h        # Defines syscall number
│   │   ├── trap.c           # Tick-based process stat updates + log interval triggers
│   │   └── ... (others unchanged)
│   └── user/
│       ├── llmhelper.c      # Reads ADVICE:PID=X from stdin, calls set_llm_advice
│       ├── cpubound.c       # CPU-heavy workload
│       ├── iobound.c        # IO-heavy workload
│       ├── mixed.c          # Mixed CPU/IO workload
│       ├── init.c           # Spawns llmhelper at boot
│       ├── user.h           # Declares set_llm_advice()
│       ├── usys.pl          # Generates user stub
│       └── ... (others unchanged)
│
├── runner.py                # Streams xv6 output, extracts logs, pipes ADVICE into xv6
├── analyze_results.py       # Parses sched_log.txt → generates CPU/wait/IO plots
├── requirements.txt
├── LICENSE
└── README.md
```

## Software Requirements

**Windows 11**

* Ollama (installed via console in setup below)
* Local model: `phi3:mini` (or any supported Ollama model)

**Ubuntu 22.04 (WSL)**

* `qemu-system-misc`
* `python3-venv`

## 🖥️ Windows 11 – Installation Setup

```bash
# 1. Install Ollama via PowerShell
winget install Ollama.Ollama

# 2. Verify installation
ollama --version

# 3. Pull a model (example)
ollama pull phi3:mini

# 4. Allow Ollama access from WSL
setx OLLAMA_HOST "0.0.0.0:11434"

# 5. Restart Ollama
taskkill /IM ollama.exe /F 2>nul
ollama serve
```

## 🐧 Ubuntu 22.04 (WSL) – Installation Setup

```bash
# 1. Install Dependencies
sudo apt-get update
sudo apt-get install -y qemu-system-misc python3-venv

# 2. Clone the repository
git clone https://github.com/Huey-Lewy/OSMiniProject2
cd OSMiniProject2

# 3. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4. Install Python dependencies
pip install -r requirements.txt
```

## Run Instructions

### 🖥️ Windows 11 (Terminal A)

```bash
# Start the Ollama LLM server
ollama serve
```

### 🧠 Ubuntu WSL (Terminal B)

```bash
# Start the LLM scheduler bridge
python3 agent/agent_bridge.py
```

### 🧩 Ubuntu WSL (Terminal C)

```bash
# Build and launch xv6 with LLM integration
python3 runner.py
```

## System Flow

```
Ollama (Windows)
   ⇅
agent_bridge.py (WSL)
   ⇅
shared/{sched_log.txt, llm_advice.txt}
   ⇅
runner.py (WSL)
   ⇅
xv6 kernel → llmhelper → scheduler
```
