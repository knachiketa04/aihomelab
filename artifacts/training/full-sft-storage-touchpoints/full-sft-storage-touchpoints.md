# Storage Touch Points in AI Training, with One DGX Spark as the Worked Example

**Date:** 2026-05-04 · **Node:** spark01 · **Drive:** Samsung 9100 PRO OEM (MZALC4T0HBL1-00B07), Gen5 x4, 4 TB · **Container:** `nvcr.io/nvidia/nemo-automodel:26.02` · **Recipe:** `examples/llm_finetune/qwen/qwen3_8b_squad_spark.yaml`

> **What we asked:** where does storage actually matter in an AI training workload, and can a single small UMA workstation surface architectural findings that transfer to larger infrastructure? Measured on DGX Spark UMA, single-node, ARM64 Ubuntu, with the specific software versions above. See [artifacts/scope-and-caveats.md](../../scope-and-caveats.md) for what bounds how this generalizes.

## The touch-point map

Seven storage interactions in any AI training workload. The map is the organizing lens; everything below populates it with one workload's measurements.

| # | Touch point | When | Direction | This setup | Approx size |
|---|---|---|---|---|---|
| 1 | Dataset acquire | first run only | net → NVMe | HF Hub → local cache | 16 MB SQuAD (trivial here) |
| 2 | Dataset load + packing | per epoch / per validation | NVMe → UMA | local cache → RAM | <16 MB read; in-RAM packed seqs |
| 3 | Model acquire | first run only | net → NVMe | HF Hub (xet) → local cache | 16 GB safetensors, ~17 sec at 890 MB/s |
| 4 | Model load into compute | once per process start | NVMe → UMA | local cache → UMA | 16 GB; **three regimes** (see below) |
| 5 | Checkpoint save | per cadence + end-of-train | UMA → NVMe | UMA → local NVMe | 62 GB (16 GB DCP + 15 GB consolidated HF + 31 GB Adam DCP) |
| 6 | Checkpoint restore | resume only | NVMe → UMA | local NVMe → UMA | 50 GB read at ~880 MB/s effective; iostat peak 1153 |
| 7 | Deploy-format export | per save (folds into 5 here) | NVMe → UMA → NVMe | shard re-read + HF write | 16 GB read + 15 GB write; **6× wall-clock variance based on cache state** |

Cross-cutting concern: page cache state — the coupling between 5, 6, and 7. Same workload, same hardware, very different wall-clock depending on which pages are still resident.

## Findings

1. **You can do this on a single small workstation, and the framework transfers up.** The seven-touch-point map is universal across cloud, on-prem, and a single-workstation lab. The conventional intuition in enterprise storage is that you need fleet-scale workloads to learn architectural lessons about AI infrastructure storage. That intuition is wrong; one 8B-class fine-tune on one UMA node, instrumented carefully, surfaces the same architectural lessons that apply at much larger scale. The work is the same — measure your touch points, see which ones matter for your workload class, plan capacity around the ones that do.
2. **Page cache state alone produces a 6× difference in checkpoint consolidation wall-clock.** Same 16 GB read, same code path: cache-hot first checkpoint at 12-14 sec, cache-cold subsequent checkpoints at 50-80 sec. Reproduced three times independently. The architecture matters: NeMo's full-SFT consolidation reads the just-written DCP shard back from disk to write out HF safetensors; whether that read comes from RAM or from cold NVMe is the only thing that changes between the two regimes.
3. **`drop_caches` mid-run is largely ineffective on a memory-pressured training workload.** Tested in-flight: dropped 43 GiB of buff/cache, only 2 GiB came back. The workload pins the rest — mmap'd files, open file descriptors, and CUDA-managed UMA tensors that show up under `shared` in `free -h`. The kernel correctly refuses to evict pages an active process needs. The "just drop the cache" mitigation people reach for to unblock training throughput doesn't apply here.
4. **The checkpoint touch points (5, 6, 7) are CPU+NVMe events with GPU idle.** During the 58-second restore, the GPU sat at 50°C, 11.6 W, 2% utilization. During the 50-80 sec consolidation, similar. Storage tier choice matters for these touch points; GPU choice does not. Worth knowing when a "faster GPU" is being proposed as the answer to slow checkpoint cycles.

## Why this matters

Most public AI infrastructure write-ups treat training as a single black box with "checkpoint write" as a footnote. That framing hides where storage actually shows up — in seven distinct interactions with three or four different bottleneck mechanisms across them. The touch-point map is a planning lens: for each new workload class, walk the seven points, ask which ones the workload exercises at meaningful scale, and size your storage tiers around those. Cloud object storage, on-prem parallel filesystems, and local NVMe each have their own failure modes per touch point (parallel-upload throttles, metadata server contention, SLC cache fall-off), but the touch points are the same. And a single workstation with one 8B-class fine-tune is enough to walk the map end-to-end and produce numbers a peer will recognize when they look at their own infrastructure.

## Measured

**Touch points 1 + 2 (dataset acquire + load).** SQuAD at 16 MB is too small to register. The framework still applies — at scale these touch points dominate; characterizing them is a separate experiment with a meaningful dataset.

