# Contributing

## Setup

```bash
git clone https://github.com/jfreemansh/Anonproxy && cd Anonproxy
pip install -e '.[dev]'        # pyenv python 3.12+ recommended
python3 -m pytest -q           # must be green before any PR
ruff check anonproxy tests scripts/benchmark_roundtrip.py
```

## Layout

```
anonproxy/
  engine.py            detect -> vault -> single-pass replace; consistency rescan
  restorer.py          tolerant + streaming restoration (the core IP)
  surrogates.py        format-preserving surrogate generation (engagement-keyed)
  vault.py             per-engagement sqlite; optional AES-GCM at rest
  detectors/           regex floor + pluggable contextual backends
  proxy/               FastAPI app, request/response transform, SSE streaming
  profiles.py          engagement profiles; close-out ritual
scripts/
  anon                 profile-driven launcher (self-locating)
  anonbar.swift        native macOS status-bar app (embedded-runtime builds)
  release.sh X.Y.Z     bump versions, gate on tests, tag + push
```

## Conventions

* Tests and ruff must pass; run `anonproxy verify` if you touched detection,
  transformation or restoration — exit code is the contract.
* Comments explain *why*, not *what*. No AI attribution trailers
  (`Co-Authored-By` etc.) in commits — keep history human-attributed.
* Never reuse a released version number for new artifacts (PyPI is immutable).

## Releases

`scripts/release.sh X.Y.Z` bumps the three version locations, gates on the
full suite, then tags and pushes. CI attaches both app zips to the GitHub
release and publishes sdist/wheel to PyPI via trusted publishing.

## Security issues

Private disclosure only — see SECURITY.md. Do not open public issues for
anything that could weaken the anonymization guarantees.
