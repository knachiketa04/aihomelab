#!/bin/bash
# Reproduce kit — cold-cache checkpoint restore (touch point 6 in isolation)
#
# Reproduces: cold-cache resume from c25's epoch_0_step_99 checkpoint,
#             5 more training steps, end-of-training auto-checkpoint at step 104.
# Verifies:   touch point 6 effective read rate (~880 MB/s) and iostat peak
#             (~1153 MB/s) for the 50 GB checkpoint state load. Two-phase
#             signature in iostat: DCP shard read at 600–700 MB/s for ~22 sec,
#             then optimizer DCP read at 1019–1153 MB/s for ~28 sec.
# Runtime:    ~5 minutes.
# Disk:       +62 GB (one new checkpoint at step 104, written into checkpoints-c25/).
#
# REQUIRES SUDO: drops the kernel page cache so restore reads come from NVMe,
# not from RAM. Without the drop, we'd be measuring page-cache read rate
# (~3+ GB/s) instead of touch point 6 (~880 MB/s effective from cold NVMe).
#
# Run this AFTER run-c25.sh has produced epoch_0_step_99. Resume target is
# the LATEST symlink in checkpoints-c25/.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/sparks/full-sft-touchpoints-reproduce}"
HF_TOKEN_FILE="${HF_TOKEN_FILE:-$HOME/.huggingface_token}"
CONTAINER="${CONTAINER:-nvcr.io/nvidia/nemo-automodel:26.02}"

[ -r "$HF_TOKEN_FILE" ] || { echo "FATAL: cannot read $HF_TOKEN_FILE"; exit 1; }
[ -d "$EXP_ROOT/hf-cache" ] || { echo "FATAL: $EXP_ROOT/hf-cache missing — run run-smoke.sh first"; exit 1; }
[ -d "$EXP_ROOT/checkpoints-c25/epoch_0_step_99" ] || { echo "FATAL: $EXP_ROOT/checkpoints-c25/epoch_0_step_99 missing — run run-c25.sh first"; exit 1; }

# Pre-flight: drop page cache so the restore reads come from NVMe, not RAM.
# This is the load-bearing step — without it, the touch-point-6 measurement
# is meaningless. Will prompt for sudo password.
echo "=== pre-flight: drop kernel page cache (sudo required) ==="
sync
sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'
free -h
echo "=== cache cleared at $(date -Iseconds) ==="

mkdir -p "$EXP_ROOT/logs"

cat > "$EXP_ROOT/sft-restore.sh" <<'CONTAINER_SCRIPT'
#!/bin/bash
set -euo pipefail
cd /opt/Automodel
echo "=== START $(date -Iseconds) ==="
free -h
echo "=== checkpoints/ contents (NeMo will auto-resume from LATEST) ==="
ls -lah /opt/Automodel/checkpoints/
echo "=== launching python (restore: max_steps=105, resume from LATEST=step_99) ==="
python3 examples/llm_finetune/finetune.py \
  -c examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml \
  --model.pretrained_model_name_or_path Qwen/Qwen3-8B \
  --step_scheduler.global_batch_size 1 \
  --step_scheduler.local_batch_size 1 \
  --step_scheduler.max_steps 105 \
  --step_scheduler.ckpt_every_steps 25 \
  --step_scheduler.val_every_steps 25 \
  --packed_sequence.packed_sequence_size 1024 \
  2>&1 | tee /opt/Automodel/checkpoints/restore-output.log
echo "=== END $(date -Iseconds) ==="
echo "=== checkpoints after restore + 5 more steps + auto-checkpoint ==="
du -sh /opt/Automodel/checkpoints/
ls -lah /opt/Automodel/checkpoints/
CONTAINER_SCRIPT
chmod +x "$EXP_ROOT/sft-restore.sh"

if command -v iostat >/dev/null; then
  iostat -t -dxm 2 nvme0n1 > "$EXP_ROOT/logs/iostat-restore.log" 2>&1 &
  IOSTAT_PID=$!
  trap 'kill "$IOSTAT_PID" 2>/dev/null || true' EXIT
  echo "iostat backgrounded as PID $IOSTAT_PID — log at $EXP_ROOT/logs/iostat-restore.log"
fi

# Bind-mount checkpoints-c25 (NOT a fresh dir) so NeMo finds LATEST → step_99 and resumes.
docker run --rm \
  --gpus all \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  --shm-size=16g \
  -e HF_TOKEN="$(cat "$HF_TOKEN_FILE")" \
  -e HF_HUB_OFFLINE=1 \
  -v "$EXP_ROOT/hf-cache:/root/.cache/huggingface" \
  -v "$EXP_ROOT/checkpoints-c25:/opt/Automodel/checkpoints" \
  -v "$EXP_ROOT/sft-restore.sh:/tmp/sft.sh:ro" \
  --entrypoint /usr/bin/bash \
  "$CONTAINER" /tmp/sft.sh

echo
echo "=== headline check (touch point 6) ==="
echo "Resume timeline (expect ~58 sec total restore phase):"
echo
grep -E "Param L2 norm|Loading checkpoint|step 100 |step 101 " "$EXP_ROOT/checkpoints-c25/restore-output.log" | head -10
echo
echo "Auto-checkpoint at step 104 (expect cache-hot ~12–13 sec consolidation since"
echo "this is the first checkpoint of the resumed process):"
echo
grep -E "Done consolidating" "$EXP_ROOT/checkpoints-c25/restore-output.log" | tail -3
echo
echo "Run analyze-iostat.sh against \"$EXP_ROOT/logs/iostat-restore.log\" to see the"
echo "two-phase TP6 signature: DCP shard read at 600–700 MB/s, then optimizer DCP"
echo "at 1019–1153 MB/s sustained. Peak read ~1153 MB/s."
