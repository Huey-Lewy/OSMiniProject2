// user/init.c
// init: The initial user-level program.
//
// In this version, init also acts as a small input router:
//   - It is the *only* process that reads from the real console (fd 0).
//   - It forwards normal lines to the shell via a pipe.
//   - It forwards lines starting with "ADVICE:PID=" to llmhelper via a
//     separate pipe.
//
// This keeps the shell interactive on the console, while allowing
// llmhelper to receive LLM advice without stealing console input.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "kernel/spinlock.h"
#include "kernel/sleeplock.h"
#include "kernel/fs.h"
#include "kernel/file.h"
#include "user/user.h"
#include "kernel/fcntl.h"

char *argv_sh[]  = { "sh", 0 };
char *argv_llm[] = { "llmhelper", 0 };

#define LINE_BUF 512

// Simple helper to check whether a line starts with "ADVICE:PID=".
static int
is_advice_line(char *s)
{
  const char *prefix = "ADVICE:PID=";
  int i;

  for(i = 0; prefix[i] != 0; i++){
    if(s[i] != prefix[i])
      return 0;
  }
  return 1;
}

// Router loop: read from the real console (fd 0), one line at a time,
// and forward it to either the shell pipe (sh_fd) or the llm pipe (llm_fd)
// depending on the prefix.
static void
router_loop(int sh_fd, int llm_fd)
{
  char buf[LINE_BUF];
  int n = 0;

  for(;;){
    char c;
    int r = read(0, &c, 1);
    if(r < 1){
      // EOF or error on console; nothing more to route.
      exit(0);
    }

    // xv6 uses '\r' for enter; normalize to '\n' for convenience.
    if(c == '\r')
      c = '\n';

    if(c == '\n'){
      buf[n] = 0;

      // Classify and forward this completed line.
      if(n > 0 && is_advice_line(buf)){
        if(llm_fd >= 0){
          write(llm_fd, buf, n);
          write(llm_fd, "\n", 1);
        }
      } else {
        if(sh_fd >= 0){
          write(sh_fd, buf, n);
          write(sh_fd, "\n", 1);
        }
      }

      n = 0;  // reset buffer for next line
    } else {
      if(n < LINE_BUF - 1){
        buf[n++] = c;
      }
      // If the line is too long, we silently truncate; the router will
      // still deliver something sensible to the shell or llmhelper.
    }
  }
}

int
main(void)
{
  int shpipe[2];
  int llmpipe[2];
  int pid, wpid;

  // Ensure the console device exists, then hook stdin/stdout/stderr to it.
  if(open("console", O_RDWR) < 0){
    mknod("console", CONSOLE, 0);
    open("console", O_RDWR);
  }
  dup(0);  // stdout
  dup(0);  // stderr

  // Create pipes:
  //   shpipe:   init/router writes, shell reads.
  //   llmpipe:  init/router writes, llmhelper reads.
  if(pipe(shpipe) < 0 || pipe(llmpipe) < 0){
    printf("init: pipe failed\n");
    exit(1);
  }

  // Fork a child that will act purely as the input router. It is the
  // only process that reads from the real console (fd 0).
  pid = fork();
  if(pid < 0){
    printf("init: fork router failed\n");
    exit(1);
  }
  if(pid == 0){
    // Router child.
    // Close the read ends of the pipes; we only write into them.
    close(shpipe[0]);
    close(llmpipe[0]);

    // The router inherits fd 0/1/2 pointing at the console.
    // It will read from fd 0 and forward lines into the pipes.
    router_loop(shpipe[1], llmpipe[1]);

    // Should never return.
    exit(0);
  }

  // Parent (manager): keep the read ends, we will hand them to children.
  // We do not write into the pipes from here.
  close(shpipe[1]);
  close(llmpipe[1]);

  // Start llmhelper once at boot. It listens on its stdin (llmpipe[0])
  // for ADVICE:PID=... lines routed by the router process.
  pid = fork();
  if(pid < 0){
    printf("init: fork llmhelper failed\n");
  } else if(pid == 0){
    // Child: llmhelper
    close(0);
    dup(llmpipe[0]);     // stdin from llm pipe
    close(llmpipe[0]);   // no longer need the original fd
    close(shpipe[0]);    // not used in this process

    exec("llmhelper", argv_llm);
    printf("init: exec llmhelper failed\n");
    exit(1);
  }
  printf("init: started llmhelper (pid=%d)\n", pid);

  // Main loop: (re)start the shell whenever it exits. The shell's stdin
  // comes from shpipe[0], which is fed by the router.
  for(;;){
    printf("init: starting sh\n");
    int shpid = fork();
    if(shpid < 0){
      printf("init: fork sh failed\n");
      exit(1);
    }
    if(shpid == 0){
      // Child: shell
      close(0);
      dup(shpipe[0]);     // stdin from shell pipe
      close(shpipe[0]);   // no longer need the original fd
      close(llmpipe[0]);  // not used in this process

      exec("sh", argv_sh);
      printf("init: exec sh failed\n");
      exit(1);
    }

    // Parent: record which PID is the shell so we can correlate with logs.
    printf("init: started sh (pid=%d)\n", shpid);

    // Wait until the shell exits; restart it if needed.
    for(;;){
      wpid = wait((int *) 0);
      if(wpid == shpid){
        // The shell exited; restart it in the outer loop.
        break;
      } else if(wpid < 0){
        printf("init: wait returned an error\n");
        exit(1);
      } else {
        // Some other child (e.g., a zombie from user processes); ignore.
      }
    }
  }
}
