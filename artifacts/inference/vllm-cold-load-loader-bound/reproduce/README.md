# vLLM cold load is loader-bound — reproduce kit

Reproduces a cold-load A/B/C plus a serving-touch-point measurement of an 8B model on a single UMA workstation (Grace-class node, single Gen5 NVMe, no GPUDirect Storage):

1. **The default vLLM loader is 18-36x slower than a streaming loader on the same bytes.** Cold-loading base Qwen3-8B (15.35 GiB safetensors) takes ~106 s with the stock `auto` loader, ~3.0 s with RunAI Model Streamer (36x), ~6.0 s with fastsafetensors (18x). All three serve byte-identical output.
2. **The mechanism is a single-threaded CPU path, not storage.** During the default load exactly one CPU core is pinned ~100% `%usr` while the rest sit idle (mpstat), and the NVMe loafs: bursty reads peaking ~177 MB/s (~1% of Gen5 capacity), `%util` <=14%, `%iowait` ~0 (iostat). The ~106 s is per-tensor Python materialization; the streaming loaders parallelize the same bytes.
3. **At serving time the storage touch points are over-provisioned.** Under saturating eval traffic (concurrency 64): prefix cache hits 83% on a constant system prompt (a compute saver, not a storage cost), KV cache peaks at 5.5% utilization, and the full-fidelity audit log writes ~4 KB/s. None is a storage constraint for recipe-length serving at this scale.

Numbers and tolerances in [`expected-output.md`](expected-output.md). Platform and workload bounds in [`../../../scope-and-caveats.md`](../../../scope-and-caveats.md).

> **CAUTION — do not loop the default arm dozens of times in one session.** Sustained vLLM cold-load cycles can wedge the NVRM allocator after ~25-30 cycles on this UMA class (the GPU stops accepting new contexts until the host is rebooted). This kit runs ~3 reps per loader (well under the threshold). If you script extra reps, keep the per-session total under ~20 and reboot if a launch hangs at CUDA init.

## Environment

- 1 UMA workstation (Grace-class node), single Gen5 NVMe, >=64 GiB unified memory, ARM64 (aarch64) Linux kernel 6.x. No GPUDirect Storage required (both streaming loaders run CPU-buffered / `nogds` fallback).
- `docker` with `--gpus all`; the base image `nvcr.io/nvidia/vllm:26.02-py3` (vLLM 0.15.1) pullable.
- `sysstat` (`mpstat`, `iostat`) on the host for the side-channels.
- A narrow sudoers entry permitting the page-cache drop (see `run-loader-cold-warm.sh` header), or run that one step interactively with `sudo`.
- base Qwen3-8B in a local HF cache. Adapter serving is OPTIONAL (the 18-36x headline reproduces on base alone).

## Files

| File | Purpose | Runtime | Disk |
| --- | --- | --- | --- |
| `build-loaders-image.sh` | Derive `vllm-loaders` image: base image + RunAI streamer + fastsafetensors. | ~2-10 min | ~1 GB layers |
| `serve-arm.sh` | Launch one serve arm `<auto\|runai_streamer\|fastsafetensors>`. Base model; adapter optional via env. | <load-time | — |
| `run-loader-cold-warm.sh` | Cold/warm protocol: drop cache, capture launch ts, arm, side-channels, headline grep, correctness probe, warm rep. >=3 cold reps. | ~10 min/loader | small logs |
| `analyze-loaders.py` | Parse load-time lines + mpstat/iostat side-channels into the comparison matrix. | <30 sec | — |
| `run-eval-traffic.py` | Phase-2 serving driver: fires `sample-prompts.jsonl` at the server, measures TP3 prefix / TP2 KV peak / TP6 audit bytes. | ~5 min | ~1 MB out |
| `sample-prompts.jsonl` | ~40 generic recipe-style prompts (public-safe) for the Phase-2 driver. | — | small |
| `strip-lm-head-lora.py` | OPTIONAL adapter preprocessor: strip the `lm_head` LoRA so vLLM accepts a `match_all_linear` PEFT adapter. | <10 sec | small |
| `expected-output.md` | Reference numbers + pass criteria. | — | — |

## Run order

