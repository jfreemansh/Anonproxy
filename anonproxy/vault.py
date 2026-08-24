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

Keys are exact-text: an original always resolves to the same surrogate on an
exact re-sighting, but two DIFFERENT casings of the same real-world entity
(``WordPress.org`` vs ``wordpress.org`` both appearing in one page) get their
own independent surrogates rather than collapsing onto one. Collapsing them
onto one broke round-trip: the vault can only remember ONE original spelling
per surrogate, so restoring a second, differently-cased occurrence produced
the wrong casing (or, when the surrogate's own boundary-swallow logic used to
strip content around it, lost data entirely). The ``norm`` column is kept for
lookup/analysis but is no longer the identity key.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import surrogates
from .config import Settings

log = logging.getLogger("anonproxy.vault")

# --- at-rest encryption (opt-in) --------------------------------------------
# Whole-file AES-GCM envelope next to where the plaintext sqlite would live.
# The DB runs from a private temp file while the process is up; every commit
# re-seals the envelope. Format: ANPENC1 | salt(16) | nonce(12) | ciphertext.
_ENC_MAGIC = b"ANPENC1"
_SALT_LEN = 16
_NONCE_LEN = 12
_KDF_ITERS = 600_000


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, _KDF_ITERS)


class Vault:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._enc_key: Optional[bytes] = None
        self._enc_path: Optional[str] = None
        self._tmp_path: Optional[Path] = None

        passphrase = settings.vault_passphrase or ""
        if settings.vault_keyfile:
            kf = Path(settings.vault_keyfile)
            passphrase = kf.read_text().splitlines()[0].strip()
        if passphrase:
            safe = "".join(c if c.isalnum() or c in "-_." else "_"
                           for c in settings.engagement_id)
            self._enc_path = str(settings.vault_path()) + ".enc"
            self._tmp_path = settings.vault_dir / f".{safe}.plain.sqlite"
            # one salt per envelope, chosen once (file's salt if present)
            enc_exists = os.path.exists(self._enc_path)
            self._salt = self._read_envelope_salt() if enc_exists \
                else secrets.token_bytes(_SALT_LEN)
            self._passphrase = passphrase.encode()
            self._enc_key = _derive_key(self._passphrase, self._salt)
            if enc_exists:
                self._unseal_to(self._tmp_path)
            elif settings.vault_path().exists():
                # adopt an existing plaintext vault: next persist encrypts it
                log.info("adopting plaintext vault %r — will be encrypted "
                         "on next write", settings.engagement_id)
                import shutil
                shutil.copyfile(settings.vault_path(), self._tmp_path)
                os.chmod(self._tmp_path, 0o600)
            self._conn = sqlite3.connect(str(self._tmp_path), check_same_thread=False)
        elif settings.ephemeral:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        elif os.path.exists(str(settings.vault_path()) + ".enc"):
            raise RuntimeError(
                f"engagement {settings.engagement_id!r} has an ENCRYPTED vault "
                f"but no key is configured — set ANONPROXY_VAULT_PASSPHRASE or "
                f"restore ~/.anonproxy/vault.key")
        else:
            self._conn = sqlite3.connect(str(settings.vault_path()), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        # in-process caches for hot-path speed
        # ponytail: single-process only — loaded once with no cross-process
        # invalidation, so two workers sharing this vault file could mint
        # different surrogates for the same original. cli.py always runs one
        # process (no uvicorn `workers=`); if that ever changes, this needs a
        # shared cache (e.g. push the uniqueness check into SQLite itself).
        self._fwd: dict[str, str] = {}     # normalized original -> surrogate
        self._rev: dict[str, str] = {}     # surrogate -> original
        self._load_cache()

    @staticmethod
    def _safe_name() -> str:
        s = Settings()
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in s.engagement_id)
        return safe

    def _read_envelope_salt(self) -> bytes:
        enc = str(self.settings.vault_path()) + ".enc"
        with open(enc, "rb") as fh:
            if fh.read(len(_ENC_MAGIC)) != _ENC_MAGIC:
                raise RuntimeError(f"{enc} is not an anonproxy encrypted vault")
            return fh.read(_SALT_LEN)

    def _seal(self) -> None:
        """Re-encrypt the temp DB into the envelope (atomic replace)."""
        assert self._enc_key and self._enc_path and self._tmp_path
        with open(self._tmp_path, "rb") as fh:
            plaintext = fh.read()
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = AESGCM(self._enc_key).encrypt(nonce, plaintext, None)
        tmp_enc = self._enc_path + ".tmp"
        with open(tmp_enc, "wb") as fh:
            fh.write(_ENC_MAGIC + self._salt + nonce + ct)
        os.chmod(tmp_enc, 0o600)
        os.replace(tmp_enc, self._enc_path)

    def _unseal_to(self, dest: Path) -> None:
        assert self._enc_key and self._enc_path
        with open(self._enc_path, "rb") as fh:
            blob = fh.read()
        header = len(_ENC_MAGIC) + _SALT_LEN + _NONCE_LEN
        try:
            plaintext = AESGCM(self._enc_key).decrypt(
                blob[len(_ENC_MAGIC) + _SALT_LEN:len(_ENC_MAGIC) + _SALT_LEN + _NONCE_LEN],
                blob[header:], None)
        except Exception as e:
            raise RuntimeError(
                f"wrong ANONPROXY_VAULT_PASSPHRASE for engagement "
                f"{self.settings.engagement_id!r} (decryption failed)") from e
        with open(dest, "wb") as fh:
            fh.write(plaintext)
        os.chmod(dest, 0o600)

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mappings (
                original    TEXT NOT NULL,
                norm        TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                surrogate   TEXT NOT NULL,
                created_at  REAL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (original)
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_surrogate ON mappings(surrogate)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_norm ON mappings(norm)")
        self._conn.commit()

    def _load_cache(self) -> None:
        for original, _norm, surrogate in self._conn.execute(
            "SELECT original, norm, surrogate FROM mappings"
        ):
            self._fwd[original] = surrogate
            self._rev[surrogate] = original

    @staticmethod
    def _norm(text: str) -> str:
        return text.casefold()

    # -- public API ---------------------------------------------------------
    def get_or_create(self, original: str, entity_type: str) -> tuple[str, bool]:
        """Return ``(surrogate, is_new)`` for ``original`` (exact text)."""
        norm = self._norm(original)
        with self._lock:
            existing = self._fwd.get(original)
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
                log.error("could not generate a unique surrogate for %r (%s) "
                          "after 64 salted attempts", original, entity_type)
                raise RuntimeError("could not generate a unique surrogate")

            self._conn.execute(
                "INSERT OR REPLACE INTO mappings(original, norm, entity_type, surrogate) "
                "VALUES (?,?,?,?)",
                (original, norm, entity_type, surrogate),
            )
            self._conn.commit()
            if self._enc_key:
                self._seal()
            self._fwd[original] = surrogate
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

    def delete_originals(self, originals: list[str]) -> list[str]:
        """Remove mappings by exact original text (vault hygiene: prune
        detection artifacts without wiping the engagement). Returns the
        originals that existed and were deleted."""
        gone: list[str] = []
        with self._lock:
            for original in originals:
                surrogate = self._fwd.get(original)
                if surrogate is None:
                    continue
                self._conn.execute(
                    "DELETE FROM mappings WHERE original = ?", (original,))
                self._fwd.pop(original, None)
                self._rev.pop(surrogate, None)
                gone.append(original)
            if gone:
                self._conn.commit()
                if self._enc_key:
                    self._seal()
        return gone

    def surrogate_for(self, original: str) -> Optional[str]:
        return self._fwd.get(original)

    def original_for(self, surrogate: str) -> Optional[str]:
        return self._rev.get(surrogate)

    def stats(self) -> dict:
        with self._lock:
            by_type: dict[str, int] = {}
            for (etype,) in self._conn.execute("SELECT entity_type FROM mappings"):
                by_type[etype] = by_type.get(etype, 0) + 1
            return {"total": len(self._rev), "by_type": by_type,
                    "engagement": self.settings.engagement_id,
                    "encrypted": bool(self._enc_key)}

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
            if self._enc_key:
                try:
                    self._seal()
                except Exception:      # best effort on shutdown
                    log.exception("final vault seal failed")
            self._conn.close()
            if self._tmp_path and self._tmp_path.exists():
                try:
                    os.unlink(self._tmp_path)
                except OSError:
                    pass
