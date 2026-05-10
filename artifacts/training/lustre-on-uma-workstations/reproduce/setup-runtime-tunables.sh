#!/usr/bin/env bash
# setup-runtime-tunables.sh — Lustre runtime knobs + persistence via MGS config log.
# Run on BOTH hosts after both OSTs are up.
#
# Knobs (the second + third members of the load-bearing trio for this stack;
# the first, primarycache=metadata, is a ZFS dataset property already applied
# by setup-host{1,2}-ost*.sh):
#
#   obdfilter.<fs>-OST*.brw_size=4         # OST-side, raises BRW chunk to 4 MiB
#   osc.<fs>-OST*.max_rpcs_in_flight=32    # client-side, deepens RPC pipeline
#   osc.<fs>-OST*.checksums=0              # client-side, drops per-RPC checksum cost
#
# Both `set_param` (runtime, this host) and `set_param -P` (persistent, written
# to MGS config log, applies cluster-wide on every mount) are issued. Without
# the -P pass, runtime knobs reset to defaults on every remount — confirmed bug
# during the 014 canonical run.

set -euo pipefail

LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"

echo "==> setup-runtime-tunables.sh on $(hostname) — fsname=${LUSTRE_FSNAME}"

# --- Runtime (immediate) ---
echo "==> runtime: obdfilter brw_size + osc max_rpcs_in_flight + osc checksums"
sudo lctl set_param "obdfilter.${LUSTRE_FSNAME}-OST*.brw_size=4" 2>/dev/null || true   # only effective if this host runs an OST
sudo lctl set_param "osc.${LUSTRE_FSNAME}-OST*.max_rpcs_in_flight=32"
sudo lctl set_param "osc.${LUSTRE_FSNAME}-OST*.checksums=0"

# --- Persistent (-P writes to MGS config log; cluster-wide; applies on every mount) ---
# Idempotent — running on both hosts is benign (same values).
echo "==> persistent: lctl set_param -P"
sudo lctl set_param -P "obdfilter.${LUSTRE_FSNAME}-OST*.brw_size=4"
sudo lctl set_param -P "osc.${LUSTRE_FSNAME}-OST*.max_rpcs_in_flight=32"
sudo lctl set_param -P "osc.${LUSTRE_FSNAME}-OST*.checksums=0"

# --- Verify runtime values ---
echo "==> verify runtime values:"
sudo lctl get_param "obdfilter.${LUSTRE_FSNAME}-OST*.brw_size" 2>/dev/null || true
sudo lctl get_param "osc.${LUSTRE_FSNAME}-OST*.max_rpcs_in_flight"
sudo lctl get_param "osc.${LUSTRE_FSNAME}-OST*.checksums"

echo "==> setup-runtime-tunables.sh OK on $(hostname)"
