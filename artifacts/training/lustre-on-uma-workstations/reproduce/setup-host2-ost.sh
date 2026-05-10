#!/usr/bin/env bash
# setup-host2-ost.sh — add OST0001 on host 2 to host 1's MGS over o2ib0.
# Plus client mount on /mnt/lustre. Run on host 2 only after setup-host1-osts.sh
# completes successfully on host 1.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"
OST_IMG_SIZE="${OST_IMG_SIZE:-600G}"
ZFS_ARC_MAX_BYTES="${ZFS_ARC_MAX_BYTES:-8589934592}"

POOL_DIR="/var/lib/lustre-pools"
MGS_NID="${HOST1_QSFP_IP}@o2ib0"

echo "==> setup-host2-ost.sh on $(hostname) — connecting to MGS at ${MGS_NID}"

# --- Pre-condition: host 1's MGS must be reachable over o2ib0 ---
if ! sudo lctl ping "${MGS_NID}" >/dev/null 2>&1; then
    echo "ERROR: cannot ping MGS at ${MGS_NID}. Confirm setup-host1-osts.sh completed and setup-lnet.sh ran on both hosts."
    exit 1
fi

# --- ARC cap: persistent + immediate ---
echo "==> applying ZFS ARC cap (${ZFS_ARC_MAX_BYTES} bytes)"
echo "options zfs zfs_arc_max=${ZFS_ARC_MAX_BYTES}" | sudo tee /etc/modprobe.d/zfs-arc.conf > /dev/null
echo "${ZFS_ARC_MAX_BYTES}" | sudo tee /sys/module/zfs/parameters/zfs_arc_max > /dev/null
echo "  zfs_arc_max=$(cat /sys/module/zfs/parameters/zfs_arc_max)"

# --- Pre-allocate OST1 image ---
sudo mkdir -p "${POOL_DIR}"
if [ ! -f "${POOL_DIR}/ost1.img" ]; then
    echo "==> fallocate -l ${OST_IMG_SIZE} ${POOL_DIR}/ost1.img"
    sudo fallocate -l "${OST_IMG_SIZE}" "${POOL_DIR}/ost1.img"
fi
ls -lh "${POOL_DIR}/ost1.img"

# --- zpool ost1-pool ---
if ! sudo zpool list ost1-pool >/dev/null 2>&1; then
    echo "==> zpool create ost1-pool"
    sudo zpool create -f ost1-pool "${POOL_DIR}/ost1.img"
fi
sudo zpool list ost1-pool

# --- mkfs.lustre OST0001 ---
if ! sudo zfs list -H -o name ost1-pool/ost1 2>/dev/null | grep -q "ost1-pool/ost1"; then
    echo "==> mkfs.lustre OST0001"
    sudo mkfs.lustre --ost --fsname="${LUSTRE_FSNAME}" --index=1 \
        --mgsnode="${MGS_NID}" --backfstype=zfs ost1-pool/ost1
fi

# --- Mount OST0001 (registers with host 1 MGS over o2ib0) + client mount ---
sudo mkdir -p /mnt/ost1 /mnt/lustre
mount | grep -qE "^ost1-pool/ost1 on /mnt/ost1 " || sudo mount -t lustre ost1-pool/ost1 /mnt/ost1
mount | grep -qE "^${MGS_NID}:/${LUSTRE_FSNAME} on /mnt/lustre " || \
    sudo mount -t lustre "${MGS_NID}:/${LUSTRE_FSNAME}" /mnt/lustre
sudo chown "$USER:$USER" /mnt/lustre

# --- ZFS knob trio (persistent dataset properties) ---
echo "==> applying ZFS knob trio on ost1-pool/ost1"
sudo zfs set primarycache=metadata ost1-pool/ost1
sudo zfs set atime=off ost1-pool ost1-pool/ost1
sudo zfs get primarycache,atime,recordsize,compression ost1-pool/ost1

# --- Verify both OSTs ACTIVE ---
echo "==> lfs osts /mnt/lustre"
lfs osts /mnt/lustre
echo "==> lfs df -h /mnt/lustre"
lfs df -h /mnt/lustre

echo "==> setup-host2-ost.sh OK on $(hostname)"
echo "    Next: setup-runtime-tunables.sh on BOTH hosts before running any measurement."
