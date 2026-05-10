#!/usr/bin/env bash
set -euo pipefail

NODE1_SSH="${NODE1_SSH:-sparks@192.168.20.21}"
NODE2_SSH="${NODE2_SSH:-sparks@192.168.20.22}"
NODE1_MGMT_IP="${NODE1_MGMT_IP:-192.168.20.21}"
NODE2_MGMT_IP="${NODE2_MGMT_IP:-192.168.20.22}"
NODE1_QSFP_IP="${NODE1_QSFP_IP:-169.254.188.115}"
NODE2_QSFP_IP="${NODE2_QSFP_IP:-169.254.10.122}"
MN_IF_NAME="${MN_IF_NAME:-enp1s0f0np0}"

run_remote() {
  local node="$1"
  local peer_mgmt_ip="$2"
  local peer_qsfp_ip="$3"

  echo "== ${node} =="
  ssh -o BatchMode=yes -o ConnectTimeout=8 "${node}" \
    "set -euo pipefail
# Add CUDA and user-local bins to PATH (non-interactive ssh skips /etc/profile.d/*.sh)
export PATH=\"/usr/local/cuda/bin:/home/sparks/.local/bin:/opt/bin:\$PATH\"
echo '# identity'
hostname
uptime

echo '# gpu'
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi; else echo 'nvidia-smi: missing'; fi

echo '# cuda toolkit'
if command -v nvcc >/dev/null 2>&1; then nvcc --version; else echo 'nvcc: missing on host'; fi

echo '# python'
python3 --version || true

echo '# memory and storage'
free -h
df -h
lsblk

echo '# docker'
docker ps >/dev/null && echo 'docker ps: ok' || echo 'docker ps: failed'
docker version --format '{{.Server.Version}}' 2>/dev/null || true

echo '# network interfaces'
ip -br addr show enP7s7 || true
ip -br addr show ${MN_IF_NAME} || true

echo '# peer reachability'
ping -c 3 ${peer_mgmt_ip} || true
ping -c 3 ${peer_qsfp_ip} || true"
}

echo "Checking SSH reachability and read-only cluster health."
echo "Using NODE1_SSH=${NODE1_SSH}"
echo "Using NODE2_SSH=${NODE2_SSH}"
echo

overall_status=0

run_remote "${NODE1_SSH}" "${NODE2_MGMT_IP}" "${NODE2_QSFP_IP}" || overall_status=1
echo
run_remote "${NODE2_SSH}" "${NODE1_MGMT_IP}" "${NODE1_QSFP_IP}" || overall_status=1

exit "${overall_status}"

