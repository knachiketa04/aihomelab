# Lustre checkpoint storage — reproduce kit

Reproduces one finding from full-parameter SFT of an 8B model checkpointing to a shared distributed filesystem (Lustre-on-ZFS) on two UMA workstations:

**Checkpoint storage on file-backed-zpool Lustre is always client-bound, write or read — the substrate sits idle in every regime.** A single DCP-sharded writer lands ~0.78 GB/s (mean 59 s per 46 GB checkpoint, range 55-66 s); two concurrent writers land 1.32 GB/s = **1.69×**, on the independently-fio-measured 1.35 GB/s concurrent ceiling; a single-client restore read lands 1.37 GB/s. Local NVMe is < 55% busy on writes and ~20% busy on reads in every case. Attribution is proven via ZFS transaction-group kstat (`/proc/spl/kstat/zfs/*/txgs`, `ndirty` ~1% of cap, no throttle) **plus** per-thread CPU (writer off-CPU 70-95%), **not** `iostat %util` alone — `%util` is degenerate here because 0.78 GB/s sits inside this lab's own file-backed-zpool substrate band.

Full numbers and the attribution-probe reference table in [`expected-output.md`](expected-output.md). Scope and platform constraints: [`scope-and-caveats.md`](../../../scope-and-caveats.md).

> **Note on the consolidation path.** This kit's checkpoint arms write `save_consolidated:false` DCP-sharded checkpoints, which ride full 4 MiB aligned RPCs and are safe by construction. The full-SFT DCP→HF consolidation path (`save_consolidated:true`) issues a page-unaligned buffered append that trips a separate, known-open Lustre client defect (assertion signature matches **LU-18874**) on Lustre 2.16.0+ with Hybrid-IO enabled. That defect is being reported through the upstream channel (lustre-discuss), not reproduced here — a reliable host-crash procedure does not belong in a public kit. The client-side mitigation, if you hit it on your own stack, is `lctl set_param llite.<fsname>-*.hybrid_io=0` (or `save_consolidated:false`, as this kit uses).

## Environment requirements

- **Two UMA workstations or Grace-class nodes** with an RDMA-capable fabric pair (RoCE / IB / equivalent) cross-connected. Discrete-VRAM systems will not reproduce the UMA memory-pressure numbers.
- A working **distributed Lustre-on-ZFS filesystem** mounted at a shared path on both hosts, with an OST on each host (file-backed zpool on local NVMe). Building that stack is out of scope for this kit; see the [`lustre-on-uma-workstations`](../../lustre-on-uma-workstations/reproduce/) kit.
- ≥ 250 GB free on the shared FS for the A/B checkpoints; ≥ 121 GiB unified memory per host.
- Docker + GPU-container runtime; `sysstat` (for `iostat`/`pidstat`) on both hosts; `sudo` on both hosts for `drop_caches` (see `DROP_CACHE_CMD`) and the `lctl get_param` OST byte-counter snapshots. For the 2-node arm, the OST split needs `sudo lctl` on **each** node — enter the password on both terminals, or whitelist `lctl get_param` in the sudoers; the aggregate throughput and ZFS attribution don't depend on it.
- Hugging Face token at `~/.huggingface_token` (mode 600), [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) gate accepted.

## What's in this kit

| File | Purpose | Runtime | Disk |
| --- | --- | --- | --- |
| `qwen3_8b_vegan_fullsft.yaml` | The full-SFT recipe both arms run. Container-internal paths: dataset+tokenizer at `/data`, checkpoints at `/checkpoints`. `save_consolidated:false`. | — | — |
| `run-checkpoint-singlenode.sh` | Single-writer arm. 100-step SFT, `ckpt_every=25` → 4 × 46 GB DCP-sharded writes, `save_consolidated:false`. Bracketed by the attribution probes. | ~25 min | ~184 GB |
| `run-checkpoint-2node.sh` | 2-node-concurrent arm. Same recipe under 2-node FSDP2 over RoCE. Run on both hosts; pass node rank. | ~25 min | ~92 GB/host |
| `probe-checkpoint-io.sh` | Attribution-probe helper: `iostat -x`, `lctl` OST byte counters, per-thread `pidstat` CPU, ZFS `txgs` kstat. Sourced by the run scripts; also standalone. | for run duration | n/a |
| `analyze-checkpoint.py` | Parse per-arm captures into the A/B table (writers, aggregate GB/s, mean s/ckpt, OST split, disk %util, peak UMA). | < 30 sec | n/a |
| `expected-output.md` | Reference numbers, tolerances, and the attribution-probe signatures. | — | — |

