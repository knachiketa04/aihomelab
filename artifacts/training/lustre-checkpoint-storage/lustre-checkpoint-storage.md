# Checkpoint Storage on Lustre is Client-Bound, Not Substrate-Bound

**Date:** 2026-05-30 · **Nodes:** two UMA workstations (Grace-class GB10), RoCE pair · **Filesystem:** distributed Lustre-on-ZFS, one file-backed-zpool OST per node · **Model:** Qwen3-8B full SFT · **Container:** `nvcr.io/nvidia/nemo-automodel:26.02`

> Findings are measured on UMA workstations (Grace-class), two-node, ARM64 Linux, over a distributed Lustre-on-ZFS filesystem with a file-backed zpool per OST, using the software versions above. See [artifacts/scope-and-caveats.md](../../scope-and-caveats.md) for what bounds how this generalizes.

*Part of an end-to-end pipeline: this is the full-SFT checkpoint stage of the [LLM pipeline on a workstation](../../concepts/llm-pipeline-on-a-workstation/llm-pipeline-on-a-workstation.md) walk.*

## Findings

1. **Full-SFT checkpoint storage is client-bound in every regime: single write, concurrent write, and restore read.** A single sharded writer lands ~0.78 GB/s; two concurrent writers land 1.32 GB/s; a single-client restore read lands 1.37 GB/s. In all three, the local NVMe sits under 55% busy on writes and around 20% on reads. The lever for faster checkpoints on this class is writer concurrency and client latency, not a faster storage tier.
2. **`iostat %util` alone cannot tell client-bound from substrate-bound here, so don't trust it on a file-backed-zpool stack.** The single-writer rate (0.78 GB/s) sits inside this stack's own substrate band (roughly 0.5 to 0.8 GB/s), which makes "the client is the cap" and "the disk is the cap" look identical from `%util`. The ZFS transaction-group kstat (`ndirty` a few percent of the dirty cap, sync `stime` far under the txg timeout) plus per-thread CPU (the writer is off-CPU 70 to 95% of the write) are what adjudicate, and they say the substrate is idle.
3. **Two concurrent writers scale to 1.69x, not 2x, and the disk is still only about 42% busy at that ceiling.** The aggregate lands on the same ~1.35 GB/s concurrent-write number an independent fio characterization measured on this stack, so the cap is writer concurrency, not the substrate. Reads pipeline and so the restore is faster than the write (1.37 vs 0.78 GB/s), but it is no more storage-bound: the disk loafs at ~20%.

## Why this matters

When checkpoint cycles feel slow, the reflex is to reach for faster storage. On this class of stack that reflex is wrong, and proving it wrong needs more than the metric everyone reaches for first. `iostat %util` is degenerate when the workload's throughput happens to sit inside the substrate's own delivered band, which is exactly the regime a file-backed-zpool Lustre OST lives in. The way out is to read the layer that actually knows whether the storage is working: the ZFS txg kstat tells you if the pool is throttling (dirty-data pressure, long sync times) or idle, and per-thread CPU tells you whether the writer is burning a core or waiting on completions. Once you can see that the substrate is idle in every regime, the planning conclusion follows: size for client concurrency and per-completion latency, not for raw tier bandwidth. A storage practitioner saying "for this checkpoint workload on this hardware class, storage is not the knob" is calibration, not retreat, and the attribution probe is what makes the claim falsifiable rather than a hunch.

## Measured

**The A/B spine.** One row per configuration. The client-bound conclusion holds in every regime, with the substrate idle throughout. Each checkpoint is ~46 GB DCP-sharded (model ~16 GB plus optimizer ~30 GB).

