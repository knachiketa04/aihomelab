# Expected output — Lustre checkpoint storage reproduce kit

Reference numbers from this lab's canonical run (an 8B full-SFT checkpointing to a distributed
Lustre-on-ZFS filesystem on two UMA workstations). Numerical drift of ±10-15% across hardware
variations is normal; the **qualitative shape** is what reproduces, and is called out per
section. Platform constraints: [`scope-and-caveats.md`](../../../scope-and-caveats.md).

## Checkpoint storage is always client-bound (write or read)

The A/B spine. Each row below is one configuration; the client-bound conclusion holds in every
regime, with the storage substrate idle throughout.

| Pattern | Aggregate throughput | Mean s/ckpt (range) | OST0/OST1 split | Disk %util | Peak UMA | Bound by |
| --- | --- | --- | --- | --- | --- | --- |
| Write, 1 writer (single-node) | **0.78 GB/s** | 59 s (55-66) | ~50/50 | < 55% | ~66 GiB | client latency (sync-DIO per-completion) |
| Write, 2 writers (2-node concurrent) | **1.32 GB/s** = **1.69×** | 35 s (32-39) | **50.0/50.0** | ~42% | ~45-46 GiB/rank | client concurrency (on the 1.35 GB/s concurrent ceiling, disk still idle) |
| Read, 1 reader (restore) | **1.37 GB/s** | — | — | ~20% | — | client latency (pipelined → faster than write) |

Each checkpoint is ~46 GB DCP-sharded (model ~16 GB + optimizer ~30 GB). Single-node writes
4 × 46 GB = ~184 GB; the 2-node arm writes the same logical volume split ~50/50 across the two
OSTs (OST0 98.36 GB / OST1 98.38 GB measured on both `lctl` ends).

> **Second validation run (2026-06-01, same hardware class).** 1-writer **0.72 GB/s** / 64 s/ckpt,
> 2-writer **1.24 GB/s** / 37 s/ckpt = **1.72×**, peak UMA 66.16 / 45.75 GiB/rank, no EFAULT/panic.
> Absolutes ran ~6-8% under the reference but the **concurrency ratio (1.72×) and the client-bound
> shape held exactly.** Treat the absolute GB/s as a ±10-15% band; the ratio and the substrate-idle
> attribution are the invariant. Two caveats from that run: (1) disk `%util` peaked higher (57% / 71%)
> than the reference — expected, `%util` is the degenerate metric here, which is the whole point of the
> ZFS probe. (2) The 2-node `OST0/OST1` split capture needs `sudo lctl get_param` on **each** node; if
> the narrow sudoers doesn't whitelist `lctl`, enter the password on both node terminals, else the
> split shows only the node(s) where it ran (this run captured OST0 only). The aggregate GB/s and the
> attribution do **not** depend on the OST split.

**What to look for (qualitative shape that must hold):**

- **Client-bound in every regime.** The disk is < 55% busy on writes and ~20% busy on reads.
  The 2-writer aggregate is ~1.69× the single writer (not 2×), and the disk is still only ~42%
  busy at that "ceiling" — so the cap is writer concurrency, not the substrate.
- **The 1.69× lands on the independently-measured concurrent ceiling.** The `lustre-on-uma`
  fio characterization measured ~1.35 GB/s concurrent-write aggregate on this stack; the
  full-SFT checkpoint A/B reproduces it from a real training workload (1.32 GB/s).
- **Reads pipeline, so the restore is faster than the write (1.37 vs 0.78), but no more
  storage-bound** — the disk loafs at ~20%. Reads aren't storage-throughput-bound on UMA either.

### Why `iostat %util` alone is not enough (the attribution probe)

0.78 GB/s sits dead-center in this lab's OWN published file-backed-zpool substrate band
(0.5-0.8 GB/s). So from `%util` alone, "single DCP writer is the cap (client-bound)" and
"the .img-on-ext4 zpool substrate is the cap (storage-bound)" are observationally degenerate.
The `probe-checkpoint-io.sh` captures resolve it:

| Probe signal | Reference observation | Reads as |
| --- | --- | --- |
| Writer `python3`/`pt_*` per-thread CPU (`pidstat -t`) | off-CPU 70-95% during the write (5-39% busy, `%usr` ~1) | NOT CPU-bound, NOT client-CPU-serialized; latency-bound (waiting on sync-DIO completions) |
| ZFS `txgs` `ndirty` (dirty bytes/txg) | 5-194 MB across runs, well under the dirty cap (~1-5%) | ZFS not throttling → storage has headroom |
| ZFS `txgs` `stime` (sync duration) | 13-98 ms across runs | short syncs (≪ the 5 s txg timeout), no backpressure |
| `z_wr_iss` / `txg_sync` / `ptlrpcd` / `kiblnd` CPU | all < 3% | not ZFS-bound, not transport-bound |

The substrate-ceiling hypothesis is **rejected** by the idle ZFS. Lever is write concurrency,
not faster storage. If your reproduction shows a hot `z_wr_iss`/`txg_sync` with `ndirty` near
the cap and long `stime`, you are on a different (faster-client or slower-substrate) balance
point and the attribution flips — that's the whole reason the probe is in the kit.

## A note on the consolidation path (out of scope for this kit)

The checkpoint arms here use `save_consolidated:false`, so they ride full 4 MiB aligned RPCs and
never touch the path discussed below. The DCP→HF consolidation step (`save_consolidated:true`)
issues a page-unaligned buffered append (the safetensors header is `8 + len(JSON)` = 2053 bytes,
putting every tensor byte off the 4 KiB page grid). On Lustre 2.16.0+ with Hybrid-IO enabled,
that unaligned buffered write converts to the unaligned-DIO path and trips a known-open Lustre
client defect whose assertion signature matches **LU-18874**
(`osc_build_rpc() ASSERTION(sdio->csd_write_copied)`).

That defect is being reported through the upstream channel (lustre-discuss) rather than
reproduced in this public kit — a reliable host-crash procedure does not belong here. The two
client-side ways to avoid it on your own stack:

- `save_consolidated:false` (what this kit uses — the sharded write is aligned by construction), or
- `lctl set_param llite.<fsname>-*.hybrid_io=0`, which makes the identical unaligned write
  complete cleanly.

Unaligned-DIO shipped in Lustre 2.16.0, so the affected range is ≈ 2.16.0+. Re-check the ticket's
current status before relying on either path.

## How to compare your run

Per arm, run `analyze-checkpoint.py --capture-dir <dir> --writers <1|2>` and place the two rows
side by side:

- Within ±10-15% on absolute GB/s and s/ckpt: clean reproduction.
- The **ratio** (2-writer / 1-writer ≈ 1.69×) and the **disk-idle shape** (%util well under
  saturation in both arms) must hold exactly; those are the finding, not the absolute numbers.
- If the attribution probe shows a hot ZFS/transport thread instead of an off-CPU writer, your
  client/substrate balance differs from this lab's — report it; the conclusion "client-bound"
  is stack-specific and the probe is what makes it falsifiable.
