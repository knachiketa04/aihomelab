# Scope and Caveats

Findings in `artifacts/` are measured on a specific platform with specific software versions. The numbers are real, but how far they generalize depends on constraints that are easy to miss if you only read the headlines. This page enumerates them.

## Platform constraints

**DGX Spark UMA (no discrete VRAM).** Both lab nodes use NVIDIA's GB10 Grace Blackwell platform with unified memory shared between CPU and GPU. The OOM ceiling is total anonymous-RSS against a single physical pool, not "GPU memory full." Findings about memory feasibility (e.g. "8B full SFT fits comfortably") translate to other UMA platforms (Apple Silicon, AMD MI300A, future Grace-class systems) but **do not translate cleanly** to discrete-VRAM systems where CPU and GPU memory are separate budgets.

**Single-node measurements.** Every experiment to date runs on one Spark node. Multi-node behavior (FSDP sharding, RoCE-fabric checkpoint writes, all-reduce overhead) is not measured here and should not be inferred from per-node findings.

**ARM64 Ubuntu host.** DGX Spark runs an ARM64 Linux distribution. Container images, Python wheels, and CUDA builds all need ARM64 variants. Findings about container pull time, image size, or wheel-install behavior may differ on x86_64.

## Workload constraints

**Specific software versions matter.** Container image versions (`nvcr.io/nvidia/nemo-automodel:26.02`), CUDA versions, kernel versions, and HF Hub client versions all affect measured behavior — sometimes by 2–10× on I/O-bound paths. Results are valid as of the date stamped on each `artifacts/**/*.md`. Treat anything older than ~6 months as an indicative baseline, not a guaranteed reproduction.

**NVIDIA Container Toolkit pattern.** All training and serving workloads run inside Docker containers via the NVIDIA Container Toolkit. Bare-metal runs of the same workloads may behave differently, particularly around page cache locality and bind-mount filesystem semantics.

**Local NVMe baseline.** Storage findings are anchored to a single local NVMe device per node. Network storage (GCS, parallel filesystems, NFS) introduces additional variables — concurrency, network jitter, metadata round-trips — that are not reflected in measurements until experiments explicitly target those tiers.

## Methodological constraints

**Experiments are observational, not controlled trials.** The lab measures what happens under realistic configurations rather than ablating one variable at a time. When a result claims a causal relationship (e.g. "page cache state is the dominant variable in checkpoint write wall-clock"), it is supported by side-channel evidence (iostat) and a plausible mechanism, but it is not a randomized comparison.

**Single-node runs do not generalize linearly.** A finding measured on one node should not be multiplied by N to predict an N-node cluster. Networking, scheduling, and storage-tier sharing introduce non-linear effects.

**Cold vs warm runs are reported separately.** Where a measurement is sensitive to page cache state, both cold and warm numbers are given; readers should match their planning case (steady-state vs first-run) to the appropriate column.

## What this means for using the lab's findings

- For **capacity planning** at scale: use the lab's findings as a per-node lower bound, then add headroom for the constraints above.
- For **architectural decisions**: the qualitative shape of a finding (e.g. "checkpoint dual-format retention doubles model-side write amplification") generalizes more reliably than the absolute number.
- For **vendor or product choices**: the lab is not a product comparison; cloud services and software stacks named in experiments are technology targets, not endorsements.

When in doubt, read the linked artifact under `artifacts/<stage>/<topic-name>/` for the full measurement context, then ask whether your environment shares the platform, workload, and methodological assumptions above.
