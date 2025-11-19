// user/cpubound.c
// CPU-bound workload used for scheduler testing and LLM advisor evaluation.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  // Default iteration count: keeps the CPU busy for a while.
  // You can override this with:
  //   cpubound <iterations>
  int iters = 1000000000;  // 1e9

  if(argc >= 2){
    int v = atoi(argv[1]);
    if(v > 0){
      iters = v;
    }
  }

  printf("cpubound: starting CPU-intensive loop (iters=%d)\n", iters);

  // Tight arithmetic loop to generate pure CPU load.
  // acc is volatile so the compiler keeps the loop body.
  volatile long long acc = 0;
  for(int i = 0; i < iters; i++){
    acc += i;
    // No printing inside the loop: we want CPU-bound behavior, not I/O.
  }

  printf("cpubound: finished (acc=%d)\n", (int)(acc & 0x7fffffff));
  exit(0);
}
