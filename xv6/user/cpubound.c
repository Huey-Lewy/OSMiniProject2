#include "kernel/types.h"
#include "kernel/stat.h"
#include "user.h"

int main(void) {
  int i, x = 0;
  for (i = 0; i < 1000000000; i++) {
    x += i;
  }
  printf("CPU-bound finished\n");
  exit(0);
}