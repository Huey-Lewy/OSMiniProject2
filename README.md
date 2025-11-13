# About OSMiniProject2

**OSMiniProject2** is an experimental extension of the **xv6 operating system** that incorporates an **external LLM agent** to give the kernel **real-time scheduling advice**.

## Repository Layout

```
├── agent/                 # Python LLM bridge
│   ├── agent_bridge.py          # Reads xv6 logs, sends scheduling advice
│   └── test_agent.py            # Test harness for mock communication
│
├── shared/                # Shared communication directory
│   ├── sched_log.txt            # Scheduler log (produced by xv6)
│   └── llm_advice.txt           # LLM-generated advice (read by xv6)
│
├── xv6/
│   ├── Makefile
│   ├── kernel/
│   │   ├── defs.h                 # Adds prototypes for scheduling stats + LLM advice
│   │   ├── proc.h                 # Adds proc fields for scheduling stats + LLM advice
│   │   ├── proc.c                 # Adds tick accounting, state logging, advice storage, scheduler use
│   │   ├── sysfile.c              # Increments io_count on FS operations
│   │   ├── sysproc.c              # Implements sys_set_llm_advice
│   │   ├── syscall.c              # Adds syscall to dispatch table
│   │   ├── syscall.h              # Defines SYS_set_llm_advice number
│   │   ├── trap.c                 # Calls update_sched_stats_on_tick on timer interrupts
│   │   └── ... (Other kernel files unchanged)
│   └── user/
│       ├── llmhelper.c            # Reads stdin and calls set_llm_advice
│       ├── cpubound.c             # CPU-bound workload
│       ├── iobound.c              # IO-bound workload
│       ├── mixed.c                # Mixed CPU/IO workload
│       ├── user.h                 # Declares set_llm_advice
│       ├── usys.pl                # Generates syscall stub for set_llm_advice
│       └── ... (Other user programs unchanged)
│
├── runner.py              # Orchestrates xv6 + QEMU + agent communication
├── requirements.txt       # Python dependencies
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
