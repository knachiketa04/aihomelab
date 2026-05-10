#!/bin/bash
# Reproduce kit — iostat -t timeline analyzer
#
# Parses an iostat -t -dxm 2 nvme0n1 log into a touch-point timeline, surfacing
# only the high-activity windows (>50 MB/s read OR write). Pairs each window
# with its host-time timestamp so you can map iostat events to python log lines.
#
# Column layout assumes sysstat 12.6.1 (rMB/s at column 3, wMB/s at column 9).
# Older sysstat had rMB/s at $4, wMB/s at $5 — verify with
#   iostat -dxmt 1 1 nvme0n1
# and adjust the awk indices if your version differs.
#
# Usage:
#   ./analyze-iostat.sh <iostat-log> [threshold-MB-s]
#
# Examples:
#   ./analyze-iostat.sh ~/full-sft-touchpoints-reproduce/logs/iostat-c25.log
#   ./analyze-iostat.sh ~/full-sft-touchpoints-reproduce/logs/iostat-c25.log 100  # tighter filter

set -euo pipefail

IOSTAT_LOG="${1:?usage: $0 <iostat-log> [threshold-MB-s]}"
THRESHOLD="${2:-50}"

[ -r "$IOSTAT_LOG" ] || { echo "FATAL: cannot read $IOSTAT_LOG"; exit 1; }

echo "=== high-activity windows (r > ${THRESHOLD} MB/s OR w > ${THRESHOLD} MB/s) in ${IOSTAT_LOG} ==="
echo

awk -v threshold="$THRESHOLD" '
  /^[0-9][0-9]\// { ts = $0; next }
  /^nvme0n1/ {
    if (count++ == 0) next  # skip the since-boot cumulative summary
    if ($3 > threshold || $9 > threshold) {
      printf "%s | r=%7.1f MB/s  w=%7.1f MB/s  %%util=%5.1f\n", ts, $3, $9, $NF
    }
  }
' "$IOSTAT_LOG"

echo
echo "=== peak rates across the whole log ==="
awk '
  /^nvme0n1/ {
    if (count++ == 0) next
    if ($3 > max_r) { max_r = $3 }
    if ($9 > max_w) { max_w = $9 }
    if ($3 + $9 > max_total) { max_total = $3 + $9 }
  }
  END {
    printf "  peak read:  %7.1f MB/s\n", max_r
    printf "  peak write: %7.1f MB/s\n", max_w
    printf "  peak r+w:   %7.1f MB/s\n", max_total
  }
' "$IOSTAT_LOG"

echo
echo "=== interpretation hints ==="
echo
echo "Touch point 3 (cold pull, smoke run only): write bursts climbing 100 → 3000+ MB/s,"
echo "  with idle gaps. Network is the rate-limiter at ~900 MB/s aggregate; NVMe absorbs"
echo "  bursts at 4× higher peak."
echo
echo "Touch point 4 (model load): on cold-cache start (post drop_caches), expect"
echo "  583–732 MB/s sustained read for ~22 sec. On warm-cache start (the"
echo "  cold-pull-then-immediate-load case), expect ZERO NVMe reads — the"
echo "  shards are still in page cache."
echo
echo "Touch points 5+7 (checkpoint consolidation): cache-hot first checkpoint shows"
echo "  pure write at 1.0–1.7 GB/s for ~13 sec; cache-cold subsequent checkpoints show"
echo "  mixed read+write at 600–800 MB/s each direction for ~50–80 sec (the 6× pattern)."
echo
echo "Touch point 6 (cold-cache restore): two-phase read window. First ~22 sec at"
echo "  600–700 MB/s (DCP shard); next ~28 sec at 1019–1153 MB/s sustained (optimizer DCP)."
echo
echo "Optimizer DCP write rate: sustains at 1.5–1.7 GB/s past the SLC budget — matches"
echo "  the post-SLC TLC sustained rate from the Spark NVMe FIO baseline."
