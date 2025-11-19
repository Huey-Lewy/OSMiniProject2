// user/iobound.c
// I/O-bound workload used for scheduler testing and LLM advisor evaluation.
// Produces console output and calls pause() between operations so the
// process spends most of its time blocked rather than doing CPU work.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  // Defaults:
  //   - 1000 I/O operations
  //   - pause(5) ticks between operations
  int iterations  = 1000;
  int sleep_ticks = 5;

  if(argc >= 2){
    int v = atoi(argv[1]);
    if(v > 0)
      iterations = v;
  }
  if(argc >= 3){
    int v = atoi(argv[2]);
    if(v > 0)
      sleep_ticks = v;
  }

  printf("iobound: starting I/O-heavy loop (iters=%d, sleep=%d ticks)\n",
         iterations, sleep_ticks);

  for(int i = 0; i < iterations; i++){
    // Console printing acts as I/O activity that should correlate
    // with the kernel's I/O accounting.
    printf("iobound: I/O op %d/%d\n", i + 1, iterations);

    // Pause to simulate blocking on I/O; this hits the sys_pause()
    // syscall, which we instrumented for io_count.
    if(sleep_ticks > 0)
      pause(sleep_ticks);
  }

  printf("iobound: finished\n");
  exit(0);
}
