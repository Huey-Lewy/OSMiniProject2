#include "types.h"
#include "stat.h"
#include "user.h"

int main(void) {
  int i, x = 0;
  for (i = 0; i < 1000000000; i++) {
    x += i;
  }
  printf(1, "CPU-bound finished\n");
  exit(0);
}