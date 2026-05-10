# Cluster Validation

I use this checklist after powering on the DGX Spark nodes and before running experiments. The goal is simple: do not debug NeMo, vLLM, or storage behavior until the cluster itself is known to be healthy.

## Power-On Sanity Checks

Run from the workstation:

```bash
ping -c 3 192.168.20.21
ping -c 3 192.168.20.22
ssh sparks@192.168.20.21
ssh sparks@192.168.20.22
```

Expected hostnames:

- `spark01`
- `spark02`

## Per-Node Health Checks

Run on each node:

```bash
hostname
uptime
uname -a
nvidia-smi
docker ps
docker run --rm --gpus all nvcr.io/nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi
free -h
df -h
lsblk
ip addr show enP7s7
ip addr show enp1s0f0np0
```

## Network Checks

From `spark01`:

```bash
ping -c 5 192.168.20.22
ping -c 5 169.254.10.122
```

From `spark02`:

```bash
ping -c 5 192.168.20.21
ping -c 5 169.254.188.115
```

## NeMo Playbook Prerequisites

The NVIDIA NeMo fine-tuning playbook validates:

```bash
nvcc --version
python3 --version
nvidia-smi
free -h
docker ps
```

Note: [environment/cluster-env.md](cluster-env.md) currently records that `nvcc` is not installed on the host. That may be acceptable if the required CUDA tooling is available inside the NeMo container, but the playbook lists it as a host prerequisite, so capture the actual result before running the first experiment.

## Readiness Script

From the repo root on the workstation:

```bash
./infra/scripts/check-cluster-readiness.sh
```

The script does not modify the nodes. It runs read-only checks over SSH and reports what is available.
