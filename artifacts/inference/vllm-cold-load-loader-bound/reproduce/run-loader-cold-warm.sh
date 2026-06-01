#!/usr/bin/env bash
# Reproduce kit — cold/warm cold-load protocol for one loader.
#
# Usage:  run-loader-cold-warm.sh <load-format>   (auto | runai_streamer | fastsafetensors)
#
# For one loader, runs COLD_REPS cold reps (page cache dropped before each) plus
# one warm rep (no drop). Each rep:
#   1. drop the page cache (cold reps only)
#   2. capture a launch timestamp BEFORE the arm starts (brackets the run in
#      iostat clock space; host iostat clock and container log clock can differ)
#   3. launch the arm via serve-arm.sh
#   4. start side-channels: mpstat -P ALL, iostat -x, a VmHWM/RSS sampler
#   5. wait for the headline "Loading weights took / Model loading took" line
#   6. health gate, then a MANDATORY correctness probe (catches a no-op'd adapter
#      or a half-loaded model — required on non-default arms)
#   7. stop the arm, stop the side-channels
#
# CAUTION: keep COLD_REPS modest. Sustained vLLM cold-load cycles can wedge the
# NVRM allocator after ~25-30 cycles on this UMA class. Default 3 reps/loader is
# well under that; do not loop this dozens of times in one session.
#
# Page-cache drop needs root. Wire a NARROW sudoers entry so this one step does
# not prompt mid-run, e.g. in /etc/sudoers.d/cold-load (visudo):
#   <user> ALL=(root) NOPASSWD: /usr/bin/sh -c sync; echo 3 > /proc/sys/vm/drop_caches
# or just run interactively and approve the sudo prompt each cold rep.

set -euo pipefail

LF="${1:?usage: run-loader-cold-warm.sh <load-format>  (auto|runai_streamer|fastsafetensors)}"

EXP_ROOT="${EXP_ROOT:-/home/$USER/vllm-cold-load-reproduce}"
COLD_REPS="${COLD_REPS:-3}"
NVME_DEVICE="${NVME_DEVICE:-nvme0n1}"
PORT="${PORT:-8000}"
NAME="${NAME:-vllm-coldload}"
READY_TIMEOUT="${READY_TIMEOUT:-600}"   # seconds to wait for the load line (auto can take ~2 min)
DROP_CACHE_CMD="${DROP_CACHE_CMD:-sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'}"
PROBE_PROMPT="${PROBE_PROMPT:-Vegan substitute for eggs in a cake?}"

SERVED_NAME="${SERVED_NAME:-recipe-qwen3-8b}"
ENABLE_LORA="${ENABLE_LORA:-0}"
# When the adapter tier is on, probe the adapter name "recipe"; else the base served name.
PROBE_MODEL="$SERVED_NAME"
[ "$ENABLE_LORA" = "1" ] && PROBE_MODEL="recipe"

