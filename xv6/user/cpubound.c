// user/cpubound.c
// CPU-bound workload used for scheduler testing and LLM advisor evaluation.
//
// Each worker:
//   - runs a tight arithmetic loop for its share of the total iterations
//   - does not print inside the inner loop, to stay CPU-heavy
//   - optionally breaks work into chunks and pause()s between chunks
//     so the scheduler (influenced by LLM advice) has more chances
//     to pick other PIDs.
//
// Usage:
//   cpubound [total_iters] [workers] [chunks] [sleep_ticks]
//
//   total_iters   - total iterations across *all* workers
//   workers       - number of worker processes (parent + children)
//   chunks        - how many chunks each worker splits its work into
//                   (0 or 1 => single big chunk, no extra pauses)
//   sleep_ticks   - if >0 and chunks>1, call pause(sleep_ticks)
//                   between chunks
//
// Examples:
//   cpubound
//     # default: total_iters=200000000, workers=4, chunks=1 (no pause)
//
//   cpubound 80000000 8
//     # 8 workers, 80M total iterations (~10M each), single chunk
//
//   cpubound 40000000 4 20 2
//     # 4 workers, 40M total, each worker does 20 chunks and pauses
//     # for 2 ticks between chunks (lots of scheduler/LLM decisions).

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

int
main(int argc, char *argv[])
{
  // Total iterations across *all* workers.
  int total_iters = 200000000;  // 2e8: heavy but okay for testing
  int workers     = 4;          // parent + children (total worker procs)
  int chunks      = 1;          // per-worker chunks (1 => no extra chunking)
  int sleep_ticks = 0;          // pause() between chunks if >0 and chunks>1

  if(argc >= 2){
    int v = atoi(argv[1]);
    if(v > 0)
      total_iters = v;
  }
  if(argc >= 3){
    int v = atoi(argv[2]);
    if(v > 0)
      workers = v;
  }
  if(argc >= 4){
    int v = atoi(argv[3]);
    if(v > 0)
      chunks = v;
  }
  if(argc >= 5){
    int v = atoi(argv[4]);
    if(v > 0)
      sleep_ticks = v;
  }

  // Clamp workers to a sane range.
  if(workers < 1)
    workers = 1;
  if(workers > 16)
    workers = 16;

  // At least one chunk.
  if(chunks < 1)
    chunks = 1;

  // Split work roughly evenly across workers.
  int local_iters = total_iters / workers;
  if(local_iters < 1)
    local_iters = 1;

  // Chunk size for this worker.
  int chunk_iters = local_iters / chunks;
  if(chunk_iters < 1)
    chunk_iters = 1;

  int parent_pid = getpid();

  printf("cpubound: parent pid=%d, workers=%d, total_iters=%d, "
         "per_worker=%d, chunks=%d, chunk_iters=%d, sleep=%d\n",
         parent_pid, workers, total_iters,
         local_iters, chunks, chunk_iters, sleep_ticks);

  // Fork workers-1 children; each child breaks out and runs its own loop.
  for(int i = 0; i < workers - 1; i++){
    int pid = fork();
    if(pid < 0){
      printf("cpubound: fork failed\n");
      break;
    }
    if(pid == 0){
      // Child: do not fork further.
      break;
    }
  }

  int mypid = getpid();
  printf("cpubound(pid=%d): starting CPU-intensive work (iters=%d)\n",
         mypid, local_iters);

  volatile int acc = 0;
  int done = 0;
  int chunk = 0;

  while(done < local_iters){
    int this_chunk = chunk_iters;
    if(this_chunk > (local_iters - done))
      this_chunk = local_iters - done;

    // CPU burst: tight arithmetic loop.
    for(int i = 0; i < this_chunk; i++){
      acc += (done + i);
    }
    done += this_chunk;
    chunk++;

    // Optional pause between chunks to give scheduler/LLM more chances
    // to run other processes. This will also contribute to io_count if
    // you instrument pause() that way, but is useful for testing.
    if(sleep_ticks > 0 && chunks > 1 && done < local_iters){
      printf("cpubound(pid=%d): chunk %d/%d done, pausing %d ticks\n",
             mypid, chunk, chunks, sleep_ticks);
      pause(sleep_ticks);
    }
  }

  printf("cpubound(pid=%d): finished (acc=%d)\n",
         mypid, acc & 0x7fffffff);

  // Only the original parent waits for children, to avoid zombies.
  if(mypid == parent_pid){
    int w;
    while((w = wait(0)) > 0){
      printf("cpubound: child %d exited\n", w);
    }
    printf("cpubound(pid=%d): all children finished\n", parent_pid);
  }

  exit(0);
}
