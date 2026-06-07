# Artifacts

Three kinds of artifacts live here:

- **Experimental artifacts** — measurement-driven write-ups, each paired with a reproducible kit you can run on similar hardware. Organized by AI pipeline stage:
  - **`data-prep/`** — dataset ingest, transformation, tokenization, cache behavior.
  - **`training/`** — fine-tuning, checkpointing, dataloaders, restart behavior.
  - **`inference/`** — model loading, serving startup, model cache, runtime behavior.
- **Concept artifacts** — framing-and-explanation guides written from a storage practitioner's lens, anchored to industry-standard references and to the experimental artifacts above. No reproduce kit; the structural integrity comes from anchoring discipline (every framing either links to a measurement or carries a *descriptive only* tag).
  - **`concepts/`** — pipeline maps, storage decision frameworks, cross-stage patterns.
- **Published assets** — datasets and model adapters released to the community on Hugging Face. The card here mirrors the live repo; the actual data and weights live on the Hub and the cluster, never in Git.
  - **`datasets/`** — released instruction-tuning datasets.
  - **`models/`** — released fine-tuned model adapters.

The catalog grows as experiments earn their place in it (measured, framed, packaged for someone else to repeat) and as concept artifacts cover patterns that emerge across multiple experiments.

## Catalog

### training

- **[full-sft-storage-touchpoints](training/full-sft-storage-touchpoints/full-sft-storage-touchpoints.md)** (2026-05-04) — How storage actually behaves in an AI training workload, with one DGX Spark + Qwen3-8B as the worked example. Seven storage touch points; the 6× page-cache pattern in checkpoint consolidation reproduced 3× independently; methodologically: a small UMA workstation surfaces architectural lessons that transfer to large-scale infrastructure planning.
- **[multi-node-training-storage](training/multi-node-training-storage/multi-node-training-storage.md)** (2026-05-06) — How to choose distributed-training storage by measuring read-vs-write dominance over your specific fabric, with NFSoRDMA on a 2-host UMA cluster as the worked example. Three-way controlled comparison: shared FS pays a 5× per-checkpoint tax to eliminate the post-training sync layer entirely; the falsified-prediction surprise that NFS-over-TCP writes outperform NFS-over-RDMA in sync-export regime by 13%.
- **[lustre-on-uma-workstations](training/lustre-on-uma-workstations/lustre-on-uma-workstations.md)** (2026-05-09) — How to stand up minimum-viable distributed Lustre on 2 UMA workstations with stock single-NVMe layouts (no destructive partitioning), and which configuration knobs are load-bearing. Three-knob trio (`primarycache=metadata`, `atime=off`, `obdfilter.brw_size=4`) recovers ~85% of the single-node loopback ceiling on bulk IO; without them, default config delivers 32× less on 64 KiB writes and 400× less on 4 KiB random IOPS. Cross-node Lustre is architecturally ~6× slower than NFSoRDMA on cached reads on identical hardware (RPC framing + LDLM + osd-zfs + ko2iblnd stack depth, not pipeline depth — BDP analysis rules that out); distributed Lustre's win condition is concurrent multi-client access where aggregate throughput reaches 60–85% of the loopback ceiling.
- **[lustre-checkpoint-storage](training/lustre-checkpoint-storage/lustre-checkpoint-storage.md)** (2026-05-30) — Full-SFT checkpoint storage on Lustre-on-ZFS is client-bound in every regime: single writer 0.78 GB/s, two concurrent writers 1.32 GB/s (1.69×), restore read 1.37 GB/s, with the NVMe under 55% busy on writes and ~20% on reads throughout. The transferable lesson is methodological: on a file-backed-zpool stack `iostat %util` is degenerate because the workload's throughput sits inside the substrate's own delivered band, so the ZFS transaction-group kstat and per-thread CPU are what adjudicate client-bound vs substrate-bound. Size checkpoints for writer concurrency and per-completion latency, not tier bandwidth; independently reproduced at 1.72× on the same hardware class.

