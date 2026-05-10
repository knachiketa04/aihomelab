#!/usr/bin/env bash
# run-multinode-rsync-host1.sh — multi-node training (training only) on host 1, rank 0.
#
# Reproduces: 250-step SFT across 2 hosts with FSDP-2 sharding, ckpt_every=100. Each
#             rank writes its own DCP shard to its own local NVMe. Per-rank memory
#             plateau ~35.5 GiB (vs single-node ~61.5 GiB). Cold-cache penalty
#             disappears (per-checkpoint sync block ~4-5 sec flat, 16.6× speedup at
#             step_199 vs single-node).
# Runtime:    ~30 min (training only; rsync runs separately via sync-after-training.sh).
# Disk:       ~190 GB on host 1 (3 checkpoints × ~62 GB, rank-0 view with singletons).
#
# IMPORTANT: launch this within ~10 sec of run-multinode-rsync-host2.sh on host 2.
# Rank 0 (this script) is the rendezvous master; rank 1 connects to it.
#
# Refuses to clobber an existing run; rm -rf ${EXP_ROOT}/multinode-rsync to re-run.

set -euo pipefail

# --- Tunables ---
EXP_ROOT="${EXP_ROOT:-/home/sparks/multi-node-storage-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"
HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
HOST1_IFACE="${HOST1_IFACE:-enp1s0f0np0}"
NCCL_IB_HCA="${NCCL_IB_HCA:-rocep1s0f0}"

PHASE="multinode-rsync"
NODE_RANK=0
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

date +%s.%N | tee "${LOG_DIR}/launch-${PHASE}-${NODE}.ts"

# --- Container-side script ---
cat > "${RUN_DIR}/sft.sh" <<CONTAINER_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
cd /opt/Automodel
echo "=== START \$(date -Iseconds) ==="
free -h
echo "=== launching torchrun (rank $NODE_RANK, nnodes=2, master=$HOST1_QSFP_IP:29500) ==="
torchrun --nnodes=2 --node_rank=$NODE_RANK --nproc_per_node=1 \\
  --master_addr=$HOST1_QSFP_IP --master_port=29500 \\
  /opt/Automodel/examples/llm_finetune/finetune.py \\
    -c /opt/Automodel/examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \\
    --model.pretrained_model_name_or_path Qwen/Qwen3-8B \\
    --step_scheduler.global_batch_size 2 \\
    --step_scheduler.local_batch_size 1 \\
    --step_scheduler.max_steps 250 \\
    --step_scheduler.ckpt_every_steps 100 \\
    --packed_sequence.packed_sequence_size 1024
echo "=== END \$(date -Iseconds) ==="
echo "=== checkpoint footprint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "${RUN_DIR}/sft.sh"

# --- Run ---
echo "==> launching multinode-rsync training (rank $NODE_RANK on $NODE)"
docker run --rm --network=host --gpus all --ipc=host --shm-size=64g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${EXP_ROOT}/hf-cache:/root/.cache/huggingface" \
  -v "${RUN_DIR}/checkpoints:/opt/Automodel/checkpoints" \
  -v "${RUN_DIR}/sft.sh:/tmp/sft.sh:ro" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  -e NCCL_DEBUG=INFO -e "NCCL_SOCKET_IFNAME=$HOST1_IFACE" -e "NCCL_IB_HCA=$NCCL_IB_HCA" \
  -e "GLOO_SOCKET_IFNAME=$HOST1_IFACE" -e "TP_SOCKET_IFNAME=$HOST1_IFACE" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh \
  2>&1 | tee "${LOG_DIR}/training-${PHASE}-${NODE}.log"

# --- Hand off ownership + permissions so rsync (running as $USER) can sync metadata ---
echo "==> sudo chown + chmod on ${RUN_DIR}/checkpoints (training container ran as root)"
sudo chown -R "$USER:$USER" "${RUN_DIR}/checkpoints"
sudo chmod -R u+rwX,go+rX "${RUN_DIR}/checkpoints"

# --- Quick headline check ---
echo
echo "=== headline check (multi-node memory plateau + flat checkpoint cost) ==="
echo "Memory plateau (expect ~35.5 GiB per rank):"
grep -E "mem [0-9.]+ GiB" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | tail -5 || true
echo "Consolidation events (expect ~4-5 sec flat across all 3 checkpoints):"
grep -E "Done consolidating|saved.*checkpoint" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | head -10 || \
  echo "  (no consolidation lines matched — check the training log directly)"

echo
echo "==> run-multinode-rsync-host1.sh (training only) complete on $NODE."
echo "    Logs: ${LOG_DIR}/{iostat,rss,training}-${PHASE}-${NODE}.log"
echo "    NEXT: wait for run-multinode-rsync-host2.sh to finish on host 2,"
echo "          then run sync-after-training.sh on this host (host 1)."