**Touch points 3 + 4 (model acquire + load).** Acquire is bandwidth-bound by the registry/protocol; load has three regimes depending on the gap between acquire and load.

| Touch point | Phase | Measurement |
|---|---|---|
| 3 — model acquire (cold) | xet protocol, 5 shards | 16 GB in 17 sec = ~890 MB/s aggregate |
| 4 — page-cache-served | cold-pull-then-immediate-load | **zero NVMe reads** — RAM-served from just-written shards |
| 4 — partial / mixed | typical warm restart | mixed, mostly cache-served |
| 4 — cold-cache served | post-`drop_caches` | 583-732 MB/s sustained for 22 sec, peak 732 |

The same touch point yields radically different storage profiles depending on the cache state at workload start. For "pull and run" pipelines, NVMe read bandwidth is irrelevant for touch point 4 (the read goes to RAM). For "long gap pull/load" or cross-node scenarios where the cache has gone cold, it reverts to NVMe-read at the rates above.

**Touch points 5 + 7 (checkpoint save + consolidate).** The headline 6× page-cache pattern is in the consolidation phase, which reads the just-written DCP shard back to write out HF safetensors.

| Checkpoint | Cache regime | Consolidation wall-clock | iostat signature |
|---|---|---|---|
| First checkpoint of a run | hot — DCP shard still in page cache | **12-14 sec** | pure write 1.0-1.7 GB/s |
| Second + subsequent | cold — evicted by intervening writes | **50-80 sec** | mixed read+write 600-800 MB/s each direction |

Reproduced three times independently across separate runs of this lab's training-storage characterization. Cross-run cache-hot average 12.9 sec; cache-cold average ~58 sec. The optimizer DCP write phase sustains at 1500-1600 MB/s — within 5% of the post-SLC TLC sustained rate from the [Spark NVMe FIO Baseline](../../data-prep/spark-nvme-fio-baseline/spark-nvme-fio-baseline.md), which is what a synthetic FIO probe predicts an ML checkpoint write should see past the SLC budget. Synthetic baseline matches actual training workload.

**Touch point 6 (cold-cache checkpoint restore).** A clean two-phase signature in iostat after dropping the page cache.

| Phase | Window | Read rate |
|---|---|---|
| DCP shard read | 22 sec | 600-700 MB/s |
| Optimizer DCP read | 28 sec | **1019-1153 MB/s sustained** |
| **Total** | **58 sec for ~50 GB** | **~880 MB/s effective; iostat peak 1153** |

The optimizer DCP rate is near the single-thread sequential read ceiling for this NVMe — the loader saturates the disk for that phase, which is the rare case where storage architecture matters more than the loader code path.

**Cadence cost — synchronous mode worked example.** With NeMo Automodel 26.02's default synchronous DCP+HF checkpoint pipeline:

| Cadence | Mid-training checkpoints | Total wall-clock | Throughput tax |
|---|---|---|---|
| `ckpt_every_steps=25` | 4 | ~13 min | 58% |
| `ckpt_every_steps=100` | 0 | ~6 min | ~22% |

Plus a sustained 10-25 step training-slowdown tail after each cache-cold synchronous checkpoint, while disk is idle — page cache pressure from the just-written 62 GB carries forward and slows the next 10-25 forward passes through UMA bandwidth contention. The c25-cadence run vs the c100-cadence run, training-only wall-clock (subtracting checkpoint blocks), was 1.49× slower with c25, and disk was idle for most of that delta. These specific tax numbers apply to synchronous checkpointing; an asynchronous checkpoint pipeline would change the timing of when the slowdown manifests, though the underlying page-cache pressure mechanism remains.

## Reproduce

A self-contained kit lives at [reproduce/](reproduce/). Four scripts (smoke / c25 / c100 / restore) plus an iostat-timeline analyzer and an optional anon-rss tracker. ~35 minutes of total wall-clock and ~450 GB free disk to walk all four scripts end-to-end. The kit's [README.md](reproduce/README.md) lists the environment requirements and [expected-output.md](reproduce/expected-output.md) carries the reference numbers above for sanity-checking on similar hardware.

## Bounds

DGX Spark UMA, single-node, ARM64 Ubuntu, NeMo Automodel 26.02 with bf16 Adam and the dual-format DCP+HF retention pipeline. **Measurements use synchronous checkpointing — the default for full-SFT in this build.** Asynchronous checkpointing would change the timing of the post-checkpoint training-slowdown tail; the 6× hot/cold consolidation pattern persists regardless because both modes need the read-back. The qualitative shape (touch-point map structure, three TP4 regimes, hot/cold consolidation ratio) generalizes to UMA platforms (Apple Silicon, AMD MI300A, Grace-class). Absolute numbers (47 GiB plateau, 58% vs 22% sync tax, 880 MB/s TP6 effective rate, 1153 MB/s peak) are platform-specific and depend on the UMA-vs-checkpoint-size ratio. Touch points 1+2 use a 16 MB dataset here that doesn't exercise them; characterizing them at meaningful scale is a separate experiment. Full bounds: [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).
