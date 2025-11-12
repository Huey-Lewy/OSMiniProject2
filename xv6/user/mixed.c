#include "kernel/types.h"
#include "kernel/stat.h"
#include "user.h"

int main(void) {
  int i, j, x = 0;
  for (i = 0; i < 100; i++) {
    // CPU burst
    for (j = 0; j < 1000000; j++) {
      x += j;
    }
    // I/O operation
    printf("Mixed %d\n", i);
    sleep(2);
  }
  printf("Mixed finished\n");
  exit(0);
}