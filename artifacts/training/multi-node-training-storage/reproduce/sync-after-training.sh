#!/usr/bin/env bash
# sync-after-training.sh — post-training bidirectional rsync, run on host 1 ONLY.
#
# Reproduces: the post-training sync layer measured for the multinode-rsync configuration.
#             Mirrors checkpoint state cross-node so both hosts have a recoverable view.
# Runtime:    ~5-20 min depending on payload size and fabric utilization.
# Disk:       ~190 GB on host 2 (after host 1's checkpoints are pushed there).
#
# WHY ONE SHELL: the original experiment ran rsync from two terminals concurrently
# and reproduced the bidirectional race documented in lessons.md (file-vanished
# errors, ~4× wall-clock inflation). This script does both directions sequentially
# (`pull && push`) from a single shell on host 1 — race structurally impossible.
#
# RUN ONLY AFTER both run-multinode-rsync-host{1,2}.sh have printed END timestamps.

set -euo pipefail

# --- Tunables ---
EXP_ROOT="${EXP_ROOT:-/home/sparks/multi-node-storage-reproduce}"
HOST2_QSFP_IP="${HOST2_QSFP_IP:-169.254.10.122}"
HOST2_USER="${HOST2_USER:-$USER}"
HOST2_SSH_TARGET="${HOST2_SSH_TARGET:-${HOST2_USER}@${HOST2_QSFP_IP}}"

PHASE="multinode-rsync"
RUN_DIR="${EXP_ROOT}/${PHASE}"
LOG_DIR="${EXP_ROOT}/logs"
NODE="$(hostname)"

# --- Pre-flight ---
[ -d "${RUN_DIR}/checkpoints" ] || {
  echo "ERROR: ${RUN_DIR}/checkpoints missing. Run run-multinode-rsync-host1.sh first." >&2
  exit 1
}

echo "==> verifying ssh to host 2 ($HOST2_SSH_TARGET)"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST2_SSH_TARGET" "hostname" || {
  echo "ERROR: ssh to $HOST2_SSH_TARGET failed. Set up passwordless ssh or HOST2_SSH_TARGET." >&2
  exit 1
}

echo "==> verifying host 2 has a checkpoint dir at ${RUN_DIR}/checkpoints"
ssh "$HOST2_SSH_TARGET" "test -d '${RUN_DIR}/checkpoints'" || {
  echo "ERROR: host 2 has no ${RUN_DIR}/checkpoints. Run run-multinode-rsync-host2.sh first." >&2
  exit 1
}

# --- Capture sync wall-clock ---
date +%s.%N | tee "${LOG_DIR}/sync-launch-${PHASE}-${NODE}.ts"

# --- Direction 1: pull host 2's view to host 1 ---
echo
echo "==> [1/2] rsync host 2 -> host 1 (pull)"
time rsync -av --info=progress2 \
  "${HOST2_SSH_TARGET}:${RUN_DIR}/checkpoints/" \
  "${RUN_DIR}/checkpoints/" \
  2>&1 | tee "${LOG_DIR}/rsync-pull-${PHASE}.log"

# --- Direction 2: push merged state to host 2 ---
echo
echo "==> [2/2] rsync host 1 -> host 2 (push)"
time rsync -av --info=progress2 \
  "${RUN_DIR}/checkpoints/" \
  "${HOST2_SSH_TARGET}:${RUN_DIR}/checkpoints/" \
  2>&1 | tee "${LOG_DIR}/rsync-push-${PHASE}.log"

date +%s.%N | tee "${LOG_DIR}/sync-end-${PHASE}-${NODE}.ts"

# --- Verify post-state: both sides see the same LATEST target ---
echo
echo "=== verify mutually-recoverable state ==="
LATEST_LOCAL="$(readlink "${RUN_DIR}/checkpoints/LATEST" 2>/dev/null || echo MISSING)"
LATEST_REMOTE="$(ssh "$HOST2_SSH_TARGET" "readlink '${RUN_DIR}/checkpoints/LATEST' 2>/dev/null || echo MISSING")"
echo "host 1 LATEST -> $LATEST_LOCAL"
echo "host 2 LATEST -> $LATEST_REMOTE"
if [ "$LATEST_LOCAL" = "$LATEST_REMOTE" ] && [ "$LATEST_LOCAL" != "MISSING" ]; then
  echo "  OK: both hosts resolve LATEST to the same checkpoint."
else
  echo "  WARNING: LATEST mismatch — investigate ${RUN_DIR}/checkpoints on both hosts." >&2
fi

# --- Headline ---
echo
echo "=== sync wall-clock ==="
SYNC_START="$(cat "${LOG_DIR}/sync-launch-${PHASE}-${NODE}.ts")"
SYNC_END="$(cat "${LOG_DIR}/sync-end-${PHASE}-${NODE}.ts")"
awk -v s="$SYNC_START" -v e="$SYNC_END" 'BEGIN { printf "Sync total: %.1f sec (~%.1f min)\n", e-s, (e-s)/60 }'
echo "(reference: 5.7 min clean / 20 min when bidirectional race triggers; this script avoids the race)"

echo
echo "==> sync-after-training.sh complete on $NODE."
echo "    Logs: ${LOG_DIR}/rsync-{pull,push}-${PHASE}.log"
echo "    Next: run-multinode-nfsordma-host{1,2}.sh for the shared-FS configuration."
