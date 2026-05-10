# Artifacts

Self-contained experimental artifacts from AIHomeLab — each one a packaged write-up plus a reproducible kit you can run on similar hardware.

Organized by AI pipeline stage, since storage behaves differently in each:

- **`data-prep/`** — dataset ingest, transformation, tokenization, cache behavior.
- **`training/`** — fine-tuning, checkpointing, dataloaders, restart behavior.
- **`inference/`** — model loading, serving startup, model cache, runtime behavior.

The catalog grows as experiments earn their place in it. Most experiments stay internal; what surfaces here has been measured, framed, and packaged for someone else to repeat.

## Catalog

### training

- **[full-sft-storage-touchpoints](training/full-sft-storage-touchpoints/full-sft-storage-touchpoints.md)** (2026-05-04) — How storage actually behaves in an AI training workload, with one DGX Spark + Qwen3-8B as the worked example. Seven storage touch points; the 6× page-cache pattern in checkpoint consolidation reproduced 3× independently; methodologically: a small UMA workstation surfaces architectural lessons that transfer to large-scale infrastructure planning.
- **[multi-node-training-storage](training/multi-node-training-storage/multi-node-training-storage.md)** (2026-05-06) — How to choose distributed-training storage by measuring read-vs-write dominance over your specific fabric, with NFSoRDMA on a 2-host UMA cluster as the worked example. Three-way controlled comparison: shared FS pays a 5× per-checkpoint tax to eliminate the post-training sync layer entirely; the falsified-prediction surprise that NFS-over-TCP writes outperform NFS-over-RDMA in sync-export regime by 13%.
- **[lustre-on-uma-workstations](training/lustre-on-uma-workstations/lustre-on-uma-workstations.md)** (2026-05-09) — How to stand up minimum-viable distributed Lustre on 2 UMA workstations with stock single-NVMe layouts (no destructive partitioning), and which configuration knobs are load-bearing. Three-knob trio (`primarycache=metadata`, `atime=off`, `obdfilter.brw_size=4`) recovers ~85% of the single-node loopback ceiling on bulk IO; without them, default config delivers 32× less on 64 KiB writes and 400× less on 4 KiB random IOPS. Cross-node Lustre is architecturally ~6× slower than NFSoRDMA on cached reads on identical hardware (RPC framing + LDLM + osd-zfs + ko2iblnd stack depth, not pipeline depth — BDP analysis rules that out); distributed Lustre's win condition is concurrent multi-client access where aggregate throughput reaches 60–85% of the loopback ceiling.

### data-prep

- **[spark-nvme-fio-baseline](data-prep/spark-nvme-fio-baseline/spark-nvme-fio-baseline.md)** (2026-05-03) — How to baseline NVMe for AI infrastructure, with a single Gen5 NVMe as the worked example. The SLC fall-off (6×, burst → post-SLC TLC sustained) is the dominant performance gap for AI workloads writing more than the SLC cache — larger than the loader gap (3.3×). Reproduce kit documents three FIO methodology bugs that systematically inflate published SSD benchmarks.

### inference

(no public artifacts yet)

---

For the underlying methodology and lab context, see the [README](../README.md), [environment notes](../environment/), and [scope-and-caveats](scope-and-caveats.md).
