#!/usr/bin/env bash
# AIHomeLab Lustre cluster orchestrator.
#
# Laptop-side driver for the two-node Spark Lustre cluster.
# Replaces the lab's manual Phase 1-5 walkthrough
# with a single command, while preserving the same canonical sequence.
#
# Cluster shape (hardcoded — this script is specific to this lab):
#   spark01: MGS + MDT0000 + OST0000 + client      (mgs-pool, mdt0-pool, ost0-pool)
#   spark02: OST0001 + client                       (ost1-pool)
#   fsname:  spark014
#   MGS NID: 169.254.188.115@o2ib  (spark01 over QSFP RDMA)
#
# Subcommands:
#   bringup                    Phase 1-5 across both nodes, idempotent.
#   teardown                   Reverse-order clean unmount + pool export.
#   status                     Quick health snapshot (no changes).
#   --install-sudoers <node>   Interactive bootstrap of the broader Lustre
#                              NOPASSWD sudoers entry on the given node.
#                              Required once per node before bringup/teardown
#                              can run unattended from the laptop.
#   --help                     This message.
#
# Recovery handling: bringup defaults to --on-recovery=wait (up to 900s).
# To abort a target stuck in recovery instead, re-run bringup with:
#   bash lustre-cluster.sh bringup --abort-recovery <target>
# e.g. --abort-recovery spark014-MDT0000

set -uo pipefail

NODE1_SSH="${NODE1_SSH:-sparks@192.168.20.21}"
NODE2_SSH="${NODE2_SSH:-sparks@192.168.20.22}"

# MGS NID — spark01 over the QSFP RDMA fabric. Both clients mount via this.
MGS_NID="${MGS_NID:-169.254.188.115@o2ib}"
FSNAME="${FSNAME:-spark014}"
LNET_IF="${LNET_IF:-enp1s0f0np0}"
POOL_DIR="${POOL_DIR:-/var/lib/lustre-pools}"

RECOVERY_TIMEOUT_S="${RECOVERY_TIMEOUT_S:-900}"

# Targets stuck in recovery to abort during bringup (space-separated).
# Populated by --abort-recovery flag on the laptop side.
ABORT_TARGETS=""

SUDOERS_PATH="/etc/sudoers.d/aihomelab-lustre"
# ZFS on these nodes is built from source and installs to /usr/local/sbin/.
# Lustre userland (lctl, lnetctl, lustre_rmmod) installs to /usr/sbin/.
# Both /usr/sbin/ and /usr/local/sbin/ are listed for zpool/zfs so the rule
# survives a future package-managed reinstall to /usr/sbin/.
read -r -d '' SUDOERS_CONTENT <<'EOF' || true
# /etc/sudoers.d/aihomelab-lustre
# AIHomeLab: NOPASSWD for Lustre cluster bring-up/teardown automation.
# Scope is intentionally broad — these commands together can mount, unmount,
# import/export zpools, and load/unload kernel modules. Justified because
# this is a personal home lab where the sparks user already owns the cluster.
Cmnd_Alias AIHL_LUSTRE_BRINGUP = \
  /usr/local/sbin/zpool, \
  /usr/local/sbin/zfs, \
  /usr/sbin/zpool, \
  /usr/sbin/zfs, \
  /usr/sbin/modprobe, \
  /usr/sbin/lustre_rmmod, \
  /usr/sbin/lnetctl, \
  /usr/sbin/lctl, \
  /usr/bin/mount, \
  /usr/bin/umount, \
  /usr/bin/dmesg
%sudo ALL=(root) NOPASSWD: AIHL_LUSTRE_BRINGUP
EOF

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

# ─── status tracking ───────────────────────────────────────────────────────
WORST_STATUS=0
bump_status() {
  case "$1" in
    ok)   ;;
    warn) [[ $WORST_STATUS -lt 1 ]] && WORST_STATUS=1 ;;
    red)  WORST_STATUS=2 ;;
  esac
}