1. `build-loaders-image.sh` — build the derived image once. (Skip if stock image already has both loaders; the script checks.)
2. (OPTIONAL adapter tier) `strip-lm-head-lora.py` inside the image, then export `ENABLE_LORA=1 ADAPTER_DIR=...` for the serve scripts.
3. `run-loader-cold-warm.sh auto` -> `run-loader-cold-warm.sh runai_streamer` -> `run-loader-cold-warm.sh fastsafetensors`. Each runs >=3 cold reps + a warm rep and writes per-rep logs.
4. `analyze-loaders.py --log-dir <dir>` — the loader comparison matrix.
5. Phase 2: launch one arm with `serve-arm.sh runai_streamer` (fast), wait for `/health`, then `run-eval-traffic.py` to measure the serving touch points.

## Tunables (env vars)

| Var | Default | Notes |
| --- | --- | --- |
| `EXP_ROOT` | `/home/$USER/vllm-cold-load-reproduce` | Working dir for logs |
| `CONTAINER` | `vllm-loaders:cold-load` | Derived image tag (built by `build-loaders-image.sh`) |
| `BASE_IMAGE` | `nvcr.io/nvidia/vllm:26.02-py3` | Stock vLLM base |
| `HF_CACHE` | `/home/$USER/hf-cache` | Host HF cache dir, bind-mounted read-only |
| `MODEL_REPO` | `models--Qwen--Qwen3-8B` | HF-cache repo dir name |
| `SNAPSHOT_HASH` | `b968826d9c46dd6066d109eabc6255188de91218` | Snapshot dir under `.../snapshots/`. Override to your cache's hash. |
| `COLD_REPS` | `3` | Cold reps per loader (keep modest; see CAUTION) |
| `NVME_DEVICE` | `nvme0n1` | Local NVMe device for iostat |
| `DROP_CACHE_CMD` | `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` | Page-cache drop; wire a narrow sudoers entry |
| `ENABLE_LORA` | `0` | `1` adds `--enable-lora --lora-modules` + chat template (adapter tier) |
| `ADAPTER_DIR` | (unset) | Host dir with the lm_head-stripped adapter; required when `ENABLE_LORA=1` |
| `CHAT_TEMPLATE` | (unset) | Host path to a chat template `.jinja`; mounted when set |
| `SERVED_NAME` | `recipe-qwen3-8b` | `--served-model-name` |
| `TEST_FILE` | (ships `sample-prompts.jsonl`) | Phase-2 prompt set; override to point at your own JSONL |

## Cherry-picking

- `build-loaders-image.sh` — standalone recipe for adding both streaming loaders to any vLLM image (RunAI layer first so a fastsafetensors source-build failure still leaves a usable RunAI layer).
- `analyze-loaders.py` — parses vLLM load-time log lines + sysstat side-channels into a matrix; reusable for any loader A/B.
- `strip-lm-head-lora.py` — standalone PEFT-adapter fixer for any `match_all_linear` adapter rejected by vLLM's per-architecture allowlist.

`serve-arm.sh`, `run-loader-cold-warm.sh`, and `run-eval-traffic.py` are kit-specific (paths + measurement logic wired to this experiment).

## Verification (pass criteria, +-10-15% drift on absolutes OK)

- **Loader ordering must hold**: `runai_streamer` fastest (~3 s), `fastsafetensors` next (~6 s), `auto` slowest (~106 s). The 18-36x gap is the load-bearing finding; the absolutes can drift.
- **Mechanism signature**: during the `auto` load, mpstat shows one core ~100% `%usr` with the rest idle, and iostat shows NVMe `%util` <=14% with peak read well under 200 MB/s. If the NVMe is saturated instead, your cache wasn't cold or the device is far slower than Gen5.
- **Correctness**: on every non-default arm, the mandatory probe returns a coherent completion (catches a silently no-op'd adapter or a half-loaded model). Output should match the `auto` arm.
- **Phase 2**: prefix-cache hit-rate ~80%+ with the constant system prompt; KV peak in single digits (over-provisioned); audit log in single-digit KB/s.

If `auto` comes in near the streaming loaders' time, the model is too small to show the per-tensor path, or you measured a warm load. If a streaming arm is slow, confirm `--load-format` actually accepted the value (`analyze-loaders.py` flags a value the build rejected).

## Out of scope

- Lustre / network-storage source arms (dropped: the bottleneck is the loader, not the tier; cite the cold-load tier-irrelevance result).
- PD-disaggregated prefill/decode KV transfer (NIXL-on-UMA blocked separately).
- Algorithmic quality screening of generated text. One methodology note only: an ingredient blocklist works for filtering a declarative corpus but mis-flags generated instructional text ("omit the eggs", "replace the fish with tofu") as violations, so the right quality eval is an LLM judge, not a blocklist. That is a quality-eval methodology point, not a storage measurement.
