#!/usr/bin/env bash
# run-single-node-tuning.sh — Pillar 1: 4-job fio battery on host 1.
# Files pinned to OST0000 (host-local) via lfs setstripe -c 1 -i 0 → pure loopback.
# 256 GiB working set per write/read test, 180s runtime.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
LOG_DIR="${EXP_ROOT}/logs"
DATA_DIR="/mnt/lustre/pillar1-single-node"

echo "==> run-single-node-tuning.sh on $(hostname)"

# --- Pre-flight: Lustre mounted, fio installed ---
mount | grep -q "/mnt/lustre type lustre" || { echo "ERROR: /mnt/lustre not mounted. Run setup-host1-osts.sh first."; exit 1; }
command -v fio >/dev/null || sudo apt-get install -y -qq fio

mkdir -p "${LOG_DIR}"
mkdir -p "${DATA_DIR}"

# --- Idempotency guard: refuse to clobber prior results ---
if ls "${LOG_DIR}"/pillar1-*.log >/dev/null 2>&1; then
    echo "ERROR: prior Pillar 1 logs exist in ${LOG_DIR}. Remove first:"
    echo "  rm -f ${LOG_DIR}/pillar1-*.log ${DATA_DIR}/seq-* ${DATA_DIR}/rand-*"
    exit 1
fi

# --- Pin Pillar 1 dir to OST0000 (single-OST, host-local for loopback) ---
lfs setstripe -c 1 -i 0 "${DATA_DIR}"
lfs getstripe -d "${DATA_DIR}"

# --- Bracket with launch timestamp (host iostat clock vs container clock can differ) ---
date +%s.%N > "${LOG_DIR}/pillar1-launch.ts"

# --- Trap to capture end timestamp + clean test files ---
cleanup() {
    date +%s.%N > "${LOG_DIR}/pillar1-end.ts"
    rm -f "${DATA_DIR}"/seq-* "${DATA_DIR}"/rand-*
}
trap cleanup EXIT

# --- Battery (4 tests, sequential, ~14 min total) ---
echo "==> seq-write-1m (256 GiB working set, 180s)"
fio --name=seq-write-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar1-seq-write-1m.log"

echo "==> seq-read-1m"
fio --name=seq-read-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=read --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar1-seq-read-1m.log"

echo "==> seq-write-64k"
fio --name=seq-write-64k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=64k \
    --size=64G --numjobs=4 --iodepth=8 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar1-seq-write-64k.log"

echo "==> rand-rw-4k (4 GiB working set, 180s)"
fio --name=rand-rw-4k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=randrw --bs=4k \
    --size=4G --numjobs=4 --iodepth=16 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar1-rand-rw-4k.log"

# --- Headline check ---
echo
echo "=== Pillar 1 (single-node loopback) summary ==="
for log in "${LOG_DIR}"/pillar1-seq-*.log "${LOG_DIR}"/pillar1-rand-*.log; do
    echo "--- $(basename "$log") ---"
    grep -E "READ:|WRITE:" "$log" || echo "  (no headline match — check log)"
done

echo "==> run-single-node-tuning.sh OK"
