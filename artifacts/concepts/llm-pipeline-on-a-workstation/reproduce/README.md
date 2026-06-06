# Reproduce the vegan-recipe pipeline, end to end

These are the actual scripts behind [the capstone article](../llm-pipeline-on-a-workstation.md): ingest and clean a recipe corpus, generate an instruction set from a teacher model, fine-tune a small student two ways, evaluate it, and serve it. The recipe assistant is the vehicle; the storage behavior is the subject the article measures.

**What this kit is, and is not.** This is the **as-run** material, the scripts and configs used during the original runs, validated piecemeal stage by stage at the time, not re-executed as one fresh end-to-end pass. Every number in the article traces to those runs. Paths are parameterized with environment variables (host scripts use plain `/data` and `/out` constants you edit at the top of each file). Adjust them to your environment before running. The NCCL/RoCE interface names in the two-node shell scripts are this lab's NICs; set them to your fabric's.

The two storage A/Bs the article reports (checkpoint write/read, loader cold-load) ship as their own validated, self-contained kits; see **Storage measurements** below. This kit is the wider pipeline those measurements sit inside.

## What's here

- **`data-gen/`**: ingest and clean the corpus, then generate the synthetic instruction set from a teacher model.
- **`finetune-eval/`**: LoRA and full-parameter SFT fine-tunes (single-node and two-node) plus the eval drivers and the recipe YAMLs.
- **`serve/`**: stand up vLLM with the loader comparison.

## Environment

Set these before running the container scripts (`finetune-eval/*.sh`, `serve/serve.sh`):

| Variable | Meaning | Container mount |
| --- | --- | --- |
| `DATA_DIR` | host dir with the dataset, tokenizer, and recipe YAML | `/data` |
| `CKPT_DIR` | host dir for checkpoint output (shared filesystem for two-node) | `/ckpt` |
| `HF_CACHE` | host HuggingFace cache dir holding the base model | `/root/.cache/huggingface` or `/hf-cache` |
| `IMG` | container image (training: `nvcr.io/nvidia/nemo-automodel:26.02`; serving: a vLLM image with streaming loaders) | n/a |
| `MASTER_ADDR` | rank-0 node IP on your training fabric (two-node only) | n/a |
| `HF_TOKEN` | optional, for gated model pulls | n/a |

The host-side data-prep and generation scripts (`data-gen/*.py`) use `/data` and `/out` as default path constants; edit them at the top of each file, or symlink, to point at your working directories. They run in a normal Python environment (vLLM is needed for the generation step), not in the training container.

## End-to-end sequence

**1. Data prep** (`data-gen/`): `pass1_schema_unify.py` then `pass2_vegan_classify.py` then `pass3_dedup.py`, with `spot_check_phase2_output.py` as a QA spot-check. Produces the diet corpus plus the grounding corpus.

**2. Synthetic generation** (`data-gen/`): `build_prompts.py` then `split_prompts.py` to fan the prompt list across nodes, then `generate.py` to drive the teacher (or `orchestrator.py` for the chunked, restart-bounded overnight run). Then `filter_clean.py` for the dietary screen, `split_dataset.py` for the train/val split, and `to_chat_format.py` for the chat formatting. `gen_nothink_tokenizer.py` builds the thinking-stripped tokenizer the fine-tune uses.

**3. Fine-tune** (`finetune-eval/`): pick an arm. LoRA: `lora_singlenode.sh` or `lora_2node.sh`. Full SFT: `fullsft_singlenode.sh` or `fullsft_2node.sh`. Each drives the matching `qwen3_8b_vegan_*.yaml` recipe; the two-node arms launch rank 0 first, then rank 1.

**4. Eval** (`finetune-eval/`): `strip_lm_head_lora.py` prepares the adapter for vLLM (the NeMo `match_all_linear` LoRA touches `lm_head`, which vLLM rejects), then `eval_traffic.py` replays the held-out prompts at the live server and `quality_screen.py` runs the cheap dietary screen. The LLM-judge step is described in the article and left to the reader.

**5. Serve** (`serve/`): `serve.sh <loader>` with `auto`, `fastsafetensors`, or `runai_streamer` to reproduce the cold-load comparison.

## Storage measurements

The two measurement-grade storage findings have their own validated kits, run them directly for the storage numbers:

- Checkpoint write/read A/B: [Lustre checkpoint-storage kit](../../../training/lustre-checkpoint-storage/reproduce/)
- Loader cold-load A/B/C: [vLLM cold-load loader kit](../../../inference/vllm-cold-load-loader-bound/reproduce/)

## Out of scope

The single-file checkpoint consolidation path (DCP to a consolidated HuggingFace safetensors file) is intentionally not included. On this lab's filesystem stack it trips a separate upstream client defect, reported through the upstream channel rather than reproduced as a public crash recipe. The fine-tune arms here use the sharded checkpoint path (`save_consolidated: false`), which is safe by construction.

## See also

The article this kit reproduces: [What I learned building an LLM pipeline on a workstation](../llm-pipeline-on-a-workstation.md).
