#!/usr/bin/env bash
# Reproduce kit — launch one serve arm.
#
# Usage:  serve-arm.sh <load-format>     (auto | runai_streamer | fastsafetensors)
#   e.g.  bash serve-arm.sh auto
#         bash serve-arm.sh runai_streamer
#
# Serves base Qwen3-8B from a local HF cache. Only --load-format varies across
# arms; everything else is held constant so it never confounds the loader A/B.
#   --enforce-eager  isolates the weight-load phase (skips compile / cudagraph).
#   -d, no --rm      so a crash leaves readable logs in `docker logs`.
#
# Adapter serving is OPTIONAL. The 18-36x headline reproduces on base alone.
# To serve an adapter-on-base (LoRA) instead, export before calling:
#   ENABLE_LORA=1
#   ADAPTER_DIR=/host/path/to/lm_head-stripped-adapter   (see strip-lm-head-lora.py)
#   CHAT_TEMPLATE=/host/path/to/chat_template.jinja        (optional; mounted if set)
#
# Standalone usage: tunables are all env vars; safe to source outside the kit.

set -euo pipefail

LF="${1:?usage: serve-arm.sh <load-format>  (auto|runai_streamer|fastsafetensors)}"

CONTAINER="${CONTAINER:-vllm-loaders:cold-load}"
HF_CACHE="${HF_CACHE:-/home/$USER/hf-cache}"
MODEL_REPO="${MODEL_REPO:-models--Qwen--Qwen3-8B}"
SNAPSHOT_HASH="${SNAPSHOT_HASH:-b968826d9c46dd6066d109eabc6255188de91218}"
SERVED_NAME="${SERVED_NAME:-recipe-qwen3-8b}"
PORT="${PORT:-8000}"
NAME="${NAME:-vllm-coldload}"

ENABLE_LORA="${ENABLE_LORA:-0}"
ADAPTER_DIR="${ADAPTER_DIR:-}"
CHAT_TEMPLATE="${CHAT_TEMPLATE:-}"
MAX_LORA_RANK="${MAX_LORA_RANK:-16}"

command -v docker >/dev/null || { echo "FATAL: docker not on PATH"; exit 1; }

SNAP="/hf-cache/hub/${MODEL_REPO}/snapshots/${SNAPSHOT_HASH}"

# --- assemble optional adapter + chat-template flags ------------------------
EXTRA_MOUNTS=()
EXTRA_ARGS=()

if [ -n "$CHAT_TEMPLATE" ]; then
  [ -r "$CHAT_TEMPLATE" ] || { echo "FATAL: CHAT_TEMPLATE not readable: $CHAT_TEMPLATE"; exit 1; }
  EXTRA_MOUNTS+=(-v "$CHAT_TEMPLATE:/tok/chat_template.jinja:ro")
  EXTRA_ARGS+=(--chat-template /tok/chat_template.jinja)
fi

if [ "$ENABLE_LORA" = "1" ]; then
  [ -n "$ADAPTER_DIR" ] || { echo "FATAL: ENABLE_LORA=1 but ADAPTER_DIR unset"; exit 1; }
  [ -d "$ADAPTER_DIR" ] || { echo "FATAL: ADAPTER_DIR not a directory: $ADAPTER_DIR"; exit 1; }
  EXTRA_MOUNTS+=(-v "$ADAPTER_DIR:/adapter:ro")
  EXTRA_ARGS+=(--enable-lora --lora-modules "recipe=/adapter" --max-lora-rank "$MAX_LORA_RANK")
  echo "adapter tier ON: serving recipe=/adapter (rank<=$MAX_LORA_RANK) on base"
else
  echo "adapter tier OFF: serving base $MODEL_REPO (the 18-36x headline reproduces here)"
fi

docker rm -f "$NAME" 2>/dev/null || true
docker run -d --name "$NAME" \
  --network=host --gpus all --ipc=host --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "$HF_CACHE:/hf-cache:ro" \
  "${EXTRA_MOUNTS[@]}" \
  -e HF_HUB_OFFLINE=1 \
  "$CONTAINER" \
  vllm serve "$SNAP" \
    --host 0.0.0.0 --port "$PORT" \
    --load-format "$LF" \
    "${EXTRA_ARGS[@]}" \
    --served-model-name "$SERVED_NAME" \
    --gpu-memory-utilization 0.5 --max-model-len 16384 --max-num-seqs 64 \
    --enforce-eager \
    --tensor-parallel-size 1

echo
echo "launched $NAME with --load-format $LF"
echo "read load time with:"
echo "  docker logs $NAME 2>&1 | grep -iE 'Loading weights took|Model loading took'"
echo "health gate:"
echo "  curl -s http://localhost:$PORT/health -o /dev/null -w '%{http_code}\\n'"