| Pattern | Aggregate throughput | Mean s/ckpt (range) | Disk %util | Peak UMA | Bound by |
| --- | --- | --- | --- | --- | --- |
| Write, 1 writer (single-node) | **0.78 GB/s** | 59 s (55-66) | < 55% | ~66 GiB | client latency (per-completion) |
| Write, 2 writers (2-node concurrent) | **1.32 GB/s** = **1.69x** | 35 s (32-39) | ~42% | ~45-46 GiB/rank | writer concurrency (on the ~1.35 GB/s ceiling) |
| Read, 1 reader (restore) | **1.37 GB/s** | n/a | ~20% | n/a | client latency (pipelined, faster than write) |

**Why `%util` is not enough: the attribution probe.** The single-writer rate sits dead-center in this stack's own file-backed-zpool substrate band, so "client-bound" and "substrate-ceiling" are observationally identical from `%util` alone. These signals resolve it.

| Probe signal | Observation | Reads as |
| --- | --- | --- |
| `iostat %util` on the NVMe | 0.78 GB/s, disk < 55% busy | degenerate: inside the 0.5-0.8 GB/s substrate band |
| ZFS `txgs` `ndirty` (dirty bytes/txg) | ~1 to 5% of the dirty-data cap | ZFS not throttling, substrate has headroom |
| ZFS `txgs` `stime` (sync duration) | tens of ms, far under the 5 s txg timeout | short syncs, no backpressure |
| Writer per-thread CPU (`pidstat -t`) | off-CPU 70 to 95% during the write | latency-bound, not CPU-serialized |

The substrate-ceiling hypothesis is rejected by the idle ZFS. If a reproduction instead shows a hot `z_wr_iss`/`txg_sync` with `ndirty` near the cap and long `stime`, that is a different client-vs-substrate balance point and the attribution flips, which is the whole reason the probe is in the kit.

**Independent reproduction (2026-06-01, same hardware class).** A second controlled run landed single-writer 0.72 GB/s, two-writer 1.24 GB/s = **1.72x**, both client-bound by the same ZFS-idle attribution, with no failure under concurrency. Absolutes ran about 6 to 8% under the primary run; the concurrency ratio and the client-bound shape held. Treat the absolute GB/s as a plus-or-minus 10 to 15% band and the ratio plus the substrate-idle attribution as the invariant.

## Reproduce

A self-contained kit lives at [reproduce/](reproduce/). Two run scripts (single-node and two-node arms), the attribution probe, and an analyzer that emits the A/B row plus the ZFS-idle verdict. About 25 minutes per arm and roughly 184 GB of free shared-filesystem space per arm to walk it end to end. The kit's [README.md](reproduce/README.md) lists the environment requirements (a working distributed Lustre-on-ZFS filesystem, two UMA or Grace-class nodes, an RDMA fabric pair) and [expected-output.md](reproduce/expected-output.md) carries the reference numbers above for comparison on similar hardware.

The consolidated DCP-to-HF safetensors export path is out of scope for this kit: on Lustre clients with Hybrid-IO it trips a separate, known-open client defect, which is reported through the upstream channel rather than reproduced here. The checkpoint arms use the sharded write path (`save_consolidated:false`), which is safe by construction.

## Bounds

This is measured on UMA workstations (Grace-class), two-node, ARM64 Linux, over Lustre-on-ZFS with a file-backed zpool per OST and NeMo Automodel 26.02 writing DCP-sharded full-SFT checkpoints. The "client-bound in every regime" conclusion is stack-specific: it holds because the single-writer rate sits inside this particular file-backed-zpool substrate band. On a raw-partition zpool, a faster substrate, or a different client-to-storage balance, the attribution can shift, and the probe (ZFS txg kstat plus per-thread CPU) is exactly what tells you when it has. The qualitative shape (client-bound writes that scale sub-linearly with concurrency while the disk loafs, and a pipelined restore read that is faster but still not storage-bound) generalizes to UMA platforms with a similar file-backed parallel-filesystem substrate. Absolute numbers (0.78 / 1.32 / 1.37 GB/s, the 1.69x concurrency ratio, the ~46 GB checkpoint size) are platform-specific. Full bounds: [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).