emit() {
  # PHASE=<n> NODE=<host> ACTION=<name> STATUS=<ok|warn|red> DETAIL=<...>
  local phase="$1" node="$2" action="$3" status="$4" detail="$5"
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' \
    "$phase" "$node" "$action" "$status" "$detail"
  bump_status "$status"
}

relay() {
  local line status
  while IFS= read -r line; do
    if [[ "$line" == PHASE=* ]]; then
      status=$(printf '%s' "$line" | sed -n 's/.*STATUS=\([a-z]*\).*/\1/p')
      [[ -n "$status" ]] && bump_status "$status"
    fi
    printf '%s\n' "$line"
  done
}

# ─── remote phase scripts ──────────────────────────────────────────────────
# Each remote script emits PHASE= lines. Substitution placeholders:
#   __EXPECTED_POOLS__   space-separated zpool names this host should own
#   __SERVER_MOUNTS__    semicolon-separated triples: dataset|mountpoint|target_name
#   __CLIENT_MOUNT__     1 or 0 — should this node mount the client at /mnt/lustre
#   __LNET_IF__          NIC for the o2ib net
#   __MGS_NID__          MGS NID for client mount
#   __FSNAME__           filesystem name
#   __POOL_DIR__         directory of file-backed pool vdevs
#   __RECOVERY_TIMEOUT__ seconds to wait per server mount on first-time mount
#   __ABORT_TARGETS__    space-separated target names to abort_recovery on

phase_1_2_3_script() {
  cat <<'REMOTE'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"
EXPECTED_POOLS="__EXPECTED_POOLS__"
LNET_IF=__LNET_IF__
POOL_DIR=__POOL_DIR__

emit() {
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3" "$4"
}

# Phase 1: zpool import (idempotent)
MISSING_POOLS=""
for P in $EXPECTED_POOLS; do
  if ! sudo zpool list -H -o name "$P" >/dev/null 2>&1; then
    MISSING_POOLS="$MISSING_POOLS $P"
  fi
done
if [[ -z "${MISSING_POOLS// /}" ]]; then
  emit 1 zpool_import ok "all expected pools online (${EXPECTED_POOLS})"
else
  if sudo zpool import -d "$POOL_DIR" -a >/dev/null 2>&1; then
    # Re-verify
    STILL_MISSING=""
    for P in $EXPECTED_POOLS; do
      sudo zpool list -H -o name "$P" >/dev/null 2>&1 || STILL_MISSING="$STILL_MISSING $P"
    done
    if [[ -z "${STILL_MISSING// /}" ]]; then
      emit 1 zpool_import ok "imported missing pools:${MISSING_POOLS}"
    else
      emit 1 zpool_import red "import ran but still missing:${STILL_MISSING}"
      exit 0
    fi
  else
    emit 1 zpool_import red "zpool import -d ${POOL_DIR} -a failed"
    exit 0
  fi
fi

# Phase 2: modprobe -a zfs lnet lustre osd_zfs (idempotent)
NEED_MODS=""
for M in zfs lnet lustre osd_zfs; do
  lsmod | awk '{print $1}' | grep -qx "$M" || NEED_MODS="$NEED_MODS $M"
done
if [[ -z "${NEED_MODS// /}" ]]; then
  emit 2 modprobe ok "all 4 modules already loaded"
else
  if sudo modprobe -a $NEED_MODS 2>/dev/null; then
    emit 2 modprobe ok "loaded:${NEED_MODS}"
  else
    emit 2 modprobe red "modprobe -a${NEED_MODS} failed"
    exit 0
  fi
fi

# Phase 3: LNet configure + o2ib net (idempotent)
# `lnetctl lnet configure` is no-op if already configured.
sudo lnetctl lnet configure >/dev/null 2>&1 || true

CUR_NIDS=$(sudo lctl list_nids 2>/dev/null || echo)
if printf '%s' "$CUR_NIDS" | grep -q '@o2ib'; then
  emit 3 lnet_configure ok "o2ib NID already advertised ($(printf '%s' "$CUR_NIDS" | grep '@o2ib' | head -1))"
else
  if sudo lnetctl net add --net o2ib0 --if "$LNET_IF" 2>/dev/null; then
    NEW_NID=$(sudo lctl list_nids 2>/dev/null | grep '@o2ib' | head -1)
    if [[ -n "$NEW_NID" ]]; then
      emit 3 lnet_configure ok "added o2ib0 on ${LNET_IF}: ${NEW_NID}"
    else
      emit 3 lnet_configure red "lnetctl net add returned ok but no @o2ib NID present"
      exit 0
    fi
  else
    emit 3 lnet_configure red "lnetctl net add --net o2ib0 --if ${LNET_IF} failed"
    exit 0
  fi
fi

# Verify ko2iblnd loaded as side-effect
if lsmod | awk '{print $1}' | grep -qx ko2iblnd; then
  emit 3 ko2iblnd_loaded ok "RDMA driver present"
else
  emit 3 ko2iblnd_loaded warn "ko2iblnd not loaded; o2ib NID present but RDMA path may not work"
fi
REMOTE
}

