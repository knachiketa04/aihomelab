#!/usr/bin/env bash
# AIHomeLab cluster readiness probe.
#
# Default (no args): run read-only probes across spark01/spark02 and emit
#   PROBE=<name> NODE=<node> STATUS=<ok|warn|red> DETAIL=<one-line>
# lines, plus human-readable sections. Exit code is the worst status:
#   0 = ok, 1 = warn, 2 = red.
#
# Subcommands:
#   --install-sudoers <node>   Interactive (ssh -t) install/repair of the
#                              narrow-NOPASSWD /etc/sudoers.d/sparks-cache-drop
#                              entry. Requires the sudo password on that node.
#   --help                     This message.

set -euo pipefail

NODE1_SSH="${NODE1_SSH:-sparks@192.168.20.21}"
NODE2_SSH="${NODE2_SSH:-sparks@192.168.20.22}"
NODE1_MGMT_IP="${NODE1_MGMT_IP:-192.168.20.21}"
NODE2_MGMT_IP="${NODE2_MGMT_IP:-192.168.20.22}"
NODE1_QSFP_IP="${NODE1_QSFP_IP:-169.254.188.115}"
NODE2_QSFP_IP="${NODE2_QSFP_IP:-169.254.10.122}"
MN_IF_NAME="${MN_IF_NAME:-enp1s0f0np0}"

DISK_WARN_PCT="${DISK_WARN_PCT:-40}"
DISK_RED_PCT="${DISK_RED_PCT:-60}"
STALE_FILE_MIN_SIZE_GB="${STALE_FILE_MIN_SIZE_GB:-10}"
STALE_FILE_AGE_MIN="${STALE_FILE_AGE_MIN:-1440}"

SUDOERS_PATH="/etc/sudoers.d/sparks-cache-drop"
read -r -d '' SUDOERS_CONTENT <<'EOF' || true
# /etc/sudoers.d/sparks-cache-drop
# AIHomeLab: narrow NOPASSWD for benchmark cache drops only.
Cmnd_Alias AIHL_CACHE_DROP = /usr/bin/sync, /usr/bin/tee /proc/sys/vm/drop_caches
%sudo ALL=(root) NOPASSWD: AIHL_CACHE_DROP
EOF

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
}

# Worst-status tracking. red beats warn beats ok.
WORST_STATUS=0
bump_status() {
  local s="$1"
  case "$s" in
    ok)   ;;
    warn) [[ $WORST_STATUS -lt 1 ]] && WORST_STATUS=1 ;;
    red)  WORST_STATUS=2 ;;
  esac
}

# Print a structured PROBE line and bump the worst-status.
emit_probe() {
  local name="$1" node="$2" status="$3" detail="$4"
  printf 'PROBE=%s NODE=%s STATUS=%s DETAIL=%s\n' "$name" "$node" "$status" "$detail"
  bump_status "$status"
}

# Parse PROBE lines emitted by the remote bash block, bump local status,
# and re-emit verbatim so the caller / agent sees the same stream.
relay_remote_probes() {
  local line
  while IFS= read -r line; do
    if [[ "$line" == PROBE=* ]]; then
      local status
      status=$(printf '%s' "$line" | sed -n 's/.*STATUS=\([a-z]*\).*/\1/p')
      [[ -n "$status" ]] && bump_status "$status"
    fi
    printf '%s\n' "$line"
  done
}

# ─── remote probe block ────────────────────────────────────────────────────
# Runs inside ssh on each node. Emits PROBE= lines on stdout.
# Args interpolated by the caller: PEER_MGMT_IP, PEER_QSFP_IP, MN_IF_NAME,
# DISK_WARN_PCT, DISK_RED_PCT, STALE_FILE_MIN_SIZE_GB, STALE_FILE_AGE_MIN,
# SUDOERS_PATH.

