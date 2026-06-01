# Expected output — vLLM cold load is loader-bound

Reference numbers from the canonical run. **Numerical drift of +-10-15% on absolutes is normal across hardware**; the qualitative shape (the 18-36x loader ordering; the single-core-pin mechanism; storage NOT being the bottleneck at load or at serving) is what must reproduce.

Reference hardware class: 1 UMA workstation (Grace-class node), single Gen5 NVMe (~5-7 GB/s class), >=64 GiB unified memory, ARM64 (aarch64) Linux kernel 6.x, no GPUDirect Storage. Model: base Qwen3-8B, 15.35 GiB safetensors (5 shards). Container: derived from `nvcr.io/nvidia/vllm:26.02-py3` (vLLM 0.15.1) plus RunAI Model Streamer + fastsafetensors.

## Phase 1 — loader cold-load A/B/C (the headline)

Cold = page cache dropped before each rep. Source = local NVMe. N reps per loader in parentheses.

| Loader | Source | Cache | Load Y (s) | sec/GiB | vs default |
|---|---|---|---|---|---|
| `auto` (default) | local NVMe | cold | **99.2 / 108.0 / 112.1** (mean ~106, N=3) | ~7.0 | 1x |
| `auto` (default) | local NVMe | warm | ~107.5 (uncontrolled smoke) | ~7.0 | warm == cold |
| `fastsafetensors` | local NVMe | cold | **6.37 / 5.58** (mean ~6.0, N=2) | ~0.39 | **18x** |
| `runai_streamer` | local NVMe | cold | **3.17 / 2.87 / 2.86** (mean ~3.0, N=3) | ~0.19 | **36x** |

**The ordering is the load-bearing finding**: `runai_streamer` fastest, `fastsafetensors` next, `auto` ~18-36x slower. Absolutes drift with the NVMe and CPU; the ordering and the ~order-of-magnitude gap must hold.

**Warm == cold for the default loader** is itself a result: dropping the page cache does not change the load time, because the work is CPU per-tensor materialization, not disk reads. If your warm run is dramatically faster than cold, you were storage-bound (slower-than-Gen5 device) and the mechanism below will look different.

### Mechanism signature (this is what proves it is the loader, not storage)

During the **default** `auto` cold load:

| Signal | Reference | What it means |
|---|---|---|
| CPU (mpstat -P ALL) | **one core pinned ~100% `%usr`, the rest idle** | single-threaded per-tensor Python path |
| NVMe read (iostat -x) | **bursty, peak ~177 MB/s** (~1% of Gen5 capacity) | storage is loafing |
| NVMe `%util` | **<=14%**, `%iowait` ~0 | device nowhere near busy |
| Effective load rate | **~0.14 GiB/s** (15.35 GiB / ~106 s) | two orders of magnitude below the NVMe |
| Peak host RSS (VmHWM) | ~15.35 GiB | weights resident; no runaway |

> **Run-to-run variance (validated 2026-06-01).** The NVMe **peak** read and `%util` are noisier than the row above: a re-validation saw the readahead burst peak ~0.5 GB/s at `%util` ~60% (vs ~177 MB/s / ≤14%). That does **not** change the conclusion — 0.5 GB/s is still ~5% of the drive's multi-GB/s bandwidth, and `%util` is a weak NVMe saturation proxy (one in-flight I/O reads as "busy" at trivial bandwidth). The load-bearing invariants are the **one-core pin** plus **read ≪ drive bandwidth while the streamers read at multi-GB/s** — not the exact peak. `analyze-loaders.py` reads its verdict from the actual numbers rather than asserting a fixed threshold.
>
> **Peak host RSS caveat.** The kit samples RSS with host `pgrep -f "vllm serve"`, but the engine runs in the container's PID namespace, so the host sampler under-reads it (≈0.9 GiB artifact, not the ~15 GiB resident set). For the true figure run `docker stats <name>` during a load. RSS is a secondary sanity check; the mechanism is carried by mpstat + iostat.

