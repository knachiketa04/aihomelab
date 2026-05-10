#!/usr/bin/env bash
# setup-zfs-build.sh — OpenZFS 2.4.1 from source, idempotent.
#
# Build deps + git clone + autogen + configure + make + install + depmod +
# Module.symvers symlink workaround + ldconfig + module-load verification.
#
# Run on BOTH hosts. The Module.symvers symlink (module/Module.symvers ->
# Module.symvers at source root) is the load-bearing workaround for OpenZFS
# 2.4's build-tree reorg vs Lustre's KBUILD_EXTRA_SYMBOLS path expectation —
# without it, the subsequent Lustre osd-zfs build silently produces an
# unresolvable osd_zfs.ko.
#
# Standalone usage (without the rest of the kit):
#   BUILD_ROOT=/path/to/build ZFS_TAG=zfs-2.4.1 ./setup-zfs-build.sh

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
BUILD_ROOT="${BUILD_ROOT:-${EXP_ROOT}/build}"
ZFS_TAG="${ZFS_TAG:-zfs-2.4.1}"

echo "==> setup-zfs-build.sh on $(hostname) — kernel $(uname -r)"

# --- Deps (idempotent; apt install -y is benign on already-installed packages) ---
echo "==> apt install build deps"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    build-essential autoconf automake libtool pkg-config kmod \
    gawk alien fakeroot git \
    zlib1g-dev uuid-dev libblkid-dev libudev-dev libdevmapper-dev \
    libtirpc-dev libelf-dev libssl-dev \
    python3-dev python3-cffi python3-setuptools python3-packaging

# --- Source clone (skip if already present) ---
mkdir -p "${BUILD_ROOT}"
ZFS_SRC="${BUILD_ROOT}/zfs"
if [ ! -d "${ZFS_SRC}/.git" ]; then
    echo "==> git clone OpenZFS"
    git clone https://github.com/openzfs/zfs.git "${ZFS_SRC}"
fi
cd "${ZFS_SRC}"
echo "==> git checkout ${ZFS_TAG}"
git fetch --tags --quiet
git checkout "${ZFS_TAG}" --quiet
git log -1 --oneline

# --- autogen + configure + build (always re-run; idempotent against current source state) ---
if [ ! -x configure ]; then
    echo "==> autogen.sh"
    sh autogen.sh 2>&1 | tail -5
fi

echo "==> configure (--with-linux=/lib/modules/$(uname -r)/build)"
./configure --with-linux=/lib/modules/$(uname -r)/build 2>&1 | tail -10

echo "==> make -s -j$(nproc)"
make -s -j"$(nproc)" 2>&1 | tail -5

# --- Install + depmod + ldconfig ---
echo "==> sudo make install"
sudo make install 2>&1 | tail -3
sudo depmod -a
sudo ldconfig

# --- The load-bearing workaround: Module.symvers symlink for OpenZFS 2.4 build-tree reorg ---
# Lustre's KBUILD_EXTRA_SYMBOLS expects Module.symvers at the ZFS source root;
# OpenZFS 2.4+ moved it under module/. Symlink papers over the path mismatch.
if [ ! -e "${ZFS_SRC}/Module.symvers" ] && [ -f "${ZFS_SRC}/module/Module.symvers" ]; then
    ln -sf module/Module.symvers "${ZFS_SRC}/Module.symvers"
fi
ls -la "${ZFS_SRC}/Module.symvers"

# --- Module-load verification ---
echo "==> modprobe zfs"
if ! sudo modprobe zfs 2>/dev/null; then
    cat <<'EOF'
ERROR: modprobe zfs failed. Most likely cause: Secure Boot is enabled and the
locally-built (unsigned) module was rejected by the kernel signature check.
See setup-secure-boot-trip.md and re-run this script after disabling Secure
Boot, OR enroll a MOK and sign the modules.
EOF
    exit 1
fi

lsmod | grep -E '^(zfs|spl)' | awk '{print $1, $2}'
zpool version

echo "==> setup-zfs-build.sh OK on $(hostname)"
