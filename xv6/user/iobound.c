#include "types.h"
#include "stat.h"
#include "user.h"

int main(void) {
  int i;
  for (i = 0; i < 1000; i++) {
    printf(1, "I/O op %d\n", i);
    sleep(5);
  }
  printf(1, "I/O-bound finished\n");
  exit(0);
}