#!/usr/bin/env bash
# setup-host1-osts.sh — bring up MGS + MDT0000 + OST0000 on host 1.
#
# Persistent ARC cap (load-bearing) + fallocate image files + zpool create +
# mkfs.lustre with --mgsnode=$HOST1_QSFP_IP@o2ib0 (skips later tunefs --writeconf
# step) + mount in order + ZFS knob trio applied on the OST dataset.
# Run on host 1 only.
#
# Idempotent: skips zpool/mkfs/mount steps if state already exists.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
HOST1_QSFP_IP="${HOST1_QSFP_IP:-169.254.188.115}"
LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"
OST_IMG_SIZE="${OST_IMG_SIZE:-600G}"
MDT_IMG_SIZE="${MDT_IMG_SIZE:-50G}"
MGS_IMG_SIZE="${MGS_IMG_SIZE:-2G}"
ZFS_ARC_MAX_BYTES="${ZFS_ARC_MAX_BYTES:-8589934592}"

POOL_DIR="/var/lib/lustre-pools"
MGS_NID="${HOST1_QSFP_IP}@o2ib0"

echo "==> setup-host1-osts.sh on $(hostname) — MGS NID will be ${MGS_NID}"

# --- ARC cap: persistent (modprobe.d) + immediate (sysfs). ---
echo "==> applying ZFS ARC cap (${ZFS_ARC_MAX_BYTES} bytes)"
echo "options zfs zfs_arc_max=${ZFS_ARC_MAX_BYTES}" | sudo tee /etc/modprobe.d/zfs-arc.conf > /dev/null
echo "${ZFS_ARC_MAX_BYTES}" | sudo tee /sys/module/zfs/parameters/zfs_arc_max > /dev/null
echo "  zfs_arc_max=$(cat /sys/module/zfs/parameters/zfs_arc_max)"

# --- Pre-allocate image files (fallocate, NOT truncate; sparse extension is a real cost) ---
sudo mkdir -p "${POOL_DIR}"
for pair in "mgs.img:${MGS_IMG_SIZE}" "mdt0.img:${MDT_IMG_SIZE}" "ost0.img:${OST_IMG_SIZE}"; do
    f="${pair%%:*}"; sz="${pair##*:}"
    if [ ! -f "${POOL_DIR}/${f}" ]; then
        echo "==> fallocate -l ${sz} ${POOL_DIR}/${f}"
        sudo fallocate -l "${sz}" "${POOL_DIR}/${f}"
    fi
done
ls -lh "${POOL_DIR}"/*.img

# --- zpools: create if missing ---
for p in mgs mdt0 ost0; do
    if ! sudo zpool list "${p}-pool" >/dev/null 2>&1; then
        echo "==> zpool create ${p}-pool"
        sudo zpool create -f "${p}-pool" "${POOL_DIR}/${p}.img"
    fi
done
sudo zpool list

# --- mkfs.lustre: format if dataset is fresh ---
mkfs_if_needed() {
    local target=$1 args=$2
    if ! sudo zfs list -H -o name "${target}" 2>/dev/null | grep -q "${target}"; then
        echo "==> mkfs.lustre ${target}"
        # shellcheck disable=SC2086
        sudo mkfs.lustre ${args} --backfstype=zfs "${target}"
    else
        echo "==> ${target} already formatted, skip"
    fi
}
mkfs_if_needed "mgs-pool/mgs"   "--mgs --servicenode=${MGS_NID}"
mkfs_if_needed "mdt0-pool/mdt0" "--mdt --fsname=${LUSTRE_FSNAME} --index=0 --mgsnode=${MGS_NID}"
mkfs_if_needed "ost0-pool/ost0" "--ost --fsname=${LUSTRE_FSNAME} --index=0 --mgsnode=${MGS_NID}"

# --- Mount: mgt -> mdt0 -> ost0 -> client ---
sudo mkdir -p /mnt/mgt /mnt/mdt0 /mnt/ost0 /mnt/lustre
mount_if_needed() {
    local dev=$1 mp=$2
    if ! mount | grep -qE "^${dev//\//\\/} on ${mp} "; then
        echo "==> mount ${dev} -> ${mp}"
        sudo mount -t lustre "${dev}" "${mp}"
    fi
}
mount_if_needed "mgs-pool/mgs"   "/mnt/mgt"
mount_if_needed "mdt0-pool/mdt0" "/mnt/mdt0"
mount_if_needed "ost0-pool/ost0" "/mnt/ost0"
mount_if_needed "${MGS_NID}:/${LUSTRE_FSNAME}" "/mnt/lustre"

sudo chown "$USER:$USER" /mnt/lustre

# --- ZFS knob trio (persistent dataset properties) ---
echo "==> applying ZFS knob trio on ost0-pool/ost0"
sudo zfs set primarycache=metadata ost0-pool/ost0
sudo zfs set atime=off ost0-pool ost0-pool/ost0
sudo zfs set atime=off mdt0-pool mdt0-pool/mdt0
sudo zfs set atime=off mgs-pool mgs-pool/mgs
sudo zfs get primarycache,atime,recordsize,compression ost0-pool/ost0

# --- Verify ---
echo "==> mount | grep lustre"
mount | grep lustre
echo "==> lfs df -h /mnt/lustre"
lfs df -h /mnt/lustre

echo "==> setup-host1-osts.sh OK on $(hostname)"
echo "    Next: setup-host2-ost.sh on host 2 to add OST0001 to the same MGS."
