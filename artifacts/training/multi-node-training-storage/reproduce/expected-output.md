# Expected output — multi-node training storage reproduce kit

Reference numbers from this lab's measurements. Use these to compare your reproduction against the canonical run. Numerical drift of ±10-15% across hardware variations is normal; **qualitative shape** (the 7× single-node spread, disappearance of cold-cache penalty multi-node, ~5× per-checkpoint tax of NFSoRDMA vs local) is what reproduces.

## Phase 0 — NFSoRDMA setup

Verification, not measurement. After running `setup-nfsordma-server.sh` on host 1 and `setup-nfsordma-client.sh` on host 2:

| Check | Where | Expected output |
| --- | --- | --- |
| NFS server portlist | host 1: `cat /proc/fs/nfsd/portlist` | contains `rdma 20049` AND `tcp 2049` |
| NFS client mount transport | host 2: `mount \| grep $NFS_MOUNT_PATH` | contains `proto=rdma,port=20049` |
| Client-side smoke `dd` | host 2: `dd ... 1 GiB ... fdatasync` | completes in <5 sec; no kernel errors in `dmesg` |

If the portlist shows only `tcp 2049`, the `[nfsd] rdma=y` edit didn't take. The fallback (non-persistent across restarts) is `echo "rdma 20049" | sudo tee /proc/fs/nfsd/portlist` and document the version skew.

If the client mount shows `proto=tcp` instead of `proto=rdma`, the kernel silently fell back. Stop and debug — do NOT proceed with training against a TCP-fallback mount.

## Single-node baseline (`run-single-node.sh`)

The cold-cache penalty baseline. Single host, no FSDP-2 sharding, all checkpoint memory pressure on one node.

| Metric | Reference value |
| --- | --- |
| Wall-clock | ~17 min |
| Throughput | ~15.6 steps/min |
| Memory plateau (anon-rss) | ~61.5 GiB |
| step_99 consolidation (cache-hot) | ~11 sec |
| step_199 consolidation (cache-cold) | 50-80 sec |
| step_249 consolidation | 30-50 sec |
| Hot/cold ratio | ~7× |
| Total checkpoint sync block (3 ckpts) | ~120 sec |
| Disk after run | ~190 GB on host 1 (3 ckpts × ~62 GB) |

**What to look for**: the ~7× spread between step_99 (cache-hot) and step_199 (cache-cold). On UMA the workload pins the cache so DCP shards get evicted before consolidation reads them; mid-training checkpoints incur a read-back-from-NVMe + sustained training-slowdown tail (~20 steps at 178-225 tps after the cold checkpoint, vs the steady-state 500-560 tps).

## Multi-node + post-training rsync (`run-multinode-rsync-host{1,2}.sh` + `sync-after-training.sh`)

FSDP-2 sharding halves per-node memory pressure → DCP shards stay cached → cold-cache penalty disappears. Trade-off: bytes are split across nodes during training, so a separate sync layer is needed afterward.

| Metric | Reference value |
| --- | --- |
| Training wall-clock | ~30 min |
| Aggregate throughput | ~595 tps (~297 per GPU) |
| Memory plateau per rank | ~35.5 GiB |
| step_99 sync block | ~4.5 sec |
| step_199 sync block | ~4.8 sec |
| step_249 sync block | ~4.9 sec |
| Hot/cold ratio | ~1.06× (cold-cache penalty eliminated) |
| Total checkpoint sync block (3 ckpts) | ~14 sec |
| Disk after training | ~95 GB per host (rank-0 view: 27 GB × 3 + singletons; rank-1 view: 32 GB × 3) |
| Rsync wall-clock (clean) | ~5.7 min |
| Rsync wall-clock (race-prone bidirectional) | ~20 min |
| **Total cost** = training + sync | **~36-50 min** depending on rsync path |

**What to look for**: the **16.6× speedup at step_199** compared to single-node (4.76 sec vs 78.93 sec). Mechanism is per-node memory pressure halving — each rank now writes ~25 GB per ckpt instead of ~47 GB, well within 121 GiB UMA's working budget. The cold-cache regime structurally doesn't apply.

