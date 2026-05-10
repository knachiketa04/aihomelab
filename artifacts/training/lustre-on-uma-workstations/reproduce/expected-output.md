# Expected output — Lustre on UMA workstations

Reference numbers from the canonical run. **Numerical drift of ±10–15% is normal across hardware variations**; qualitative shape (the 5×/32×/400× knob-trio lifts; cross-node being slower than loopback by an architectural margin; per-client symmetry; concurrency reaching 60–85% of loopback ceiling) is what must reproduce.

Reference hardware class: 2× UMA workstations, 121 GiB unified memory each, single Gen5 NVMe (~5–7 GB/s class), RoCE QSFP fabric (~109 Gb/sec sustained, ~2 µs RTT), Linux ARM64 kernel 6.x.

**Pillar 1 references are SUSTAINED (post-SLC), not burst.** A 180s × 256 GiB working set crosses the SLC cache (~442 GB on Samsung 9100 PRO), so the headline reference 0.5–0.8 GB/s for 1 MiB writes is sustained-TLC-bound — what you actually get for any workload that writes more than the SLC cache. Earlier transient observations of 2.20 GB/s in this lab's history were burst-SLC-aided and are NOT representative of sustained operation.

Validated baselines (this lab, post-180s sustained):

- ext4-direct (no Lustre, no ZFS, sustained 256 GiB): **1.8–2.2 GB/s** (n=2 in the canonical run) — the NVMe sustained TLC ceiling
- Pillar 1 through Lustre+ZFS file-backed-zpool: **0.5–0.8 GB/s** = 25–40% of NVMe sustained — Lustre+ZFS file-backed substrate overhead in normal range
- Burst (≤30s, ≤8 GiB total) ext4-direct: ~11 GB/s — the SLC ceiling, irrelevant for sustained workloads

Reads have a different state dependency: Pillar 1 reads at 11 GB/s require ext4 page cache warm (last full write recent). Cold-cache reads (page cache aged or after `drop_caches`) land 5–7 GB/s — true cold-NVMe-bound for the file-vdev path. Both are valid; the kit reports whichever state the prior test left you in.

**Pillar 2 + Pillar 3 are network-bound, not NVMe-bound** — their reference numbers reproduce regardless of local NVMe state. Use them as the primary validation signal; treat Pillar 1 numbers as the sustained-storage characterization, not a "fast" baseline to recover.

## Setup verification (no measurement, just shape)

| After step | Where | Expected |
|---|---|---|
| `setup-zfs-build.sh` | both hosts | `zpool version` reports 2.4.1; `lsmod` shows `zfs` + `spl` |
| `setup-lustre-build.sh` | both hosts | `lctl --version` reports `2.17.52_125_g805cece` (or whichever `LUSTRE_COMMIT` you pinned); `lsmod` shows ~28 Lustre modules |
| `setup-lnet.sh` | both hosts | `lnetctl net show` lists `o2ib` net with the host's `*@o2ib` NID |
| Cross-node ping | from either host | `lctl ping <peer>@o2ib0` returns peer's NID list |
| `setup-host1-osts.sh` | host 1 | `lfs osts` shows `OST0000 ACTIVE`; `lfs df -h` reports ~573 GiB |
| `setup-host2-ost.sh` | host 2 | `lfs osts` shows `OST0000 ACTIVE` AND `OST0001 ACTIVE`; `lfs df` reports ~1.1 TiB total |
| `setup-runtime-tunables.sh` | both hosts | `lctl get_param osc.*-OST*.max_rpcs_in_flight` returns 32; `obdfilter.*-OST*.brw_size` returns 4 |

## Pillar 1 — single-node tuning (host 1, loopback to OST0000)

The knob-trio finding. Files pinned to OST0000 via `lfs setstripe -c 1 -i 0`; LNet routes locally over `0@lo`.

| Test | Reference | Pass threshold |
|---|---|---|
| 1 MiB seq write | **2.20 GB/s** | ≥ 2.0 GB/s |
| 1 MiB seq read (warm, ext4 page cache active) | **11.0 GB/s** | ≥ 9.0 GB/s |
| 64 KiB seq write | **2.03 GB/s** | ≥ 1.7 GB/s |
| 4 KiB random read IOPS each direction | **~187K** (749 MB/s) | ≥ 150K |
| 4 KiB random write IOPS each direction | **~187K** (749 MB/s) | ≥ 150K |

If write-1m falls below ~0.5 GB/s, the knob trio didn't apply — verify `zfs get primarycache,atime ost0-pool/ost0` shows `metadata` and `off`.

**Default-config baseline (for the lift comparison if you re-run before applying the trio):**

| Test | Default | Tuned lift |
|---|---|---|
| 1 MiB seq write | ~0.44 GB/s | **5×** |
| 64 KiB seq write | ~0.064 GB/s | **32×** |
| 4 KiB random IOPS | ~470 each | **400×** |

The 32× and 400× lifts are the load-bearing finding. `primarycache=metadata` is the single dominant knob; without it, default ARC at 8 GiB cap thrashes on the 256 GiB working set.

## Pillar 2 — cross-node single-OST (host 2 client → OST0000 over o2ib)

Same battery, same OST, but accessed over RoCE from the other host. Pillar 2 numbers being **architecturally slower** than Pillar 1 is the finding.

