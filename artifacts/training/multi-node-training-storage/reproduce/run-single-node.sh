#!/usr/bin/env bash
# run-single-node.sh — single-node baseline for the multi-node training storage kit.
#
# Reproduces: 250-step SFT on host 1 only with ckpt_every=100 → checkpoints at steps 99,
#             199, 249. Establishes the cold-cache consolidation penalty baseline (~7×
#             hot/cold spread between step 99 and step 199).
# Runtime:    ~17 min (first run includes ~5-10 min HF cache pull; subsequent runs
#             ~17 min with the cache warm).
# Disk:       ~190 GB (3 checkpoints × ~62 GB).
#
# Refuses to clobber an existing run; rm -rf ${EXP_ROOT}/single-node to re-run.

set -euo pipefail

# --- Tunables ---
EXP_ROOT="${EXP_ROOT:-/home/sparks/multi-node-storage-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"

PHASE="single-node"
RUN_DIR="${EXP_ROOT}/${PHASE}"
LOG_DIR="${EXP_ROOT}/logs"
NODE="$(hostname)"

# --- Pre-flight ---
[ -r "$HF_TOKEN_FILE" ] || { echo "ERROR: HF token not readable at $HF_TOKEN_FILE" >&2; exit 1; }
if [ -e "${RUN_DIR}/checkpoints/LATEST" ]; then
  echo "ERROR: ${RUN_DIR}/checkpoints/LATEST exists. Refusing to clobber." >&2
  echo "       To re-run: rm -rf ${RUN_DIR}" >&2
  exit 1
fi

mkdir -p "${RUN_DIR}/checkpoints" "${RUN_DIR}/logs" "${LOG_DIR}" "${EXP_ROOT}/hf-cache"
chmod 777 "${RUN_DIR}/checkpoints" "${RUN_DIR}/logs" "${EXP_ROOT}/hf-cache" "${LOG_DIR}"

# --- Cold cache + instrumentation ---
echo "==> dropping page cache (sudo required)"
sync && sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
free -h

echo "==> starting iostat on $NODE (device: $NVME_DEVICE)"
iostat -t -dxm 2 "$NVME_DEVICE" > "${LOG_DIR}/iostat-${PHASE}-${NODE}.log" 2>&1 &
IOSTAT_PID=$!

# rss-tracker uses 'python.*finetune\.py' regex (path-agnostic, excludes self-match).
echo "==> starting rss-tracker"
nohup bash -c '
while true; do
  PID=$(pgrep -f "python.*finetune\.py" | head -1)
  if [ -n "$PID" ]; then
    awk "/^Vm(RSS|HWM|Swap)/ || /^State/" /proc/$PID/status | tr "\n" " "
    echo " ts=$(date +%s)"
  else
    echo "no finetune.py found ts=$(date +%s)"
  fi
  sleep 2
done' > "${LOG_DIR}/rss-${PHASE}-${NODE}.log" 2>&1 &
RSS_PID=$!

cleanup() {
  echo "==> stopping instrumentation (iostat PID $IOSTAT_PID, rss PID $RSS_PID)"
  kill "$IOSTAT_PID" "$RSS_PID" 2>/dev/null || true
  date +%s.%N | tee -a "${LOG_DIR}/end-${PHASE}-${NODE}.ts"
}
trap cleanup EXIT

# --- Bracket the run in iostat clock space ---
echo "==> launch ts captured to ${LOG_DIR}/launch-${PHASE}-${NODE}.ts"
date +%s.%N | tee "${LOG_DIR}/launch-${PHASE}-${NODE}.ts"

# --- Container-side script ---
cat > "${RUN_DIR}/sft.sh" <<'CONTAINER_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
cd /opt/Automodel
echo "=== START $(date -Iseconds) ==="
free -h
echo "=== launching python (single-node, max_steps=250, ckpt_every=100) ==="
python3 examples/llm_finetune/finetune.py \
  -c examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \
  --model.pretrained_model_name_or_path Qwen/Qwen3-8B \
  --step_scheduler.global_batch_size 1 \
  --step_scheduler.local_batch_size 1 \
  --step_scheduler.max_steps 250 \
  --step_scheduler.ckpt_every_steps 100 \
  --packed_sequence.packed_sequence_size 1024
echo "=== END $(date -Iseconds) ==="
echo "=== checkpoint footprint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "${RUN_DIR}/sft.sh"

# --- Run ---
echo "==> launching single-node training"
docker run --rm --gpus all --ipc=host --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${EXP_ROOT}/hf-cache:/root/.cache/huggingface" \
  -v "${RUN_DIR}/checkpoints:/opt/Automodel/checkpoints" \
  -v "${RUN_DIR}/sft.sh:/tmp/sft.sh:ro" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh \
  2>&1 | tee "${LOG_DIR}/training-${PHASE}-${NODE}.log"

# --- Quick headline check ---
echo
echo "=== headline check (cold-cache penalty) ==="
echo "Consolidation events (expect step_99 cache-hot ~11 sec, step_199 cache-cold 50-80 sec):"
grep -E "Done consolidating|saved.*checkpoint" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | head -20 || \
  echo "  (no consolidation lines matched — check the training log directly)"

echo
echo "==> run-single-node.sh complete."
echo "    Logs: ${LOG_DIR}/{iostat,rss,training}-${PHASE}-${NODE}.log"
echo "    Next: run-multinode-rsync.sh (on host 1; coordinate with host 2)."
