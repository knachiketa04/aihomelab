#!/usr/bin/env bash
# Two-node FSDP2 LoRA fine-tune of Qwen3-8B (the multi-node comparison).
# Run on BOTH nodes; pass node rank as $1 (0 = node 0 / rank 0 / rendezvous host, 1 = node 1).
# Launch rank 0 first, then rank 1 within ~10s. Same recipe as single-node, GBS 32 held constant
# (world 2 -> grad_accum 2). Checkpoints go to the SHARED-filesystem dir: this handles NeMo's
# asymmetric-write / symmetric-read pattern (rank-0 singletons + LATEST land on shared storage,
# both ranks read them back).
#
# Env vars to set:
#   DATA_DIR     host dir with dataset, tokenizer, recipe yaml   -> /data
#   CKPT_DIR     host SHARED-filesystem dir for checkpoints       -> /ckpt
#   HF_CACHE     host HuggingFace cache dir                       -> /root/.cache/huggingface
#   MASTER_ADDR  rank-0 node IP on your training fabric
#   IMG          container image (default below); HF_TOKEN optional
# The NCCL_*/GLOO_*/TP_* interface names below are this lab's RoCE NICs; set them to your fabric's interfaces.
set -euo pipefail
NODE_RANK="${1:?usage: lora_2node.sh <node_rank: 0=rank0/rendezvous, 1=rank1>}"

IMG=${IMG:-nvcr.io/nvidia/nemo-automodel:26.02}
DATA_DIR=${DATA_DIR:?set to the host dir holding the dataset, tokenizer, and recipe yaml}
CKPT_DIR=${CKPT_DIR:?set to the shared-filesystem dir for checkpoint output}
HF_CACHE=${HF_CACHE:?set to your HuggingFace cache dir}
MASTER_ADDR=${MASTER_ADDR:?set to the rank-0 node IP on your training fabric}
PORT=29500

docker run --rm --network=host --gpus all --ipc=host --shm-size=16g \
  --device=/dev/infiniband:/dev/infiniband --cap-add=IPC_LOCK \
  --ulimit memlock=-1 --ulimit stack=67108864 \
  -v "$HF_CACHE":/root/.cache/huggingface -v "$DATA_DIR":/data -v "$CKPT_DIR":/ckpt \
  -e HF_TOKEN="${HF_TOKEN:-}" -e HF_HUB_OFFLINE=1 \
  -e NCCL_DEBUG=INFO -e NCCL_SOCKET_IFNAME=enp1s0f0np0 -e NCCL_IB_HCA=rocep1s0f0 \
  -e GLOO_SOCKET_IFNAME=enp1s0f0np0 -e TP_SOCKET_IFNAME=enp1s0f0np0 \
  "$IMG" \
  torchrun --nnodes=2 --node_rank="$NODE_RANK" --nproc_per_node=1 \
    --master_addr="$MASTER_ADDR" --master_port="$PORT" \
    /opt/Automodel/examples/llm_finetune/finetune.py \
    -c /data/qwen3_8b_vegan_lora.yaml \
    --checkpoint.checkpoint_dir /ckpt/two-node
