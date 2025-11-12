#include "kernel/types.h"
#include "kernel/stat.h"
#include "user.h"

int main(void) {
  int i;
  for (i = 0; i < 1000; i++) {
    printf("I/O op %d\n", i);
    sleep(5);
  }
  printf("I/O-bound finished\n");
  exit(0);
}