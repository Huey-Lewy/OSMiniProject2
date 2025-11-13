// user/llmhelper.c
// Reads lines from stdin, parses ADVICE, and injects it via syscall.
//
// Accepted formats:
//   ADVICE:PID=<n>
//   ADVICE:PID=<n> TS=<t> V=1
//
// Usage inside xv6:
//   $ llmhelper
//   (type lines or have the host/runner pipe them to stdin)

#include "kernel/types.h"
#include "user/user.h"

// Trim leading spaces/tabs/CR
static const char*
ltrim(const char *s)
{
  while(*s == ' ' || *s == '\t' || *s == '\r') s++;
  return s;
}

static int
startswith(const char *s, const char *prefix)
{
  int i = 0;
  while(prefix[i]){
    if(s[i] != prefix[i]) return 0;
    i++;
  }
  return 1;
}

// Parse one advice line.
// Returns 0 on success and fills *out_pid, *out_ts (ts defaults to 0 if absent).
// Returns -1 if not a valid advice line.
static int
parse_advice_line(const char *line, int *out_pid, int *out_ts)
{
  int pid = -1;
  int ts  = 0;

  const char *p = ltrim(line);
  if(!startswith(p, "ADVICE:PID="))
    return -1;

  p += 11; // skip "ADVICE:PID="
  pid = atoi((char*)p);
  if(pid <= 0)
    return -1;

  // scan for optional TS=<t> anywhere after PID
  const char *q = p;
  while(*q){
    if(q[0] == 'T' && q[1] == 'S' && q[2] == '='){
      ts = atoi((char*)(q + 3));
      break;
    }
    q++;
  }

  *out_pid = pid;
  *out_ts  = ts;
  return 0;
}

// Simple blocking line reader from stdin (fd=0).
// Fills buf (up to max-1) and NUL-terminates; returns length (excl NUL),
// or 0 on EOF/error.
static int
readline(char *buf, int max)
{
  int i = 0;
  for(;;){
    char c;
    int r = read(0, &c, 1);
    if(r < 1){
      if(i == 0) return 0; // nothing read
      break;
    }
    if(c == '\n') break;
    if(i < max - 1) buf[i++] = c; // truncate if too long
  }
  buf[i] = 0;
  return i;
}

int
main(int argc, char **argv)
{
  char buf[256];

  printf("llmhelper: listening on stdin for advice lines...\n");
  printf("llmhelper: formats: 'ADVICE:PID=N' or 'ADVICE:PID=N TS=T V=1'\n");

  for(;;){
    int n = readline(buf, sizeof(buf));
    if(n <= 0){
      // avoid busy loop (~100ms)
      pause(10);
      continue;
    }

    int pid, ts;
    if(parse_advice_line(buf, &pid, &ts) == 0){
      int rc = set_llm_advice(pid, ts);
      if(rc == 0){
        printf("ACK PID=%d TS=%d\n", pid, ts);
      } else {
        // kernel rejected (e.g., PID not RUNNABLE)
        printf("REJECT PID=%d TS=%d\n", pid, ts);
      }
    } else {
      // Not an advice line; ignore quietly (uncomment to debug).
      // printf("IGN '%s'\n", buf);
    }
  }

  // not reached
  return 0;
}
