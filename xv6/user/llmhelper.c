// user/llmhelper.c
// Small helper that reads LLM scheduling advice from stdin and
// injects it into the kernel via set_llm_advice(pid).
//
// In the intended design, init routes only advice lines into
// llmhelper's stdin via a dedicated pipe, *not* the interactive
// console. Each line is expected to have the form:
//
//   ADVICE:PID=<n> TS=<ts> V=1
//
// Only the PID field matters; everything else is ignored.

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

#define BUF_SZ 512

// Parse a single line. If it matches ADVICE:PID=<n>..., call set_llm_advice(n).
static void
handle_line(char *line)
{
  // Strip leading spaces.
  while(*line == ' ' || *line == '\t')
    line++;

  const char *prefix = "ADVICE:PID=";
  int plen = 11; // strlen("ADVICE:PID=")
  int i;

  // Require the exact prefix.
  for(i = 0; i < plen; i++) {
    if(line[i] != prefix[i])
      return;
  }

  char *p = line + plen;

  // PID must start with a digit.
  if(*p < '0' || *p > '9')
    return;

  int pid = 0;
  while(*p >= '0' && *p <= '9') {
    pid = pid * 10 + (*p - '0');
    p++;
  }

  if(pid <= 0)
    return;

  // Best-effort: ignore errors, but print a hint on failure.
  if(set_llm_advice(pid) < 0) {
    printf("llmhelper: set_llm_advice(%d) failed\n", pid);
  } else {
    // Lightweight debug so we can see when advice is applied.
    printf("llmhelper: applied advice for pid %d\n", pid);
  }
}

int
main(int argc, char *argv[])
{
  char buf[BUF_SZ];
  int n;
  int start = 0; // start index of unprocessed data in buf
  int end   = 0; // one past last valid byte in buf

  printf("llmhelper: started, waiting for LLM advice on stdin...\n");

  for(;;) {
    // If buffer is full and no newline, drop it to avoid deadlock.
    if(end >= BUF_SZ - 1 && start == 0) {
      // Not ideal, but advice is periodic, so dropping a bad chunk is ok.
      end = 0;
    }

    n = read(0, buf + end, BUF_SZ - 1 - end);
    if(n <= 0)
      break; // EOF or error; just exit.

    end += n;
    buf[end] = 0; // keep it null-terminated for safety

    // Scan for complete lines.
    int i = start;
    while(i < end) {
      if(buf[i] == '\n') {
        buf[i] = 0;          // terminate this line
        handle_line(&buf[start]);
        start = i + 1;       // next line starts after '\0'
      }
      i++;
    }

    // Compact buffer if there is leftover partial line.
    if(start == end) {
      // All data consumed.
      start = 0;
      end = 0;
    } else if(start > 0) {
      // Move partial line to the beginning.
      int remaining = end - start;
      memmove(buf, buf + start, remaining);
      start = 0;
      end = remaining;
      buf[end] = 0;
    }
  }

  printf("llmhelper: exiting (input closed)\n");
  exit(0);
}
