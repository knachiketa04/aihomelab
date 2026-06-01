#!/usr/bin/env bash
# run-checkpoint-singlenode.sh — single-writer arm.
#
# Reproduces: 100-step full-parameter SFT of an 8B model on ONE host, ckpt_every=25 ->
#             4 checkpoints, each a ~46 GB DCP-SHARDED safetensors write to the shared
#             Lustre FS (model ~16 GB + optimizer ~30 GB). save_consolidated:false, so this
#             arm rides full 4 MiB ALIGNED RPCs and is safe by construction (see the README
#             "consolidation path" note for why the consolidated write path is out of scope).
#             Expected: mean ~59 s/ckpt (range 55-66), ~0.78 GB/s aggregate, OST split
#             ~50/50, disk %util < 55%, peak UMA ~66 GiB. The attribution probe proves
#             the cap is the single DCP writer (client), not the storage substrate.
# Runtime:    ~25 min (cold model load ~90 s + 100 steps + 4 ckpt writes); first run adds
#             the HF cache pull.
# Disk:       ~184 GB on the shared FS (4 ckpts x ~46 GB). Delete-as-you-go or keep all 4.
#
# Refuses to clobber an existing run; rm -rf ${EXP_ROOT}/checkpoints/single-node to re-run.
set -euo pipefail

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
OST0_POOL="${OST0_POOL:-ost0-pool}"
# Page-cache drop. Default assumes full sudo; override to a narrow-sudoers form, e.g.
#   DROP_CACHE_CMD="sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null"
DROP_CACHE_CMD="${DROP_CACHE_CMD:-sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'}"
PROBE_HELPER="${PROBE_HELPER:-$(dirname "$0")/probe-checkpoint-io.sh}"

PHASE="single-node"
RUN_DIR="${EXP_ROOT}/checkpoints/${PHASE}"
CAP_DIR="${EXP_ROOT}/captures/${PHASE}"
NODE="$(hostname)"

# --- Pre-flight ---
[ -r "$HF_TOKEN_FILE" ] || { echo "ERROR: HF token not readable at $HF_TOKEN_FILE" >&2; exit 1; }
[ -r "$RECIPE_FILE" ]   || { echo "ERROR: recipe not found at $RECIPE_FILE (set RECIPE_FILE)" >&2; exit 1; }
[ -r "$PROBE_HELPER" ]  || { echo "ERROR: probe helper not found at $PROBE_HELPER" >&2; exit 1; }
if [ -e "${RUN_DIR}" ] && [ -n "$(ls -A "$RUN_DIR" 2>/dev/null)" ]; then
  echo "ERROR: ${RUN_DIR} is non-empty. Refusing to clobber." >&2
  echo "       To re-run: rm -rf ${RUN_DIR}" >&2
  exit 1
fi
mkdir -p "$RUN_DIR" "$CAP_DIR" "$HF_CACHE_DIR"

# shellcheck source=probe-checkpoint-io.sh
NVME_DEVICE="$NVME_DEVICE" LUSTRE_FSNAME="$LUSTRE_FSNAME" source "$PROBE_HELPER"

# --- Cold cache (known start state) ---
echo "==> dropping page cache: $DROP_CACHE_CMD"
eval "$DROP_CACHE_CMD" || echo "WARN: cache drop failed (sudoers?) — this run is NOT truly cold" >&2
free -h

# --- OST byte counters BEFORE + start the attribution probe ---
probe_lctl_snapshot "$CAP_DIR" 0000 before
probe_start "$CAP_DIR" "$NODE" "$OST0_POOL" 0000

cleanup() {
  probe_stop
  probe_lctl_snapshot "$CAP_DIR" 0000 after
  date +%s.%N | tee -a "${CAP_DIR}/end-${PHASE}-${NODE}.ts" >/dev/null
}
trap cleanup EXIT

# --- Bracket the run in host (iostat/pidstat) clock space. The container train log is in
#     CONTAINER clock; map the checkpoint windows across this offset when extracting. ---
echo "==> launch ts captured to ${CAP_DIR}/launch-${PHASE}-${NODE}.ts"
date +%s.%N | tee "${CAP_DIR}/launch-${PHASE}-${NODE}.ts"

# --- Run: single-node, 100 steps, ckpt_every 25 (4 checkpoints), save_consolidated:false ---
echo "==> launching single-node full-SFT (max_steps=100, ckpt_every=25, sharded write)"
docker run --rm --network=host --gpus all --ipc=host --shm-size=64g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${HF_CACHE_DIR}:/root/.cache/huggingface" \
  -v "${DATA_DIR}:/data:ro" \
  -v "${RECIPE_FILE}:/tmp/recipe.yaml:ro" \
  -v "${RUN_DIR}:/checkpoints" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  "$CONTAINER" \
  torchrun --standalone --nnodes=1 --nproc_per_node=1 \
    /opt/Automodel/examples/llm_finetune/finetune.py \
    -c /tmp/recipe.yaml \
    --step_scheduler.max_steps 100 \
    --step_scheduler.ckpt_every_steps 25 \
    --checkpoint.save_consolidated false \
    --checkpoint.checkpoint_dir /checkpoints \
  2>&1 | tee "${CAP_DIR}/train-${PHASE}-${NODE}.log"

# --- Hand off ownership so analyze + du work without sudo (container ran as root) ---
echo "==> sudo chown on ${RUN_DIR} (training container ran as root)"
sudo chown -R "$USER:$USER" "${RUN_DIR}" 2>/dev/null || true
sudo chmod -R u+rwX,go+rX "${RUN_DIR}" 2>/dev/null || true

# --- Quick headline check ---
echo
echo "=== headline check (single-writer checkpoint) ==="
echo "Checkpoint save events (expect 4, at steps 25/50/75/100):"
grep -nE 'Saving checkpoint|step [0-9]+ \| epoch' "${CAP_DIR}/train-${PHASE}-${NODE}.log" | head -20 || \
  echo "  (no checkpoint lines matched — NeMo log format may differ)"
echo "Checkpoint footprint on the shared FS (expect ~46 GB each):"
du -sh "${RUN_DIR}"/*/ 2>/dev/null || true

echo
echo "==> run-checkpoint-singlenode.sh complete on $NODE."
echo "    Captures: ${CAP_DIR}/ (train log, iostat, pidstat-cpu/io, txgs, lctl OST before/after)"
echo "    Next: analyze-checkpoint.py --capture-dir ${CAP_DIR} --writers 1"
echo "    Then: run-checkpoint-2node.sh 0 (host 1) + run-checkpoint-2node.sh 1 (host 2)."
