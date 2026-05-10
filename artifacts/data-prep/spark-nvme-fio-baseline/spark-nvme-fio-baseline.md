# Spark NVMe FIO Baseline

**Date:** 2026-05-03 · **Node:** spark01 · **Drive:** Samsung 9100 PRO OEM (MZALC4T0HBL1-00B07), Gen5 x4, 4 TB · **FIO:** 3.36

> **What we asked:** how close to spec can a single Gen5 NVMe actually get under FIO, and where are the gaps that matter for AI infrastructure? Measured on DGX Spark UMA, single-node, ARM64 Ubuntu, with the specific software versions above. See [artifacts/scope-and-caveats.md](../../scope-and-caveats.md) for what bounds how this generalizes.

## Findings

1. **SLC fall-off (6×) is the dominant performance gap for AI workloads writing more than the SLC cache** — larger than the loader gap (3.3×) and larger than the spec-to-burst gap (1.13×). The 22× total drop from spec to ML-effective decomposes into three distinct mechanisms; the architectural lever differs per mechanism, and the SLC gap is the one that drive choice or workload sizing fix, not loader optimization.
2. **Baseline your storage before building AI infrastructure.** A four-line FIO sweep tells you what your drive actually delivers under the patterns your training and serving workloads create. Specs answer a different question (peak burst); your AI workload sees the sustained number. The reproduce kit below is the minimum sweep that surfaces both numbers.
3. **Three FIO methodology bugs systematically inflate published SSD benchmarks** — thread-overlap cache amplification, thread-loop cache amplification, and `size`+`offset_increment` interaction. The reproduce kit avoids them; if a published benchmark reports multi-thread sequential read above the PCIe link's theoretical ceiling, it's likely hitting one of these.

## Why this matters

For any AI infrastructure storage tier — local NVMe, cloud object storage, distributed parallel filesystem — there's a spec number measured under best-case conditions and a sustained number your workload settles into over time. Both are real; they answer different questions. The mechanism that explains the gap is different on each tier (SLC cache fall-off here; throttle limits and noisy-neighbor effects on cloud; metadata round-trips and concurrency on distributed filesystems), but the work you have to do is the same: measure what your specific setup actually delivers under the I/O patterns your training and serving create, and size your plan against the sustained number, not the best-case spec. On this Gen5 NVMe, sustained writes past the SLC budget settle 6× below the spec burst rate — the number to plan around is ~1.85–2.0 GB/s, not 13.4 GB/s. Datasheets publish both, so the gap isn't hidden; the burst is just the more visible number and most people don't sit down and work out what the sustained number means for planning. Tweaking the loader (parallelism, queue depth, larger blocks) buys back at most 3× of the remaining gap to what the ML stack actually pulls; the SLC fall-off is baked into the drive's design, and only better drives or smaller writes get around it. For other storage tiers the gap shows up differently, but the same approach works: measure first, plan against sustained.

## Measured

**Three-gap decomposition (writes).** Spec → FIO burst ceiling → FIO 180 s sustained → ML-effective.

| Level | Rate | Source | Gap to next |
| --- | --- | --- | --- |
| Spec (write) | 13,400 MB/s | datasheet | 1.13× |
| FIO burst peak (in SLC) | 11,851 MB/s | probe (iodepth=8, runtime=15 s) | **6.0×** |
| FIO sustained (180 s avg) | 1,974 MB/s | canonical 16-thread seqwrite | 3.3× |
| ML-effective (full-phase) | ~600 MB/s | [NeMo full-SFT checkpoints](../../training/full-sft-storage-touchpoints/full-sft-storage-touchpoints.md) | — |

**Total spec-to-ML gap: 22×.** SLC fall-off contributes 6× (architectural); loader pattern contributes 3.3× (software). Reads are unaffected by SLC: 10,512 MB/s sustained = 71% of 14,800 spec.

**Canonical 8-job sweep (180 s each, sustained).**

| Job | Throughput / IOPS | p99 | % of spec |
| --- | --- | --- | --- |
| seqwrite 1t 1MB | 1,662 MB/s | — | 12% |
| seqwrite 16t 1MB | 1,974 MB/s | — | 14% |
| seqread 1t 1MB | 5,105 MB/s | — | 34% |
| seqread 16t 1MB | 10,512 MB/s | — | 71% |
| randread 4k QD64 | 1,624,088 IOPS | 399 µs | 73% |
| randwrite 4k QD64 | 481,338 IOPS | 1,729 µs | 18% |
| randread 4k QD1 (latency floor) | 4,852 IOPS | **457 µs** | — |
| mixed 70/30 1MB 8t | 2,601 R + 1,113 W = 3,714 MB/s | — | — |

**Cross-validation.** Three independent measurements converge on the published post-SLC TLC sustained rate (~1,856 MB/s — disclosed in the datasheet): probe at iodepth=8/runtime=60 → 1,875; canonical 16t/180 s → 1,974; canonical 1t/180 s → 1,662. All within ±10%. iostat 2-sec peaks: read 11,096 MB/s (within 6% of FIO 10,512 sustained); write 8,184 MB/s (limited by 2-sec sampling granularity, consistent with the SLC-burst-then-fall-off timeline).

**Latency floor at QD=1 random read** is 3–5× higher than typical Gen5 direct-attach (~70–100 µs in literature). Across two experiments it ranged 334–561 µs p99 — same drive, same firmware, system-state-sensitive. Spark Grace platform NUMA + PCIe traversal is the testable hypothesis. Workloads with many small random reads at low parallelism see this overhead; QD≥4 erases it.

## Reproduce

A self-contained kit lives at [reproduce/](reproduce/). 8 FIO job files (canonical home for all parameters), a thin bash orchestrator, and a `jq`-based analyzer that prints the Measured table above. ~30 min wall clock + ~2.2 TB free disk for the testfile.

The kit's [README.md](reproduce/README.md) documents the FIO parameter rationale, the three cache-amplification bugs to avoid, and how to find your own drive's spec to fill in the % column. [expected-output.md](reproduce/expected-output.md) carries the Spark numbers as reference for sanity-checking on similar hardware.

## Bounds

DGX Spark UMA, single Gen5 x4 consumer-grade TLC NVMe with pseudo-SLC cache (drive identified in header), ARM64 Ubuntu, FIO 3.36. The qualitative shape (small spec → burst gap, dominant burst → sustained SLC fall-off, smaller loader gap) generalizes to any TLC Gen5 NVMe with a pseudo-SLC cache — most consumer and OEM drives in this class. It does **not** apply to true SLC enterprise drives, where sustained ≈ burst with no fall-off; nor to TLC drives without a pseudo-SLC cache, where the headline IS the sustained number. The 457 µs p99 latency floor is platform-specific (Grace NUMA hypothesis); other Gen5 hosts will see different numbers. Absolute numbers don't generalize. Full bounds: [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).
