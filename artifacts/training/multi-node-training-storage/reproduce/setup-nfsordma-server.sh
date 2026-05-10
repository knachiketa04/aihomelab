#!/usr/bin/env bash
# setup-nfsordma-server.sh — general-purpose NFSoRDMA server-side setup.
#
# Configures an NFS server with the RDMA listener enabled on port 20049, idempotently:
# package install, persistent rpcrdma module load, /etc/exports entry, [nfsd] rdma=y in
# /etc/nfs.conf, server restart, and /proc/fs/nfsd/portlist verification.
#
# Re-running detects existing state (export line, rdma=y already set, module loaded)
# and skips already-applied steps. Verifies via /proc/fs/nfsd/portlist. Full bandwidth
# characterization (fio) is out of scope; write your own if you want it.
#
# Standalone usage (without the rest of the kit):
#   HOST1_QSFP_IP=10.0.0.1 \
#   NFS_EXPORT_PATH=/srv/nfs/myshare \
#   NFS_EXPORT_SUBNET=10.0.0.0/24 \
#   ./setup-nfsordma-server.sh
#
# Pair with setup-nfsordma-client.sh on the client host (using the same HOST1_QSFP_IP
# and NFS_EXPORT_PATH).

set -euo pipefail

# --- Tunables (env-var overridable) ---
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
NFS_EXPORT_PATH="${NFS_EXPORT_PATH:-/srv/nfs/multi-node-storage}"
NFS_EXPORT_SUBNET="${NFS_EXPORT_SUBNET:-169.254.0.0/16}"
NFS_RDMA_PORT="${NFS_RDMA_PORT:-20049}"

echo "==> setup-nfsordma-server.sh on $(hostname)"

# --- Packages + rpcrdma module loaded persistently ---
echo "==> installing nfs + rdma packages (idempotent)"
sudo apt-get update -qq
sudo apt-get install -y -qq nfs-kernel-server nfs-common rdma-core

if ! find "/lib/modules/$(uname -r)" -name 'rpcrdma*' 2>/dev/null | grep -q .; then
  echo "==> rpcrdma not in base kernel; installing linux-modules-extra"
  sudo apt-get install -y -qq "linux-modules-extra-$(uname -r)"
fi

if [ ! -f /etc/modules-load.d/rpcrdma.conf ]; then
  echo "==> adding rpcrdma to /etc/modules-load.d for persistence"
  echo rpcrdma | sudo tee /etc/modules-load.d/rpcrdma.conf > /dev/null
fi

if ! lsmod | grep -q '^rpcrdma'; then
  echo "==> modprobe rpcrdma"
  sudo modprobe rpcrdma
fi

lsmod | grep -E '^(rpcrdma|svcrdma|xprtrdma)' || {
  echo "ERROR: rpcrdma module did not load — check dmesg" >&2
  exit 2
}

# --- NFS export configuration ---
echo "==> preparing export dir at $NFS_EXPORT_PATH"
sudo mkdir -p "$NFS_EXPORT_PATH"
sudo chmod 777 "$NFS_EXPORT_PATH"

EXPORT_LINE="$NFS_EXPORT_PATH ${NFS_EXPORT_SUBNET}(rw,sync,no_root_squash,no_subtree_check)"
if ! grep -qF "$NFS_EXPORT_PATH" /etc/exports 2>/dev/null; then
  echo "==> adding export line to /etc/exports"
  echo "$EXPORT_LINE" | sudo tee -a /etc/exports > /dev/null
else
  echo "==> /etc/exports already references $NFS_EXPORT_PATH (skipping)"
fi

# --- /etc/nfs.conf [nfsd] rdma=y ---
if ! grep -qE '^\s*rdma\s*=\s*y' /etc/nfs.conf; then
  echo "==> backing up /etc/nfs.conf and enabling [nfsd] rdma=y"
  sudo cp -n /etc/nfs.conf "/etc/nfs.conf.bak.$(date +%Y%m%d-%H%M%S)"
  if grep -qE '^\[nfsd\]' /etc/nfs.conf; then
    sudo sed -i '/^\[nfsd\]/a rdma=y\nrdma-port='"$NFS_RDMA_PORT" /etc/nfs.conf
  else
    printf '\n[nfsd]\nrdma=y\nrdma-port=%s\n' "$NFS_RDMA_PORT" | sudo tee -a /etc/nfs.conf > /dev/null
  fi
else
  echo "==> /etc/nfs.conf already has rdma=y (skipping)"
fi

echo "==> applying exports + restarting nfs-server"
sudo exportfs -ra
sudo systemctl restart nfs-server

# --- Verification ---
echo "==> verifying /proc/fs/nfsd/portlist"
PORTLIST="$(cat /proc/fs/nfsd/portlist)"
echo "$PORTLIST"
if ! echo "$PORTLIST" | grep -q "rdma $NFS_RDMA_PORT"; then
  echo "ERROR: portlist missing 'rdma $NFS_RDMA_PORT'." >&2
  echo "Fallback: 'echo \"rdma $NFS_RDMA_PORT\" | sudo tee /proc/fs/nfsd/portlist' (non-persistent)." >&2
  exit 2
fi

echo "==> setup-nfsordma-server.sh complete."
echo "    Export: $NFS_EXPORT_PATH on $HOST1_QSFP_IP via rdma:$NFS_RDMA_PORT"
echo "    Next: run setup-nfsordma-client.sh on host 2."
