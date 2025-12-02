# OSMiniProject2

**OSMiniProject2** is an experimental extension of the **xv6 operating system** that uses an **external LLM agent** (running via Ollama on Windows) to provide **real-time scheduling advice** to the kernel.

The high-level idea:

- xv6 periodically logs per-process scheduler stats into a shared text file.
- A Python **agent bridge** (running in WSL) reads those logs and queries an LLM for “which PID to run next”.
- The agent writes its decision back to a shared advice file.
- xv6 reads the advice and uses it to influence its scheduler.

## Repository Layout

```text
├── agent/
│   ├── agent_bridge.py      # Reads scheduler logs, queries Ollama, writes ADVICE lines
│   ├── analyze_results.py   # Offline analysis: parses shared/sched_log.txt and plots CPU/wait/IO
│   ├── test_agent.py        # Unit-style tests: log parsing, prompt generation, LLM connectivity
│   ├── test_xv6.py          # Sends synthetic SCHED_LOG blocks → checks agent PID choices
│   └── test_scheduling.py   # Full simulated scheduler that uses agent advice end-to-end
│
├── shared/
│   ├── sched_log.txt        # Scheduler snapshots (produced by the modified xv6 kernel)
│   └── llm_advice.txt       # Advice lines (written by agent_bridge.py, consumed by xv6)
│
├── xv6/
│   ├── Makefile             # Adds llmhelper + test workloads to UPROGS
│   ├── kernel/
│   │   ├── defs.h           # Prototypes for scheduling-stat helpers + set_llm_advice
│   │   ├── proc.h           # Extended struct proc: cpu_ticks, wait_ticks, io_count, recent_cpu
│   │   ├── proc.c           # Tick accounting, state logging, scheduler uses LLM advice
│   │   ├── sysproc.c        # sys_set_llm_advice, increments io_count via sys_sleep
│   │   ├── syscall.c        # Adds SYS_set_llm_advice to syscall dispatch table
│   │   ├── syscall.h        # Defines syscall number
│   │   ├── trap.c           # Tick-based stat updates + SCHED_LOG interval triggers
│   │   └── ... (others unchanged)
│   └── user/
│       ├── llmhelper.c      # Reads ADVICE:PID=X from stdin, calls set_llm_advice()
│       ├── cpubound.c       # CPU-heavy workload
│       ├── iobound.c        # IO-heavy workload
│       ├── mixed.c          # Mixed CPU/IO workload
│       ├── init.c           # Spawns llmhelper at boot
│       ├── user.h           # Declares set_llm_advice()
│       ├── usys.pl          # Generates user stub for set_llm_advice()
│       └── ... (others unchanged)
│
├── requirements.txt
├── LICENSE
└── README.md
```

## Software Requirements

### Windows 11 (Host)

