#!/usr/bin/env bash
# One-time install: builds Anonbar and installs it as a proper macOS .app.
#
#   scripts/install_anonbar.sh
#   open -a Anonbar            # or Spotlight: ⌘Space → "Anonbar"
#
# After this you never touch swiftc again — rerun ONLY when anonbar.swift
# changes. Optional: System Settings → General → Login Items → add Anonbar
# to auto-start it at login.
set -euo pipefail
cd "$(dirname "$0")/.."

./scripts/build_anonbar.sh >/dev/null

APP="build/Anonbar.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp build/anonbar "$APP/Contents/MacOS/anonbar"
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
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <!-- status-bar only: no Dock icon -->
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
touch "$APP"   # refresh Finder/LaunchServices metadata

DEST="/Applications"
if ! [ -w "/Applications" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
rm -rf "$DEST/Anonbar.app"
cp -R "$APP" "$DEST/"

echo "installed: $DEST/Anonbar.app"
echo "launch:    open -a Anonbar   (or Spotlight: ⌘Space → \"Anonbar\")"
echo "auto-start: System Settings → General → Login Items → add Anonbar"
