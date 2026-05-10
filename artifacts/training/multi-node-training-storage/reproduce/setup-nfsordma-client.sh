#!/usr/bin/env bash
# setup-nfsordma-client.sh — general-purpose NFSoRDMA client-side mount.
#
# Mounts an NFS export from a server with proto=rdma (port 20049), idempotently:
# package install, persistent rpcrdma module load, mount-point creation, NFSoRDMA mount.
#
# Verifies the transport via /proc/mounts (catches silent-TCP-fallback) and a 1 GiB
# dd smoke. Full bandwidth characterization (fio) is out of scope.
#
# Standalone usage (without the rest of the kit):
#   HOST1_QSFP_IP=10.0.0.1 \
#   NFS_EXPORT_PATH=/srv/nfs/myshare \
#   NFS_MOUNT_PATH=/mnt/myshare \
#   ./setup-nfsordma-client.sh
#
# Run AFTER setup-nfsordma-server.sh on the server host (with matching HOST1_QSFP_IP
# and NFS_EXPORT_PATH).

set -euo pipefail

# --- Tunables (env-var overridable) ---
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
NFS_EXPORT_PATH="${NFS_EXPORT_PATH:-/srv/nfs/multi-node-storage}"
NFS_MOUNT_PATH="${NFS_MOUNT_PATH:-/mnt/multi-node-storage}"
NFS_RDMA_PORT="${NFS_RDMA_PORT:-20049}"

echo "==> setup-nfsordma-client.sh on $(hostname)"

# --- Packages + rpcrdma module loaded persistently ---
echo "==> installing nfs + rdma packages (idempotent)"
sudo apt-get update -qq
sudo apt-get install -y -qq nfs-common rdma-core

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

# --- Mount point + NFSoRDMA mount ---
echo "==> preparing mount dir at $NFS_MOUNT_PATH"
sudo mkdir -p "$NFS_MOUNT_PATH"

if mount | grep -q " on $NFS_MOUNT_PATH "; then
  echo "==> $NFS_MOUNT_PATH already mounted (skipping mount)"
else
  echo "==> mounting NFSoRDMA from $HOST1_QSFP_IP:$NFS_EXPORT_PATH"
  sudo mount -t nfs \
    -o "rdma,port=$NFS_RDMA_PORT,vers=4.2,rsize=1048576,wsize=1048576,hard,timeo=600" \
    "$HOST1_QSFP_IP:$NFS_EXPORT_PATH" \
    "$NFS_MOUNT_PATH"
fi

# --- Verification ---
echo "==> verifying transport is rdma (not silent-tcp-fallback)"
if ! grep -F "$NFS_MOUNT_PATH" /proc/mounts | grep -q 'proto=rdma'; then
  echo "ERROR: $NFS_MOUNT_PATH mount is not using proto=rdma." >&2
  echo "Check: grep '$NFS_MOUNT_PATH' /proc/mounts" >&2
  exit 2
fi

echo "==> dd 1 GiB smoke write (smoke only, not a benchmark)"
sudo dd if=/dev/zero of="$NFS_MOUNT_PATH/.smoke.bin" bs=1M count=1024 \
  conv=fdatasync status=progress
sudo rm -f "$NFS_MOUNT_PATH/.smoke.bin"

echo "==> setup-nfsordma-client.sh complete."
echo "    Mount: $NFS_MOUNT_PATH proto=rdma vers=4.2"
echo "    Next: run-single-node.sh on host 1 to start the comparison."
