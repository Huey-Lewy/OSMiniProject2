#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "memlayout.h"
#include "spinlock.h"
#include "proc.h"
#include "vm.h"

// LLM advice state lives in the scheduler (proc.c).
// This syscall only updates that shared state.
extern struct spinlock llm_lock;
extern int  llm_recommended_pid;
extern int  llm_advice_valid;
extern uint llm_advice_timestamp;

uint64
sys_exit(void)
{
  int n;
  argint(0, &n);
  kexit(n);
  return 0;  // not reached
}

uint64
sys_getpid(void)
{
  return myproc()->pid;
}

uint64
sys_fork(void)
{
  return kfork();
}

uint64
sys_wait(void)
{
  uint64 p;
  struct proc *cur = myproc();

  // Waiting for a child is a blocking-style operation from the
  // scheduler's point of view, so treat it as I/O-like activity.
  if(cur != 0) {
    cur->io_count++;
  }

  argaddr(0, &p);
  return kwait(p);
}

uint64
sys_sbrk(void)
{
  uint64 addr;
  int t;
  int n;

  argint(0, &n);
  argint(1, &t);
  addr = myproc()->sz;

  if(t == SBRK_EAGER || n < 0) {
    if(growproc(n) < 0) {
      return -1;
    }
  } else {
    // Lazily allocate memory for this process: increase its memory
    // size but don't allocate memory. If the process uses the
    // memory, vmfault() will allocate it.
    if(addr + n < addr)
      return -1;
    if(addr + n > TRAPFRAME)
      return -1;
    myproc()->sz += n;
  }
  return addr;
}

uint64
sys_pause(void)
{
  int n;
  uint ticks0;
  struct proc *p = myproc();

  // Count pause as an I/O-style blocking event so the scheduler
  // can treat it like a simple sleep-like syscall.
  if(p != 0) {
    p->io_count++;
  }

  argint(0, &n);
  if(n < 0)
    n = 0;
  acquire(&tickslock);
  ticks0 = ticks;
  while(ticks - ticks0 < (uint)n){
    if(killed(myproc())){
      release(&tickslock);
      return -1;
    }
    sleep(&ticks, &tickslock);
  }
  release(&tickslock);
  return 0;
}

uint64
sys_kill(void)
{
  int pid;

  argint(0, &pid);
  return kkill(pid);
}

// return how many clock tick interrupts have occurred
// since start.
uint64
sys_uptime(void)
{
  uint xticks;

  acquire(&tickslock);
  xticks = ticks;
  release(&tickslock);
  return xticks;
}

// Inject LLM scheduling advice into the kernel.
//
// User space (llmhelper) calls set_llm_advice(pid),
// which is wired to this syscall. The scheduler reads
// llm_recommended_pid / llm_advice_valid under llm_lock
// and will try to run that PID next, subject to sanity checks.
uint64
sys_set_llm_advice(void)
{
  int pid = -1;

  // argint() has void return type in this tree; it writes into pid.
  argint(0, &pid);

  // Simple sanity check; the scheduler will do the final validation.
  if(pid <= 0)
    return -1;

  acquire(&llm_lock);
  llm_recommended_pid  = pid;
  llm_advice_valid     = 1;
  llm_advice_timestamp = ticks;
  release(&llm_lock);

  return 0;
}
