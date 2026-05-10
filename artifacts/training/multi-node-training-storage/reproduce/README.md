# Multi-node training storage — reproduce kit

Reproduces the headline findings from a controlled comparison of multi-node distributed training storage on two UMA hosts:

1. Multi-node FSDP-2 sharded training **eliminates the single-node UMA cold-cache checkpoint-consolidation penalty** (7× hot/cold spread → 1.1×).
2. **Shared FS via NFSoRDMA wins on total cost** vs. multi-node + post-training rsync, despite a 5× per-checkpoint tax during training.
3. **NeMo restore over NFSoRDMA: ~1.4 GB/s effective** under cold cache (~1.6× faster than equivalent local-NVMe NeMo restore).

Full numbers in [`expected-output.md`](expected-output.md).

## Environment requirements

- **Two UMA hosts** with a fabric pair capable of RDMA (RoCE / IB / equivalent). UMA workstations or Grace-class nodes; discrete-VRAM systems will not reproduce the memory-pressure findings — see [`scope-and-caveats.md`](../../scope-and-caveats.md).
- ≥ 121 GiB unified memory per host.
- ≥ 800 GB free local NVMe on the NFS-server-designated host; ≥ 200 GB on the other.
- Docker + GPU-container runtime (NVIDIA Container Toolkit or equivalent).
- Hugging Face token at `~/.huggingface_token` (mode 600), with [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) gate accepted.
- `nfs-kernel-server`, `nfs-common`, `rdma-core`, `sysstat` on both hosts.
- `sudo` on both hosts (setup edits `/etc/exports` + `/etc/nfs.conf`; restore does `drop_caches`).
- Kernel that ships `rpcrdma` in the base package; otherwise install it first, e.g. on Ubuntu: `sudo apt install -y linux-modules-extra-$(uname -r) && sudo modprobe rpcrdma`.

## What's in this kit

| File | Purpose | Runtime | Disk |
| --- | --- | --- | --- |
| `setup-nfsordma-server.sh` | One-time server-side NFSoRDMA setup: export, `[nfsd] rdma=y`, portlist verification. Run on host 1. | ~10 min | n/a |
| `setup-nfsordma-client.sh` | One-time client-side mount: `proto=rdma` verification + 1 GiB dd smoke. Run on host 2 after server-side completes. | ~5 min | n/a |
| `run-single-node.sh` | 250-step SFT on host 1, `ckpt_every=100`. Cold-cache penalty baseline. | ~17 min | ~190 GB |
| `run-multinode-rsync-host1.sh` | 250-step SFT, rank 0 (training only). Run on host 1. | ~30 min | ~190 GB on host 1 |
| `run-multinode-rsync-host2.sh` | 250-step SFT, rank 1 (training only). Run on host 2 within ~10 sec of host 1. | ~30 min | ~190 GB on host 2 |
| `sync-after-training.sh` | Post-training bidirectional rsync — pull-then-push from a single shell on host 1 (race-free). Run on host 1 only, after both training scripts finish. | ~5-20 min | mirrors state cross-node |
| `run-multinode-nfsordma-host1.sh` | 250-step SFT, rank 0 with checkpoints on host 1's NFS-export tree. Run on host 1. | ~28 min | ~190 GB on host 1 |
| `run-multinode-nfsordma-host2.sh` | 250-step SFT, rank 1 writing through the NFSoRDMA mount. Run on host 2 within ~10 sec of host 1. | ~28 min | ~0 GB local (all writes flow over NFSoRDMA) |
| `run-cold-restore-nfsordma-host1.sh` | TP6 cold-cache restore, rank 0. Auto-resumes from LATEST + runs 10 post-restore steps. Run on host 1. | ~5 min | none |
| `run-cold-restore-nfsordma-host2.sh` | TP6 cold-cache restore, rank 1. Run on host 2 within ~10 sec of host 1. | ~5 min | none |
| `analyze-iostat.sh` | Merge per-host iostat logs, surface peak/avg read/write per phase. | <30 sec | n/a |
| `analyze-checkpoint-events.sh` | Extract per-checkpoint event timings from training logs. | <30 sec | n/a |
| `expected-output.md` | Reference numbers per phase. | — | — |

## Run order

