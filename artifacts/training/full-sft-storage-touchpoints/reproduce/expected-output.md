# Expected output — what you should see

Reference values from this lab's measurements on `spark01` with `nvcr.io/nvidia/nemo-automodel:26.02`. Your numbers will differ slightly (different drive, kernel, container build), but the **ratios** and **qualitative shape** should hold.

## After `run-smoke.sh`

**Wall-clock breakdown** (typical):

| Phase | Wall-clock |
| --- | --- |
| Cold pull (5 shards via xet) | ~18 sec at ~890 MB/s aggregate |
| Model load + setup (touch point 4 served from page cache here) | ~50 sec |
| 3 training steps | ~5 sec total (~1.5–2 sec/step) |
| End-of-training validation | ~10 sec |
| Checkpoint write (touch point 5 + cache-hot consolidation) | ~46 sec |
| **Total** | **~10 min** including container startup |

**Disk after run:**

```
$EXP_ROOT/hf-cache/                                 ~16 GB  (~1× param count, safetensors-only via xet)
$EXP_ROOT/checkpoints-smoke/epoch_0_step_2/         ~62 GB  (1 checkpoint)
  ├── model/                                        ~31 GB  (DCP shard + consolidated HF — dual format)
  ├── optim/                                        ~31 GB  (Adam moments at bf16, ≈ 2× model bytes not 4×)
  └── ...metadata                                   <50 KB
$EXP_ROOT/checkpoints-smoke/smoke-output.log        <1 MB
```

**Headline checks from the log** (commands the script prints at the end):

```
$ grep "Param L2 norm" smoke-output.log
2026-XX-XX XX:XX:XX | INFO | root | Param L2 norm: 2459.6220
```

`2459.6220` is byte-identical across runs because Qwen3-8B materialization is deterministic. If your number differs, the model materialization went wrong (different model? different precision?).

```
$ grep "Done consolidating" smoke-output.log
... | Rank 0: Done consolidating. Processed 5 unique indices in 12.36 secs.
```

Cache-hot first-checkpoint consolidation: **~12–13 sec.** If yours is significantly longer, your kernel may have evicted the just-written DCP shard before the consolidation read — uncommon for a 3-step run but possible on a memory-tight host.

