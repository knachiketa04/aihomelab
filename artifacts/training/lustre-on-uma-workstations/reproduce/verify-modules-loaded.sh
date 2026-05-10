#!/usr/bin/env bash
# verify-modules-loaded.sh — post-Secure-Boot-trip verification.
#
# Confirms zfs / spl / lnet / lustre / osd_zfs all load and userland tools
# (zpool, lctl) report expected versions. Run on BOTH hosts after the
# Secure Boot disable trip.
#
# Standalone usage: ./verify-modules-loaded.sh

set -euo pipefail

echo "==> verify-modules-loaded.sh on $(hostname) — kernel $(uname -r)"

# --- Lockdown state — should be [none] after the SB trip ---
LOCKDOWN=$(cat /sys/kernel/security/lockdown)
echo "lockdown: ${LOCKDOWN}"
if [[ "${LOCKDOWN}" != *"[none]"* ]]; then
    echo "WARNING: lockdown is not [none]. modprobe may fail. See setup-secure-boot-trip.md."
fi
sudo mokutil --sb-state

# --- Load modules in dependency order ---
echo "==> modprobe zfs lnet lustre osd_zfs"
sudo modprobe zfs
sudo modprobe lnet
sudo modprobe lustre
sudo modprobe osd_zfs

# --- Verify ---
echo "==> loaded modules:"
lsmod | grep -E '^(lustre|osd_zfs|zfs|spl|lnet|libcfs|ko2iblnd|ksocklnd|obdclass|ptlrpc|mdt|mgs|ofd|mdd|lod|osp|lfsck|quota|obdfilter)' | awk '{print $1, $2}'

echo "==> versions:"
zpool version
lctl --version

echo "==> verify-modules-loaded.sh OK on $(hostname)"