## Run order

1. **`run-checkpoint-singlenode.sh`** on host 1 (single-writer baseline). Self-contained; sources `probe-checkpoint-io.sh` around each checkpoint.
2. **`run-checkpoint-2node.sh 0`** on host 1, then **`run-checkpoint-2node.sh 1`** on host 2 within ~10 sec (concurrent arm).
3. **`analyze-checkpoint.py --capture-dir <dir>`** per arm to extract the comparison table.

Both arms write `save_consolidated:false` DCP-sharded checkpoints, which ride aligned RPCs and are safe by construction.

## Tunables (env vars honored by the scripts)

- `EXP_ROOT` — working dir on the shared FS. Default: `/mnt/lustre/lustre-checkpoint-storage-reproduce`.
- `LUSTRE_FSNAME` — Lustre fsname for the `lctl get_param obdfilter.<fsname>-OST*.stats` OST byte counters in the attribution probe. Default: `lustrefs`.
- `HF_TOKEN_FILE` — token path. Default: `~/.huggingface_token`.
- `CONTAINER` — container image. Default: `nvcr.io/nvidia/nemo-automodel:26.02`.
- `RECIPE_FILE` — the full-SFT recipe. Default: the `qwen3_8b_vegan_fullsft.yaml` shipped beside the scripts.
- `DATA_DIR` — the dataset+tokenizer dir mounted at `/data`. Must contain `train.jsonl`, `val.jsonl`, and the tokenizer dir `qwen3-tok-nothink/` (the recipe references `/data/train.jsonl`, `/data/val.jsonl`, `/data/qwen3-tok-nothink`). Default: `${EXP_ROOT}/data`.
- `DROP_CACHE_CMD` — page-cache drop for the cold-start state. Default: `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` (assumes full sudo). For a narrow NOPASSWD sudoers that whitelists only `sync`+`tee`, override to `sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null`.
- `HF_CACHE_DIR` — local HF cache. Default: `${HOME}/hf-cache`.
- `HOST1_QSFP_IP`, `HOST2_QSFP_IP` — RDMA-fabric IPs (2-node arm). Defaults: `169.254.188.115`, `169.254.10.122`.
- `HOST1_IFACE` — RDMA NIC interface. Default: `enp1s0f0np0`. `NCCL_IB_HCA` — RoCE HCA. Default: `rocep1s0f0`.
- `NVME_DEVICE` — local NVMe device name for `iostat`. Default: `nvme0n1`.
- `OST0_POOL`, `OST1_POOL` — ZFS pool names for the `txgs` kstat. Defaults: `ost0-pool`, `ost1-pool`.
- `HF_HUB_OFFLINE` — `0` first run (pulls model), `1` after the cache warms. Default: `0`.

## Cherry-picking individual scripts

- **`probe-checkpoint-io.sh`** — the client-vs-storage attribution probe (per-thread CPU + ZFS `txgs` + OST byte counters + `iostat`). Reusable around any checkpoint/IO workload on a Lustre-on-ZFS client. See its header `Standalone usage` block.
- **`analyze-checkpoint.py`** — parses any directory of this kit's per-arm captures into the A/B table.

The `run-checkpoint-*.sh` scripts are coupled to this kit's 100-step Qwen3-8B full-SFT workload and aren't directly reusable.

## Verification checklist

After all phases:

- **Single-writer:** mean ~59 s/ckpt (range 55-66), ~0.78 GB/s aggregate, OST split ~50/50, disk %util < 55%, peak UMA ~66 GiB. Attribution probe: writer `python3` off-CPU 70-95%, ZFS `ndirty` ~1% of cap, no `txgs` throttle → client-bound.
- **2-node concurrent:** mean ~35 s/ckpt, 1.32 GB/s aggregate = **1.69×** the single writer, OST split 50.0/50.0, peak UMA ~45-46 GiB/rank, RoCE active.
- **Restore read:** ~1.37 GB/s single-client, disk ~20% busy → still client-bound (reads pipeline → faster than writes, no more storage-bound).

Numerical drift of ±10-15% is normal hardware variation. The **qualitative shape** must hold: client-bound in every regime (write 1×, write 2×, read); the ~1.69× concurrency scaling that lands on the independently-measured concurrent ceiling.
