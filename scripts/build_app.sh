#!/usr/bin/env bash
# Build + assemble build/Anonbar.app.
#
#   scripts/build_app.sh                          # dev build (uses system python)
#   RUNTIME_DIR=/path/to/python-tree scripts/build_app.sh
#                                                 # fully self-contained app:
#                                                 # embeds interpreter+deps,
#                                                 # zero machine prerequisites
#
# RUNTIME_DIR is an extracted python-build-standalone install_only tree
# (bin/python3 at its root). Dependencies are pip-installed INTO the copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p build
xcrun swiftc -O -swift-version 5 "$ROOT/scripts/anonbar.swift" \
    -o "$ROOT/build/anonbar" -framework AppKit

APP="$ROOT/build/Anonbar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$ROOT/build/anonbar" "$APP/Contents/MacOS/anonbar"

cp -R "$ROOT/anonproxy" "$APP/Contents/Resources/anonproxy"
find "$APP/Contents/Resources/anonproxy" -name __pycache__ -type d \
    -exec rm -rf {} + 2>/dev/null || true
cp "$ROOT/requirements.txt" "$APP/Contents/Resources/requirements.txt"

if [ -n "${RUNTIME_DIR:-}" ]; then
    echo "embedding runtime: $RUNTIME_DIR"
    mkdir -p "$APP/Contents/Frameworks"
    cp -R "$RUNTIME_DIR" "$APP/Contents/Frameworks/python"
    PYBIN="$APP/Contents/Frameworks/python/bin/python3"
    "$PYBIN" -m pip install --quiet --no-input -r "$ROOT/requirements.txt"
    find "$APP/Contents/Frameworks/python" -name __pycache__ -type d \
        -exec rm -rf {} + 2>/dev/null || true
    rm -rf "$APP/Contents/Frameworks/python/lib"/python3*/test \
           "$APP/Contents/Frameworks/python/lib"/python3*/idlelib 2>/dev/null || true
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Anonbar</string>
    <key>CFBundleIdentifier</key><string>local.anonproxy.anonbar</string>
    <key>CFBundleExecutable</key><string>anonbar</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.2</string>
    <!-- status-bar only: no Dock icon -->
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
touch "$APP"
echo "assembled $APP"
