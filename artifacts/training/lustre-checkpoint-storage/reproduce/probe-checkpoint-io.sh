#!/usr/bin/env bash
# probe-checkpoint-io.sh — client-vs-storage attribution probe for checkpoint I/O on a
# Lustre-on-ZFS client.
#
# WHY THIS EXISTS (load-bearing): on a file-backed-zpool (.img-on-ext4) Lustre OST, the
# single-writer checkpoint rate (~0.78 GB/s) sits dead-center in this lab's OWN published
# file-backed-zpool substrate band. So `iostat %util` on the NVMe CANNOT separate
# "the single DCP writer is the cap (client-bound)" from "the ZFS/.img substrate is the cap
# (storage-bound)" — the two are observationally degenerate from %util alone.
#
# This probe captures the signals that DO adjudicate:
#   - per-thread CPU (pidstat -t): a single python/torch thread pegged ~100% on one core =>
#     client-side serialization. Hot z_wr_iss/txg_sync => ZFS. Hot ptlrpcd/kiblnd => transport.
#   - ZFS txg kstat (/proc/spl/kstat/zfs/<pool>/txgs): ndirty (dirty bytes per txg) and stime
#     (sync duration). ndirty near the dirty-data cap + long stime => ZFS backpressure (the
#     throttle iostat cannot see). ndirty ~1% of cap + short stime => storage idle.
#   - lctl OST byte counters (obdfilter.*.stats write_bytes/read_bytes): exact per-OST bytes,
#     for the ~50/50 stripe split and effective GB/s.
#   - iostat -x: disk %util / w_await as the coarse (but degenerate-on-its-own) view.
#
# USAGE (sourced by the run scripts):
#   source probe-checkpoint-io.sh
#   probe_start  <capture_dir> <host_tag> <ost_pool> <ost_index>   # backgrounds the samplers
#   ... run the checkpoint workload ...
#   probe_lctl_snapshot <capture_dir> <ost_index> before|after     # bracket the run
#   probe_stop                                                      # kill the samplers
#
# Standalone usage (probe any checkpoint/IO workload on a Lustre-on-ZFS client):
#   source probe-checkpoint-io.sh
#   probe_start /tmp/probe spark01 ost0-pool 0000
#   <your workload>
#   probe_stop
#
# Tunables (env): NVME_DEVICE (default nvme0n1), LUSTRE_FSNAME (default lustrefs),
#                 PROBE_INTERVAL (default 2 sec).

NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"
LUSTRE_FSNAME="${LUSTRE_FSNAME:-lustrefs}"
PROBE_INTERVAL="${PROBE_INTERVAL:-2}"

# Broad -C regex: python/pt_* (app serialization, PyTorch names threads pt_main_thread/pt_*),
# z_wr/z_rd/txg_sync (ZFS), ptlrpc/kiblnd/ll_ost/spl_ (Lustre client RPC + transport).
_PROBE_CPU_REGEX='python|pt_|torch|z_wr|z_rd|txg_sync|ptlrpc|kiblnd|ll_ost|spl_'

# PIDs of the backgrounded samplers (set by probe_start, killed by probe_stop).
_PROBE_PIDS=()

probe_start() {
  local cap_dir="$1" host_tag="$2" ost_pool="$3" _ost_index="$4"
  mkdir -p "$cap_dir"

  echo "==> probe_start on ${host_tag}: pidstat(cpu+io) + iostat + ZFS txgs(${ost_pool})"

  # per-thread CPU (the client-serialization signal)
  pidstat -t -u -C "$_PROBE_CPU_REGEX" "$PROBE_INTERVAL" \
    > "${cap_dir}/pidstat-cpu-${host_tag}.log" 2>&1 &
  _PROBE_PIDS+=("$!")

  # per-process logical write rate (block-layer kB_wr reads low on a networked FS; wchar is
  # the app-side write signal)
  pidstat -d "$PROBE_INTERVAL" \
    > "${cap_dir}/pidstat-io-${host_tag}.log" 2>&1 &
  _PROBE_PIDS+=("$!")

  # ZFS txg sync durations + dirty-data (the backpressure iostat misses)
  if [ -r "/proc/spl/kstat/zfs/${ost_pool}/txgs" ]; then
    bash -c '
      while true; do
        date +%H:%M:%S
        tail -6 "/proc/spl/kstat/zfs/'"${ost_pool}"'/txgs"
        echo ---
        sleep '"$PROBE_INTERVAL"'
      done' > "${cap_dir}/txgs-${host_tag}.log" 2>&1 &
    _PROBE_PIDS+=("$!")
  else
    echo "  WARNING: /proc/spl/kstat/zfs/${ost_pool}/txgs not readable — wrong OST0_POOL/OST1_POOL?" >&2
  fi

  # coarse disk view (degenerate on its own; kept for cross-reference + the %util band)
  iostat -t -dxm "$PROBE_INTERVAL" "$NVME_DEVICE" \
    > "${cap_dir}/iostat-${host_tag}.log" 2>&1 &
  _PROBE_PIDS+=("$!")

  # host-clock launch marker — container train log is in CONTAINER clock; the host samplers
  # are in HOST clock. Capture the host launch ts so the checkpoint window can be mapped.
  date +%s.%N | tee "${cap_dir}/probe-launch-${host_tag}.ts" >/dev/null
}

# Snapshot the OST byte counters (write_bytes / read_bytes) before and after the run; the
# delta is the exact bytes per OST (the 50/50 stripe split + effective GB/s).
probe_lctl_snapshot() {
  local cap_dir="$1" ost_index="$2" when="$3"
  mkdir -p "$cap_dir"
  sudo lctl get_param "obdfilter.${LUSTRE_FSNAME}-OST${ost_index}.stats" \
    > "${cap_dir}/lctl-OST${ost_index}-${when}.txt" 2>&1 || \
    echo "  WARNING: could not read obdfilter.${LUSTRE_FSNAME}-OST${ost_index}.stats" >&2
}

probe_stop() {
  echo "==> probe_stop: killing ${#_PROBE_PIDS[@]} samplers"
  local pid
  for pid in "${_PROBE_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  _PROBE_PIDS=()
}

# Allow running standalone as a quick one-shot demo: `probe-checkpoint-io.sh /tmp/probe tag pool 0000`
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  if [ $# -lt 4 ]; then
    echo "sourced helper. standalone demo: $0 <capture_dir> <host_tag> <ost_pool> <ost_index>" >&2
    exit 2
  fi
  probe_start "$1" "$2" "$3" "$4"
  echo "samplers running; press Ctrl-C to stop." >&2
  trap probe_stop EXIT
  while true; do command sleep "$PROBE_INTERVAL" 2>/dev/null || break; done
fi
