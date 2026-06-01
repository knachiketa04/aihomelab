#!/usr/bin/env bash
# run-checkpoint-2node.sh — 2-node-concurrent arm.
#
# Reproduces: the SAME 100-step full-SFT as the single-node arm, but under 2-node FSDP2 over
#             RoCE. Two independent rank-writers write DCP shards CONCURRENTLY to the shared
#             Lustre FS. save_consolidated:false -> aligned RPCs -> safe by construction; the
#             sharded write stays aligned even under concurrency (concurrency is not a trigger
#             for the consolidation-path defect; see the README "consolidation path" note).
#             Expected: mean ~35 s/ckpt, 1.32 GB/s aggregate = ~1.69x the single writer,
#             landing on the independently-fio-measured 1.35 GB/s concurrent ceiling; OST split
#             50.0/50.0; peak UMA ~45-46 GiB/rank; disk still < ~42% busy (substrate still idle).
# Runtime:    ~25 min. Disk: ~92 GB/host (each rank's shards land ~50/50 across the two OSTs).
#
# Run on BOTH hosts. Pass node rank as $1 (0 = host 1 / master / rendezvous, 1 = host 2).
# Launch rank 0 FIRST, then rank 1 within ~10 sec:
#   host 1:  ./run-checkpoint-2node.sh 0
#   host 2:  ./run-checkpoint-2node.sh 1
#
# Refuses to clobber an existing run; rm -rf ${EXP_ROOT}/checkpoints/2node to re-run.
set -euo pipefail

NODE_RANK="${1:?usage: run-checkpoint-2node.sh <node_rank: 0=host1/master, 1=host2>}"

# --- Tunables ---
EXP_ROOT="${EXP_ROOT:-/mnt/lustre/lustre-checkpoint-storage-reproduce}"
LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
HF_CACHE_DIR="${HF_CACHE_DIR:-$HOME/hf-cache}"
RECIPE_FILE="${RECIPE_FILE:-$(dirname "$0")/qwen3_8b_vegan_fullsft.yaml}"
DATA_DIR="${DATA_DIR:-${EXP_ROOT}/data}"
NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"
# Page-cache drop. Default assumes full sudo; override to a narrow-sudoers form, e.g.
#   DROP_CACHE_CMD="sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null"
DROP_CACHE_CMD="${DROP_CACHE_CMD:-sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'}"
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
HOST2_QSFP_IP="${HOST2_QSFP_IP:-169.254.10.122}"
HOST1_IFACE="${HOST1_IFACE:-enp1s0f0np0}"
NCCL_IB_HCA="${NCCL_IB_HCA:-rocep1s0f0}"
MASTER_PORT="${MASTER_PORT:-29500}"
OST0_POOL="${OST0_POOL:-ost0-pool}"
OST1_POOL="${OST1_POOL:-ost1-pool}"
PROBE_HELPER="${PROBE_HELPER:-$(dirname "$0")/probe-checkpoint-io.sh}"

# This rank's local OST (OST0 on host 1, OST1 on host 2) for the txgs probe.
if [ "$NODE_RANK" = "0" ]; then
  THIS_OST_INDEX=0000; THIS_OST_POOL="$OST0_POOL"
else
  THIS_OST_INDEX=0001; THIS_OST_POOL="$OST1_POOL"
fi

PHASE="2node"
RUN_DIR="${EXP_ROOT}/checkpoints/${PHASE}"     # SHARED dir on the Lustre FS (both ranks write here)
CAP_DIR="${EXP_ROOT}/captures/${PHASE}"
NODE="$(hostname)"

# --- Pre-flight ---
[ -r "$HF_TOKEN_FILE" ] || { echo "ERROR: HF token not readable at $HF_TOKEN_FILE" >&2; exit 1; }
[ -r "$RECIPE_FILE" ]   || { echo "ERROR: recipe not found at $RECIPE_FILE (set RECIPE_FILE)" >&2; exit 1; }
[ -r "$PROBE_HELPER" ]  || { echo "ERROR: probe helper not found at $PROBE_HELPER" >&2; exit 1; }
if [ "$NODE_RANK" = "0" ] && [ -e "${RUN_DIR}" ] && [ -n "$(ls -A "$RUN_DIR" 2>/dev/null)" ]; then
  echo "ERROR: ${RUN_DIR} is non-empty (shared FS). Refusing to clobber." >&2
  echo "       To re-run: rm -rf ${RUN_DIR}" >&2
  exit 1
fi
mkdir -p "$RUN_DIR" "$CAP_DIR" "$HF_CACHE_DIR"