# Phase 4 server mounts script. Driven per-node by the orchestrator with the
# correct dataset/mountpoint/target_name triples and ordering.
phase_4_script() {
  cat <<'REMOTE'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"
SERVER_MOUNTS="__SERVER_MOUNTS__"
RECOVERY_TIMEOUT=__RECOVERY_TIMEOUT__
ABORT_TARGETS="__ABORT_TARGETS__"

emit() {
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3" "$4"
}

# Optional pre-step: abort recovery on listed targets before any mount.
for TGT in $ABORT_TARGETS; do
  if sudo lctl dl 2>/dev/null | awk '{print $4}' | grep -qx "$TGT"; then
    if sudo lctl --device "$TGT" abort_recovery 2>/dev/null; then
      emit 4 abort_recovery ok "aborted recovery on ${TGT}"
    else
      emit 4 abort_recovery warn "abort_recovery on ${TGT} failed (target may not be in recovery)"
    fi
  fi
done

# Mount each server target in the order given.
IFS=';' read -ra MOUNTS <<< "$SERVER_MOUNTS"
for SPEC in "${MOUNTS[@]}"; do
  [[ -z "$SPEC" ]] && continue
  DS="${SPEC%%|*}"
  REST="${SPEC#*|}"
  MP="${REST%%|*}"
  TGT="${REST#*|}"
  ACTION="mount_${TGT}"

  if mountpoint -q "$MP"; then
    emit 4 "$ACTION" ok "${TGT} already mounted at ${MP}"
    continue
  fi

  sudo mkdir -p "$MP" 2>/dev/null || true

  # Background the mount so we can poll dmesg for recovery hangs.
  ( sudo mount -t lustre "$DS" "$MP" 2>&1 ) &
  MNT_PID=$!

  WAITED=0
  RECOVERY_DETECTED=0
  while kill -0 "$MNT_PID" 2>/dev/null; do
    sleep 5
    WAITED=$((WAITED + 5))
    # Check dmesg for the recovery-wait marker on this target
    if sudo dmesg --since "1 minute ago" 2>/dev/null | grep -q "${TGT}: in recovery but waiting"; then
      RECOVERY_DETECTED=1
    fi
    if (( WAITED >= RECOVERY_TIMEOUT )); then
      emit 4 "$ACTION" red "mount of ${TGT} hung >${RECOVERY_TIMEOUT}s; recovery suspected. Re-run bringup with --abort-recovery ${TGT}"
      kill "$MNT_PID" 2>/dev/null
      exit 0
    fi
  done

  wait "$MNT_PID"
  MNT_RC=$?
  if [[ $MNT_RC -eq 0 ]]; then
    if [[ $RECOVERY_DETECTED -eq 1 ]]; then
      emit 4 "$ACTION" warn "mounted ${TGT} at ${MP} after ${WAITED}s in recovery wait"
    else
      emit 4 "$ACTION" ok "mounted ${TGT} at ${MP}"
    fi
  else
    emit 4 "$ACTION" red "mount -t lustre ${DS} ${MP} failed (rc=${MNT_RC})"
    exit 0
  fi
done
REMOTE
}

