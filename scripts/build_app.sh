#!/usr/bin/env bash
# Build + assemble build/Anonbar.app (binary + bundled package snapshot).
# Used by install_anonbar.sh locally and by the GitHub release workflow.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

./scripts/build_anonbar.sh >/dev/null

APP="build/Anonbar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp build/anonbar "$APP/Contents/MacOS/anonbar"
mkdir -p "$APP/Contents/Resources"
rm -rf "$APP/Contents/Resources/anonproxy"
cp -R "$ROOT/anonproxy" "$APP/Contents/Resources/anonproxy"
find "$APP/Contents/Resources/anonproxy" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cp "$ROOT/requirements.txt" "$APP/Contents/Resources/requirements.txt"
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
    <key>CFBundleShortVersionString</key><string>0.1.1</string>
    <!-- status-bar only: no Dock icon -->
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
touch "$APP"   # refresh Finder/LaunchServices metadata

echo "assembled $ROOT/build/Anonbar.app"
