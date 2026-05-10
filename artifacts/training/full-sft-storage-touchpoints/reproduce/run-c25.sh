#!/bin/bash
# Reproduce kit — extended c25 run (the headline 6× reproduction)
#
# Reproduces: 100-step SFT with ckpt_every=25 → 4 mid-training checkpoints
#             (steps 24, 49, 74, 99). Plus end-of-training validation.
# Verifies:   page-cache 6× wall-clock multiplier between checkpoint 1 (cache-hot)
#             and checkpoint 2 (cache-cold). Steps 74 + 99 settle at the
#             steady-state cache-cold rate.
# Runtime:    ~13 minutes (assumes hf-cache is warm from run-smoke.sh).
# Disk:       +250 GB on top of smoke (~330 GB total in EXP_ROOT).
#
# Run this AFTER run-smoke.sh has populated the HF cache. Set MAX_STEPS=250
# if you also want to reproduce the post-checkpoint sustained-slowdown
# phenomenon directly (six mid-training checkpoints; ~17 min wall-clock,
# +375 GB disk).

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/full-sft-touchpoints-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"
MAX_STEPS="${MAX_STEPS:-100}"

[ -r "$HF_TOKEN_FILE" ] || { echo "FATAL: cannot read $HF_TOKEN_FILE"; exit 1; }
[ -d "$EXP_ROOT/hf-cache" ] || { echo "FATAL: $EXP_ROOT/hf-cache missing — run run-smoke.sh first"; exit 1; }

mkdir -p "$EXP_ROOT/checkpoints-c25" "$EXP_ROOT/logs"
chmod 777 "$EXP_ROOT/checkpoints-c25" "$EXP_ROOT/logs"

cat > "$EXP_ROOT/sft-c25.sh" <<CONTAINER_SCRIPT
#!/bin/bash
set -euo pipefail
cd /opt/Automodel
echo "=== START \$(date -Iseconds) ==="
free -h
echo "=== launching python (c25: max_steps=${MAX_STEPS}, ckpt_every=25, val_every=25) ==="
python3 examples/llm_finetune/finetune.py \\
  -c examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \\
  --model.pretrained_model_name_or_path Qwen/Qwen3-8B \\
  --step_scheduler.global_batch_size 1 \\
  --step_scheduler.local_batch_size 1 \\
  --step_scheduler.max_steps ${MAX_STEPS} \\
  --step_scheduler.ckpt_every_steps 25 \\
  --step_scheduler.val_every_steps 25 \\
  --packed_sequence.packed_sequence_size 1024 \\
  2>&1 | tee /opt/Automodel/checkpoints/c25-output.log
echo "=== END \$(date -Iseconds) ==="
echo "=== checkpoint footprint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "$EXP_ROOT/sft-c25.sh"

if command -v iostat >/dev/null; then
  iostat -t -dxm 2 nvme0n1 > "$EXP_ROOT/logs/iostat-c25.log" 2>&1 &
  IOSTAT_PID=$!
  trap 'kill "$IOSTAT_PID" 2>/dev/null || true' EXIT
  echo "iostat backgrounded as PID $IOSTAT_PID — log at $EXP_ROOT/logs/iostat-c25.log"
fi

docker run --rm \
  --gpus all \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=16g \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e HF_HUB_OFFLINE=1 \
  -v "$EXP_ROOT/hf-cache:/root/.cache/huggingface" \
  -v "$EXP_ROOT/checkpoints-c25:/opt/Automodel/checkpoints" \
  -v "$EXP_ROOT/sft-c25.sh:/tmp/sft.sh:ro" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh

echo
echo "=== checkpoints on host ==="
du -sh "$EXP_ROOT/checkpoints-c25/"
du -sh "$EXP_ROOT/checkpoints-c25"/epoch_0_step_* 2>/dev/null || true
echo
echo "=== headline check (the 6× page-cache pattern) ==="
echo "Per-checkpoint consolidation timings (cache-hot first / cache-cold subsequent):"
echo
grep -E "Done consolidating" "$EXP_ROOT/checkpoints-c25/c25-output.log" || echo "  (no consolidation lines found — check the log directly)"
echo
echo "Expected pattern: step 24 ~12–13 sec (cache-hot), step 49 ~50–80 sec (cache-cold),"
echo "steps 74 + 99 ~50–60 sec (steady-state cold). The 6× ratio is between step 24 and step 49."
echo
echo "Next: run analyze-iostat.sh against \"$EXP_ROOT/logs/iostat-c25.log\" to see the storage timeline."
