#!/usr/bin/env bash
# One-command release: bumps the three version locations, gates on tests,
# tags and pushes. Prevents the manual triple-edit mistake class entirely.
#
#   scripts/release.sh 0.2.0
set -euo pipefail
VER="${1:-}"
[[ "$VER" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: scripts/release.sh X.Y.Z"; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

git diff --quiet && git diff --cached --quiet || { echo "working tree not clean"; exit 1; }
git rev-parse -q --verify "refs/tags/v$VER" >/dev/null && { echo "tag v$VER exists"; exit 1; }

sed -i '' "s/^version = \".*\"/version = \"$VER\"/" pyproject.toml
sed -i '' "s/title=\"Anonproxy\", version=\"[^\"]*\"/title=\"Anonproxy\", version=\"$VER\"/" anonproxy/proxy/app.py
sed -i '' "s/\${ANONBAR_VERSION:-[^}]*}/\${ANONBAR_VERSION:-$VER}/" scripts/build_app.sh

echo "== gating: ruff + pytest"
ruff check anonproxy tests scripts/benchmark_roundtrip.py
python3 -m pytest -q

git add pyproject.toml anonproxy/proxy/app.py scripts/build_app.sh
git commit -q -m "Bump to $VER"
git tag -a "v$VER" -m "v$VER"
git push -q origin main "v$VER"
echo "released v$VER — CI will attach app zips and publish to PyPI"