The kit's `sync-after-training.sh` runs `pull && push` from a single shell on host 1 → race is structurally impossible. Reference rsync wall-clock should land near the clean ~5.7 min, not the race-inflated 20 min.

## Multi-node + NFSoRDMA shared FS (`run-multinode-nfsordma-host{1,2}.sh`)

The headline configuration. Both ranks see one shared checkpoint dir via NFSoRDMA. All bytes physically land on host 1's NVMe; host 2's NVMe accumulates 0 bytes.

| Metric | Reference value |
| --- | --- |
| Training wall-clock | ~28 min |
| Aggregate throughput | ~600 tps (~300 per GPU) |
| Memory plateau per rank | ~35.5 GiB |
| step_99 full sync block (DCP write + barrier + consolidation) | ~21 sec |
| step_199 full sync block | ~24 sec |
| step_249 full sync block | ~23 sec |
| Rank 0 `Done consolidating` solo time per ckpt | ~5-7 sec (rank-parallel: 2 of 5 chunks) |
| Hot/cold ratio (consolidation phase) | ~1.13× (fsync-bound, not cache-bound) |
| Total full-sync-block (3 ckpts) | ~70 sec |
| Disk delta on host 1 | +190 GB (NFS server holds all 3 training checkpoints) |
| Disk delta on host 2 | 0 GB (true centralization) |
| Additional disk after `run-cold-restore-nfsordma-host{1,2}.sh` | +50 GB on host 1 (NeMo writes a step_260 end-of-training checkpoint regardless of `ckpt_every`) |
| Post-training sync overhead | 0 (shared FS handles cross-node coordination inline) |
| **Total cost** | **~28 min** ← lowest of the three configurations |

**What to look for**: per-checkpoint cost is ~5× the local-NVMe-with-rsync configuration (22 sec vs 4.5 sec) but it's **flat** — no hot/cold spread because fsync time doesn't depend on page cache state. The ~5× per-checkpoint tax is more than offset by eliminating the 5.7-20 min post-training rsync.

Verify on host 2's iostat: ~zero local NVMe writes during checkpoint phases. Rank 1's DCP shard goes over NFSoRDMA, not to local disk.

## Three-way comparison

The artifact-grade table. Pulled together from the three phases above.

| Metric | Single-node | Multi-node + rsync | Multi-node + NFSoRDMA |
| --- | --- | --- | --- |
| Wall-clock | 17 min | 30 min + sync | 28 min |
| Throughput | 15.6 steps/min | 595 tps aggregate | ~600 tps aggregate |
| Memory plateau per rank | 61.5 GiB | 35.5 GiB | 35.5 GiB |
| Per-ckpt block (hot) | 11 sec | 4.5 sec | 21 sec |
| Per-ckpt block (cold) | 79 sec | 4.8 sec | 24 sec |
| Hot/cold variance | 7× | 1.06× | 1.13× |
| Total ckpt block (3) | ~120 sec | ~14 sec | ~70 sec |
| Post-training sync overhead | 0 (single-node only) | 5.7-20 min | 0 |
| **Total TP5 cost** | **~120 sec (single-node)** | **~370-1320 sec total** | **~70 sec total** ← lowest |
| Mutually recoverable mid-training? | n/a | NO (rank 1 lacks singletons) | YES (shared FS) |
| Ckpt physical location | host 1 only | both hosts (after rsync) | host 1 only (NFS server) |
| Operational complexity | trivial | bidirectional rsync race-prone | trivial (shared FS handles sync) |

The predictive heuristic the artifact rests on: **shared FS pays a 5× per-checkpoint tax to eliminate the sync layer entirely.** Whether that's a win depends on the ratio of training cost to sync cost — at 250 steps with 3 checkpoints it's a clean win for shared FS; at smaller cadences or shorter runs the math could flip.