During a **streaming** load (`runai_streamer` / `fastsafetensors`): no single-core pin, multiple cores active, and the same 15.3 GiB moved with concurrency. RunAI streams at **8.1-8.4 GiB/s**; fastsafetensors parallelizes with a clean `nogds` fallback (no GPUDirect Storage on this box). The streamer turns a ~0.14 GiB/s path into a multi-GiB/s one without touching the storage tier.

### Correctness (mandatory on non-default arms)

Both streaming loaders serve **byte-identical correct output** to the default loader on the probe prompt. A streaming loader that loads fast but returns garbage (or silently no-ops an adapter) is a failure, not a pass. The kit's correctness probe gates this on every non-default arm.

## Phase 2 — serving touch points under eval traffic

Canonical run: 579 requests, 0 errors, ~300 s wall-clock, ~1.93 req/s, ~520 out-tok/s, concurrency 64, constant system prompt.

| TP | Touch point | Reference | Read |
|---|---|---|---|
| TP3 | prefix cache | **83.0% hit-rate** (Δhits 56,480 / Δqueries 68,050) | constant system prompt cuts prefill; a **compute** saver, not a storage cost |
| TP2 | KV cache | **peak 5.5%** (~17.7K of ~319.8K tokens), peak batch 64, waiting 0 | **over-provisioned** for recipe-length serving at this scale |
| TP6 | audit log | **4.0 KB/s** full-fidelity prompt+response (1.21 MB / 579); ~0.4 KB/s vLLM default request log | trivially small; not a storage constraint |

**None of the serving touch points is a storage constraint.** The only storage-adjacent win, 83% prefix-cache hits, reduces compute.

### Reproducing Phase 2 with the shipped sample set

The kit ships ~40 generic recipe prompts (`sample-prompts.jsonl`) instead of the private 579-example split, so it runs for anyone. At that smaller scale the **absolute** throughput and the prefix warm-up fraction differ, but the qualitative shape holds:

- **Prefix-cache hit-rate ~80%+** once the constant system prompt's blocks are cached (the first few requests prime it; with ~40 prompts the steady-state fraction is slightly lower than the 579-prompt run's 83%).
- **KV peak in single digits** at concurrency 64.
- **Audit log single-digit KB/s.**

To reproduce the headline 83% / 5.5% / 4.0 KB/s exactly, point `TEST_FILE` at a few-hundred-prompt set with a shared system prompt.

## Common reproduction failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `auto` loads in a few seconds (no ~106 s) | warm load, or device far slower than Gen5 read but the per-tensor path masked | confirm the page-cache drop ran; check mpstat for the single-core pin |
| a streaming arm is as slow as `auto` | `--load-format` value not actually accepted by this build | re-run `build-loaders-image.sh`'s CLI check; `analyze-loaders.py` flags a missing loader |
| streaming arm fast but probe returns garbage | half-loaded model or a no-op'd adapter | check `docker logs`; on the adapter tier confirm the lm_head strip ran |
| NVMe `%util` near 100% during `auto` | not the canonical mechanism — device is the bottleneck on this box | note it: on a slow enough device the cold load can become storage-bound; the 18-36x gap may shrink |
| build fails on the fastsafetensors layer | no aarch64 source-build path on this stack | RunAI-only image still built (a finding); the 36x primary arm proceeds |
| launches hang at CUDA init after many reps | NVRM allocator wedged by repeated cold-load cycles | reboot the host; keep per-session reps under ~20 (see README CAUTION) |

## Out of scope (and why)

- **Network-storage / Lustre source arms**: dropped. The bottleneck is the loader, not the storage tier, so a slower source would only re-confirm tier-irrelevance for the default loader.
- **Algorithmic quality screening of generated text**: one methodology note only. An ingredient blocklist works for filtering a declarative training corpus but mis-flags generated instructional text ("omit the eggs", "replace the fish with tofu") as violations, so the correct quality eval is an LLM judge, not a blocklist. This is a quality-eval methodology point, not a storage measurement.
