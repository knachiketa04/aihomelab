#!/usr/bin/env bash
# setup-lnet.sh — configure LNet o2ib0 on the RDMA NIC. Run on BOTH hosts.
#
# Auto-detects tcp on the management interface (lnetctl lnet configure does
# this automatically); adds o2ib0 on $RDMA_IFACE. After both hosts run this
# successfully, verify with:
#   sudo lctl ping <peer-NID>@o2ib0
#
# Standalone usage:
#   RDMA_IFACE=enp1s0f0np0 ./setup-lnet.sh

set -euo pipefail

RDMA_IFACE="${RDMA_IFACE:-enp1s0f0np0}"

echo "==> setup-lnet.sh on $(hostname) — RDMA_IFACE=${RDMA_IFACE}"

# --- Verify the RDMA NIC is up ---
if ! ip -br link show dev "${RDMA_IFACE}" 2>/dev/null | grep -q UP; then
    echo "ERROR: ${RDMA_IFACE} is not UP. Check: ip -br link show"
    exit 1
fi

# --- LNet base configure (auto-detects tcp on management interface) ---
# Idempotent — running on already-configured LNet is a no-op.
echo "==> lnetctl lnet configure"
sudo lnetctl lnet configure 2>/dev/null || true

# --- Add o2ib0 if not already present ---
if sudo lnetctl net show 2>/dev/null | grep -q 'net type: o2ib$'; then
    echo "==> o2ib already configured; skipping add"
else
    echo "==> lnetctl net add --net o2ib0 --if ${RDMA_IFACE}"
    sudo lnetctl net add --net o2ib0 --if "${RDMA_IFACE}"
fi

# --- Verify ---
echo "==> lnetctl net show"
sudo lnetctl net show

# Extract this host's o2ib NID for the "next step" hint.
LOCAL_O2IB_NID=$(sudo lnetctl net show 2>/dev/null | awk '/net type: o2ib$/{getline;getline;print $3}')
echo
echo "==> setup-lnet.sh OK on $(hostname) — local o2ib NID: ${LOCAL_O2IB_NID:-<not detected>}"
echo "    After both hosts complete, verify cross-node with:"
echo "      sudo lctl ping <peer-o2ib-NID>@o2ib0"