phase_5_script() {
  cat <<'REMOTE'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"
MGS_NID=__MGS_NID__
FSNAME=__FSNAME__
CLIENT_MOUNT=__CLIENT_MOUNT__

emit() {
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3" "$4"
}

if [[ "$CLIENT_MOUNT" != "1" ]]; then
  emit 5 client_mount ok "skipped (not configured to mount client here)"
  exit 0
fi

if mountpoint -q /mnt/lustre; then
  emit 5 client_mount ok "/mnt/lustre already mounted"
else
  sudo mkdir -p /mnt/lustre 2>/dev/null || true
  if sudo mount -t lustre "${MGS_NID}:/${FSNAME}" /mnt/lustre 2>&1; then
    emit 5 client_mount ok "/mnt/lustre mounted (MGS=${MGS_NID} fsname=${FSNAME})"
  else
    emit 5 client_mount red "mount -t lustre ${MGS_NID}:/${FSNAME} /mnt/lustre failed"
    exit 0
  fi
fi
REMOTE
}

teardown_script() {
  cat <<'REMOTE'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"
SERVER_MOUNTPOINTS="__SERVER_MOUNTPOINTS__"
CLIENT_MOUNT=__CLIENT_MOUNT__
EXPECTED_POOLS="__EXPECTED_POOLS__"

emit() {
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3" "$4"
}

# Phase T1: client unmount (only where mounted)
if [[ "$CLIENT_MOUNT" == "1" ]] && mountpoint -q /mnt/lustre; then
  if sudo umount /mnt/lustre 2>&1; then
    emit T1 client_umount ok "/mnt/lustre unmounted"
  else
    emit T1 client_umount red "umount /mnt/lustre failed (clients may be busy)"
    exit 0
  fi
else
  emit T1 client_umount ok "/mnt/lustre not mounted; skipped"
fi

# Phase T2: server unmounts in the supplied order
IFS=';' read -ra MPS <<< "$SERVER_MOUNTPOINTS"
for MP in "${MPS[@]}"; do
  [[ -z "$MP" ]] && continue
  if mountpoint -q "$MP"; then
    if sudo umount "$MP" 2>&1; then
      emit T2 "server_umount_${MP//\//_}" ok "${MP} unmounted"
    else
      emit T2 "server_umount_${MP//\//_}" red "umount ${MP} failed"
      exit 0
    fi
  else
    emit T2 "server_umount_${MP//\//_}" ok "${MP} not mounted; skipped"
  fi
done

# Phase T3: pool export (clean — clears connected-clients list)
if [[ -n "${EXPECTED_POOLS// /}" ]]; then
  if sudo zpool export -a 2>&1; then
    emit T3 zpool_export ok "exported pools"
  else
    emit T3 zpool_export warn "zpool export -a returned non-zero (some pools may still be in use)"
  fi
fi
REMOTE
}

status_script() {
  cat <<'REMOTE'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"

emit() {
  printf 'PHASE=%s NODE=%s ACTION=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3" "$4"
}

# Pools online
POOLS=$(sudo zpool list -H -o name 2>/dev/null | paste -sd, -)
if [[ -n "$POOLS" ]]; then
  emit S pools ok "online: ${POOLS}"
else
  emit S pools warn "no zpools imported"
fi

# Lustre modules loaded
LOADED=$(lsmod | awk '{print $1}' | grep -E '^(zfs|lnet|lustre|osd_zfs|ko2iblnd)$' | paste -sd, -)
if [[ -n "$LOADED" ]]; then
  emit S modules ok "loaded: ${LOADED}"
else
  emit S modules warn "no Lustre modules loaded"
fi

# NIDs
NIDS=$(sudo lctl list_nids 2>/dev/null | paste -sd, -)
if [[ -n "$NIDS" ]]; then
  emit S nids ok "${NIDS}"
else
  emit S nids warn "no NIDs (LNet not configured)"
fi

# Lustre mounts present
LMOUNTS=$(mount 2>/dev/null | awk '/ type lustre /{print $3}' | paste -sd, -)
if [[ -n "$LMOUNTS" ]]; then
  emit S mounts ok "${LMOUNTS}"
else
  emit S mounts warn "no Lustre mounts present"
fi

# Client view (only meaningful if /mnt/lustre mounted)
if mountpoint -q /mnt/lustre; then
  DFL=$(df -h /mnt/lustre 2>/dev/null | tail -1 | awk '{print $2 " total, " $4 " avail"}')
  emit S client_df ok "/mnt/lustre: ${DFL}"
fi
REMOTE
}

