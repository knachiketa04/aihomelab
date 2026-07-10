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

Note: [environment/cluster-env.md](cluster-env.md) currently records that `nvcc` is not installed on the host. That may be acceptable if the required CUDA tooling is available inside the NeMo container, but the playbook lists it as a host prerequisite. In practice the container-bundled CUDA toolchain proved sufficient across all of the lab's experiments; host nvcc was never needed.

## Readiness Script

From the repo root on the workstation:

```bash
./infra/scripts/check-cluster-readiness.sh
```

The script does not modify the nodes. It runs read-only checks over SSH and reports what is available. Output is two-layer: human-readable section headers for terminal use, plus structured `PROBE=<name> NODE=<node> STATUS=<ok|warn|red> DETAIL=<...>` lines that the `aihomelab-cluster-ready` skill parses. Exit code is the worst probe status (0 ok, 1 warn, 2 red).

Probes (v1): `ssh_reachable`, `gpu_present`, `docker_runtime`, `gpu_docker_passthrough`, `sudoers_cache_drop`, `disk_headroom_home`, `disk_headroom_root`, `leftover_k3s`, `leftover_network_mounts`, `orphan_containers`, `stale_large_files`, `net_mgmt_peer`, `net_qsfp_peer`, `net_qsfp_bandwidth`, `hf_token`.

### Agentic invocation

In a Claude Code session, say "get ready for experiment" (or "cluster ready", "are the sparks up") and the `aihomelab-cluster-ready` skill runs the script directly and produces a 4-section briefing — no need to read the raw output by hand. This is the only pre-experiment exception to companion mode; the probes are read-only.

### Bootstrap: install the cache-drop sudoers (one-time per node)

The harness and the readiness probe both depend on a narrow-NOPASSWD `/etc/sudoers.d/sparks-cache-drop`. If `sudoers_cache_drop` comes back red because the file is missing, install it interactively:

```bash
./infra/scripts/check-cluster-readiness.sh --install-sudoers spark01
./infra/scripts/check-cluster-readiness.sh --install-sudoers spark02
```

This uses `ssh -t` so sudo can prompt for the password on the target node. The script writes the file via a `visudo -cf` validation step, then verifies NOPASSWD callability before exiting. It is the only path that installs this sudoers entry — the readiness probe itself never attempts the install (it would need a password it doesn't have).
