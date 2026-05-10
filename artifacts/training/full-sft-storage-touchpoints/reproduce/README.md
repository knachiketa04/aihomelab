# Full SFT storage touch-points — reproduce kit

The minimum runnable artifacts to reproduce the headline findings of the touch-point-map characterization on a single DGX Spark node:

1. **The 6× page-cache-eviction pattern in checkpoint consolidation** (cache-hot ~13 sec / cache-cold ~50–80 sec for the same workload).
2. **The cadence-cost contrast** (mid-training checkpoints are doubly expensive — sync block plus a sustained training-slowdown tail).
3. **Three regimes of model-load behavior** (page-cache-served, partial, cold-cache).
4. **drop_caches mid-run is largely ineffective** on a memory-pressured training workload.

## What the kit covers (and what it doesn't)

The kit reproduces the **touch-point measurements**, not every variant from the experiment. Specifically:

- Touch points 3 + 4 (acquire + load) via `run-smoke.sh` on cold-cache start.
- Touch points 5 + 7 cache-hot/cold pattern via `run-c25.sh`.
- Cadence-cost contrast via `run-c100.sh` (paired with `run-c25.sh`).
- Touch point 6 (cold-cache checkpoint restore) via `run-restore.sh`.
- Touch points 1 + 2 (dataset acquire/load) are deliberately left out — the playbook workload uses a 16 MB SQuAD dataset that doesn't exercise them. Characterizing those at meaningful dataset size is a separate experiment.

The kit does **not** reproduce the post-checkpoint sustained-slowdown phenomenon directly. That requires longer training windows (250+ steps) with multiple cache-cold checkpoints — runnable but disk-expensive (~700 GB across all checkpoints). The cadence-cost contrast surfaces the wall-clock impact in a more compact form.

## Environment requirements

You need *all* of these for a faithful reproduction:

- A UMA host (DGX Spark, Grace-class, Apple Silicon, AMD MI300A, or similar — any platform where CPU and GPU share one physical memory pool).
- ≥ 121 GB unified memory (matches the Spark; smaller pools may not fit even 8B full SFT).
- ≥ 500 GB free local NVMe (smoke = ~80 GB; c25 = +250 GB; c100 = +125 GB; restore = +62 GB).
- Docker + NVIDIA Container Toolkit (or your platform's container runtime that exposes GPUs).
- Hugging Face token at `~/.huggingface_token` (mode 600), with [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) gate already accepted in the same account.
- `iostat` (from `sysstat`) for the side-channel. The headline finding is most visible when iostat is captured alongside the run.
- `sudo` access for `drop_caches` if you run `run-restore.sh` (the only script that needs it — see the script's pre-flight comment).

If you are on a discrete-VRAM system, the memory-feasibility findings will not translate cleanly — see [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).

## What's in this kit

| File | Purpose | Runtime | Disk |
| --- | --- | --- | --- |
| `run-smoke.sh` | 3-step SFT + 1 checkpoint write on cold-cache start. Establishes touch points 3 + 4 + 5 + 7 once each. | ~10 min | ~80 GB |
| `run-c25.sh` | 100-step SFT with `ckpt_every=25` → 4 checkpoints. **Surfaces the 6× page-cache pattern between checkpoint 1 (cache-hot) and checkpoint 2 (cache-cold).** | ~13 min on top of smoke (warm) | +250 GB |
| `run-c100.sh` | 100-step SFT with `ckpt_every=100` → 1 final checkpoint, no mid-training checkpoints. **The cadence-cost contrast against c25.** | ~6 min | +62 GB |
| `run-restore.sh` | Cold-cache resume from `c25/epoch_0_step_99` + 5 more steps + final checkpoint. **Touch point 6 in isolation.** Requires sudo for `drop_caches`. | ~5 min | +62 GB |
| `analyze-iostat.sh` | Parses `iostat -t` log into a touch-point timeline (high-activity windows mapped to read/write phases). | <30 sec | none |
| `anon-rss-tracker.sh` | Optional — captures python anon-rss + system memory at 5-sec cadence during a run. Use it in a second SSH session if you want to verify the ~47 GiB load-phase HWM peak. | for length of training run | <1 MB log |
| `expected-output.md` | Reference numbers from this lab's measurements for comparison. | — | — |

## Suggested order

1. **`run-smoke.sh`** — verifies your hardware fits the workload and produces a clean cold-pull timing for touch points 3 + 4. Typical wall-clock: ~10 min.
2. **`run-c25.sh`** — the headline run. Watch the consolidation wall-clocks for steps 24, 49, 74, 99: step 24 ~13 sec (cache-hot), step 49 ~50–80 sec (cache-cold), 6× ratio reproduces.
3. **`run-c100.sh`** — runs the same 100-step workload with no mid-training checkpoints. Sanity-check: the run completes ~6 min vs c25's ~13 min, and step_99's consolidation should be cache-hot (~13 sec) because there's no prior checkpoint to evict the just-written DCP shard.
4. **`run-restore.sh`** — picks up the c25 step_99 checkpoint after `drop_caches`, validates touch point 6 at ~880 MB/s effective read.
5. **`analyze-iostat.sh`** on each `iostat-*.log` to extract the touch-point timeline.

## Tunables

All scripts honor these env vars:

- `EXP_ROOT` — where everything lands. Default: `/home/sparks/full-sft-touchpoints-reproduce`.
- `HF_TOKEN_FILE` — token path. Default: `~/.huggingface_token`.
- `CONTAINER` — container image. Default: `nvcr.io/nvidia/nemo-automodel:26.02`.

## Why the kit is small

The kit reproduces the touch-point **measurements**, not the full experimental session that produced them. The lab's original characterization ran five variants (smoke, c25, c100, restore-105, restore-250) and surfaced the post-checkpoint sustained-slowdown phenomenon over ~17-min runs; this kit's four-script matrix gets you the load-bearing findings in roughly 35 minutes of total wall-clock and ~450 GB disk. If you want to reproduce the sustained-slowdown phenomenon directly, run `run-c25.sh` with `MAX_STEPS=250` to get six mid-training checkpoints; the scripts honor a `MAX_STEPS` override for that purpose.

## Verification checklist

After running the kit, confirm:

- Smoke: cold-pull aggregate ~900 MB/s (xet protocol), L2 norm 2459.6220 (deterministic), checkpoint footprint ~62 GB exactly.
- c25: step 24 consolidation 12–14 sec, step 49 consolidation 50–80 sec, ratio ≥ 4×.
- c100: step 99 consolidation cache-hot at ~13 sec.
- Restore: ~58 sec restore phase, effective read ~880 MB/s.

Numerical drift of ±10% is normal across hardware variations. Qualitative shape (the 6× ratio, the cadence-cost contrast) holds.
