#!/usr/bin/env bash
# Build the native Anonbar status-bar app (no Xcode project needed).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
xcrun swiftc -O -swift-version 5 scripts/anonbar.swift -o build/anonbar \
    -framework AppKit
echo "built $(pwd)/build/anonbar"
