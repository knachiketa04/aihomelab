#!/usr/bin/env bash
# run-multinode-nfsordma-host1.sh — multi-node training on host 1, rank 0, with the
# checkpoint dir on the NFSoRDMA-shared mount.
#
# Reproduces: 250-step SFT across 2 hosts, ckpt_every=100, all checkpoint bytes
#             physically land on host 1's NVMe via the NFS export. Per-checkpoint
#             sync block ~22-24 sec flat regardless of cache state (NFS-server-sync
#             write-bound, not page-cache-bound). Total checkpoint cost across 3
#             ckpts ~70 sec — lowest of the three configurations once the
#             post-training rsync cost of the local-NVMe variant is included.
# Runtime:    ~28 min (no separate sync phase needed; shared FS handles it inline).
# Disk:       ~190 GB on host 1 (NFS server); host 2's NVMe accumulates 0 bytes.
#
# IMPORTANT:
# - Run setup-nfsordma-server.sh on host 1 and setup-nfsordma-client.sh on host 2
#   first; this script assumes the NFSoRDMA mount is live.
# - Launch this within ~10 sec of run-multinode-nfsordma-host2.sh on host 2.
#
# Refuses to clobber an existing run; rm -rf ${NFS_EXPORT_PATH}/multinode-nfsordma to re-run.

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

PHASE="multinode-nfsordma"
NODE_RANK=0
RUN_DIR="${NFS_EXPORT_PATH}/${PHASE}"
LOG_DIR="${EXP_ROOT}/logs"
NODE="$(hostname)"

# --- Pre-flight ---
[ -r "$HF_TOKEN_FILE" ] || { echo "ERROR: HF token not readable at $HF_TOKEN_FILE" >&2; exit 1; }
[ -d "$NFS_EXPORT_PATH" ] || {
  echo "ERROR: $NFS_EXPORT_PATH missing. Run setup-nfsordma-server.sh first." >&2
  exit 1
}
if [ -e "${RUN_DIR}/checkpoints/LATEST" ]; then
  echo "ERROR: ${RUN_DIR}/checkpoints/LATEST exists. Refusing to clobber." >&2
  echo "       To re-run: rm -rf ${RUN_DIR}" >&2
  exit 1
fi

sudo mkdir -p "${RUN_DIR}/checkpoints" "${RUN_DIR}/logs"
sudo chmod -R 777 "${RUN_DIR}"
mkdir -p "${LOG_DIR}" "${EXP_ROOT}/hf-cache"
chmod 777 "${LOG_DIR}" "${EXP_ROOT}/hf-cache"

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
cat > "${EXP_ROOT}/sft-nfsordma.sh" <<CONTAINER_SCRIPT
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
echo "=== checkpoint footprint (NFS-export tree on host 1) ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "${EXP_ROOT}/sft-nfsordma.sh"

# --- Run (rank 0 on host 1: bind-mount the NFS-export directory directly, not the mount) ---
echo "==> launching multinode-nfsordma training (rank $NODE_RANK on $NODE)"
docker run --rm --network=host --gpus all --ipc=host --shm-size=64g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "${EXP_ROOT}/hf-cache:/root/.cache/huggingface" \
  -v "${RUN_DIR}/checkpoints:/opt/Automodel/checkpoints" \
  -v "${EXP_ROOT}/sft-nfsordma.sh:/tmp/sft.sh:ro" \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e "HF_HUB_OFFLINE=$HF_HUB_OFFLINE" \
  -e NCCL_DEBUG=INFO -e "NCCL_SOCKET_IFNAME=$HOST1_IFACE" -e "NCCL_IB_HCA=$NCCL_IB_HCA" \
  -e "GLOO_SOCKET_IFNAME=$HOST1_IFACE" -e "TP_SOCKET_IFNAME=$HOST1_IFACE" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh \
  2>&1 | tee "${LOG_DIR}/training-${PHASE}-${NODE}.log"

# --- Hand off ownership so subsequent kit scripts can read state without sudo ---
echo "==> sudo chown on ${RUN_DIR}/checkpoints (training container ran as root)"
sudo chown -R "$USER:$USER" "${RUN_DIR}/checkpoints"
sudo chmod -R u+rwX,go+rX "${RUN_DIR}/checkpoints"

# --- Quick headline check ---
echo
echo "=== headline check (NFSoRDMA shared FS) ==="
echo "Memory plateau (expect ~35.5 GiB per rank):"
grep -E "mem [0-9.]+ GiB" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | tail -5 || true
echo "Consolidation events (expect ~22-24 sec FLAT, no hot/cold spread — fsync-bound):"
grep -E "Done consolidating|saved.*checkpoint" "${LOG_DIR}/training-${PHASE}-${NODE}.log" | head -10 || \
  echo "  (no consolidation lines matched — check the training log directly)"

echo
echo "==> run-multinode-nfsordma-host1.sh complete on $NODE."
echo "    Logs: ${LOG_DIR}/{iostat,rss,training}-${PHASE}-${NODE}.log"
echo "    Note: no separate sync phase. Host 2's NVMe accumulated 0 bytes; all checkpoint"
echo "          data physically lives on host 1 via the NFS export."
echo "    Cleanup: checkpoint files are root-owned (training container default). Use"
echo "             'sudo rm -rf ${RUN_DIR}' to remove them."
echo "    Next: run-cold-restore-nfsordma.sh for the TP6 cold-cache restore measurement."
