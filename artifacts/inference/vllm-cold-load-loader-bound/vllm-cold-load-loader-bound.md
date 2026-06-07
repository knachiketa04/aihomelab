# vLLM Cold Model Load on a UMA Workstation Is Loader-Bound, Not Storage-Bound

**Date:** 2026-05-31 · **Node:** UMA workstation (Grace-class, ARM64) · **Model:** base Qwen3-8B (15.27 GiB safetensors, 5 shards) · **Container:** `vllm-loaders` (from `nvcr.io/nvidia/vllm:26.02-py3`, vLLM 0.15.1) + RunAI Model Streamer + fastsafetensors

> Measured on a single UMA workstation (Grace-class node), ARM64, single Gen5 NVMe, no GPUDirect Storage, with the software versions above. See [artifacts/scope-and-caveats.md](../../scope-and-caveats.md) for what bounds how this generalizes.

*Part of an end-to-end pipeline: this is the serving / cold-load stage of the [LLM pipeline on a workstation](../../concepts/llm-pipeline-on-a-workstation/llm-pipeline-on-a-workstation.md) walk.*

## Findings

1. **The default vLLM loader is single-thread-CPU-bound, and swapping it cuts cold load 14 to 36x.** Cold-loading a base 8B model (15.27 GiB of safetensors) takes about 106 seconds with the stock `auto` loader, about 6 seconds with fastsafetensors, and about 3 seconds with RunAI Model Streamer, all serving byte-identical output. The storage tier never changes across the three; only the `--load-format` does.
2. **The bottleneck is one CPU core, not the disk, and "warm equals cold" proves it.** During the default load exactly one core sits pinned near 100% while the rest idle, and the NVMe loafs at a peak around 0.5 GB/s, a few percent of a Gen5 drive's bandwidth. Dropping the page cache before the load does not change the wall-clock, so the work is per-tensor materialization in Python, not disk reads.
3. **At serving time, none of the storage touch points is a constraint.** Under saturating eval traffic behind a constant system prompt, the prefix cache hits about 83% (a compute saver, since it cuts prefill), the KV cache peaks in the single-digit percent (over-provisioned for recipe-length output), and full-fidelity audit logging writes single-digit KB/s.
4. **Tier-irrelevance is a property of the slow loader, not a universal rule.** Once a streaming loader removes the CPU wall, it reads the same bytes near the local-NVMe ceiling (RunAI at about 9.2 GB/s on a roughly 10 GB/s Gen5 drive). The storage tier is invisible only because the default loader is slow enough to hide it; fix the loader and the tier re-enters as the next ceiling.

## Why this matters

A 90-second model load reads like a storage problem, and the reflex for a storage practitioner is to reach for a faster tier or a faster filesystem. The measurement says the opposite: the disk is busy a few percent of the time while one CPU core carries the whole load. The lever is at the application layer (the loader implementation), not the infrastructure layer (the storage tier), and the cheapest win is a one-line `--load-format` change rather than a hardware change. The general discipline holds beyond this case: measure whether the disk is actually saturated before sizing a faster tier, because a slow workload that pins one core is telling you it is compute-bound, not I/O-bound.

## Measured

**Loader cold-load.** Base 8B model, 15.27 GiB, local Gen5 NVMe, page cache dropped before each cold rep.

| Loader | Cold load | sec/GiB | vs default |
| --- | --- | --- | --- |
| `auto` (default safetensors) | ~106 s | ~7.0 | 1x |
| `fastsafetensors` | ~6.0 s | ~0.39 | 18x |
| `runai_streamer` (RunAI Model Streamer) | ~3.0 s | ~0.19 | 36x |

Independently reproduced through the validated kit (N=2): `auto` 88.5 s, `fastsafetensors` 6.4 s, `runai_streamer` 3.1 s. The default landed at the fast end of its range on that run, so the ratio read 14 to 28x; the ordering and the order-of-magnitude gap held.

**Mechanism signature (default load).** What proves it is the loader, not the disk.

| Signal | Default loader | What it means |
| --- | --- | --- |
| CPU | one core pinned ~100%, rest idle (sustained: 84/106 mpstat samples) | single-threaded per-tensor materialization |
| NVMe read | peak ~0.5 GB/s | a few percent of the Gen5 ceiling; the disk is loafing |
| Effective load rate | ~0.14 GiB/s | two orders of magnitude below the drive |
| Page cache | warm == cold | dropping the cache does not change load time |
| Streaming loaders | not pinned; read 5.6 to 9.2 GB/s | parallelize the same bytes |

A note on `%util`: it is a weak NVMe saturation proxy. A single in-flight I/O reads as "busy" at trivial bandwidth, so a high `%util` during the default load does not mean disk-bound. Judge by bandwidth against the drive ceiling instead.

**Serving touch points (eval traffic, constant system prompt).**

| Touch point | Measured | Read |
| --- | --- | --- |
| Prefix cache | ~83% hit | constant system prompt cuts prefill; a compute saver |
| KV cache | peak single-digit % | over-provisioned for recipe-length serving |
| Audit log | single-digit KB/s | trivially small |

## Reproduce

A self-contained kit lives at [reproduce/](reproduce/). It builds a derived image with both streaming loaders, runs the cold/warm protocol for each of the three loaders (about 10 minutes per loader, small logs), and fires a sample serving load to measure the three serving touch points. The kit's [README.md](reproduce/README.md) lists environment requirements and [expected-output.md](reproduce/expected-output.md) carries the reference numbers and tolerances. Validated end-to-end on a UMA workstation on 2026-06-01.

## Bounds

UMA workstation (Grace-class node), ARM64, single Gen5 NVMe, no GPUDirect Storage, safetensors weights loaded through vLLM. The 14 to 36x ratio assumes the load is CPU-bound on this hardware class; on a box with a faster single-thread CPU or much slower storage the gap narrows. The tier-irrelevance is loader-dependent: a streaming loader reads near the local-NVMe ceiling (about 9.2 GB/s here), so a slower source tier such as a networked filesystem at a lower ceiling would throttle the fast loaders and make the tier matter again, where it stays invisible to the default loader. That cross-tier consequence follows from this lab's measured [Lustre ceilings](../../training/lustre-on-uma-workstations/lustre-on-uma-workstations.md) but was not run here. Both streaming loaders run CPU-buffered on this box because there is no GPUDirect Storage; on a discrete-GPU server with GDS, fastsafetensors has a faster path that this hardware cannot exercise. Full bounds: [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).
