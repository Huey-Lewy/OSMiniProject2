// user/mixed.c
// Mixed workload used for scheduler testing and LLM advisor evaluation.
// Alternates between CPU-intensive bursts and I/O + blocking pauses so
// the process shows both CPU-bound and I/O-bound behavior over time.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  // Defaults:
  //   - 100 outer iterations
  //   - 1,000,000 inner loop steps per iteration (CPU burst)
  //   - pause(2) ticks after each burst (I/O-style blocking)
  int iterations  = 100;
  int inner_loops = 1000000;
  int sleep_ticks = 2;

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

  printf("mixed: starting (iters=%d, inner_loops=%d, sleep=%d ticks)\n",
         iterations, inner_loops, sleep_ticks);

  int x = 0;
  for(int i = 0; i < iterations; i++){
    // CPU burst: tight arithmetic loop.
    for(int j = 0; j < inner_loops; j++){
      x += j;
    }

    // I/O + blocking phase: console output plus pause().
    printf("mixed: iteration %d/%d complete\n", i + 1, iterations);
    if(sleep_ticks > 0)
      pause(sleep_ticks);
  }

  // Use x so the compiler keeps the CPU work.
  printf("mixed: finished (final x=%d)\n", x);
  exit(0);
}