**`free -h` plateau during training:** ~61.5 GiB used (NeMo's `mem` tracker). If your UMA pool is < 80 GB you'll likely OOM during the consolidation phase.

## After `run-c25.sh` (the headline 6× reproduction)

**Per-checkpoint consolidation timings** (from `c25-output.log`):

```
$ grep "Done consolidating" c25-output.log
[step 24] ... in 12.99 secs.
[step 49] ... in 73.98 secs.
[step 74] ... in 60.11 secs.
[step 99] ... in 46.66 secs.
```

**The headline pattern:**

| Checkpoint | Consolidation | Cache regime |
| --- | --- | --- |
| step 24 (first) | ~12–14 sec | **hot** — DCP shard still in page cache |
| step 49 | ~70–80 sec | **cold** — evicted by step 24's optimizer write |
| step 74 | ~50–60 sec | steady-state cold |
| step 99 | ~45–60 sec | steady-state cold |

**Ratio: step 49 / step 24 ≈ 5.8–6.3×.** Same workload, same hardware — page cache state alone explains the difference. Step 24 reads the just-written shard from page cache (hot); by step 49, intervening writes have evicted it, and the kernel must re-read from NVMe to consolidate (cold).

If your ratio is significantly less than 4× or greater than 8×, your UMA-vs-checkpoint-size ratio differs enough to change the eviction breakpoint — the qualitative shape (cold > hot) holds, but the absolute multiplier is platform-specific. See [artifacts/scope-and-caveats.md](../../scope-and-caveats.md).

**Disk after c25:**

```
$EXP_ROOT/checkpoints-c25/epoch_0_step_{24,49,74,99}/   ~62 GB each = 248 GB
$EXP_ROOT/checkpoints-c25/c25-output.log                <1 MB
```

**Per-step training tps**: warm-up 150–500 in steps 0–2; steady-state ~500–570 tps thereafter; first step after each checkpoint at 7–17 tps (post-checkpoint flush tax). Memory plateau holds at 61.49 GiB.

## After `run-c100.sh` (the cadence-cost contrast)

**Single checkpoint consolidation** (from `c100-output.log`):

```
$ grep "Done consolidating" c100-output.log
[step 99] ... in ~13 sec.
```

The c100 run produces ONE final checkpoint (step 99, cadence trigger and end-of-training auto-checkpoint coincide). Its consolidation lands in the cache-hot regime because there's no prior checkpoint to evict the just-written DCP shard.

**Wall-clock totals:**

| Run | Mid-training ckpts | Total wall-clock | % in checkpoint overhead |
| --- | --- | --- | --- |
| c25 | 4 | ~13 min | ~58% |
| c100 | 0 | ~6 min | ~22% |

**Run-only training time** (subtracting checkpoint sync blocks): c100 ~3 min vs c25 ~4.5 min — the difference is the post-checkpoint sustained-slowdown tail in c25, where steps 65–99 ran at ~200–280 tps instead of ~500–570 tps. **That's 1.49× slower training, sustained, even outside checkpoint sync windows.** Mid-training checkpoints are doubly expensive: sync block plus a slowdown tail.

## After `run-restore.sh` (touch point 6 in isolation)

**Restore phase wall-clock** (from `restore-output.log`):

```
$ grep -E "Param L2 norm|Loading checkpoint|step 100 " restore-output.log
2026-XX-XX XX:23:34 | INFO | root | Param L2 norm: 2459.6220
Loading checkpoint from /opt/Automodel/checkpoints/epoch_0_step_99
2026-XX-XX XX:25:03 | INFO | root | step 100 | ... | tps 226 ...
```

**Total restore window:** ~89 sec (L2 norm → step 100). Of which:
- TP4 (model load from cold-cache HF cache): ~22 sec at 583–732 MB/s sustained.
- TP2 (dataset packing): ~17 sec, mostly CPU-bound (no NVMe).
- TP6 (checkpoint state load): ~58 sec, two-phase signature in iostat.

**TP6 in iostat** (from `iostat-restore.log` via `analyze-iostat.sh`):

| Phase | Window | Read rate |
| --- | --- | --- |
| DCP shard read | ~22 sec | 600–700 MB/s |
| Optimizer DCP read | ~28 sec | **1019–1153 MB/s sustained** |

**Effective TP6 rate: ~880 MB/s** (50 GB / 58 sec). **Peak read: ~1153 MB/s** (near single-thread sequential NVMe ceiling on this drive).

**Step 100 first-step tps: ~226** (~2.5× slower than steady ~564). Lighter than the post-checkpoint first-step tax (5–13 tps) — restore leaves training in a cleaner state than a checkpoint write does.

**Auto-checkpoint at step 104**: cache-hot ~12–13 sec consolidation (first checkpoint of the resumed process; no prior in-process checkpoint to evict the DCP shard).

## Touch-point timeline from `analyze-iostat.sh`

Run against any `iostat-*.log`. The script extracts high-activity windows (>50 MB/s) and pairs them with wall-clock timestamps. You should see:

- **In `iostat-smoke.log`**: cold-pull write bursts climbing 100 → 3900 MB/s with idle gaps (NVMe absorbs 4× faster than network delivers); zero NVMe reads during touch point 4 (page-cache served); checkpoint write at 1–3 GB/s for ~46 sec.
- **In `iostat-c25.log`**: 4 distinct checkpoint write windows. Cache-cold consolidations show mixed read+write at 600–800 MB/s each direction; cache-hot show pure write. **The optimizer DCP write phases sustain at 1.4–1.7 GB/s** — that's exactly the post-SLC TLC sustained rate from the [Spark NVMe FIO baseline](../../../data-prep/spark-nvme-fio-baseline/spark-nvme-fio-baseline.md).
- **In `iostat-c100.log`**: 1 final checkpoint window at the very end, no mid-training storage activity.
- **In `iostat-restore.log`**: ~22-sec read window at 600–700 MB/s (DCP shard), then ~28-sec read window at 1019–1153 MB/s sustained (optimizer DCP), then write window for the auto-checkpoint at step 104.

## Optional: anon-rss tracker (verifies the 47 GiB python HWM peak)

Run `anon-rss-tracker.sh` in a second SSH session before launching any of the run scripts. The tracker waits for python, then samples every 5 sec. Expected:

- Python startup: rss climbs to ~330 MiB.
- Model load + FSDP wrap: system `used` jumps from ~3 GiB to ~22 GiB (CUDA-managed UMA tensors visible in `shared`).
- Steady-state training: rss plateau at ~3 GiB python (the rest is in CUDA-managed UMA, not python's anon allocations).
- **Load + first-checkpoint peak: VmHWM ~47 GiB.** This is the load-phase memory ceiling — within ~10% of a 52 GiB anon-rss OOM cliff observed in earlier in-lab measurements. Closer to the cliff than "comfortably 61.5 GiB plateau" framing implies; the plateau is steady but the load + first-checkpoint write is the real risk window.
- System `used` reaches ~121 GiB during the first checkpoint write — UMA at-the-edge with single-digit-GiB margin.
- Swap engagement: <100 MiB on a c25 run (4 ckpts, ~13 min); 1–3 GiB on a c25 run with `MAX_STEPS=250` (6 ckpts, ~17 min).

## What `WIDE` numerical drift means

If a single number is off by ±10%, that's normal hardware variation. If multiple numbers drift in the same direction (everything slower, everything different):

- Different drive: SLC budget differs → cache-cold consolidation timing differs more than cache-hot.
- Smaller UMA pool: page-cache eviction breakpoint shifts → first cache-cold checkpoint engages earlier or later.
- Different host CPU: setup overhead (tokenizer init, dataset packing) is CPU-bound; ~50 sec on Spark could be 30–80 sec elsewhere.

For SUSTAINED qualitative shape — the 6× pattern, the cadence-cost contrast, the cold-cache-served TP4 vs page-cache-served TP4 — that should hold across UMA platforms.