# ─── orchestrator helpers ──────────────────────────────────────────────────

render() {
  # render <script_fn> <key=val>...
  # Uses ^A (SOH, \x01) as the sed delimiter so values containing /, |, or #
  # (mount specs, paths, NIDs) substitute cleanly.
  local fn="$1"; shift
  local out
  out=$($fn)
  while [[ $# -gt 0 ]]; do
    local kv="$1"; shift
    local k="${kv%%=*}" v="${kv#*=}"
    out=$(printf '%s' "$out" | sed $'s\x01__'"${k}"$'__\x01'"${v}"$'\x01g')
  done
  printf '%s' "$out"
}

run_remote() {
  # run_remote <node_ssh> <rendered_script>
  local node="$1" script="$2"
  relay < <(ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" "bash -s" <<<"$script" 2>&1)
}

verify_lnet_ping() {
  # Check bidirectional o2ib reachability after both nodes finish Phase 3.
  if ssh -o BatchMode=yes "$NODE1_SSH" "sudo lctl ping 169.254.10.122@o2ib >/dev/null 2>&1"; then
    emit 3 spark01 lnet_ping_n1_to_n2 ok "spark01 -> spark02 @o2ib reachable"
  else
    emit 3 spark01 lnet_ping_n1_to_n2 red "spark01 cannot lctl ping spark02 @o2ib"
    return 1
  fi
  if ssh -o BatchMode=yes "$NODE2_SSH" "sudo lctl ping 169.254.188.115@o2ib >/dev/null 2>&1"; then
    emit 3 spark02 lnet_ping_n2_to_n1 ok "spark02 -> spark01 @o2ib reachable"
  else
    emit 3 spark02 lnet_ping_n2_to_n1 red "spark02 cannot lctl ping spark01 @o2ib"
    return 1
  fi
}

verify_ost_registration() {
  # Both OSTs should register active=1 with MDT0000 after Phase 4 completes.
  local out
  out=$(ssh -o BatchMode=yes "$NODE1_SSH" \
    "sudo lctl get_param -n osp.${FSNAME}-OST*-osc-MDT0000.active 2>/dev/null" || true)
  local active_count
  active_count=$(printf '%s\n' "$out" | grep -c '^1$' || true)
  if [[ "$active_count" -ge 2 ]]; then
    emit 4 spark01 ost_registration ok "both OSTs active=1 with MDT0000 (queried from spark01)"
  else
    emit 4 spark01 ost_registration warn "OST registration partial (${active_count}/2 active); rerun status in a few seconds"
  fi
}

# ─── bringup ───────────────────────────────────────────────────────────────
do_bringup() {
  echo "=== Lustre bringup — fsname=${FSNAME}, MGS=${MGS_NID} ==="
  [[ -n "$ABORT_TARGETS" ]] && echo "Will abort recovery on: ${ABORT_TARGETS}"
  echo

  echo "--- Phase 1-3: pools + modules + LNet (both nodes, parallel) ---"
  local n1_script n2_script
  n1_script=$(render phase_1_2_3_script \
    "EXPECTED_POOLS=mgs-pool mdt0-pool ost0-pool" \
    "LNET_IF=${LNET_IF}" \
    "POOL_DIR=${POOL_DIR}")
  n2_script=$(render phase_1_2_3_script \
    "EXPECTED_POOLS=ost1-pool" \
    "LNET_IF=${LNET_IF}" \
    "POOL_DIR=${POOL_DIR}")

  # Run both nodes in parallel (writes to separate temp files, then drain both).
  local n1_log n2_log
  n1_log=$(mktemp); n2_log=$(mktemp)
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE1_SSH" "bash -s" <<<"$n1_script" 2>&1 ) > "$n1_log" &
  local pid1=$!
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE2_SSH" "bash -s" <<<"$n2_script" 2>&1 ) > "$n2_log" &
  local pid2=$!
  wait "$pid1" || true
  wait "$pid2" || true
  echo "[spark01]"; relay < "$n1_log"
  echo "[spark02]"; relay < "$n2_log"
  rm -f "$n1_log" "$n2_log"

  if [[ "$WORST_STATUS" -ge 2 ]]; then
    echo
    echo "RED in Phase 1-3; halting before Phase 4."
    return 2
  fi

  echo
  echo "--- Phase 3 verify: bidirectional LNet RDMA ping ---"
  verify_lnet_ping || { echo; echo "LNet ping failed; halting."; return 2; }

  echo
  echo "--- Phase 4a: MGS (spark01) ---"
  local mgs_script
  mgs_script=$(render phase_4_script \
    "SERVER_MOUNTS=mgs-pool/mgs|/mnt/mgt|MGS" \
    "RECOVERY_TIMEOUT=${RECOVERY_TIMEOUT_S}" \
    "ABORT_TARGETS=${ABORT_TARGETS}")
  run_remote "$NODE1_SSH" "$mgs_script"
  [[ "$WORST_STATUS" -ge 2 ]] && { echo; echo "RED at Phase 4a; halting."; return 2; }

  echo
  echo "--- Phase 4b: MDT0 (spark01) ---"
  local mdt_script
  mdt_script=$(render phase_4_script \
    "SERVER_MOUNTS=mdt0-pool/mdt0|/mnt/mdt0|${FSNAME}-MDT0000" \
    "RECOVERY_TIMEOUT=${RECOVERY_TIMEOUT_S}" \
    "ABORT_TARGETS=${ABORT_TARGETS}")
  run_remote "$NODE1_SSH" "$mdt_script"
  [[ "$WORST_STATUS" -ge 2 ]] && { echo; echo "RED at Phase 4b; halting."; return 2; }

  echo
  echo "--- Phase 4c+4d: OST0 (spark01) + OST1 (spark02) parallel ---"
  local ost0_script ost1_script
  ost0_script=$(render phase_4_script \
    "SERVER_MOUNTS=ost0-pool/ost0|/mnt/ost0|${FSNAME}-OST0000" \
    "RECOVERY_TIMEOUT=${RECOVERY_TIMEOUT_S}" \
    "ABORT_TARGETS=${ABORT_TARGETS}")
  ost1_script=$(render phase_4_script \
    "SERVER_MOUNTS=ost1-pool/ost1|/mnt/ost1|${FSNAME}-OST0001" \
    "RECOVERY_TIMEOUT=${RECOVERY_TIMEOUT_S}" \
    "ABORT_TARGETS=${ABORT_TARGETS}")
  local o0_log o1_log
  o0_log=$(mktemp); o1_log=$(mktemp)
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE1_SSH" "bash -s" <<<"$ost0_script" 2>&1 ) > "$o0_log" &
  local p0=$!
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE2_SSH" "bash -s" <<<"$ost1_script" 2>&1 ) > "$o1_log" &
  local p1=$!
  wait "$p0" || true
  wait "$p1" || true
  echo "[spark01]"; relay < "$o0_log"
  echo "[spark02]"; relay < "$o1_log"
  rm -f "$o0_log" "$o1_log"
  [[ "$WORST_STATUS" -ge 2 ]] && { echo; echo "RED at Phase 4c/4d; halting."; return 2; }

  echo
  echo "--- Phase 4 verify: OST registration with MDT ---"
  sleep 3   # OSP needs a moment to register the new OSTs
  verify_ost_registration

  echo
  echo "--- Phase 5: client mounts (both nodes, parallel) ---"
  local c_script_n1 c_script_n2
  c_script_n1=$(render phase_5_script "MGS_NID=${MGS_NID}" "FSNAME=${FSNAME}" "CLIENT_MOUNT=1")
  c_script_n2=$(render phase_5_script "MGS_NID=${MGS_NID}" "FSNAME=${FSNAME}" "CLIENT_MOUNT=1")
  local c1_log c2_log
  c1_log=$(mktemp); c2_log=$(mktemp)
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE1_SSH" "bash -s" <<<"$c_script_n1" 2>&1 ) > "$c1_log" &
  local cp1=$!
  ( ssh -o BatchMode=yes -o ConnectTimeout=10 "$NODE2_SSH" "bash -s" <<<"$c_script_n2" 2>&1 ) > "$c2_log" &
  local cp2=$!
  wait "$cp1" || true
  wait "$cp2" || true
  echo "[spark01]"; relay < "$c1_log"
  echo "[spark02]"; relay < "$c2_log"
  rm -f "$c1_log" "$c2_log"

  echo
  case "$WORST_STATUS" in
    0) echo "SUMMARY: Lustre cluster up. fsname=${FSNAME} on ${MGS_NID}, clients at /mnt/lustre on both nodes." ;;
    1) echo "SUMMARY: Lustre up with warnings; review above." ;;
    2) echo "SUMMARY: red findings — cluster bring-up did not complete." ;;
  esac
}