| Test | Reference | Pass threshold (qualitative) |
|---|---|---|
| 1 MiB seq write | **0.49 GB/s** | 0.4–0.6 GB/s |
| 1 MiB seq read | **2.28 GB/s** | 2.0–2.5 GB/s |
| 64 KiB seq write | **0.065 GB/s** | 0.05–0.08 GB/s |
| 4 KiB random | **1.55 MB/s each** | < 5 MB/s |

The cross-node-vs-loopback gap (~5× on writes, ~5× on reads) is **not** a misconfiguration — it's the cost of Lustre's deeper RPC stack (OSC → ko2iblnd → osd-zfs → return path) vs the loopback baseline. Bandwidth-delay product analysis rules out pipeline depth: RoCE 13.6 GB/s × 2 µs RTT = 27 KB BDP, default 8-RPC × 1 MiB pipeline already 295× over.

For a same-hardware reference point: NFSoRDMA on identical fabric hits ~13.6 GB/s cached reads (~6× faster than Pillar 2 here). NFS's read path is shallower; this is architectural.

## Pillar 3 — distributed concurrent (both clients, 2-way striped)

The headline configuration. Both clients run identical batteries simultaneously; files stripe across both OSTs.

### Per-client (must be symmetric within ~5%)

| Test | host 1 | host 2 |
|---|---|---|
| 1 MiB seq write | **645 MiB/s** | **641 MiB/s** |
| 1 MiB seq read | **4447 MiB/s** | **4440 MiB/s** |
| 64 KiB seq write | **83.1 MiB/s** | **83.4 MiB/s** |
| 4 KiB random each direction | **2.47 MB/s** | **2.43 MB/s** |

Per-client symmetry is a cluster-health signal for the **same workload phase**. >5% asymmetry on writes suggests one host has a different config (tunables not applied, different ARC cap, different stripe layout, or asymmetric network leg).

**Caveat for reads**: per-client read asymmetry can drift to 2× or more if the two hosts' batteries desync — once one host finishes its writes faster, its subsequent read phase runs alongside the other host's still-running write phase, which biases throughput. The aggregate (sum across both clients) over the full test window remains valid for capacity claims; it's just the per-client snapshot that can mislead. To get clean per-client reads, run the read phase as its own concurrent test with a barrier between phases — out of scope for this kit.

### Aggregate (sum across both clients) — the headline numbers

| Test | Reference aggregate | Pass threshold | Vs Pillar 1 ceiling |
|---|---|---|---|
| 1 MiB seq write | **1.35 GB/s** | ≥ 1.2 GB/s | 60% |
| 1 MiB seq read | **9.32 GB/s** | ≥ 8.5 GB/s | 85% |
| 64 KiB seq write | **167 MiB/s** | ≥ 140 MiB/s | 8% |
| 4 KiB random each direction | **4.9 MB/s** | ≥ 4.0 MB/s | < 1% |

Reads come within 15% of single-node loopback ceiling — distributed Lustre is competitive here. Writes are bottlenecked by the network leg (each client writes half its bytes cross-node). Small-block and random aggregate stays low — small-block is fsync-rate-bound regardless of concurrency.

## Cross-pillar shape (the predictive heuristic)

| Pattern | Pillar 1 (loopback) | Pillar 2 (cross-node) | Pillar 3 (concurrent) |
|---|---|---|---|
| 1 MiB seq write | 2.20 GB/s | 0.49 GB/s | **1.35 GB/s** |
| 1 MiB seq read | 11.0 GB/s | 2.28 GB/s | **9.32 GB/s** |

The shape: **single-client cross-node systematically undersells distributed Lustre by ~2×**. Pillar 3's win condition is concurrent multi-client access (the realistic FSDP / multi-rank training pattern), not single-client benchmarks.

## Common reproduction failures

| Symptom | Likely cause | Fix |
|---|---|---|
| Pillar 1 write < 0.5 GB/s | Knob trio not applied | Re-run `setup-host1-osts.sh`; confirm `zfs get primarycache,atime` |
| Pillar 3 aggregate writes < 0.7 GB/s | Runtime tunables reset on remount | Re-run `setup-runtime-tunables.sh` (must use `set_param -P` for persistence) |
| Per-client asymmetry > 10% | Different config on each host | Diff `lctl get_param osc.*` and `zfs get all` between hosts |
| Pillar 2 write 0.1–0.2 GB/s (way low) | OSC `max_rpcs_in_flight` still at default 8 | `setup-runtime-tunables.sh` |
| Pillar 1 read drops 11 → ~3 GB/s on second run | ext4 page cache evicted between runs (working set > free RAM) | Expected — first run is warm, subsequent runs are cold-NVMe-bound until cache rebuilds |

## Tested-but-excluded knobs (for reference)

These were measured during the canonical run and **deliberately not included** in the kit:

| Knob | Effect measured | Why not in kit |
|---|---|---|
| `zfs set direct=always` on OST datasets | -5 to -8% regression | Slight regression on file-backed-zpool substrate |
| `osc.*.max_pages_per_rpc=4096` (16 MiB RPCs) | rejected at runtime | Build-time MTU limit caps to 1024 (4 MiB); would need rebuild |
| Multi-rail LNet on second QSFP NIC | not measured | Blocked by upstream `lnetctl` ARM64 stack-smash bug |
| `osc.*.checksums=0` | included in trio (small contribution) | Accept as part of `setup-runtime-tunables.sh` |