remote_probe_script() {
  # Placeholders are substituted by render_remote_script. They are bare
  # identifiers (no ${} wrapping) so the substituted text is valid bash.
  cat <<'REMOTE_SCRIPT'
set -uo pipefail
NODE_TAG="$(hostname -s 2>/dev/null || echo unknown)"
export PATH="/usr/local/cuda/bin:/home/sparks/.local/bin:/opt/bin:$PATH"

SUDOERS_PATH=__SUDOERS_PATH__
PEER_MGMT_IP=__PEER_MGMT_IP__
PEER_QSFP_IP=__PEER_QSFP_IP__
MN_IF_NAME=__MN_IF_NAME__
DISK_WARN_PCT=__DISK_WARN_PCT__
DISK_RED_PCT=__DISK_RED_PCT__
STALE_GB=__STALE_FILE_MIN_SIZE_GB__
STALE_MIN=__STALE_FILE_AGE_MIN__

emit() {
  printf 'PROBE=%s NODE=%s STATUS=%s DETAIL=%s\n' "$1" "$NODE_TAG" "$2" "$3"
}

# gpu_present
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
  if [[ -n "$GPU_NAME" ]]; then
    emit gpu_present ok "nvidia-smi: ${GPU_NAME}"
  else
    emit gpu_present red "nvidia-smi present but no GPU reported"
  fi
else
  emit gpu_present red "nvidia-smi missing"
fi

# docker_runtime
if docker ps >/dev/null 2>&1; then
  DV=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)
  emit docker_runtime ok "docker ps ok, server ${DV}"
else
  if groups 2>/dev/null | grep -qw docker; then
    emit docker_runtime red "docker ps failed despite docker group membership"
  else
    emit docker_runtime red "docker ps failed; user not in docker group (run: sudo usermod -aG docker \$USER then re-login)"
  fi
fi

# gpu_docker_passthrough — only attempt if both prerequisites ok
if command -v nvidia-smi >/dev/null 2>&1 && docker ps >/dev/null 2>&1; then
  GPU_DOCKER_IMG="${GPU_DOCKER_IMG:-nvcr.io/nvidia/cuda:13.0.0-base-ubuntu22.04}"
  if docker image inspect "$GPU_DOCKER_IMG" >/dev/null 2>&1; then
    if OUT=$(docker run --rm --gpus all "$GPU_DOCKER_IMG" nvidia-smi -L 2>&1); then
      GPU_LINE=$(printf '%s' "$OUT" | head -1 | tr -d '\n')
      emit gpu_docker_passthrough ok "container saw: ${GPU_LINE}"
    else
      emit gpu_docker_passthrough red "docker run --gpus all failed: $(printf '%s' "$OUT" | tail -1)"
    fi
  else
    emit gpu_docker_passthrough warn "image ${GPU_DOCKER_IMG} not pulled; skipped passthrough check (set GPU_DOCKER_IMG or docker pull)"
  fi
else
  emit gpu_docker_passthrough warn "skipped (gpu or docker not ready)"
fi

# sudoers_cache_drop — NOPASSWD callability for sync+tee.
# sudo -n -l <cmd> checks whether the command is allowed WITHOUT running it.
# This is critical: actually running sync/tee /proc/sys/vm/drop_caches would
# drop caches on every readiness run, polluting the next experiment's cold run.
# We do NOT test [[ -r $SUDOERS_PATH ]] — the file is 0440 root:root and the
# sparks user cannot read it, so a readability gate would always false-red.
# `sudo -n -l` already proves the file exists and the NOPASSWD rule is wired.
if sudo -n -l /usr/bin/sync >/dev/null 2>&1 \
   && sudo -n -l /usr/bin/tee /proc/sys/vm/drop_caches >/dev/null 2>&1; then
  emit sudoers_cache_drop ok "${SUDOERS_PATH} NOPASSWD callable for sync+tee"
else
  emit sudoers_cache_drop red "${SUDOERS_PATH} missing or sudo -n -l rejected sync/tee; run: infra/scripts/check-cluster-readiness.sh --install-sudoers ${NODE_TAG}"
fi

# disk_headroom_home and disk_headroom_root
for MP in "$HOME" /; do
  USED_PCT=$(df --output=pcent "$MP" 2>/dev/null | tail -1 | tr -d ' %')
  AVAIL_H=$(df -h --output=avail "$MP" 2>/dev/null | tail -1 | tr -d ' ')
  if [[ -z "$USED_PCT" ]]; then
    emit "disk_headroom_${MP//\//_}" warn "df failed for ${MP}"
    continue
  fi
  if [[ "$MP" == "/" ]]; then LABEL="disk_headroom_root"
  elif [[ "$MP" == "$HOME" ]]; then LABEL="disk_headroom_home"
  else LABEL="disk_headroom_${MP//\//_}"
  fi
  if (( USED_PCT >= DISK_RED_PCT )); then
    emit "$LABEL" red "${MP} used ${USED_PCT}% (avail ${AVAIL_H}); >= ${DISK_RED_PCT}% disk gate"
  elif (( USED_PCT >= DISK_WARN_PCT )); then
    emit "$LABEL" warn "${MP} used ${USED_PCT}% (avail ${AVAIL_H})"
  else
    emit "$LABEL" ok "${MP} used ${USED_PCT}% (avail ${AVAIL_H})"
  fi