# ─── teardown ──────────────────────────────────────────────────────────────
do_teardown() {
  echo "=== Lustre teardown — clean unmount + pool export ==="
  echo "Reverse order: clients first, then OSTs, then MDT, then MGS."
  echo

  echo "--- Phase T1: unmount clients (both nodes, parallel) ---"
  # Use empty SERVER_MOUNTPOINTS for T1 so the teardown script only does clients.
  local t1_n1 t1_n2
  t1_n1=$(render teardown_script "CLIENT_MOUNT=1" "SERVER_MOUNTPOINTS=" "EXPECTED_POOLS=")
  t1_n2=$(render teardown_script "CLIENT_MOUNT=1" "SERVER_MOUNTPOINTS=" "EXPECTED_POOLS=")
  local l1 l2; l1=$(mktemp); l2=$(mktemp)
  ( ssh -o BatchMode=yes "$NODE1_SSH" "bash -s" <<<"$t1_n1" 2>&1 ) > "$l1" &
  local p1=$!
  ( ssh -o BatchMode=yes "$NODE2_SSH" "bash -s" <<<"$t1_n2" 2>&1 ) > "$l2" &
  local p2=$!
  wait "$p1" || true; wait "$p2" || true
  echo "[spark01]"; relay < "$l1"
  echo "[spark02]"; relay < "$l2"
  rm -f "$l1" "$l2"

  echo
  echo "--- Phase T2a: unmount OST1 on spark02 ---"
  local t2a
  t2a=$(render teardown_script "CLIENT_MOUNT=0" "SERVER_MOUNTPOINTS=/mnt/ost1" "EXPECTED_POOLS=ost1-pool")
  run_remote "$NODE2_SSH" "$t2a"

  echo
  echo "--- Phase T2b: unmount OST0, MDT0, MGS on spark01 (this order) ---"
  local t2b
  t2b=$(render teardown_script "CLIENT_MOUNT=0" "SERVER_MOUNTPOINTS=/mnt/ost0;/mnt/mdt0;/mnt/mgt" "EXPECTED_POOLS=mgs-pool mdt0-pool ost0-pool")
  run_remote "$NODE1_SSH" "$t2b"

  echo
  case "$WORST_STATUS" in
    0) echo "SUMMARY: Lustre torn down cleanly. Safe to poweroff." ;;
    1) echo "SUMMARY: teardown completed with warnings; review above." ;;
    2) echo "SUMMARY: teardown encountered red findings; manual cleanup may be needed." ;;
  esac
}

