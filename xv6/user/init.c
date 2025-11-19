// init: The initial user-level program.
//
// This version also starts a background LLM helper process
// that reads scheduling advice from stdin and injects it into
// the kernel via the set_llm_advice() syscall.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "kernel/spinlock.h"
#include "kernel/sleeplock.h"
#include "kernel/fs.h"
#include "kernel/file.h"
#include "user/user.h"
#include "kernel/fcntl.h"

char *argv_sh[]   = { "sh", 0 };
char *argv_llm[]  = { "llmhelper", 0 };

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

  // Start the LLM helper once at boot. It runs in the background
  // and listens for ADVICE:PID=... lines on its stdin.
  pid = fork();
  if(pid < 0){
    printf("init: fork llmhelper failed\n");
  } else if(pid == 0){
    exec("llmhelper", argv_llm);
    printf("init: exec llmhelper failed\n");
    exit(1);
  }

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