* [Ollama](https://ollama.com/)
* At least one local model, e.g. `phi3:mini` (or any compatible Ollama model)

### Ubuntu 22.04 (WSL)

* `qemu-system-misc` (for xv6 / QEMU)
* `python3-venv` (for virtual env + Python tooling)
* Python 3.10+ recommended

## 🖥️ Windows 11 – Ollama Setup

Run these in **PowerShell** (or a similar terminal) on Windows:

```powershell
# 1. Install Ollama via winget
winget install Ollama.Ollama

# 2. Verify installation
ollama --version

# 3. Pull a model (example: phi3:mini)
ollama pull phi3:mini

# 4. Listen on all interfaces so WSL can reach Ollama
setx OLLAMA_HOST "0.0.0.0:11434"

# 5. Restart Ollama to apply OLLAMA_HOST
taskkill /IM ollama.exe /F 2>$null
ollama serve
```

## 🐧 Ubuntu 22.04 (WSL) – Project Setup

```bash
# 1. Install dependencies
sudo apt update
sudo apt install -y qemu-system-misc python3-venv

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

You'll need to use **three terminals** (one on Windows, two in WSL). All paths below assume you're in the project root unless noted.

### 🖥️ Terminal A (Windows 11): Start Ollama

```bash
# Start the Ollama LLM server (already configured to listen on 0.0.0.0:11434)
ollama serve
```

### 🧠 Terminal B (Ubuntu WSL): Start the Agent Bridge

```bash
# From the project root

# Clear any existing logs from sched_log.txt and llm_advice.txt
: > shared/sched_log.txt
: > shared/llm_advice.txt

# Make sure the shared dir and FIFO exist at the ROOT level
mkdir -p shared
[ -p shared/llm_advice.fifo ] || mkfifo shared/llm_advice.fifo

# Start the agent from the agent/ directory
cd agent
python3 agent_bridge.py
```

The agent will:

* Tail `shared/sched_log.txt` for new `SCHED_LOG_START` / `SCHED_LOG_END` blocks.
* For each snapshot, call Ollama with a strict “pick one PID” prompt.
* Write decisions as `ADVICE:PID=<n> TS=<ts> V=1` lines into `shared/llm_advice.txt`.

### 🧩 Terminal C (Ubuntu WSL): Build and Run xv6

```bash
# From the project root
cd xv6

# (Re)build kernel + filesystem image
make clean
make fs.img kernel/kernel CPUS=1

# Run QEMU with the input/output pipeline:
#   console_mux → QEMU stdin
#   QEMU stdout/stderr → sched_log_splitter
python3 ../agent/console_mux.py ../shared/llm_advice.fifo \
  | qemu-system-riscv64 \
      -machine virt \
      -bios none \
      -kernel kernel/kernel \
      -m 256M \
      -smp 1 \
      -nographic \
      -global virtio-mmio.force-legacy=false \
      -drive file=fs.img,if=none,format=raw,id=x0 \
      -device virtio-blk-device,drive=x0,bus=virtio-mmio-bus.0 \
  | python3 ../agent/sched_log_splitter.py
```

At runtime:

* The modified xv6 kernel periodically logs scheduler snapshots into `shared/sched_log.txt`.
* The user-space helper `llmhelper` reads `shared/llm_advice.txt` and calls the `set_llm_advice()` syscall.
* The xv6 scheduler reads the current advice and uses it to influence which process to run next.

## System Flow

```text
          ┌───────────────────────────── Windows 11 ─────────────────────────────┐
          │                                                                      │
          │    Ollama server                                                     │
          │    (phi3:mini, HTTP API)                                             │
          │                                                                      │
          └───────────────────────↑──────────────────────────────────────────────┘
                                  │ HTTP (LLM calls)
                                  │
       ┌──────────────────────────┴─────────────────────────────┐
       │                        WSL / Ubuntu                    │
       │                                                        │
       │  agent_bridge.py                                       │
       │    ├─ tails shared/sched_log.txt  (scheduler logs)     │
       │    ├─ calls Ollama over HTTP                           │
       │    ├─ writes ADVICE:PID=... to:                        │
       │    │     • shared/llm_advice.txt  (log)                │
       │    │     • shared/llm_advice.fifo  (live pipe)         │
       │                                                        │
       │  console_mux.py                                        │
       │    ├─ reads:                                           │
       │    │     • your keyboard (stdin)                       │
       │    │     • shared/llm_advice.fifo                      │
       │    └─ merges both → QEMU stdin                         │
       │                                                        │
       │  qemu-system-riscv64 (xv6)                             │
       │    └─ stdout/stderr → sched_log_splitter.py            │
       │                                                        │
       │  sched_log_splitter.py                                 │
       │    ├─ reads QEMU output                                │
       │    ├─ strips SCHED_LOG_* blocks → shared/sched_log.txt │
       │    └─ forwards everything else → your terminal         │
       │                                                        │
       └──────────────────────────↓─────────────────────────────┘
                                  │
                                  │ console (stdin/stdout) over QEMU
                                  ↓
                  ┌─────────────────────────────────────────────┐
                  │                 xv6 kernel                  │
                  │                                             │
                  │  init (input router)                        │
                  │    ├─ only process that reads /dev/console  │
                  │    ├─ echoes what you type back to console  │
                  │    ├─ if line starts with "ADVICE:PID=":    │
                  │    │     → send to llmhelper via pipe       │
                  │    └─ else:                                 │
                  │          → send to sh via pipe              │
                  │                                             │
                  │  sh (shell)                                 │
                  │    └─ reads commands from its pipe          │
                  │       (still “feels” interactive to you)    │
                  │                                             │
                  │  llmhelper                                  │
                  │    └─ reads ADVICE:PID=... lines from pipe  │
                  │       → calls set_llm_advice(pid) syscall   │
                  │                                             │
                  │  scheduler                                  │
                  │    ├─ logs SCHED_LOG_* snapshots            │
                  │    ├─ consults latest LLM advice            │
                  │    └─ biases RUNNABLE selection accordingly │
                  └─────────────────────────────────────────────┘
```

## Offline Analysis

After running experiments, you can generate basic plots from the captured scheduler logs:

```bash
# From the project root
cd agent
python3 analyze_results.py
```

This reads `shared/sched_log.txt` and produces a PNG summary (CPU ticks, wait ticks, and I/O counts over time) for each PID, saved in the `shared/` project directory.
