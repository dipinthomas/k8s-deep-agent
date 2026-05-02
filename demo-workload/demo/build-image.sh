#!/usr/bin/env bash
# Build + push the synthesizer image. Multi-arch (amd64 + arm64).
#
# Pre-req: docker buildx builder named "multiarch-builder" exists.
#   docker buildx create --name multiarch-builder --use
#
# Usage:  bash demo/build-image.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

if [[ -f "$HERE/.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/.env"
fi

: "${SYNTHESIZER_IMAGE:?SYNTHESIZER_IMAGE not set}"

echo "→ Building $SYNTHESIZER_IMAGE (linux/amd64,linux/arm64)"
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --builder multiarch-builder \
  -t "$SYNTHESIZER_IMAGE" \
  -f "$ROOT/synthesizer/Dockerfile" \
  --push \
  "$ROOT/synthesizer"

echo "✓ Pushed $SYNTHESIZER_IMAGE"
