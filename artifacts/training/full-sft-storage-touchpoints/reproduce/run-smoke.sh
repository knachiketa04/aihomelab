#!/bin/bash
# Reproduce kit — smoke test
#
# Reproduces: full SFT of Qwen3-8B for 3 steps + 1 checkpoint write, cold-cache start.
# Verifies:   touch points 3 (model acquire via xet), 4 (model load — note iostat
#             will show ZERO NVMe reads here because the just-pulled shards are
#             still in the page cache), 5 (checkpoint save) and 7 (consolidation,
#             cache-hot first-checkpoint pattern at ~13 sec).
# Runtime:    ~10 minutes (after the container image is cached locally).
# Disk:       ~80 GB total (16 GB HF cache + 62 GB checkpoint + small logs).
#
# See README.md in this directory for environment requirements.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/full-sft-touchpoints-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"

[ -r "$HF_TOKEN_FILE" ] || { echo "FATAL: cannot read $HF_TOKEN_FILE"; exit 1; }
command -v docker >/dev/null || { echo "FATAL: docker not on PATH"; exit 1; }
command -v iostat >/dev/null || echo "WARN: iostat not found — side-channel will be skipped (the touch-point timeline analysis depends on this log)"

mkdir -p "$EXP_ROOT/hf-cache" "$EXP_ROOT/checkpoints-smoke" "$EXP_ROOT/logs"
chmod 777 "$EXP_ROOT/hf-cache" "$EXP_ROOT/checkpoints-smoke" "$EXP_ROOT/logs"

cat > "$EXP_ROOT/sft-smoke.sh" <<'CONTAINER_SCRIPT'
#!/bin/bash
set -euo pipefail
cd /opt/Automodel
echo "=== START $(date -Iseconds) ==="
free -h
echo "=== launching python (smoke: max_steps=3, ckpt_every=3) ==="
python3 examples/llm_finetune/finetune.py \
  -c examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \
  --model.pretrained_model_name_or_path Qwen/Qwen3-8B \
  --step_scheduler.global_batch_size 1 \
  --step_scheduler.local_batch_size 1 \
  --step_scheduler.max_steps 3 \
  --step_scheduler.ckpt_every_steps 3 \
  --packed_sequence.packed_sequence_size 1024 \
  2>&1 | tee /opt/Automodel/checkpoints/smoke-output.log
echo "=== END $(date -Iseconds) ==="
echo "=== checkpoint footprint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "$EXP_ROOT/sft-smoke.sh"

if command -v iostat >/dev/null; then
  iostat -t -dxm 2 nvme0n1 > "$EXP_ROOT/logs/iostat-smoke.log" 2>&1 &
  IOSTAT_PID=$!
  trap 'kill "$IOSTAT_PID" 2>/dev/null || true' EXIT
  echo "iostat backgrounded as PID $IOSTAT_PID — log at $EXP_ROOT/logs/iostat-smoke.log"
fi

docker run --rm \
  --gpus all \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=16g \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -v "$EXP_ROOT/hf-cache:/root/.cache/huggingface" \
  -v "$EXP_ROOT/checkpoints-smoke:/opt/Automodel/checkpoints" \
  -v "$EXP_ROOT/sft-smoke.sh:/tmp/sft.sh:ro" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh

echo
echo "=== checkpoint on host ==="
du -sh "$EXP_ROOT/checkpoints-smoke/"
du -sh "$EXP_ROOT/checkpoints-smoke"/* 2>/dev/null || true
echo
echo "=== headline checks ==="
echo "Cold-pull aggregate (expect ~900 MB/s for ~16 GB / ~18 sec):"
echo "  grep 'Fetching 5 files: 100%' \"$EXP_ROOT/checkpoints-smoke/smoke-output.log\""
echo
echo "L2 norm (expect 2459.6220 byte-identical — model load is deterministic):"
echo "  grep 'Param L2 norm' \"$EXP_ROOT/checkpoints-smoke/smoke-output.log\""
echo
echo "Checkpoint consolidation (expect cache-hot ~12–13 sec since this is the first checkpoint of the run):"
echo "  grep 'Done consolidating' \"$EXP_ROOT/checkpoints-smoke/smoke-output.log\""
echo
echo "Then run analyze-iostat.sh against \"$EXP_ROOT/logs/iostat-smoke.log\" to see the touch-point timeline."
