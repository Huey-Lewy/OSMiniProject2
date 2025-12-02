// user/mixed.c
// Mixed workload used for scheduler testing and LLM advisor evaluation.
// Alternates between CPU-intensive bursts and I/O + blocking pauses so
// the process shows both CPU-bound and I/O-bound behavior over time.
// You can also run multiple worker processes, like cpubound/iobound.
//
// Usage:
//   mixed [iterations] [inner_loops] [sleep_ticks] [workers]
//
// Defaults:
//   iterations   = 150      // outer iterations
//   inner_loops  = 50000    // CPU loop per iteration
//   sleep_ticks  = 20       // pause() ticks after each burst
//   workers      = 1        // parent only (no extra children)
//
// With these defaults and ticks ≈10ms, each worker sleeps for about
// 150 * 20 = 3000 ticks (≈30 seconds), plus CPU bursts.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  int iterations  = 150;
  int inner_loops = 50000;
  int sleep_ticks = 20;
  int workers     = 1;   // parent + children (total worker processes)

  if(argc >= 2){
    int v = atoi(argv[1]);
    if(v > 0)
      iterations = v;
  }
  if(argc >= 3){
    int v = atoi(argv[2]);
    if(v > 0)
      inner_loops = v;
  }
  if(argc >= 4){
    int v = atoi(argv[3]);
    if(v > 0)
      sleep_ticks = v;
  }
  if(argc >= 5){
    int v = atoi(argv[4]);
    if(v > 0)
      workers = v;
  }

  // Clamp workers to a sane range.
  if(workers < 1)
    workers = 1;
  if(workers > 16)
    workers = 16;

  int parent_pid = getpid();

  printf("mixed: parent pid=%d, workers=%d, iterations=%d, inner_loops=%d, sleep=%d\n",
         parent_pid, workers, iterations, inner_loops, sleep_ticks);

  // Fork workers-1 children; each child breaks out and runs the mixed loop.
  for(int i = 0; i < workers - 1; i++){
    int pid = fork();
    if(pid < 0){
      printf("mixed: fork failed\n");
      break;
    }
    if(pid == 0){
      // Child: do not fork further.
      break;
    }
  }

  int mypid = getpid();
  printf("mixed(pid=%d): starting (iters=%d, inner_loops=%d, sleep=%d ticks)\n",
         mypid, iterations, inner_loops, sleep_ticks);

  volatile int x = 0;
  for(int i = 0; i < iterations; i++){
    // CPU burst: tight arithmetic loop.
    for(int j = 0; j < inner_loops; j++){
      x += (j ^ mypid);
    }

    // I/O + blocking phase: console output plus pause().
    printf("mixed(pid=%d): iteration %d/%d complete\n", mypid, i + 1, iterations);
    if(sleep_ticks > 0)
      pause(sleep_ticks);
  }

  // Use x so the compiler keeps the CPU work.
  printf("mixed(pid=%d): finished (final x=%d)\n", mypid, x);

  // Only the original parent waits for children, to avoid zombies.
  if(mypid == parent_pid){
    int w;
    while((w = wait(0)) > 0){
      printf("mixed: child %d exited\n", w);
    }
    printf("mixed(pid=%d): all children finished\n", parent_pid);
  }

  exit(0);
}
