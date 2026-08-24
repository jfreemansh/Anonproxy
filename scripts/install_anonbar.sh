#!/usr/bin/env bash
# One-time install: builds Anonbar and installs it as a proper macOS .app.
#   scripts/install_anonbar.sh && open -a Anonbar
# Rerun after code updates to refresh both the binary and the bundled copy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Embed a Python runtime so the installed app is fully self-contained
# (no system-python requirement). Cached per-arch under build/.
ARCH="$(uname -m | sed 's/aarch64/arm64/')"
RT="$ROOT/build/runtime-$ARCH"
if [ ! -x "$RT/python/bin/python3" ]; then
    echo "fetching embedded Python runtime ($ARCH)..."
    case "$ARCH" in
        arm64)  PBS_ARCH="aarch64-apple-darwin" ;;
        x86_64) PBS_ARCH="x86_64-apple-darwin"  ;;
    esac
    URL=$(gh api repos/astral-sh/python-build-standalone/releases/latest \
          --jq '.assets[].browser_download_url' \
          | grep "cpython-3.12.*$PBS_ARCH-install_only.tar.gz" | head -1)
    mkdir -p "$RT.dl"
    curl -sL "$URL" | tar xz -C "$RT.dl"
    rm -rf "$RT"; mv "$RT.dl/python" "$RT"; rmdir "$RT.dl"
fi
RUNTIME_DIR="$RT" "$ROOT/scripts/build_app.sh"

DEST="/Applications"
if ! [ -w "/Applications" ]; then DEST="$HOME/Applications"; mkdir -p "$DEST"; fi
rm -rf "$DEST/Anonbar.app"
cp -R "$ROOT/build/Anonbar.app" "$DEST/"

mkdir -p "$HOME/.anonproxy"
printf '%s\n' "$ROOT" > "$HOME/.anonproxy/home"

echo "installed: $DEST/Anonbar.app"
echo "launch:    open -a Anonbar   (or Spotlight: ⌘Space → \"Anonbar\")"
echo "auto-start: System Settings → General → Login Items → add Anonbar"
