#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"
#include "kernel/fcntl.h"

char *argv[] = { "sh", 0 };  // For shell

int
main(void)
{
  int pid, wpid;

  if(open("console", O_RDWR) < 0){
    mknod("console", 1, 1);
    open("console", O_RDWR);
  }
  dup(0);  // stdout
  dup(0);  // stderr

  // Start llmhelper in background (as per spec)
  pid = fork();
  if(pid < 0){
    printf("init: fork failed\n");
    exit(1);
  }
  if(pid == 0){
    exec("llmhelper", argv);  // argv can be reused since llmhelper takes no args
    printf("init: exec llmhelper failed\n");
    exit(1);
  }

  // Automatically start cpubound in background
  pid = fork();
  if(pid < 0){
    printf("init: fork failed\n");
    exit(1);
  }
  if(pid == 0){
    exec("cpubound", argv);
    printf("init: exec cpubound failed\n");
    exit(1);
  }

  // Automatically start iobound in background
  pid = fork();
  if(pid < 0){
    printf("init: fork failed\n");
    exit(1);
  }
  if(pid == 0){
    exec("iobound", argv);
    printf("init: exec iobound failed\n");
    exit(1);
  }

  // Automatically start mixed in background
  pid = fork();
  if(pid < 0){
    printf("init: fork failed\n");
    exit(1);
  }
  if(pid == 0){
    exec("mixed", argv);
    printf("init: exec mixed failed\n");
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
      exec("sh", argv);
      printf("init: exec sh failed\n");
      exit(1);
    }

    // Wait for children to exit, reaping zombies
    while((wpid=wait(0)) >= 0 && wpid != pid)
      printf("zombie!\n");
  }
}