done

# leftover_k3s
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet k3s 2>/dev/null; then
  emit leftover_k3s warn "k3s active (left over from prior experiment; tear down if not reusing)"
elif command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files 2>/dev/null | grep -q '^k3s\.service'; then
  emit leftover_k3s ok "k3s installed but inactive"
else
  emit leftover_k3s ok "k3s not installed"
fi

# leftover_network_mounts (lustre / nfs)
NETMOUNTS=$(mount 2>/dev/null | awk '/ type (lustre|nfs|nfs4) /{print $3}' | paste -sd, -)
if [[ -n "$NETMOUNTS" ]]; then
  emit leftover_network_mounts warn "lustre/nfs mounts present: ${NETMOUNTS}"
else
  emit leftover_network_mounts ok "no lustre/nfs mounts"
fi

# orphan_containers (exited or created but never started)
if docker ps >/dev/null 2>&1; then
  ORPHAN_COUNT=$(docker ps -a --filter status=exited --filter status=created --format '{{.ID}}' 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${ORPHAN_COUNT:-0}" -gt 0 ]]; then
    emit orphan_containers warn "${ORPHAN_COUNT} orphan container(s); inspect: docker ps -a --filter status=exited --filter status=created"
  else
    emit orphan_containers ok "no orphan containers"
  fi
fi

# stale_large_files in $HOME (cheap; -xdev keeps it on the home filesystem)
if [[ -d "$HOME" ]]; then
  COUNT=$(find "$HOME" -xdev -type f -size "+${STALE_GB}G" -mmin "+${STALE_MIN}" 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${COUNT:-0}" -gt 0 ]]; then
    emit stale_large_files warn "${COUNT} file(s) >${STALE_GB}G older than ${STALE_MIN}min under \$HOME"
  else
    emit stale_large_files ok "no stale files >${STALE_GB}G in \$HOME"
  fi
fi

# net_mgmt_peer and net_qsfp_peer
if ping -c 2 -W 2 "$PEER_MGMT_IP" >/dev/null 2>&1; then
  emit net_mgmt_peer ok "peer ${PEER_MGMT_IP} reachable on management LAN"
else
  emit net_mgmt_peer red "peer ${PEER_MGMT_IP} unreachable on management LAN"
fi
if ping -c 2 -W 2 "$PEER_QSFP_IP" >/dev/null 2>&1; then
  emit net_qsfp_peer ok "peer ${PEER_QSFP_IP} reachable on QSFP fabric (${MN_IF_NAME})"
else
  emit net_qsfp_peer red "peer ${PEER_QSFP_IP} unreachable on QSFP fabric (${MN_IF_NAME})"
fi

# net_qsfp_bandwidth — opportunistic; only flags tool presence
if command -v iperf3 >/dev/null 2>&1; then
  emit net_qsfp_bandwidth ok "iperf3 installed (run iperf3 -c manually to measure)"
else
  emit net_qsfp_bandwidth warn "iperf3 not installed; QSFP bandwidth not verified"
fi

# hf_token presence and perms
HF_TOK="$HOME/.huggingface_token"
if [[ -e "$HF_TOK" ]]; then
  if [[ ! -r "$HF_TOK" ]]; then
    emit hf_token red "${HF_TOK} exists but not readable by current user"
  else
    SIZE=$(stat -c '%s' "$HF_TOK" 2>/dev/null || echo 0)
    PERMS=$(stat -c '%a' "$HF_TOK" 2>/dev/null || echo unknown)
    if [[ "$SIZE" -eq 0 ]]; then
      emit hf_token red "${HF_TOK} exists but is empty"
    elif [[ "$PERMS" != "600" ]]; then
      emit hf_token warn "${HF_TOK} present (perms ${PERMS}; expected 600)"
    else
      emit hf_token ok "${HF_TOK} present (perms 600)"
    fi
  fi
else
  emit hf_token warn "${HF_TOK} missing (HF-gated experiments will fail)"
fi
REMOTE_SCRIPT
}

# Build the remote script with substitutions baked in.
render_remote_script() {
  local peer_mgmt_ip="$1" peer_qsfp_ip="$2"
  remote_probe_script \
    | sed "s|__SUDOERS_PATH__|${SUDOERS_PATH}|g" \
    | sed "s|__PEER_MGMT_IP__|${peer_mgmt_ip}|g" \
    | sed "s|__PEER_QSFP_IP__|${peer_qsfp_ip}|g" \
    | sed "s|__MN_IF_NAME__|${MN_IF_NAME}|g" \
    | sed "s|__DISK_WARN_PCT__|${DISK_WARN_PCT}|g" \
    | sed "s|__DISK_RED_PCT__|${DISK_RED_PCT}|g" \
    | sed "s|__STALE_FILE_MIN_SIZE_GB__|${STALE_FILE_MIN_SIZE_GB}|g" \
    | sed "s|__STALE_FILE_AGE_MIN__|${STALE_FILE_AGE_MIN}|g"
}

probe_node() {
  local node_ssh="$1" peer_mgmt_ip="$2" peer_qsfp_ip="$3"
  local script
  script=$(render_remote_script "$peer_mgmt_ip" "$peer_qsfp_ip")

  if ssh -o BatchMode=yes -o ConnectTimeout=8 "$node_ssh" 'echo ssh_ok' >/dev/null 2>&1; then
    emit_probe ssh_reachable "$node_ssh" ok "ssh ${node_ssh} reachable"
  else
    emit_probe ssh_reachable "$node_ssh" red "ssh ${node_ssh} unreachable (BatchMode key auth, 8s timeout)"
    return
  fi

  relay_remote_probes < <(ssh -o BatchMode=yes -o ConnectTimeout=8 "$node_ssh" "bash -s" <<<"$script" 2>&1)
}

# ─── --install-sudoers subcommand ──────────────────────────────────────────
install_sudoers() {
  local node="$1"
  case "$node" in
    spark01) node_ssh="$NODE1_SSH" ;;
    spark02) node_ssh="$NODE2_SSH" ;;
    *)       node_ssh="$node" ;;  # allow user@host directly
  esac

  echo "Installing ${SUDOERS_PATH} on ${node_ssh}."
  echo "You will be prompted for the sudo password on ${node_ssh}."
  echo

  local remote_install
  remote_install=$(cat <<REMOTE
set -euo pipefail
TMP=\$(mktemp /tmp/sparks-cache-drop.NEW.XXXXXX)
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
if sudo -n -l /usr/bin/sync >/dev/null 2>&1 && sudo -n -l /usr/bin/tee /proc/sys/vm/drop_caches >/dev/null 2>&1; then
  echo "OK: sudo -n sync + tee allowed without password."
else
  echo "FAIL: sudo -n -l rejected; user may not be in sudo group, or sudoers parse mismatch."
  exit 4
fi
REMOTE
)
  # ssh -t so sudo can prompt for the password interactively.
  ssh -t "$node_ssh" "$remote_install"
}

