// user/iobound.c
// I/O-bound workload used for scheduler testing and LLM advisor evaluation.
//
// Each worker:
//   - prints "I/O op X/Y" lines
//   - calls pause() between operations, spending most time blocked
//
// Usage:
//   iobound [total_iters] [sleep_ticks] [workers]
//
// Example:
//   iobound            # default: total_iters=400, sleep=5, workers=4
//   iobound 800 3 6    # 6 workers, 800 total ops (≈133 each), sleep 3 ticks

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  // Total I/O operations across *all* workers.
  int total_iters = 400;  // short but representative
  int sleep_ticks = 5;    // pause between ops (I/O-like blocking)
  int workers     = 4;    // parent + children (total worker processes)

  if(argc >= 2){
    int v = atoi(argv[1]);
    if(v > 0)
      total_iters = v;
  }
  if(argc >= 3){
    int v = atoi(argv[2]);
    if(v > 0)
      sleep_ticks = v;
  }
  if(argc >= 4){
    int v = atoi(argv[3]);
    if(v > 0)
      workers = v;
  }

  // Clamp workers to sane range.
  if(workers < 1)
    workers = 1;
  if(workers > 16)
    workers = 16;

  // Split work roughly evenly across workers.
  int local_iters = total_iters / workers;
  if(local_iters < 1)
    local_iters = 1;

  int parent_pid = getpid();

  printf("iobound: parent pid=%d, workers=%d, total_iters=%d, per_worker=%d, sleep=%d\n",
         parent_pid, workers, total_iters, local_iters, sleep_ticks);

  // Fork workers-1 children; each child breaks out of this loop and just runs the loop.
  for(int i = 0; i < workers - 1; i++){
    int pid = fork();
    if(pid < 0){
      printf("iobound: fork failed\n");
      break;
    }
    if(pid == 0){
      // Child: do not fork further.
      break;
    }
  }

  int mypid = getpid();
  printf("iobound(pid=%d): starting I/O-heavy loop (iters=%d, sleep=%d ticks)\n",
         mypid, local_iters, sleep_ticks);

  for(int i = 0; i < local_iters; i++){
    // Console printing acts as I/O activity that should correlate with the kernel's I/O accounting.
    printf("iobound(pid=%d): I/O op %d/%d\n", mypid, i + 1, local_iters);

    // Pause to simulate blocking on I/O; this hits sys_pause(), which you instrument for io_count.
    if(sleep_ticks > 0)
      pause(sleep_ticks);
  }

  printf("iobound(pid=%d): finished\n", mypid);

  // Only the original parent waits for children, to avoid zombies.
  if(mypid == parent_pid){
    int w;
    while((w = wait(0)) > 0){
      printf("iobound: child %d exited\n", w);
    }
    printf("iobound(pid=%d): all children finished\n", parent_pid);
  }

  exit(0);
}
