# Expected Output (Spark, 2026-05-03)

Reference numbers from running this kit on the AIHomeLab Spark NVMe.

**Hardware:** SAMSUNG MZALC4T0HBL1-00B07 (OEM Samsung 9100 PRO), Gen5 x4, 4 TB, firmware NXHB202Q. Host: NVIDIA DGX Spark, kernel 6.17.0-1014-nvidia, FIO 3.36, sysstat 12.6.1.

If your drive is similar (Gen5 x4, TLC NAND, 4 TB consumer or OEM), expect numbers within ±10% per job. Larger gaps point to a different drive class, thermal throttling, NUMA / PCIe-traversal differences, or firmware divergence.

## Measured table

| Job | Throughput / IOPS | p99 lat | % of spec |
| --- | --- | --- | --- |
| seqwrite 1t 1MB | 1,662 MB/s | — | 12% |
| seqwrite 16t 1MB | 1,974 MB/s | — | 14% |
| seqread 1t 1MB | 5,105 MB/s | — | 34% |
| seqread 16t 1MB | 10,512 MB/s | — | 71% |
| randread 4k QD64 | 1,624,088 IOPS | 399 µs | 73% |
| randwrite 4k QD64 | 481,338 IOPS | 1,729 µs | 18% |
| randread 4k QD1 (latency floor) | 4,852 IOPS | **457 µs** | — |
| mixed 70/30 1MB 8t | 2,601 R + 1,113 W = 3,714 MB/s | — | — |

Spec for this drive (see Hardware line above): seq read 14,800 MB/s, seq write 13,400 MB/s, random read 2,200,000 IOPS QD64, random write 2,600,000 IOPS QD64.

## Why the write numbers are 12–14% of spec

This is the **central finding** of the experiment, not a measurement error.

This drive has a 442 GB pseudo-SLC cache. At 11 GB/s aggregate write rate, 180 sec of sustained writing puts ~2 TB onto the drive — far past the SLC. The reported throughput is the time-average of:

- ~30–45 sec of SLC-burst writes at ~11 GB/s
- ~135–150 sec of post-SLC TLC writes at ~1.85 GB/s (published post-SLC sustained)

Average works out to ~1,900 MB/s, matching what we measured.

Specs typically quote the SLC-burst rate. This kit measures the sustained number that AI workloads writing more than the SLC cache will see.

The probe phase of this experiment confirmed the SLC peak ingestion at 11,851 MB/s = 88% of spec, in the burst regime. So the drive can and does hit the headline number — for the first 30 seconds.

## Why the seqread numbers are higher

Reads aren't write-cached. Once you avoid the two cache-amplification bugs (see README §"Two cache-amplification bugs to know about"), the multi-thread seqread is bounded by the PCIe Gen5 x4 link's practical ceiling (~12 GB/s after encoding and protocol overhead). 10,512 MB/s sustained = 71% of spec, no fall-off across the 180-sec window.

## p99 latency floor (457 µs)

Higher than typical Gen5 direct-attach (literature numbers ~70–100 µs). The Spark's Grace platform NUMA + PCIe traversal contributes a ~250–400 µs floor for QD=1 random reads. Workloads with many small random reads at low parallelism (single-threaded metadata scans) will see this overhead. Parallelizing to QD≥4 erases the gap (the QD=64 random read result of 1.6 M IOPS at 399 µs p99 shows the controller is fast — the QD=1 floor is mostly platform overhead, not drive overhead).

## Per-second bandwidth logs

The write jobs (01, 02, 06, 08) emit per-second bandwidth logs in `out/<job>_bw.0.log`. Plot any of them to see the SLC fall-off curve. The 16-thread seqwrite shows the most dramatic knee, typically dropping from ~11 GB/s to ~1.8 GB/s within the first 30–45 seconds.

```bash
awk -F, '{print $1/1000, $2/1024}' out/02-seqwrite-16t_bw.0.log | head -200
```

(column 1 = seconds, column 2 = MiB/s)

## Tooling versions

- FIO: 3.36
- jq: 1.7.1
- iostat (sysstat): 12.6.1
- Kernel: 6.17.0-1014-nvidia
- Drive firmware: NXHB202Q
