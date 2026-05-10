#!/usr/bin/env bash
# run-distributed-concurrent.sh — Pillar 3: concurrent multi-client battery.
# Run from a CONTROL HOST (laptop / workstation) with passwordless SSH to both hosts.
# Both clients run an identical fio battery in parallel, writing to per-host
# subdirs. Files are 2-way striped across both OSTs.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
HOST1_SSH="${HOST1_SSH:-host1}"   # ssh-reachable target for host 1 (e.g., user@host1.lan)
HOST2_SSH="${HOST2_SSH:-host2}"   # ssh-reachable target for host 2

CONTROL_TMP="${CONTROL_TMP:-/tmp/lustre-pillar3}"

echo "==> run-distributed-concurrent.sh on $(hostname) (control)"
echo "    host1=${HOST1_SSH}  host2=${HOST2_SSH}"

mkdir -p "${CONTROL_TMP}"

# --- Pre-flight: SSH reachable + Lustre mounted on both ---
for h in "${HOST1_SSH}" "${HOST2_SSH}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=5 "${h}" 'mount | grep -q "/mnt/lustre type lustre"' \
        || { echo "ERROR: ${h} not reachable or Lustre not mounted."; exit 1; }
done

# --- Per-host battery script (identical, parameterized by client name) ---
cat > "${CONTROL_TMP}/pillar3-battery.sh" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
CLIENT="$1"
EXP_ROOT="${EXP_ROOT:-/home/$USER/lustre-on-uma-reproduce}"
LOG_DIR="${EXP_ROOT}/logs"
DATA_DIR="/mnt/lustre/pillar3-${CLIENT}"
mkdir -p "${LOG_DIR}" "${DATA_DIR}"
command -v fio >/dev/null || sudo apt-get install -y -qq fio

# 2-way stripe across both OSTs:
lfs setstripe -c 2 "${DATA_DIR}"

date +%s.%N > "${LOG_DIR}/pillar3-${CLIENT}-launch.ts"
trap 'date +%s.%N > "${LOG_DIR}/pillar3-${CLIENT}-end.ts"; rm -f "${DATA_DIR}"/seq-* "${DATA_DIR}"/rand-*' EXIT

fio --name=seq-write-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar3-${CLIENT}-seq-write-1m.log"

fio --name=seq-read-1m --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=read --bs=1m \
    --size=64G --numjobs=4 --iodepth=4 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar3-${CLIENT}-seq-read-1m.log"

fio --name=seq-write-64k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=write --bs=64k \
    --size=64G --numjobs=4 --iodepth=8 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar3-${CLIENT}-seq-write-64k.log"

fio --name=rand-rw-4k --directory="${DATA_DIR}" \
    --ioengine=libaio --direct=1 --rw=randrw --bs=4k \
    --size=4G --numjobs=4 --iodepth=16 --group_reporting \
    --time_based --runtime=180 \
    --output="${LOG_DIR}/pillar3-${CLIENT}-rand-rw-4k.log"
EOF
chmod +x "${CONTROL_TMP}/pillar3-battery.sh"

# --- Distribute battery to both hosts ---
scp -q "${CONTROL_TMP}/pillar3-battery.sh" "${HOST1_SSH}:/tmp/"
scp -q "${CONTROL_TMP}/pillar3-battery.sh" "${HOST2_SSH}:/tmp/"

# --- Launch in parallel; </dev/null prevents tty-input suspend on background ssh ---
echo "==> launching both hosts in parallel (~14 min)"
ssh "${HOST1_SSH}" 'bash /tmp/pillar3-battery.sh host1' </dev/null \
    > "${CONTROL_TMP}/host1-stdout.log" 2>&1 &
PID1=$!
ssh "${HOST2_SSH}" 'bash /tmp/pillar3-battery.sh host2' </dev/null \
    > "${CONTROL_TMP}/host2-stdout.log" 2>&1 &
PID2=$!
echo "    host1 pid=${PID1}  host2 pid=${PID2}"

wait "${PID1}" "${PID2}"
echo "==> both hosts done"

# --- Harvest summaries from both hosts ---
echo
echo "=== Pillar 3 (concurrent, 2-way striped) per-client summaries ==="
for h in "${HOST1_SSH}:host1" "${HOST2_SSH}:host2"; do
    target="${h%%:*}"; label="${h##*:}"
    echo "--- ${label} (${target}) ---"
    # \$HOME expands on the remote shell, not the control host's
    ssh "${target}" "grep -E 'READ:|WRITE:' \$HOME/lustre-on-uma-reproduce/logs/pillar3-${label}-*.log"
    echo
done

echo "==> run-distributed-concurrent.sh OK"
echo "    Use analyze-fio.py to compute aggregate (sum across both clients)."
