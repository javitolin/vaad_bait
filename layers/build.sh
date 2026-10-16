#!/usr/bin/env bash
# Builds the Lambda dependency layer. Needs no container runtime: manylinux
# wheels cross-installed for the Lambda arm64 runtime, as in aws_gallery.
set -euo pipefail

PY_VERSION="3.13"
PIP_PLATFORM="manylinux2014_aarch64"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/build"
TARGET="$BUILD/deps/python"

rm -rf "$BUILD" "$ROOT/deps.zip"
mkdir -p "$TARGET"

python3 -m pip install --quiet \
  --platform "$PIP_PLATFORM" \
  --implementation cp \
  --python-version "$PY_VERSION" \
  --only-binary=:all: \
  --target "$TARGET" \
  -r "$ROOT/../requirements.txt"

find "$TARGET" -type d \( -name tests -o -name test -o -name '__pycache__' \) -prune -exec rm -rf {} +
(cd "$BUILD/deps" && zip -qr "$ROOT/deps.zip" python)
echo "==> layers/deps.zip $(du -h "$ROOT/deps.zip" | cut -f1)"
