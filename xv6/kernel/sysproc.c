#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "memlayout.h"
#include "spinlock.h"
#include "proc.h"
#include "vm.h"

// ---- LLM-advised scheduler: syscall to set advice from user space ----
// kernel helper + proc[] access
extern int set_llm_advice_kernel(int pid, int ts);
extern struct proc proc[NPROC];


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
    // size but don't allocate memory. If the processes uses the
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

  argint(0, &n);
  if(n < 0)
    n = 0;
  acquire(&tickslock);
  ticks0 = ticks;
  while(ticks - ticks0 < n){
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

// Record LLM scheduling advice (PID + optional TS) into the kernel's advice slot
uint64
sys_set_llm_advice(void)
{
  int pid, ts = 0;

  // argint() has no return value in this tree; it simply writes into the out param.
  // If the caller omits the second argument, ts ends up with whatever argint wrote.
  argint(0, &pid);
  argint(1, &ts);

  // Minimal validation: only allow advice for processes that are currently RUNNABLE.
  // This avoids accepting advice for nonexistent, sleeping, or zombie processes.
  struct proc *p;
  int ok = 0;
  for(p = proc; p < &proc[NPROC]; p++){
    acquire(&p->lock);
    if(p->pid == pid && p->state == RUNNABLE)
      ok = 1;
    release(&p->lock);
    if(ok) break;
  }
  if(!ok)
    return -1;

  // Store the advice for the scheduler to consume.
  return set_llm_advice_kernel(pid, ts);
}
