// init: The initial user-level program.
//
// In this version we keep the console shell on stdin/stdout/stderr and
// do NOT auto-start llmhelper, since having llmhelper read from the
// console would steal input from the shell. You can run llmhelper
// manually from the shell once a separate input path is wired up.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "kernel/spinlock.h"
#include "kernel/sleeplock.h"
#include "kernel/fs.h"
#include "kernel/file.h"
#include "user/user.h"
#include "kernel/fcntl.h"

char *argv_sh[]   = { "sh", 0 };
char *argv_llm[]  = { "llmhelper", 0 }; // currently unused; kept for future use

int
main(void)
{
  int pid, wpid;

  if(open("console", O_RDWR) < 0){
    mknod("console", CONSOLE, 0);
    open("console", O_RDWR);
  }
  dup(0);  // stdout
  dup(0);  // stderr

  // NOTE: We intentionally do NOT auto-start llmhelper here.
  // If llmhelper reads from fd 0 (console), it will compete with
  // the shell for input and make the system hard to use.
  // Once a separate advice input path exists, you can re-enable
  // a background llmhelper and log its PID.
  //
  // Example (commented out on purpose):
  //
  // pid = fork();
  // if(pid < 0){
  //   printf("init: fork llmhelper failed\n");
  // } else if(pid == 0){
  //   exec("llmhelper", argv_llm);
  //   printf("init: exec llmhelper failed\n");
  //   exit(1);
  // }
  // printf("init: started llmhelper (pid=%d)\n", pid);

  for(;;){
    printf("init: starting sh\n");
    pid = fork();
    if(pid < 0){
      printf("init: fork failed\n");
      exit(1);
    }
    if(pid == 0){
      exec("sh", argv_sh);
      printf("init: exec sh failed\n");
      exit(1);
    }

    // Parent: record which PID is the shell so we can correlate with logs.
    printf("init: started sh (pid=%d)\n", pid);

    for(;;){
      // this call to wait() returns if the shell exits,
      // or if a parentless process exits.
      wpid = wait((int *) 0);
      if(wpid == pid){
        // the shell exited; restart it.
        break;
      } else if(wpid < 0){
        printf("init: wait returned an error\n");
        exit(1);
      } else {
        // it was a parentless process; do nothing.
      }
    }
  }
}
