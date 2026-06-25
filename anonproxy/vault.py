"""
Per-engagement surrogate vault.

Maps ``original <-> surrogate`` with three guarantees:

* **Consistency** — an original always resolves to the same surrogate within an
  engagement (the surrogate is also deterministically derived, so it survives a
  lost vault).
* **Reversibility** — every surrogate has exactly one original (collisions in
  the deterministic generator are detected and broken with a salt).
* **Isolation** — one SQLite file per ``engagement_id``; optionally in-memory
  only (``ephemeral``) so nothing touches disk.

Keys are stored both verbatim and normalized (casefold) so a later sighting of
the same entity in different case still maps consistently.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Callable, Optional

from .config import Settings
from . import surrogates


class Vault:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        if settings.ephemeral:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self._conn = sqlite3.connect(str(settings.vault_path()), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        # in-process caches for hot-path speed
        self._fwd: dict[str, str] = {}     # normalized original -> surrogate
        self._rev: dict[str, str] = {}     # surrogate -> original
        self._load_cache()

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                original    TEXT NOT NULL,
                norm        TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                surrogate   TEXT NOT NULL,
                created_at  REAL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (norm)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_surrogate ON mappings(surrogate)")
        self._conn.commit()

    def _load_cache(self) -> None:
        for original, norm, surrogate in self._conn.execute(
            "SELECT original, norm, surrogate FROM mappings"
        ):
            self._fwd[norm] = surrogate
            self._rev[surrogate] = original

    @staticmethod
    def _norm(text: str) -> str:
        return text.casefold()

    # -- public API ---------------------------------------------------------
    def get_or_create(self, original: str, entity_type: str) -> tuple[str, bool]:
        """Return ``(surrogate, is_new)`` for ``original``."""
        norm = self._norm(original)
        with self._lock:
            existing = self._fwd.get(norm)
            if existing is not None:
                return existing, False

            # deterministic generation, collision-broken by salt
            salt = ""
            for attempt in range(64):
                surrogate = surrogates.generate(
                    entity_type, original,
                    engagement=self.settings.engagement_id, salt=salt,
                )
                if surrogate not in self._rev and surrogate != original:
                    break
                salt = f"#{attempt}"
            else:  # pragma: no cover - astronomically unlikely
                raise RuntimeError("could not generate a unique surrogate")

            self._conn.execute(
                "INSERT OR REPLACE INTO mappings(original, norm, entity_type, surrogate) "
                "VALUES (?,?,?,?)",
                (original, norm, entity_type, surrogate),
            )
            self._conn.commit()
            self._fwd[norm] = surrogate
            self._rev[surrogate] = original
            return surrogate, True

    def all_mappings(self) -> list[tuple[str, str]]:
        """``(surrogate, original)`` pairs, longest surrogate first.

        Longest-first ordering prevents a short surrogate from matching inside a
        longer one during restoration.
        """
        with self._lock:
            items = list(self._rev.items())
        items.sort(key=lambda kv: len(kv[0]), reverse=True)
        return items

    def known_originals(self) -> list[tuple[str, str]]:
        """``(original, entity_type)`` pairs, longest original first — used by the
        consistency rescan so an entity seen once is always caught again."""
        with self._lock:
            rows = list(
                self._conn.execute("SELECT original, entity_type FROM mappings")
            )
        rows.sort(key=lambda r: len(r[0]), reverse=True)
        return rows

    def surrogate_for(self, original: str) -> Optional[str]:
        return self._fwd.get(self._norm(original))

    def original_for(self, surrogate: str) -> Optional[str]:
        return self._rev.get(surrogate)

    def stats(self) -> dict:
        with self._lock:
            by_type: dict[str, int] = {}
            for (etype,) in self._conn.execute("SELECT entity_type FROM mappings"):
                by_type[etype] = by_type.get(etype, 0) + 1
            return {"total": len(self._rev), "by_type": by_type,
                    "engagement": self.settings.engagement_id}

    def export(self) -> list[dict]:
        with self._lock:
            return [
                {"original": o, "entity_type": t, "surrogate": s}
                for o, t, s in self._conn.execute(
                    "SELECT original, entity_type, surrogate FROM mappings ORDER BY created_at"
                )
            ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
