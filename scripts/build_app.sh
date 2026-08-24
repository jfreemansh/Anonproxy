#!/usr/bin/env bash
# Build + assemble build/Anonbar.app[-$ARCH].
#
#   ARCH=arm64|x86_64 [RUNTIME_DIR=...] scripts/build_app.sh
#
# RUNTIME_DIR = extracted python-build-standalone tree for $ARCH. When set,
# the interpreter + dependencies are EMBEDDED (self-contained app). On a host
# of a different arch than ARCH we never execute the foreign runtime: the
# Swift binary is cross-compiled (-arch) and deps are fetched as prebuilt
# wheels for the target platform via pip --platform/--target.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ARCH="${ARCH:-$(uname -m | sed 's/aarch64/arm64/')}"
case "$ARCH" in
    arm64|aarch64) TARGET="arm64-apple-macos11.0";  PIP_PLAT="macosx_11_0_arm64" ;;
    x86_64)        TARGET="x86_64-apple-macos11.0"; PIP_PLAT="macosx_11_0_x86_64" ;;
    *) echo "unknown ARCH '$ARCH'"; exit 2 ;;
esac
HOST_ARCH="$(uname -m | sed 's/aarch64/arm64/')"

mkdir -p build
xcrun swiftc -O -swift-version 5 -target "$TARGET" \
    "$ROOT/scripts/anonbar.swift" -o "$ROOT/build/anonbar" -framework AppKit

SUFFIX=""
[ "$ARCH" != "arm64" ] && SUFFIX="-$ARCH"      # keep historical default name for arm64
APP="$ROOT/build/Anonbar$SUFFIX.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/build/anonbar" "$APP/Contents/MacOS/anonbar"

cp -R "$ROOT/anonproxy" "$APP/Contents/Resources/anonproxy"
find "$APP/Contents/Resources/anonproxy" -name __pycache__ -type d \
    -exec rm -rf {} + 2>/dev/null || true
cp "$ROOT/requirements.txt" "$APP/Contents/Resources/requirements.txt"

if [ -n "${RUNTIME_DIR:-}" ]; then
    echo "embedding runtime ($ARCH): $RUNTIME_DIR"
    mkdir -p "$APP/Contents/Frameworks"
    cp -R "$RUNTIME_DIR" "$APP/Contents/Frameworks/python"
    PYBIN="$APP/Contents/Frameworks/python/bin/python3"
    SITEPKG="$APP/Contents/Frameworks/python/lib/python3.12/site-packages"
    if [ "$ARCH" = "$HOST_ARCH" ]; then
        # native: let the runtime's own pip resolve normally
        "$PYBIN" -m pip install --no-input --timeout 60 --retries 5 \
            -r "$ROOT/requirements.txt"
    else
        # foreign arch: never execute it — fetch prebuilt wheels for target
        HOST_PY="${HOST_PYTHON:-python3}"
        "$HOST_PY" -m pip install --no-input --timeout 60 --retries 5 \
            --no-cache-dir --only-binary=:all: \
            --platform "$PIP_PLAT" \
            --platform macosx_10_9_x86_64 --platform macosx_10_12_x86_64 \
            --platform macosx_11_0_x86_64 --platform macosx_12_0_x86_64 \
            --platform macosx_13_0_x86_64 --platform macosx_14_0_x86_64 \
            --platform macosx_11_0_universal2 \
            --python-version 3.12 --implementation cp --abi cp312 \
            --target "$SITEPKG" -r "$ROOT/requirements.txt"
    fi
    find "$APP/Contents/Frameworks/python" -name __pycache__ -type d \
        -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$APP/Contents/Frameworks/python/lib"/python3*/test \
           "$APP/Contents/Frameworks/python/lib"/python3*/idlelib 2>/dev/null || true
fi

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Anonbar</string>
    <key>CFBundleIdentifier</key><string>local.anonproxy.anonbar</string>
    <key>CFBundleExecutable</key><string>anonbar</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>${ANONBAR_VERSION:-0.2.1}</string>
    <!-- status-bar only: no Dock icon -->
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
touch "$APP"
echo "assembled $APP"