### data-prep

- **[spark-nvme-fio-baseline](data-prep/spark-nvme-fio-baseline/spark-nvme-fio-baseline.md)** (2026-05-03) — How to baseline NVMe for AI infrastructure, with a single Gen5 NVMe as the worked example. The SLC fall-off (6×, burst → post-SLC TLC sustained) is the dominant performance gap for AI workloads writing more than the SLC cache — larger than the loader gap (3.3×). Reproduce kit documents three FIO methodology bugs that systematically inflate published SSD benchmarks.

### inference

- **[vllm-cold-load-loader-bound](inference/vllm-cold-load-loader-bound/vllm-cold-load-loader-bound.md)** (2026-05-31) — Why a slow vLLM cold model load on a UMA workstation is the loader's fault, not the storage tier's. The default safetensors loader is single-thread-CPU-bound: it pins one core for ~106 s while the NVMe loafs at a few percent of its bandwidth, so swapping in a streaming loader (RunAI Model Streamer or fastsafetensors) cuts cold model load 14 to 36x with no change to storage. Tier-irrelevance is loader-dependent: once the fast loader removes the CPU wall it reads near the local-NVMe ceiling, so on a slower tier the fast loaders would themselves become storage-bound.

### concepts

- **[storage-touchpoints-map](concepts/storage-touchpoints-map/storage-touchpoints-map.md)** (2026-06-06, revised from 2026-05-27) — a storage practitioner's first-principles map of the LLM development pipeline (data prep → training → inference), enumerating where storage shows up at each stage, when it dominates, and when it doesn't. Anchored to four industry-standard references (MLPerf Storage, NVIDIA DGX SuperPOD, Google Cloud AI/ML storage architecture, NVIDIA GPU Direct Storage) and to the lab's measured experimental artifacts.
- **[llm-pipeline-on-a-workstation](concepts/llm-pipeline-on-a-workstation/llm-pipeline-on-a-workstation.md)** (2026-06-07) — an end-to-end LLM pipeline built and measured on a two-node DGX Spark home lab (data prep → teacher-student distillation → fine-tune → eval → serve), walking each stage's real storage I/O behavior, the layer the bottleneck actually lived in, and qualitative projections to where storage flips to the constraint at enterprise scale. The functional, end-to-end companion to storage-touchpoints-map (theory put into practice).

### datasets

- **[vegan-vegetarian-recipes-qa](datasets/vegan-vegetarian-recipes-qa/README.md)** (2026-06-07) — an 11,582-pair synthetic vegan and vegetarian recipe Q&A instruction set, distilled from a Qwen3-32B teacher; the data stage of the end-to-end pipeline. The card is honest about the keyword dietary-screen's heavy over-flagging (~14% of rows flagged, dominated by correct-substitution false positives, and kept on purpose) and about the templated train/test prompt overlap. CC BY-SA 4.0. Published at [huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa](https://huggingface.co/datasets/knachiketa004/vegan-vegetarian-recipes-qa).

### models

- **[vegetarian-recipe-qwen3-8b-lora](models/vegetarian-recipe-qwen3-8b-lora/README.md)** (2026-06-07) — a Qwen3-8B LoRA adapter for vegan and vegetarian recipe Q&A; the fine-tune stage of the pipeline. Served adapter-on-base (the `lm_head` LoRA pair is stripped so vLLM accepts it), with honest limits: dietary slips in 2 of 5 samples versus 4 of 5 for the un-fine-tuned base, plus a dish-naming tic. Apache-2.0. Published at [huggingface.co/knachiketa004/vegetarian-recipe-qwen3-8b-lora](https://huggingface.co/knachiketa004/vegetarian-recipe-qwen3-8b-lora).

---

For the underlying methodology and lab context, see the [README](../README.md), [environment notes](../environment/), and [scope-and-caveats](scope-and-caveats.md).
