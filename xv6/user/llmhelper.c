// user/llmhelper.c
// Reads advice lines from stdin and injects them into the kernel scheduler.
// Expected format from host/agent:  ADVICE:PID=<number>\n

#include "kernel/types.h"
#include "kernel/stat.h"
#include "user/user.h"

#define TOK "ADVICE:PID="

static int
is_digit(char c)
{
  return c >= '0' && c <= '9';
}

static int
startswith(const char *s, const char *pfx)
{
  while(*pfx) {
    if(*s != *pfx) return 0;
    s++; pfx++;
  }
  return 1;
}

// Scan a buffer for one or more "ADVICE:PID=<n>" tokens.
// For each token found, call set_llm_advice(<n>).
static void
process_advice_line(const char *line)
{
  for (int i = 0; line[i]; i++) {
    if (line[i] == 'A' && startswith(&line[i], TOK)) {
      const char *p = &line[i] + sizeof(TOK) - 1;
      int pid = 0, have = 0;

      while (is_digit(*p)) {
        pid = pid * 10 + (*p - '0');
        p++;
        have = 1;
      }

      if (have) {
        if (set_llm_advice(pid) == 0)
          printf("[llmhelper] Injected PID %d\n", pid);
        else
          printf("[llmhelper] Failed to inject PID %d\n", pid);
      }
    }
  }
}

int
main(int argc, char *argv[])
{
  // Simple line buffer; the agent writes newline-terminated advice.
  char buf[256];
  int  n = 0;

  printf("[llmhelper] Ready. Waiting for advice on stdin...\n");

  for (;;) {
    int r = read(0, buf + n, sizeof(buf) - 1 - n);
    if (r > 0) {
      int end = n + r;
      buf[end] = 0;

      // Process complete lines.
      int start = 0;
      for (int i = n; i < end; i++) {
        if (buf[i] == '\n') {
          buf[i] = 0;                  // terminate this line
          process_advice_line(&buf[start]);
          start = i + 1;               // next segment
        }
      }

      // Move any partial tail to the front.
      if (start < end) {
        int tail = end - start;
        memmove(buf, &buf[start], tail);
        n = tail;
      } else {
        n = 0;
      }

      // If buffer is close to full without newline, process to avoid stall.
      if (n >= (int)sizeof(buf) - 8) {
        buf[n] = 0;
        process_advice_line(buf);
        n = 0;
      }
    } else {
      // No input right now; yield briefly.
      pause(10);
    }
  }
}