# ─── status ────────────────────────────────────────────────────────────────
do_status() {
  echo "=== Lustre cluster status ==="
  local sn1 sn2
  sn1=$(render status_script)
  sn2=$(render status_script)
  echo "[spark01]"; run_remote "$NODE1_SSH" "$sn1"
  echo
  echo "[spark02]"; run_remote "$NODE2_SSH" "$sn2"
}

# ─── --install-sudoers bootstrap ───────────────────────────────────────────
install_sudoers() {
  local node="$1"
  case "$node" in
    spark01) node_ssh="$NODE1_SSH" ;;
    spark02) node_ssh="$NODE2_SSH" ;;
    *)       node_ssh="$node" ;;
  esac
  echo "Installing ${SUDOERS_PATH} on ${node_ssh}."
  echo "You will be prompted for the sudo password on ${node_ssh}."
  echo "Scope: zpool, zfs, modprobe, lustre_rmmod, lnetctl, lctl, mount, umount, dmesg."
  echo

  local remote_install
  remote_install=$(cat <<REMOTE
set -euo pipefail
TMP=\$(mktemp /tmp/aihomelab-lustre.NEW.XXXXXX)
cat > "\$TMP" <<'SUDOERS_EOF'
${SUDOERS_CONTENT}
SUDOERS_EOF
chmod 0440 "\$TMP"
if ! sudo visudo -cf "\$TMP" >/dev/null; then
  echo "visudo refused the file; aborting."
  rm -f "\$TMP"
  exit 3
fi
sudo install -m 0440 -o root -g root "\$TMP" ${SUDOERS_PATH}
rm -f "\$TMP"
echo "Installed ${SUDOERS_PATH}."
echo "Verifying NOPASSWD callability (sudo -n -l, no side effects)."
ALL_OK=1
# For each logical command, resolve the actual binary path via \`command -v\`
# and probe THAT. Falls back to a sensible default if not found in PATH.
for NAME in zpool zfs modprobe lnetctl lctl mount umount dmesg; do
  P=\$(command -v "\$NAME" 2>/dev/null || true)
  if [[ -z "\$P" ]]; then
    printf '  SKIP %s (binary not in PATH on this node)\n' "\$NAME"
    continue
  fi
  if sudo -n -l "\$P" >/dev/null 2>&1; then
    printf '  OK   %s\n' "\$P"
  else
    printf '  FAIL %s (resolved %s; rule may list a different path)\n' "\$NAME" "\$P"
    ALL_OK=0
  fi
done
[[ \$ALL_OK -eq 1 ]] && echo "All NOPASSWD entries callable." || { echo "Some entries not callable. Check the sudoers file lists the resolved paths above."; exit 4; }
REMOTE
)
  ssh -t "$node_ssh" "$remote_install"
}

