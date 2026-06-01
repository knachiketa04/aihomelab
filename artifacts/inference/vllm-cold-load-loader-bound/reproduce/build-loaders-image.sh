#!/usr/bin/env bash
# Reproduce kit — build the derived "loaders" image.
#
# Derives CONTAINER from the stock vLLM image by pip-installing the two
# streaming loaders. The RunAI layer is installed FIRST so that a
# fastsafetensors aarch64 source-build failure still leaves a cached, usable
# image with the RunAI loader (the primary headline arm proceeds either way).
#
# The default-loader control arm is unaffected by the extra packages — the
# derived image is a strict superset of the stock image.
#
# Standalone usage: this recipe adds both loaders to any vLLM image. Override
# BASE_IMAGE / CONTAINER and run it.
#
# Idempotent: if CONTAINER already exists this script refuses to clobber and
# prints the docker rmi to re-build.

set -euo pipefail

EXP_ROOT="${EXP_ROOT:-/home/$USER/vllm-cold-load-reproduce}"
BASE_IMAGE="${BASE_IMAGE:-nvcr.io/nvidia/vllm:26.02-py3}"
CONTAINER="${CONTAINER:-vllm-loaders:cold-load}"

command -v docker >/dev/null || { echo "FATAL: docker not on PATH"; exit 1; }

# --- idempotency guard ------------------------------------------------------
if docker image inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Image $CONTAINER already exists — refusing to clobber."
  echo "To rebuild:  docker rmi $CONTAINER && $0"
  exit 0
fi

# --- pre-check: does the stock image already ship both loaders? -------------
echo "=== checking whether $BASE_IMAGE already has both loaders ==="
if docker run --rm --entrypoint bash "$BASE_IMAGE" -lc \
  'pip list 2>/dev/null | grep -iqE "runai-model-streamer" && pip list 2>/dev/null | grep -iqE "fastsafetensors"'; then
  echo "Stock image already has both loaders. Tagging it as $CONTAINER (no rebuild needed)."
  docker tag "$BASE_IMAGE" "$CONTAINER"
  exit 0
fi
echo "At least one loader missing from the stock image — building derived image."

mkdir -p "$EXP_ROOT"
DOCKERFILE="$EXP_ROOT/Dockerfile.loaders"

# RunAI layer FIRST (aarch64 wheels exist), fastsafetensors SECOND (may source-build).
cat > "$DOCKERFILE" <<EOF
FROM ${BASE_IMAGE}
RUN pip install --no-cache-dir runai-model-streamer
RUN pip install --no-cache-dir fastsafetensors
EOF

echo "=== Dockerfile ==="
cat "$DOCKERFILE"
echo

# --- build (fastsafetensors layer may fail to source-build on aarch64) ------
if docker build -t "$CONTAINER" -f "$DOCKERFILE" "$EXP_ROOT"; then
  echo "Both loader layers built."
else
  echo
  echo "WARN: full build failed (likely the fastsafetensors source-build on aarch64)."
  echo "Re-building RunAI-only so the primary headline arm still works:"
  cat > "$DOCKERFILE" <<EOF
FROM ${BASE_IMAGE}
RUN pip install --no-cache-dir runai-model-streamer
EOF
  docker build -t "$CONTAINER" -f "$DOCKERFILE" "$EXP_ROOT"
  echo "RunAI-only image built as $CONTAINER. Mark the fastsafetensors arms BLOCKED"
  echo "(a finding in itself: no aarch64 fastsafetensors path on this stack)."
fi

echo
echo "=== confirm load formats wired into the CLI ==="
docker run --rm --entrypoint bash "$CONTAINER" -lc \
  'pip list 2>/dev/null | grep -iE "runai|fastsafetensors"; echo "=== load-format ==="; vllm serve --help 2>&1 | grep -iE "load-format|runai_streamer|fastsafetensors" | head -8'

echo
echo "Done. CONTAINER=$CONTAINER ready for serve-arm.sh / run-loader-cold-warm.sh."
