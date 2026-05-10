#!/usr/bin/env bash
# run-cross-node-baseline.sh — Pillar 2: 4-job fio battery on host 2.
# Files pinned to OST0000 (host-1-local) via lfs setstripe -c 1 -i 0
# → all traffic crosses o2ib. Same battery shape as Pillar 1 for direct comparison.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"
LOG_DIR="${EXP_ROOT}/logs"
DATA_DIR="/mnt/lustre/pillar2-cross-node"

echo "==> run-cross-node-baseline.sh on $(hostname)"

# --- Pre-flight: Lustre mounted, both OSTs ACTIVE, runtime tunables applied ---
mount | grep -q "/mnt/lustre type lustre" || { echo "ERROR: /mnt/lustre not mounted."; exit 1; }
lfs osts /mnt/lustre 2>/dev/null | grep -q "OST0000.*ACTIVE" || { echo "ERROR: OST0000 not ACTIVE."; exit 1; }
command -v fio >/dev/null || sudo apt-get install -y -qq fio

RPCS=$(lctl get_param -n "osc.${LUSTRE_FSNAME}-OST0000-osc-*.max_rpcs_in_flight" 2>/dev/null | head -1 || true)
if [ "${RPCS}" != "32" ]; then
    echo "WARNING: osc.${LUSTRE_FSNAME}-OST0000.max_rpcs_in_flight=${RPCS:-unset} (expected 32). Run setup-runtime-tunables.sh."
fi

mkdir -p "${LOG_DIR}" "${DATA_DIR}"

# --- Idempotency guard ---
if ls "${LOG_DIR}"/pillar2-*.log >/dev/null 2>&1; then
    echo "ERROR: prior Pillar 2 logs exist. Remove first:"
    echo "  rm -f ${LOG_DIR}/pillar2-*.log ${DATA_DIR}/seq-* ${DATA_DIR}/rand-*"
    exit 1
fi

# --- Pin to OST0000 (host-1-local → cross-node from this host) ---
lfs setstripe -c 1 -i 0 "${DATA_DIR}"
lfs getstripe -d "${DATA_DIR}"

date +%s.%N > "${LOG_DIR}/pillar2-launch.ts"

cleanup() {
    date +%s.%N > "${LOG_DIR}/pillar2-end.ts"
    rm -f "${DATA_DIR}"/seq-* "${DATA_DIR}"/rand-*
}
trap cleanup EXIT

# --- Battery (4 tests, ~14 min total) ---
echo "==> seq-write-1m"
fio --name=seq-write-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar2-seq-write-1m.log"

echo "==> seq-read-1m"
fio --name=seq-read-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=read --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar2-seq-read-1m.log"

echo "==> seq-write-64k"
fio --name=seq-write-64k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=64k \
    --size=64G --numjobs=4 --iodepth=8 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar2-seq-write-64k.log"

echo "==> rand-rw-4k"
fio --name=rand-rw-4k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=randrw --bs=4k \
    --size=4G --numjobs=4 --iodepth=16 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar2-rand-rw-4k.log"

# --- Headline check ---
echo
echo "=== Pillar 2 (cross-node, host-2 client → OST0000 over o2ib) summary ==="
for log in "${LOG_DIR}"/pillar2-seq-*.log "${LOG_DIR}"/pillar2-rand-*.log; do
    echo "--- $(basename "$log") ---"
    grep -E "READ:|WRITE:" "$log" || echo "  (no headline match — check log)"
done

echo "==> run-cross-node-baseline.sh OK"
