#!/bin/bash
# Reproduce kit — extended c100 run (the cadence-cost contrast)
#
# Reproduces: 100-step SFT with ckpt_every=100 → 1 final checkpoint at step 99
#             (the cadence trigger and end-of-training auto-checkpoint coincide
#             since (99+1) % 100 == 0). NO mid-training checkpoints.
# Verifies:   the cadence-cost contrast against c25. With no prior checkpoint
#             to evict the just-written DCP shard, the final checkpoint's
#             consolidation lands in the cache-hot regime (~13 sec). Total
#             run wall-clock should be ~6 min vs c25's ~13 min.
# Runtime:    ~6 minutes (assumes hf-cache is warm from run-smoke.sh).
# Disk:       +62 GB (1 final checkpoint).
#
# Run this AFTER run-smoke.sh has populated the HF cache. Order vs run-c25.sh
# doesn't matter; they target separate checkpoint dirs.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/full-sft-touchpoints-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"

[ -r "$HF_TOKEN_FILE" ] || { echo "FATAL: cannot read $HF_TOKEN_FILE"; exit 1; }
[ -d "$EXP_ROOT/hf-cache" ] || { echo "FATAL: $EXP_ROOT/hf-cache missing — run run-smoke.sh first"; exit 1; }

mkdir -p "$EXP_ROOT/checkpoints-c100" "$EXP_ROOT/logs"
chmod 777 "$EXP_ROOT/checkpoints-c100" "$EXP_ROOT/logs"

cat > "$EXP_ROOT/sft-c100.sh" <<'CONTAINER_SCRIPT'
#!/bin/bash
set -euo pipefail
cd /opt/Automodel
echo "=== START $(date -Iseconds) ==="
free -h
echo "=== launching python (c100: max_steps=100, ckpt_every=100, val_every=100) ==="
python3 examples/llm_finetune/finetune.py \
  -c examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \
  --model.pretrained_model_name_or_path Qwen/Qwen3-8B \
  --step_scheduler.global_batch_size 1 \
  --step_scheduler.local_batch_size 1 \
  --step_scheduler.max_steps 100 \
  --step_scheduler.ckpt_every_steps 100 \
  --step_scheduler.val_every_steps 100 \
  --packed_sequence.packed_sequence_size 1024 \
  2>&1 | tee /opt/Automodel/checkpoints/c100-output.log
echo "=== END $(date -Iseconds) ==="
echo "=== checkpoint footprint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "$EXP_ROOT/sft-c100.sh"

if command -v iostat >/dev/null; then
  iostat -t -dxm 2 nvme0n1 > "$EXP_ROOT/logs/iostat-c100.log" 2>&1 &
  IOSTAT_PID=$!
  trap 'kill "$IOSTAT_PID" 2>/dev/null || true' EXIT
  echo "iostat backgrounded as PID $IOSTAT_PID — log at $EXP_ROOT/logs/iostat-c100.log"
fi

docker run --rm \
  --gpus all \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=16g \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e HF_HUB_OFFLINE=1 \
  -v "$EXP_ROOT/hf-cache:/root/.cache/huggingface" \
  -v "$EXP_ROOT/checkpoints-c100:/opt/Automodel/checkpoints" \
  -v "$EXP_ROOT/sft-c100.sh:/tmp/sft.sh:ro" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh

echo
echo "=== checkpoint on host ==="
du -sh "$EXP_ROOT/checkpoints-c100/"
ls -lah "$EXP_ROOT/checkpoints-c100/"
echo
echo "=== headline check (cadence-cost contrast) ==="
echo "Single checkpoint consolidation (expect cache-hot ~12–13 sec):"
echo
grep -E "Done consolidating" "$EXP_ROOT/checkpoints-c100/c100-output.log" || echo "  (no consolidation lines found — check the log directly)"
echo
echo "Compare wall-clock totals:"
echo "  c100 (this run): ~6 min, ~22% of total wall-clock in checkpoint sync block."
echo "  c25 (run-c25.sh): ~13 min, ~58% of total wall-clock in checkpoint-related overhead."
echo "  Run-only training time: c100 ~3 min vs c25 ~4.5 min — the difference is the"
echo "  sustained-slowdown tail after each cache-cold mid-training checkpoint."
