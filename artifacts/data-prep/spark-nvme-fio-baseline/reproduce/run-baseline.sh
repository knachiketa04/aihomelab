#!/usr/bin/env bash
# run-baseline.sh — thin orchestrator for the 8-job NVMe FIO baseline sweep.
#
# WHAT IT DOES:
#   1. Pre-allocates the 2 TiB testfile (one-time cost, ~3 min on Gen5).
#   2. For each .fio job file under fio-jobs/, in numeric order:
#      - drops the page cache
#      - invokes fio with JSON output
#      - writes JSON to out/<jobname>.json
#      - writes per-second bandwidth logs (write jobs only) to out/
#   3. Runs analyze.sh at the end to print the Measured table.
#
# WHAT IT DOES NOT DO:
#   - Carry FIO parameters. Those live in fio-jobs/*.fio. Edit those, not this.
#   - Manage iostat. Start iostat separately if you want a side-channel.
#   - Verify drive identification. Run that once before this script (see README).
#
# REQUIREMENTS:
#   - fio (≥3.30), jq, sysstat, sudo for cache drop
#   - ~2.2 TB free space at the test directory (default /home/sparks/fio-baseline-reproduce)
#   - ~30 min wall clock for the full sweep
#
# USAGE:
#   ./run-baseline.sh
#
# CONFIG (override via env vars):
#   TEST_DIR=/home/sparks/fio-baseline-reproduce   # where testfile lives (must match
#                                       # directory= in the .fio files)
#   TEST_SIZE_GiB=2048                  # testfile size; must match size=
#                                       # in the .fio files
#   SKIP_ALLOC=0                        # set to 1 if testfile already exists
#                                       # at TEST_SIZE_GiB

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOBS_DIR="$SCRIPT_DIR/fio-jobs"
OUT_DIR="$SCRIPT_DIR/out"
mkdir -p "$OUT_DIR"

: "${TEST_DIR:=/home/sparks/fio-baseline-reproduce}"
: "${TEST_SIZE_GiB:=2048}"
: "${SKIP_ALLOC:=0}"
TEST_FILE="$TEST_DIR/testfile"

# --- Prerequisite checks --------------------------------------------------
command -v fio >/dev/null || { echo "ERROR: fio not installed"; exit 1; }
command -v jq  >/dev/null || { echo "ERROR: jq not installed";  exit 1; }

iostat_count=$(pgrep -af '^iostat ' 2>/dev/null | wc -l || true)
if [[ "$iostat_count" -gt 1 ]]; then
    echo "ERROR: $iostat_count iostat processes running. Side-channel logging will be corrupted."
    echo "Stop extras before continuing."
    exit 1
fi

if ! sudo -n true 2>/dev/null; then
    echo "INFO: sudo requires password. Page-cache drop will prompt before each job."
fi

mkdir -p "$TEST_DIR"

# --- Test file pre-allocation ---------------------------------------------
need_alloc=1
if [[ "$SKIP_ALLOC" == "1" ]]; then
    need_alloc=0
elif [[ -f "$TEST_FILE" ]]; then
    actual_gib=$(($(stat -c '%s' "$TEST_FILE") / 1024 / 1024 / 1024))
    if [[ "$actual_gib" -ge "$TEST_SIZE_GiB" ]]; then
        echo "INFO: testfile already exists at ${actual_gib} GiB (≥ ${TEST_SIZE_GiB} GiB target). Skipping pre-allocation."
        need_alloc=0
    fi
fi

if [[ "$need_alloc" == "1" ]]; then
    echo "Pre-allocating ${TEST_SIZE_GiB} GiB testfile at $TEST_FILE (one-time, ~3 min on Gen5)..."
    fallocate -l "${TEST_SIZE_GiB}G" "$TEST_FILE"
    echo "Pre-allocation done."
fi

# --- Job loop -------------------------------------------------------------
cd "$OUT_DIR"  # write_bw_log emits to cwd
for jobfile in "$JOBS_DIR"/*.fio; do
    jobname=$(basename "$jobfile" .fio)
    echo
    echo "=== $jobname ==="
    echo "Dropping page cache..."
    sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
    sync

    echo "Running fio (this takes ~3 min per job)..."
    fio "$jobfile" \
        --output-format=json \
        --output="$OUT_DIR/$jobname.json"

    echo "Saved: $OUT_DIR/$jobname.json"
done

# --- Analyze --------------------------------------------------------------
echo
echo "=== Analysis ==="
"$SCRIPT_DIR/analyze.sh"