## Cold-cache restore over NFSoRDMA (`run-cold-restore-nfsordma-host{1,2}.sh`)

The TP6 headline. Restores from the prior NFSoRDMA-saved checkpoint at step_249 under cold cache; runs ~10 post-restore steps to confirm steady state.

| Metric | Reference value |
| --- | --- |
| Restore wall-clock | ~37 sec |
| Restored checkpoint volume (cluster-wide) | ~50 GB |
| Effective restore bandwidth | ~1.4 GB/s |
| NFS-server NVMe peak read | ~1.7 GB/s |
| NFS-server NVMe sustained read | ~1.4 GB/s |
| Host 2 local NVMe reads during the TP6 *checkpoint-restore* window | ~0 (NFSoRDMA carries the remote rank's reads) |
| Host 2 local NVMe reads during the *whole script* | ~16-18 GB (HF cache reads at TP3/TP4 model init; before the TP6 window) |
| Memory plateau at first post-restore step | 35.5 GiB (matches the prior multi-node training) |
| First post-restore step tps | ~268 (warmup) |
| Steady-state post-restore tps | 540-580 |

**What to look for**: ~37 sec restore wall-clock is **~1.6× faster** than equivalent local-NVMe NeMo restore on the same hardware (~880 MB/s effective at 8B). NeMo's loader extracts ~30% of fio NFSoRDMA cold-NVMe ceiling (4.6 GB/s) → ~3.3× loader-pattern tax — same order of magnitude as on local NVMe. Network FS doesn't penalize the loader pattern further; absolute throughput wins because the underlying ceiling is higher.

Verify on host 2's iostat: ~zero local NVMe reads in the restore window. Rank 1 reads its DCP shard via NFSoRDMA from host 1, not from local disk. This is the load-bearing observation that distinguishes "shared FS" from "two copies of the data."

## Side observations worth knowing

These aren't on the headline path but the kit may surface them; they appeared in this lab's measurements.

- **NFS-over-TCP writes 13% faster than NFS-over-RDMA writes for sync-export 1MiB blocks**, on the same fabric. Counterintuitive but real: in the sync-export regime, per-op fsync rate dominates throughput, and per-op transport overhead is small but measurable at fsync-bound rates. RDMA's wins concentrate in NFS reads (5× over TCP, measured) and non-NFS workloads (NCCL, raw-buffer transfers). **Do not generalize to "RDMA is slower for writes"** — strip any one factor (use `async` export, larger blocks, mature NFS-over-RDMA stack) and RDMA wins.
- **`sync` export option enforces fsync semantics on the entire exported tree, not just NFS-ingress traffic.** Local writers to the same tree (e.g., rank 0 in the NFSoRDMA configuration) inherit the slowdown. Mechanism is well-known NFS behavior; magnitude in this lab was rough 2-3× compared to non-export-tree local writes (cross-run comparison; not a clean isolation test).
- **NFSoRDMA reads sustain 13.5 GB/s through working sets up to ~1.06× RAM** — kernel readahead is heroic on sequential patterns. Cold-NVMe-bound performance (4.6 GB/s) only emerges at working sets ≥ ~4× RAM with sustained runtime ≥ 180 sec.

## How to compare your run

For each phase, run `analyze-iostat.sh` and `analyze-checkpoint-events.sh` (or pass a single log file as `$1`). Compare your numbers to the reference values above:

- Within ±10-15%: clean reproduction.
- Outside ±15% on a single metric: hardware drift or instrumentation noise; check that the qualitative shape still holds.
- Qualitative shape broken (e.g., no hot/cold spread on single-node, or hot/cold spread present on multi-node): investigate. Either the hardware fundamentally differs (discrete-VRAM platform; smaller memory pool) or the workload changed.

If the kit's headline-check greps don't surface the consolidation lines, NeMo's log format has shifted from this lab's measured version (`nvcr.io/nvidia/nemo-automodel:26.02`). Adjust the grep patterns in the kit's run scripts to match your container's actual output.
