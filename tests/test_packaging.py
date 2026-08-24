"""Packaging drift guards: requirements.txt must cover pyproject deps."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _pyproject_base_deps():
    text = open("pyproject.toml").read()
    deps_block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    names = set()
    for line in deps_block.splitlines():
        line = line.strip().strip('",')
        if not line or line.startswith("#"):
            continue
        names.add(line.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
                  .strip().lower())
    return {n for n in names if n}


def _requirements_names():
    out = set()
    for line in open("requirements.txt"):
        line = line.split("#")[0].strip()
        if line:
            out.add(line.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
                    .strip().lower())
    return out


def test_requirements_cover_pyproject_dependencies():
    missing = _pyproject_base_deps() - _requirements_names()
    assert not missing, (
        f"requirements.txt is missing pyproject deps: {sorted(missing)} — "
        "this is how the embedded runtime / venv lost 'cryptography'")


def test_version_consistency_across_the_three_sites():
    pyproject = open("pyproject.toml").read()
    version = pyproject.split('version = "', 1)[1].split('"', 1)[0]
    app_py = open("anonproxy/proxy/app.py").read()
    assert f'version="{version}"' in app_py, "FastAPI version string stale"
    import re
    plist = open("scripts/build_app.sh").read()
    m = re.search(r"\$\{ANONBAR_VERSION:-([0-9.]+)\}|"
                  r"<string>([0-9.]+)</string>", plist)
    bundled = (m.group(1) or m.group(2)) if m else None
    assert bundled == version, (
        f"app-bundle fallback version {bundled!r} != pyproject {version!r}")