# ─── arg parsing ───────────────────────────────────────────────────────────
SUBCMD="${1:-}"
shift || true

# Parse optional flags after the subcommand
while [[ $# -gt 0 ]]; do
  case "$1" in
    --abort-recovery)
      [[ $# -ge 2 ]] || { echo "--abort-recovery requires a target name" >&2; exit 2; }
      ABORT_TARGETS="${ABORT_TARGETS} $2"
      shift 2
      ;;
    *)
      # Pass through to subcommand-specific handling below
      EXTRA_ARG="$1"
      shift
      ;;
  esac
done

case "$SUBCMD" in
  --help|-h|"")
    usage
    exit 0
    ;;
  bringup)
    do_bringup
    exit "$WORST_STATUS"
    ;;
  teardown)
    do_teardown
    exit "$WORST_STATUS"
    ;;
  status)
    do_status
    exit "$WORST_STATUS"
    ;;
  --install-sudoers)
    [[ -n "${EXTRA_ARG:-}" ]] || { echo "Usage: $0 --install-sudoers <spark01|spark02|user@host>" >&2; exit 2; }
    install_sudoers "$EXTRA_ARG"
    exit $?
    ;;
  *)
    echo "Unknown subcommand: $SUBCMD" >&2
    usage >&2
    exit 2
    ;;
esac