# shellcheck source=probe-checkpoint-io.sh
NVME_DEVICE="$NVME_DEVICE" LUSTRE_FSNAME="$LUSTRE_FSNAME" source "$PROBE_HELPER"

# --- Cold cache ---
echo "==> dropping page cache on rank $NODE_RANK / $NODE: $DROP_CACHE_CMD"
eval "$DROP_CACHE_CMD" || echo "WARN: cache drop failed (sudoers?) — this run is NOT truly cold" >&2
free -h

# --- OST byte counters BEFORE (this rank's local OST) + start the attribution probe ---
probe_lctl_snapshot "$CAP_DIR" "$THIS_OST_INDEX" before
probe_start "$CAP_DIR" "$NODE" "$THIS_OST_POOL" "$THIS_OST_INDEX"

cleanup() {
  probe_stop
  probe_lctl_snapshot "$CAP_DIR" "$THIS_OST_INDEX" after
  date +%s.%N | tee -a "${CAP_DIR}/end-${PHASE}-${NODE}.ts" >/dev/null
}
trap cleanup EXIT

echo "==> launch ts captured to ${CAP_DIR}/launch-${PHASE}-${NODE}.ts"
date +%s.%N | tee "${CAP_DIR}/launch-${PHASE}-${NODE}.ts"

# --- Run: 2-node FSDP2, 100 steps, ckpt_every 25, save_consolidated:false ---
# WATCH the FIRST checkpoint. If the concurrent write EFAULTs (unaligned-DIO under concurrency),
# set hybrid_io=0 on BOTH hosts and relaunch — but the sharded write is aligned and is NOT
# expected to EFAULT (the canonical run completed 4 ckpts clean under concurrency).
echo "==> launching 2-node full-SFT (rank $NODE_RANK, master=${HOST1_QSFP_IP}:${MASTER_PORT})"
docker run --rm --network=host --gpus all --ipc=host --shm-size=64g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
  -v "${DATA_DIR}:/data:ro" \
  -v "${RECIPE_FILE}:/tmp/recipe.yaml:ro" \
  -v "${RUN_DIR}:/checkpoints" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  -e NCCL_DEBUG=INFO -e "NCCL_SOCKET_IFNAME=$HOST1_IFACE" -e "NCCL_IB_HCA=$NCCL_IB_HCA" \
  -e "GLOO_SOCKET_IFNAME=$HOST1_IFACE" -e "TP_SOCKET_IFNAME=$HOST1_IFACE" \
  "$CONTAINER" \
  torchrun --nnodes=2 --node_rank="$NODE_RANK" --nproc_per_node=1 \
    --master_addr="$HOST1_QSFP_IP" --master_port="$MASTER_PORT" \
    /opt/Automodel/examples/llm_finetune/finetune.py \
    -c /tmp/recipe.yaml \
    --step_scheduler.max_steps 100 \
    --step_scheduler.ckpt_every_steps 25 \
    --checkpoint.save_consolidated false \
    --checkpoint.checkpoint_dir /checkpoints \
  2>&1 | tee "${CAP_DIR}/train-${PHASE}-${NODE}.log"

# --- Hand off ownership (container ran as root) ---
echo "==> sudo chown on ${RUN_DIR} (training container ran as root)"
sudo chown -R "$USER:$USER" "${RUN_DIR}" 2>/dev/null || true
sudo chmod -R u+rwX,go+rX "${RUN_DIR}" 2>/dev/null || true

# --- Quick headline check ---
echo
echo "=== headline check (concurrent 2-node checkpoint) ==="
echo "Checkpoint save events (expect 4; rendezvous should show both ranks; NO EFAULT/panic):"
grep -nE 'Saving checkpoint|Bad address|EFAULT|step [0-9]+ \| epoch' "${CAP_DIR}/train-${PHASE}-${NODE}.log" | head -20 || \
  echo "  (no checkpoint lines matched — NeMo log format may differ)"
if [ "$NODE_RANK" = "0" ]; then
  echo "Checkpoint footprint on the shared FS (expect ~46 GB each):"
  du -sh "${RUN_DIR}"/*/ 2>/dev/null || true
fi

echo
echo "==> run-checkpoint-2node.sh complete on $NODE (rank $NODE_RANK)."
echo "    Captures: ${CAP_DIR}/ (this host's iostat/pidstat/txgs/lctl-OST${THIS_OST_INDEX})"
echo "    Gather BOTH hosts' captures into one dir, then:"
echo "      analyze-checkpoint.py --capture-dir ${CAP_DIR} --writers 2"
