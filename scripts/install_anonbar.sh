#!/usr/bin/env bash
# One-time install: builds Anonbar and installs it as a proper macOS .app.
#   scripts/install_anonbar.sh && open -a Anonbar
# Rerun after code updates to refresh both the binary and the bundled copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/build_app.sh"

DEST="/Applications"
if ! [ -w "/Applications" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
rm -rf "$DEST/Anonbar.app"
cp -R "$ROOT/build/Anonbar.app" "$DEST/"

mkdir -p "$HOME/.anonproxy"
printf '%s\n' "$ROOT" > "$HOME/.anonproxy/home"

echo "installed: $DEST/Anonbar.app"
echo "launch:    open -a Anonbar   (or Spotlight: ⌘Space → \"Anonbar\")"
echo "auto-start: System Settings → General → Login Items → add Anonbar"