# ─── arg parsing ───────────────────────────────────────────────────────────
case "${1:-}" in
  --help|-h)
    usage
    exit 0
    ;;
  --install-sudoers)
    [[ $# -eq 2 ]] || { echo "Usage: $0 --install-sudoers <spark01|spark02|user@host>" >&2; exit 2; }
    install_sudoers "$2"
    exit $?
    ;;
  "")
    : # default: run probes
    ;;
  *)
    echo "Unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac

# ─── default: run probes ───────────────────────────────────────────────────
echo "AIHomeLab cluster readiness probe."
echo "NODE1_SSH=${NODE1_SSH}  NODE2_SSH=${NODE2_SSH}"
echo "Disk gate: warn>=${DISK_WARN_PCT}%, red>=${DISK_RED_PCT}%."
echo

# Each probe_node MUST be allowed to fail independently — one node down,
# unreachable, or returning nonzero from the ssh pipeline (e.g. read hitting
# EOF without a trailing newline under pipefail) must not abort the other
# node's report. set -e + pipefail combined with the ssh|relay pipe was
# silently truncating output after the first node before this guard.
echo "== ${NODE1_SSH} =="
probe_node "$NODE1_SSH" "$NODE2_MGMT_IP" "$NODE2_QSFP_IP" || true
echo
echo "== ${NODE2_SSH} =="
probe_node "$NODE2_SSH" "$NODE1_MGMT_IP" "$NODE1_QSFP_IP" || true
echo

case "$WORST_STATUS" in
  0) echo "SUMMARY: all green." ;;
  1) echo "SUMMARY: warnings present; cluster usable, review warns." ;;
  2) echo "SUMMARY: red findings; resolve before starting an experiment." ;;
esac

exit "$WORST_STATUS"
