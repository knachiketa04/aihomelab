#!/usr/bin/env bash
# analyze-checkpoint-events.sh — extract per-checkpoint event timings from any
# NeMo Automodel training log.
#
# No-arg mode: scans ${EXP_ROOT}/logs/training-*.log and surfaces consolidation lines
# + the per-step lines around the c100 checkpoint boundaries (steps 99, 199, 249) plus
# the post-restore steps 250-260 if present.
#
# Single-log mode: pass a training log as $1 to scope to one log.
#
# This script is grep-driven, not arithmetic — it surfaces the lines that contain
# the load-bearing numbers. The reproducer eyeballs the lines and computes hot/cold
# ratios manually. (Computing them programmatically requires matching NeMo's exact
# log format, which can shift between versions; greps with fallback messages are
# more robust to format drift.)
#
# Standalone usage (any NeMo Automodel training log, kit or not):
#   ./analyze-checkpoint-events.sh /path/to/training.log
#
# Tunables:
#   EXP_ROOT — where logs live. Default: /home/sparks/multi-node-storage-reproduce

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/multi-node-storage-reproduce}"
LOG_DIR="${EXP_ROOT}/logs"

analyze_one() {
  local log="$1"
  local label="$2"
  echo "=== $label ==="

  echo "Consolidation events (NeMo's per-checkpoint timing):"
  if ! grep -E "Done consolidating|saved.*checkpoint|Checkpoint.*saved" "$log" 2>/dev/null | head -10; then
    echo "  (no consolidation lines matched — NeMo log format may differ)"
  fi

  echo
  echo "Per-step lines around checkpoint boundaries (compute hot/cold gaps from timestamps):"
  grep -E "step (98|99|100|198|199|200|248|249|250|255|260)\b" "$log" 2>/dev/null | head -30 || \
    echo "  (no step-boundary lines matched)"

  echo
  echo "Memory plateau samples (last 5):"
  grep -oE "mem [0-9.]+ GiB" "$log" 2>/dev/null | tail -5 | sort -u || true

  echo
  echo "Wall-clock bookends (=== START / END ===):"
  grep -E "=== (START|END)" "$log" 2>/dev/null | head -4 || \
    echo "  (no START/END markers — was the run launched via the kit's run-*.sh?)"

  echo
}

# --- Single-log mode ---
if [ $# -gt 0 ]; then
  log="$1"
  [ -r "$log" ] || { echo "ERROR: $log not readable" >&2; exit 1; }
  analyze_one "$log" "$(basename "$log" .log)"
  exit 0
fi

# --- No-arg mode: scan all training-*.log ---
[ -d "$LOG_DIR" ] || { echo "ERROR: $LOG_DIR missing — set EXP_ROOT" >&2; exit 1; }

shopt -s nullglob
LOGS=( "$LOG_DIR"/training-*.log )
if [ ${#LOGS[@]} -eq 0 ]; then
  echo "ERROR: no training-*.log files in $LOG_DIR" >&2
  exit 1
fi

echo "=== checkpoint-event extraction across all phases ==="
echo
for log in "${LOGS[@]}"; do
  label="$(basename "$log" .log | sed 's/^training-//')"
  analyze_one "$log" "$label"
done

echo "=== reference numbers (compare ±10-15% for hardware drift) ==="
echo
echo "single-node                  step_99 hot ~11 sec   | step_199 cold 50-80 sec  (ratio ~7×)"
echo "multinode-rsync host1        step_99 ~4.5 sec      | step_199 ~4.8 sec        (ratio ~1.06×)"
echo "multinode-nfsordma host1     step_99 ~21 sec       | step_199 ~24 sec         (ratio ~1.13×)"
echo "                                                   | total 3 ckpts ~70 sec    (vs single ~120, rsync ~14 + sync ~5-20 min)"
echo "cold-restore-nfsordma        Loading checkpoint -> step 250 = ~37 sec wall-clock"
echo "                                                   | step 250 mem 35.5 GiB    (confirms restore correctness)"
echo
echo "Memory plateau across all multi-node phases: ~35.5 GiB per rank (vs single-node ~61.5 GiB)."
