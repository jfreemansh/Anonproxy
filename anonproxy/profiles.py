"""
Engagement profiles — per-test context in one small JSON file.

Each profile captures everything that changes between tests: engagement id
(→ vault isolation), scope seed, detector chain, model, port, notes. Spin-up
for a different client/test becomes ``anon up <profile>`` (CLI) or a menu pick
(menubar app) instead of re-typing flags and env vars.

Layout: ``~/.anonproxy/profiles/<name>.json`` (override dir with
``ANONPROXY_PROFILE_DIR``). Names are sanitized to a filesystem-safe subset;
the sanitized name doubles as the engagement id.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings

_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")

# A sane starter chain; users edit via `profile edit` / the JSON file.
_DEFAULTS = {
    "scope_terms": [],
    "scope_file": "",
    "detectors": ["regex", "ollama"],
    "ollama_model": "qwen3:4b",
    "ephemeral": False,
    "port": 8099,
    "notes": "",
}


def sanitize_name(name: str) -> str:
    """Filesystem-safe engagement/profile name."""
    cleaned = _NAME_BAD.sub("-", (name or "").strip()).strip("-.")
    return cleaned[:64] or "engagement"


@dataclass
class Profile:
    name: str
    scope_terms: list[str] = field(default_factory=lambda: list(_DEFAULTS["scope_terms"]))
    scope_file: str = _DEFAULTS["scope_file"]
    detectors: list[str] = field(default_factory=lambda: list(_DEFAULTS["detectors"]))
    ollama_model: str = _DEFAULTS["ollama_model"]
    ephemeral: bool = _DEFAULTS["ephemeral"]
    port: int = _DEFAULTS["port"]
    notes: str = _DEFAULTS["notes"]
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def apply(self, settings: Settings | None = None) -> Settings:
        """Copy this profile onto a :class:`Settings` (engagement context)."""
        s = settings or Settings()
        s.engagement_id = self.name
        s.scope_terms = list(self.scope_terms)
        s.scope_file = self.scope_file
        s.detectors = list(self.detectors)
        s.ollama_model = self.ollama_model
        s.ephemeral = self.ephemeral
        s.port = int(self.port)
        return s


def client_env_lines(profile: Profile, host: str = "127.0.0.1") -> list[str]:
    """`export` lines pointing common clients at this profile's proxy."""
    base = f"http://{host}:{profile.port}"
    return [
        f"# engagement: {profile.name}",
        f"export ANTHROPIC_BASE_URL={base}",
        f'export OPENAI_BASE_URL="{base}/v1"',
    ]


def close_engagement(settings: Settings, keep_vault: bool = False,
                     exports_dir: str | Path | None = None) -> dict:
    """Close-out ritual: dump the full mapping table, then retire the vault.

    Writes ``<engagement>-<ts>.json`` + ``.csv`` under
    ``~/.anonproxy/exports/<engagement>-<ts>/`` (or ``exports_dir``) for the
    evidence trail, then deletes the vault file unless ``keep_vault``.
    Note: an ephemeral-profile session has no disk vault — run close-out while
    the proxy still holds mappings, or don't use ephemeral when evidence matters.
    """
    from .engine import Engine  # local import: avoids engine<->profiles cycle at load

    eng = Engine(settings=settings)
    rows = eng.export()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.environ.get("ANONPROXY_EXPORTS_DIR",
                          str(Path.home() / ".anonproxy" / "exports"))
    out_dir = Path(exports_dir) if exports_dir else (
        Path(base) / f"{settings.engagement_id}-{stamp}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"{settings.engagement_id}-{stamp}.json"
    json_path.write_text(json.dumps(
        {"engagement": settings.engagement_id, "exported_at": stamp, "mappings": rows},
        indent=2))

    import csv
    csv_path = out_dir / f"{settings.engagement_id}-{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["entity_type", "original", "surrogate", "confidence"])
        for r in rows:
            w.writerow([r["entity_type"], r["original"], r["surrogate"],
                        r.get("confidence", 1.0)])

    vault_removed = False
    if not keep_vault and not settings.ephemeral:
        vp = settings.vault_path()
        for suffix in ("", "-wal", "-shm", ".enc"):
            p = Path(str(vp) + suffix)
            try:
                p.unlink()
                vault_removed = True
            except FileNotFoundError:
                continue
            except sqlite3.OperationalError:
                continue  # locked by a running proxy — leave it, say so
    return {"count": len(rows), "json": str(json_path), "csv": str(csv_path),
            "vault_removed": vault_removed}


class ProfileStore:
    """CRUD over ``<dir>/<name>.json``."""

    def __init__(self, directory: str | Path | None = None):
        if directory is None:
            directory = os.environ.get(
                "ANONPROXY_PROFILE_DIR",
                str(Path.home() / ".anonproxy" / "profiles"),
            )
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        return self.dir / f"{sanitize_name(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def save(self, profile: Profile) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(profile.name)
        profile.name = sanitize_name(profile.name)
        p.write_text(json.dumps(asdict(profile), indent=2) + "\n")
        return p

    def get(self, name: str) -> Profile | None:
        p = self.path(name)
        if not p.exists():
            return None
        try:
            return Profile.from_dict(json.loads(p.read_text()))
        except (json.JSONDecodeError, TypeError):
            return None

    def delete(self, name: str) -> bool:
        p = self.path(name)
        if p.exists():
            p.unlink()
            return True
        return False

    def list(self) -> list[Profile]:
        out: list[Profile] = []
        if not self.dir.exists():
            return out
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(Profile.from_dict(json.loads(p.read_text())))
            except (json.JSONDecodeError, TypeError):
                continue  # half-written/hand-edited file: skip, don't die
        # most recently used first, then alphabetical
        out.sort(key=lambda pr: (-pr.last_used_at, pr.name))
        return out

    def touch(self, name: str) -> None:
        prof = self.get(name)
        if prof:
            prof.last_used_at = time.time()
            self.save(prof)

    def most_recent(self) -> Profile | None:
        lst = self.list()
        return lst[0] if lst else None


def copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard put (macOS pbcopy, X11 wl-copy/xclip)."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        try:
            proc = subprocess_run(cmd, text)
            if proc.returncode == 0:
                return True
        except FileNotFoundError:
            continue
    return False


def subprocess_run(cmd: list[str], stdin_text: str):
    import subprocess
    return subprocess.run(cmd, input=stdin_text, text=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_in_editor(path: Path) -> None:
    """Open a file/folder in the user's editor (GUI-aware, best-effort)."""
    import subprocess
    if shutil.which("open"):           # macOS
        subprocess.Popen(["open", "-t", str(path)])
    elif os.environ.get("EDITOR"):
        subprocess.Popen([os.environ["EDITOR"], str(path)])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)])
