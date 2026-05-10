#!/usr/bin/env bash
# setup-lustre-build.sh — Lustre master tip from source (server + client), idempotent.
#
# Build deps + git clone + checkout pinned commit + autogen + configure + make +
# install + depmod + module-load verification. Run on BOTH hosts after
# setup-zfs-build.sh has produced ${BUILD_ROOT}/zfs/Module.symvers.
#
# Encodes the four Lustre-build workarounds discovered during 014:
#   1. Pin master commit (release tag 2.16.1 doesn't build on kernel 6.x)
#   2. --with-zfs=${BUILD_ROOT}/zfs so osd-zfs links against our OpenZFS 2.4
#   3. --with-o2ib=<kernel-headers> bypasses --with-o2ib=yes readlink-script bug
#   4. --disable-ldiskfs (deprecated --without-ldiskfs alias also works)
#
# Standalone usage:
#   BUILD_ROOT=/path LUSTRE_COMMIT=<sha> ZFS_SRC=/path/to/zfs ./setup-lustre-build.sh

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
BUILD_ROOT="${BUILD_ROOT:-${EXP_ROOT}/build}"
LUSTRE_COMMIT="${LUSTRE_COMMIT:-805cece6747f442449f32a1d25a8b8a03b230875}"
ZFS_SRC="${ZFS_SRC:-${BUILD_ROOT}/zfs}"

echo "==> setup-lustre-build.sh on $(hostname) — kernel $(uname -r)"

# --- Pre-condition: ZFS source must have Module.symvers (see setup-zfs-build.sh) ---
if [ ! -e "${ZFS_SRC}/Module.symvers" ]; then
    echo "ERROR: ${ZFS_SRC}/Module.symvers missing. Run setup-zfs-build.sh first."
    exit 1
fi

# --- Lustre-specific deps. Ubuntu has no rdma-core-dev meta-package — must
#     enumerate sub-packages explicitly (libibverbs-dev libibumad-dev librdmacm-dev). ---
echo "==> apt install Lustre build deps"
sudo apt-get install -y -qq \
    libreadline-dev libyaml-dev libnl-3-dev libnl-genl-3-dev \
    libssl-dev libelf-dev libmount-dev libsnmp-dev \
    libibverbs-dev libibumad-dev librdmacm-dev ibverbs-utils \
    swig dwarves

# --- Source clone (skip if present). GitHub mirror; Whamcloud git is authoritative
#     but has been intermittently 403 for unauthenticated reads. ---
LUSTRE_SRC="${BUILD_ROOT}/lustre-release"
if [ ! -d "${LUSTRE_SRC}/.git" ]; then
    echo "==> git clone Lustre"
    git clone https://github.com/lustre/lustre-release.git "${LUSTRE_SRC}"
fi
cd "${LUSTRE_SRC}"
echo "==> git checkout ${LUSTRE_COMMIT}"
git fetch --quiet
git checkout "${LUSTRE_COMMIT}" --quiet
git log -1 --oneline

# --- autogen + configure + build ---
if [ ! -x configure ]; then
    echo "==> autogen.sh"
    sh autogen.sh 2>&1 | tail -5
fi

echo "==> configure (server + client + zfs at ${ZFS_SRC})"
./configure \
    --with-linux=/lib/modules/$(uname -r)/build \
    --with-o2ib=/lib/modules/$(uname -r)/build \
    --with-zfs="${ZFS_SRC}" \
    --enable-server \
    --enable-client \
    --disable-ldiskfs \
    --disable-tests \
    2>&1 | tail -10

# Sanity-grep the configure summary for the four critical signals:
CFG_LOG="${LUSTRE_SRC}/config.log"
if ! grep -qE "(checking whether to build Lustre client support... yes|enable_client.*yes)" "${CFG_LOG}" 2>/dev/null; then
    echo "WARNING: configure may not have enabled client; check config.log"
fi
if ! grep -qE "(checking whether to build Lustre server support... yes|enable_server.*yes)" "${CFG_LOG}" 2>/dev/null; then
    echo "WARNING: configure may not have enabled server; check config.log"
fi

echo "==> make -s -j$(nproc)"
make -s -j"$(nproc)" 2>&1 | tail -5

# --- Install + depmod ---
echo "==> sudo make install"
sudo make install 2>&1 | tail -3
sudo depmod -a

# --- Module-load verification ---
echo "==> modprobe lnet lustre osd_zfs"
sudo modprobe lnet
sudo modprobe lustre
sudo modprobe osd_zfs

lsmod | grep -E '^(lustre|osd_zfs|lnet|libcfs|ptlrpc|obdclass|mdt|mgs|ofd|mdd|lod)' | awk '{print $1, $2}'
lctl --version

echo "==> setup-lustre-build.sh OK on $(hostname)"
