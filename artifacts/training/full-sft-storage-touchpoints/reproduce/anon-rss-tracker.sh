#!/bin/bash
# Reproduce kit — optional anon-rss tracker
#
# Captures python's anon-rss + system memory at 5-sec cadence during a training
# run. Use it in a SECOND SSH session, started before any of the run-*.sh scripts.
# The tracker polls for the python process by name, attaches when it appears,
# samples until python exits.
#
# Usage:
#   In a second SSH to the same host:
#     ./anon-rss-tracker.sh
#   The tracker writes to $EXP_ROOT/logs/anon-rss-<timestamp>.log AND to stdout.
#
# Verifies (after a successful run):
#   - python anon-rss peak (VmHWM) ~47 GiB during the load + first-checkpoint window
#   - steady-state plateau ~3 GiB python RSS (CUDA-managed UMA tensors don't show
#     in VmRSS — they show in `free -h shared` at ~40 GiB)
#   - system used reaches 121 GiB at peak first-checkpoint write (UMA at-the-edge)
#
# The pgrep regex anchors at ^python3 to exclude any sh/bash wrapper whose argv
# happens to contain "python3 examples/llm_finetune/finetune.py" as a substring.
# Without the anchor, head -1 picks the lower-PID wrapper instead of python.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/full-sft-touchpoints-reproduce}"
mkdir -p "$EXP_ROOT/logs"

LOG="$EXP_ROOT/logs/anon-rss-$(date +%Y%m%d-%H%M%S).log"

echo "=== anon-rss tracker armed at $(date -Iseconds), waiting for python ===" | tee -a "$LOG"
while true; do
  PID=$(pgrep -f "^python3 examples/llm_finetune/finetune.py" | head -1)
  if [ -n "$PID" ] && [ -d "/proc/$PID" ]; then
    echo "=== detected python PID $PID at $(date -Iseconds) ===" | tee -a "$LOG"
    break
  fi
  sleep 1
done

while [ -d "/proc/$PID" ]; do
  if [ -r "/proc/$PID/status" ]; then
    RSS=$(awk '/^VmRSS:/ {print $2}' /proc/$PID/status)
    HWM=$(awk '/^VmHWM:/ {print $2}' /proc/$PID/status)
    USED=$(free -m | awk '/^Mem:/ {print $3}')
    AVAIL=$(free -m | awk '/^Mem:/ {print $7}')
    BUFF=$(free -m | awk '/^Mem:/ {print $6}')
    SHARED=$(free -m | awk '/^Mem:/ {print $5}')
    SWAP=$(free -m | awk '/^Swap:/ {print $3}')
    printf "%s rss=%dMiB hwm=%dMiB used=%dMiB avail=%dMiB cache=%dMiB shared=%dMiB swap=%dMiB\n" \
      "$(date +%H:%M:%S)" "$((RSS/1024))" "$((HWM/1024))" "$USED" "$AVAIL" "$BUFF" "$SHARED" "$SWAP" | tee -a "$LOG"
  fi
  sleep 5
done

echo "=== pid $PID exited at $(date -Iseconds) ===" | tee -a "$LOG"
echo
echo "=== peak hwm across the run ==="
awk '
  /hwm=/ {
    val = $0
    sub(/.*hwm=/, "", val)
    sub(/MiB.*/, "", val)
    if (val + 0 > max) max = val + 0
  }
  END { printf "  peak python VmHWM: %d MiB\n", max }
' "$LOG"
