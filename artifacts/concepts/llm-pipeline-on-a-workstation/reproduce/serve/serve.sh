#!/usr/bin/env bash
# Loader A/B/C — launch one serve arm. Usage: serve.sh <load-format>
#   e.g.  bash serve.sh auto | runai_streamer | fastsafetensors
# Serves base Qwen3-8B (from the local HF cache) + the lm_head-stripped vegan LoRA adapter.
# Only --load-format varies across arms; everything else is held constant. --enforce-eager isolates
# the weight-load phase; no --rm so a crash leaves readable logs.
#
# Env vars to set:
#   HF_CACHE  host HuggingFace cache dir holding the base model       -> container /hf-cache (ro)
#   ADAPTER   host dir with the lm_head-stripped vegan LoRA adapter   -> /adapter (ro)
#   TOK       host dir with the no-think tokenizer + chat template    -> /tok (ro)
#   SNAP      container path to the base-model snapshot under /hf-cache (adjust the hash to your cache)
#   IMG       serving container image (vLLM + streaming loaders)
set -euo pipefail
LF="${1:?usage: serve.sh <load-format>  (auto|runai_streamer|fastsafetensors)}"

IMG=${IMG:-vllm-loaders}
HF_CACHE=${HF_CACHE:?set to your HuggingFace cache dir holding the base model}
ADAPTER=${ADAPTER:?set to the dir with the lm_head-stripped vegan LoRA adapter}
TOK=${TOK:?set to the dir with the no-think tokenizer + chat template}
SNAP=${SNAP:-/hf-cache/hub/models--Qwen--Qwen3-8B/snapshots/<snapshot-hash>}

docker rm -f vllm-serve 2>/dev/null || true
docker run -d --name vllm-serve \
  --network=host --gpus all --ipc=host --shm-size=64g \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "$HF_CACHE":/hf-cache:ro \
  -v "$ADAPTER":/adapter:ro \
  -v "$TOK":/tok:ro \
  -e HF_HUB_OFFLINE=1 \
  "$IMG" \
  vllm serve "$SNAP" \
    --host 0.0.0.0 --port 8000 --load-format "$LF" --chat-template /tok/chat_template.jinja \
    --enable-lora --lora-modules vegan=/adapter --max-lora-rank 16 \
    --served-model-name vegan-recipe-qwen3-8b \
    --gpu-memory-utilization 0.5 --max-model-len 16384 --max-num-seqs 64 \
    --enforce-eager --tensor-parallel-size 1

echo "launched vllm-serve with --load-format $LF ; read load time with:"
echo "  docker logs vllm-serve 2>&1 | grep -iE 'Loading weights took|Model loading took'"
