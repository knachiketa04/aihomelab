#!/usr/bin/env bash
# analyze-iostat.sh — generic iostat log parser. Extracts peak / total / active-average
# read+write per log; works on any `iostat -t -dxm 2 <device>` capture.
#
# No-arg mode (default): scans ${EXP_ROOT}/logs/iostat-*.log and prints a per-log
# summary table covering every phase × host combination the kit produced.
#
# Single-log mode: pass an iostat log path as $1 to get high-activity windows
# (each sample where rMB/s OR wMB/s exceeds THRESHOLD) plus the same summary
# row for that one log.
#
# Column layout assumes sysstat 12+ (rMB/s at column 3, wMB/s at column 9). Older
# sysstat versions differ; verify with `iostat -dxmt 1 1 ${NVME_DEVICE}` and adjust
# the awk indices if your version differs.
#
# Standalone usage (any iostat log, kit or not):
#   ./analyze-iostat.sh /path/to/iostat-log              # event timeline + summary
#   THRESHOLD=200 ./analyze-iostat.sh /path/to/log       # tighter "active" filter
#   NVME_DEVICE=sda ./analyze-iostat.sh /path/to/log     # different device
#
# Tunables:
#   EXP_ROOT       — where logs live. Default: /home/sparks/multi-node-storage-reproduce
#   NVME_DEVICE    — device name iostat sampled. Default: nvme0n1
#   THRESHOLD      — MB/s threshold for "active" classification. Default: 50

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/multi-node-storage-reproduce}"
NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"
THRESHOLD="${THRESHOLD:-50}"
LOG_DIR="${EXP_ROOT}/logs"

analyze_one() {
  local log="$1"
  local label="$2"
  awk -v dev="$NVME_DEVICE" -v threshold="$THRESHOLD" -v label="$label" '
    /^[0-9][0-9]\// { ts = $0; next }
    $1 == dev {
      if (count++ == 0) next   # skip since-boot cumulative
      if ($3 > peak_r) peak_r = $3
      if ($9 > peak_w) peak_w = $9
      total_r += $3 * 2        # rMB/s × 2-sec sample period
      total_w += $9 * 2
      if ($3 > threshold || $9 > threshold) {
        active_r_sum += $3
        active_w_sum += $9
        active_n++
      }
    }
    END {
      printf "%-32s peakR=%7.1f peakW=%7.1f  totR=%6.2f GB totW=%6.2f GB  actAvgR=%7.1f actAvgW=%7.1f actN=%d\n",
        label, peak_r+0, peak_w+0, (total_r+0)/1024, (total_w+0)/1024,
        (active_n > 0 ? active_r_sum/active_n : 0),
        (active_n > 0 ? active_w_sum/active_n : 0),
        active_n+0
    }
  ' "$log"
}

# --- Single-log mode ---
if [ $# -gt 0 ]; then
  log="$1"
  [ -r "$log" ] || { echo "ERROR: $log not readable" >&2; exit 1; }
  echo "=== high-activity windows in $log (r OR w > $THRESHOLD MB/s) ==="
  awk -v dev="$NVME_DEVICE" -v threshold="$THRESHOLD" '
    /^[0-9][0-9]\// { ts = $0; next }
    $1 == dev {
      if (count++ == 0) next
      if ($3 > threshold || $9 > threshold) {
        printf "%s | r=%8.2f MB/s  w=%8.2f MB/s\n", ts, $3, $9
      }
    }
  ' "$log"
  echo
  echo "=== summary ==="
  analyze_one "$log" "$(basename "$log" .log)"
  exit 0
fi

# --- No-arg mode: scan all phases × hosts ---
[ -d "$LOG_DIR" ] || { echo "ERROR: $LOG_DIR missing — set EXP_ROOT" >&2; exit 1; }

shopt -s nullglob
LOGS=( "$LOG_DIR"/iostat-*.log )
if [ ${#LOGS[@]} -eq 0 ]; then
  echo "ERROR: no iostat-*.log files in $LOG_DIR" >&2
  exit 1
fi

echo "=== per-phase iostat summary (device=$NVME_DEVICE, threshold=$THRESHOLD MB/s) ==="
echo
for log in "${LOGS[@]}"; do
  label="$(basename "$log" .log | sed 's/^iostat-//')"
  analyze_one "$log" "$label"
done

echo
echo "Column key:"
echo "  peakR / peakW  — single-sample maximum (MB/s)"
echo "  totR / totW    — sum across all samples × 2-sec period (≈ GB transferred)"
echo "  actAvgR / actAvgW — average during 'active' samples (those exceeding THRESHOLD)"
echo "  actN           — count of active samples"
echo
echo "Reference numbers from this lab (compare ±10-15% for hardware drift):"
echo "  single-node host1            peakR ~0    peakW ~1700 totW ~190 GB (3 ckpts)"
echo "  multinode-rsync host1        peakW ~1700 totW ~95 GB (rank 0's ~27 GB × 3 + singletons)"
echo "  multinode-rsync host2        peakW ~1700 totW ~95 GB (rank 1's ~32 GB × 3)"
echo "  multinode-nfsordma host1     peakW ~1300 totW ~190 GB (all bytes funnel to NFS server)"
echo "  multinode-nfsordma host2     peakW ~0    totW ~0     (NFSoRDMA carries writes off-node)"
echo "  cold-restore-nfsordma host1  peakR ~1700 totR ~50 GB (~36 sec sustained ~1.4 GB/s)"
echo "  cold-restore-nfsordma host2  peakR ~0    totR ~0     (rank 1 reads via NFSoRDMA)"
