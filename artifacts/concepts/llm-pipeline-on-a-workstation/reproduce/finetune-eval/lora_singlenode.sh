#!/usr/bin/env bash
# Single-node LoRA fine-tune of Qwen3-8B on the vegan/vegetarian recipe set (the A/B baseline).
# Run inside tmux so it survives an SSH disconnect.
#
# Env vars to set:
#   DATA_DIR  host dir with dataset, tokenizer, and recipe yaml  -> container /data
#   CKPT_DIR  host dir for adapter checkpoint output             -> container /ckpt
#   HF_CACHE  host HuggingFace cache dir                         -> container /root/.cache/huggingface
#   IMG       container image (default below); HF_TOKEN optional
set -euo pipefail

IMG=${IMG:-nvcr.io/nvidia/nemo-automodel:26.02}
DATA_DIR=${DATA_DIR:?set to the host dir holding the dataset, tokenizer, and recipe yaml}
CKPT_DIR=${CKPT_DIR:?set to the host dir for adapter checkpoint output}
HF_CACHE=${HF_CACHE:?set to your HuggingFace cache dir}

docker run --rm --network=host --gpus all --ipc=host --shm-size=16g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "$HF_CACHE":/root/.cache/huggingface -v "$DATA_DIR":/data -v "$CKPT_DIR":/ckpt \
  -e HF_TOKEN="${HF_TOKEN:-}" -e HF_HUB_OFFLINE=1 \
  "$IMG" \
  torchrun --standalone --nnodes=1 --nproc_per_node=1 \
    /opt/Automodel/examples/llm_finetune/finetune.py \
    -c /data/qwen3_8b_vegan_lora.yaml \
    --checkpoint.checkpoint_dir /ckpt/single-node