1. `setup-nfsordma-server.sh` on host 1, then `setup-nfsordma-client.sh` on host 2. Verify `cat /proc/fs/nfsd/portlist` shows both `rdma 20049` and `tcp 2049` on host 1; `mount | grep $MOUNT` shows `proto=rdma` on host 2.
2. `run-single-node.sh` — confirms workload fits a single host; produces the cold-cache-penalty baseline.
3. `run-multinode-rsync-host{1,2}.sh` (one per host, started within ~10 sec of each other) → wait for both to print END timestamps → `sync-after-training.sh` on host 1 only. Establishes the rank-parallel + memory-pressure-halving win + the cross-node sync layer's wall-clock cost.
4. `run-multinode-nfsordma-host{1,2}.sh` (one per host, started within ~10 sec of each other). Headline configuration. No separate sync step — shared FS handles cross-node coordination inline.
5. `run-cold-restore-nfsordma-host{1,2}.sh` (one per host, started within ~10 sec of each other) — TP6 measurement against the prior nfsordma run's checkpoint.
6. `analyze-iostat.sh` and `analyze-checkpoint-events.sh` per phase to extract per-phase numbers.

## Tunables (env vars honored by all scripts)

- `EXP_ROOT` — where checkpoints/logs land. Default: `/home/sparks/multi-node-storage-reproduce`.
- `HF_TOKEN_FILE` — token path. Default: `~/.huggingface_token`.
- `CONTAINER` — container image. Default: `nvcr.io/nvidia/nemo-automodel:26.02`.
- `HOST1_QSFP_IP`, `HOST2_QSFP_IP` — RDMA-fabric IPs. Defaults: `169.254.188.115`, `169.254.10.122`.
- `HOST1_IFACE` — RDMA NIC interface name. Default: `enp1s0f0np0`.
- `NCCL_IB_HCA` — RoCE HCA name for NCCL. Default: `rocep1s0f0`.
- `HOST2_USER`, `HOST2_SSH_TARGET` — used by `sync-after-training.sh`. Defaults: `$USER`, `${HOST2_USER}@${HOST2_QSFP_IP}`. Passwordless ssh from host 1 to host 2 must be configured.
- `NFS_EXPORT_PATH` — export dir on host 1. Default: `/srv/nfs/multi-node-storage`.
- `NFS_MOUNT_PATH` — mount dir on host 2. Default: `/mnt/multi-node-storage`.
- `NVME_DEVICE` — local NVMe device name for `iostat` instrumentation. Default: `nvme0n1`.
- `HF_HUB_OFFLINE` — set to `1` after the first run pre-warms the cache to skip pulls. Default: `0`.

## Cherry-picking individual scripts

If you only want one piece of this kit (e.g., the NFSoRDMA setup pattern, not the full training comparison), four scripts work standalone with env-var overrides — see each script's header for a `Standalone usage` block:

- **`setup-nfsordma-server.sh` + `setup-nfsordma-client.sh`** — general-purpose NFSoRDMA stack setup. Idempotent. Works for any NFS-over-RDMA use case independent of training.
- **`analyze-iostat.sh`** — parses any `iostat -t -dxm 2 <device>` log. Single-log mode + scan-all mode.
- **`analyze-checkpoint-events.sh`** — parses any NeMo Automodel training log to surface checkpoint events.

The `run-*.sh` scripts and `sync-after-training.sh` are tightly coupled to this kit's three-way training comparison (250-step Qwen3-8B SFT) and aren't directly reusable.

## Verification checklist

After running all phases:

- **Single-node**: step_99 consolidation cache-hot ~11 sec; step_199 cache-cold 50-80 sec; ratio ≥ 4×.
- **Multi-node + rsync**: per-rank memory plateau ~35.5 GiB; per-checkpoint sync block ~4-5 sec flat; rsync wall-clock 5-20 min (bidirectional race may inflate).
- **Multi-node + NFSoRDMA**: per-checkpoint sync block ~22-24 sec flat (no hot/cold spread); total checkpoint cost across 3 ckpts ~70 sec.
- **Cold-cache restore over NFSoRDMA**: ~37 sec wall-clock; sustained NFS-server NVMe reads ~1.4 GB/s; peak ~1.7 GB/s; mem plateau 35.5 GiB at first post-restore step.

Numerical drift of ±10-15% is normal. Qualitative shape (the 7× single-node spread, disappearance of cold-cache penalty multi-node, ~5× per-ckpt tax of NFSoRDMA vs local) is what reproduces.