command -v docker >/dev/null || { echo "FATAL: docker not on PATH"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$EXP_ROOT/logs/$LF"
mkdir -p "$LOGDIR"

have_mpstat=0; command -v mpstat >/dev/null && have_mpstat=1 || echo "WARN: mpstat missing — CPU side-channel skipped (the 1-core-pin signature needs it)"
have_iostat=0; command -v iostat >/dev/null && have_iostat=1 || echo "WARN: iostat missing — NVMe side-channel skipped"

# Wait for the vLLM weight-load headline line to appear in the container logs.
# Borrowed regex (both lines emitted by vLLM 0.15.1):
#   default_loader.py  "Loading weights took Y seconds"           (loader-internal)
#   gpu_model_runner   "Model loading took X GiB and Y seconds"   (loader-agnostic headline)
wait_for_load_line() {
  local deadline=$(( $(date +%s) + READY_TIMEOUT ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if docker logs "$NAME" 2>&1 | grep -qiE "Loading weights took|Model loading took"; then
      return 0
    fi
    # bail early if the container died (crash) — leave logs in place (no --rm)
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
      echo "  container $NAME is not running — likely a crash. Inspect: docker logs $NAME"
      return 1
    fi
    sleep 2
  done
  echo "  TIMEOUT waiting for the load line after ${READY_TIMEOUT}s"
  return 1
}

# Poll /health until the API server actually serves (200). The weight-load line
# fires well BEFORE the server is ready — vLLM still profiles the KV cache and
# starts the HTTP listener after weights load. Gating the health check + probe on
# this avoids racing a server that has loaded weights but isn't listening (HTTP 000).
wait_for_server() {
  local deadline=$(( $(date +%s) + 180 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/health" 2>/dev/null)" = "200" ]; then
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != "true" ]; then
      echo "  container $NAME died before the server was ready — inspect: docker logs $NAME"
      return 1
    fi
    sleep 2
  done
  echo "  TIMEOUT waiting for /health 200 after 180s"
  return 1
}

run_one_rep() {
  local label="$1"  # e.g. cold-1, cold-2, warm
  local cold="$2"   # 1 = drop cache first, 0 = warm
  local tag="${LF}-${label}"
  echo "============================================================"
  echo "=== $LF / $label (cold=$cold) ==="
  echo "============================================================"

  docker rm -f "$NAME" 2>/dev/null || true

  if [ "$cold" = "1" ]; then
    echo "dropping page cache: $DROP_CACHE_CMD"
    eval "$DROP_CACHE_CMD" || { echo "WARN: cache drop failed (sudoers?) — this rep is NOT truly cold"; }
  fi

  # Launch timestamp BEFORE the arm starts — brackets the run in iostat clock space.
  date +%s.%N | tee "$LOGDIR/launch-${tag}.ts"

  # Start side-channels just before the launch; stop them after the load line.
  local mp_pid="" io_pid="" rss_pid=""
  if [ "$have_mpstat" = "1" ]; then
    mpstat -P ALL 1 "$READY_TIMEOUT" > "$LOGDIR/mpstat-${tag}.log" 2>&1 &
    mp_pid=$!
  fi
  if [ "$have_iostat" = "1" ]; then
    iostat -x 1 "$READY_TIMEOUT" "$NVME_DEVICE" > "$LOGDIR/iostat-${tag}.log" 2>&1 &
    io_pid=$!
  fi
  # VmHWM/RSS sampler: poll for the vllm process, then record peak host RSS.
  (
    for _ in $(seq 1 "$READY_TIMEOUT"); do
      pid=$(pgrep -f "vllm serve" | head -1 || true)
      if [ -n "$pid" ] && [ -r "/proc/$pid/status" ]; then
        grep -E "VmHWM|VmRSS" "/proc/$pid/status" 2>/dev/null | sed "s/^/$(date +%H:%M:%S) /"
      fi
      sleep 1
    done
  ) > "$LOGDIR/rss-${tag}.log" 2>&1 &
  rss_pid=$!

  # Launch the arm (serve-arm.sh inherits CONTAINER/HF_CACHE/SNAPSHOT_HASH/adapter env).
  NAME="$NAME" PORT="$PORT" bash "$HERE/serve-arm.sh" "$LF"

  # Wait for the headline load line.
  if wait_for_load_line; then
    echo "--- headline load line ($tag) ---"
    docker logs "$NAME" 2>&1 | grep -iE "Loading weights took|Model loading took" | tee -a "$LOGDIR/loadtime-${tag}.log"
  else
    echo "  no load line captured for $tag — see $LOGDIR and docker logs $NAME"
  fi

  # Stop the side-channels now that the load phase is over.
  for p in "$mp_pid" "$io_pid" "$rss_pid"; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done

  # Wait for the API server to actually serve before health-gating + probing
  # (the load line above != server ready). Non-fatal: if it never comes up we
  # still record the failure and move on rather than aborting the whole run.
  wait_for_server || echo "  server never reached /health 200 — probe may be empty"

  # Health gate.
  echo "--- health gate ---"
  curl -s "http://localhost:$PORT/health" -o /dev/null -w "  /health: %{http_code}\n" || echo "  health curl failed"

  # MANDATORY correctness probe (required on non-default arms; cheap on all).
  # Guarded with `|| echo` so a probe failure can NEVER abort the remaining reps
  # under `set -euo pipefail` (curl|tee would otherwise propagate a non-zero exit).
  echo "--- correctness probe (model=$PROBE_MODEL) ---"
  curl -s --max-time 60 "http://localhost:$PORT/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$PROBE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROBE_PROMPT\"}],\"max_tokens\":128}" \
    | tee "$LOGDIR/probe-${tag}.json" || echo "  probe curl failed"
  echo

  docker stop "$NAME" >/dev/null 2>&1 || true
}

echo "loader=$LF  COLD_REPS=$COLD_REPS  logs -> $LOGDIR"
for i in $(seq 1 "$COLD_REPS"); do
  run_one_rep "cold-$i" 1
done
run_one_rep "warm" 0

echo
echo "=== headline check ($LF) ==="
grep -hiE "Loading weights took|Model loading took" "$LOGDIR"/loadtime-*.log 2>/dev/null \
  || echo "  (no load lines captured — inspect $LOGDIR and docker logs $NAME)"
echo
echo "Next: run all three loaders, then  analyze-loaders.py --log-dir $EXP_ROOT/logs"
