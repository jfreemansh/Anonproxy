#!/usr/bin/env bash
# Build the native Anonbar status-bar app (no Xcode project needed).
# NOTE: we pass an ABSOLUTE source path on purpose — swiftc bakes #filePath
# verbatim into the binary, and the app derives its repo root from it. A
# relative bake made repo-resolution depend on the app's cwd ("/" under
# LaunchServices), which broke PYTHONPATH/cwd for every child process.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/build"
xcrun swiftc -O -swift-version 5 "$ROOT/scripts/anonbar.swift" \
    -o "$ROOT/build/anonbar" -framework AppKit
echo "built $ROOT/build/anonbar"
