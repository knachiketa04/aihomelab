#!/usr/bin/env bash
# run-cold-restore-nfsordma-host1.sh — TP6 cold-cache restore measurement on host 1, rank 0.
#
# Reproduces: cold-cache restore from the multinode-nfsordma checkpoint at step_249.
#             Drops page cache, then auto-resumes via LATEST symlink and runs ~10
#             post-restore steps (max_steps=260) to confirm steady state.
# Headline:   ~37 sec restore wall-clock for ~50 GB cluster-wide checkpoint;
#             ~1.4 GB/s sustained NFS-server NVMe reads (peak ~1.7 GB/s); mem
#             plateau 35.5 GiB at first post-restore step. ~1.6× faster than
#             008's local-NVMe NeMo restore (880 MB/s).
# Runtime:    ~5 min.
# Disk:       0 new bytes (no checkpoint writes — c=100 wouldn't trigger in 10 steps).
#
# IMPORTANT:
# - Run-multinode-nfsordma-host{1,2}.sh must have completed cleanly first.
# - Launch this within ~10 sec of run-cold-restore-nfsordma-host2.sh on host 2.

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
NFS_EXPORT_PATH="${NFS_EXPORT_PATH:-/srv/nfs/multi-node-storage}"

PHASE="cold-restore-nfsordma"
PRIOR_PHASE="multinode-nfsordma"
NODE_RANK=0
RUN_DIR="${NFS_EXPORT_PATH}/${PRIOR_PHASE}"   # restore reads from the prior run's dir
LOG_DIR="${EXP_ROOT}/logs"
NODE="$(hostname)"

# --- Pre-flight ---
[ -r "$HF_TOKEN_FILE" ] || { echo "ERROR: HF token not readable at $HF_TOKEN_FILE" >&2; exit 1; }
[ -e "${RUN_DIR}/checkpoints/LATEST" ] || {
  echo "ERROR: ${RUN_DIR}/checkpoints/LATEST missing." >&2
  echo "       Run run-multinode-nfsordma-host{1,2}.sh first to produce a checkpoint." >&2
  exit 1
}
LATEST_TARGET="$(readlink "${RUN_DIR}/checkpoints/LATEST")"
echo "==> restoring from: $LATEST_TARGET"

mkdir -p "${LOG_DIR}"
chmod 777 "${LOG_DIR}"

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

# --- Container-side script (max_steps bumped to 260; NeMo auto-resumes from LATEST) ---
cat > "${EXP_ROOT}/sft-restore.sh" <<CONTAINER_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
cd /opt/Automodel
echo "=== START \$(date -Iseconds) ==="
free -h
echo "=== launching torchrun (rank $NODE_RANK, max_steps=260, auto-resume from LATEST) ==="
torchrun --nnodes=2 --node_rank=$NODE_RANK --nproc_per_node=1 \\
  --master_addr=$HOST1_QSFP_IP --master_port=29500 \\
  /opt/Automodel/examples/llm_finetune/finetune.py \\
    -c /opt/Automodel/examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \\
    --model.pretrained_model_name_or_path Qwen/Qwen3-8B \\
    --step_scheduler.global_batch_size 2 \\
    --step_scheduler.local_batch_size 1 \\
    --step_scheduler.max_steps 260 \\
    --step_scheduler.ckpt_every_steps 100 \\
    --packed_sequence.packed_sequence_size 1024
echo "=== END \$(date -Iseconds) ==="
CONTAINER_SCRIPT
chmod +x "${EXP_ROOT}/sft-restore.sh"

# --- Run ---
echo "==> launching cold-restore (rank $NODE_RANK on $NODE)"
docker run --rm --network=host --gpus all --ipc=host --shm-size=64g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${EXP_ROOT}/hf-cache:/root/.cache/huggingface" \
  -v "${RUN_DIR}/checkpoints:/opt/Automodel/checkpoints" \
  -v "${EXP_ROOT}/sft-restore.sh:/tmp/sft.sh:ro" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  -e NCCL_DEBUG=INFO -e "NCCL_SOCKET_IFNAME=$HOST1_IFACE" -e "NCCL_IB_HCA=$NCCL_IB_HCA" \
  -e "GLOO_SOCKET_IFNAME=$HOST1_IFACE" -e "TP_SOCKET_IFNAME=$HOST1_IFACE" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh \
  2>&1 | tee "${LOG_DIR}/training-${PHASE}-${NODE}.log"

# --- Headline check ---
echo
echo "=== headline check (TP6 cold-cache restore over NFSoRDMA) ==="
echo "Restore signposts (Loading checkpoint -> first post-restore step):"
grep -E "Loading checkpoint|using checkpoint value|step 250" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | head -10 || true
echo
echo "Memory plateau at first post-restore step (expect 35.5 GiB):"
grep -E "step 250.*mem [0-9.]+ GiB" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | head -1 || true
echo
echo "Reference: ~37 sec wall-clock for ~50 GB cluster-wide restore (~1.4 GB/s effective)."
echo "Run analyze-iostat.sh to extract the actual NVMe-side read burst on host 1."

echo
echo "==> run-cold-restore-nfsordma-host1.sh complete on $NODE."
echo "    Logs: ${LOG_DIR}/{iostat,rss,training}-${PHASE}-${NODE}.log"
echo "    Next: run analyze-iostat.sh and analyze-checkpoint-events.sh to extract numbers."
