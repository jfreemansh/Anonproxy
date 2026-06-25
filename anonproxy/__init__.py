"""
Anonproxy — a reversible anonymization layer for sending pentest data to LLMs.

A more reliable successor to the match/replace approach: deterministic
format-preserving surrogates, a session-consistent vault, multi-pass detection
(regex floor + optional local LLM + known-entity rescan), and — most importantly
— a *tolerant, streaming-safe restorer* that puts the real values back even when
the model mangles a surrogate (markdown, backticks, line wraps, case changes) or
splits it across streaming chunks.

Public API:
    from anonproxy import Engine
    eng = Engine(engagement="acme-2026")
    safe = eng.anonymize(text)            # real -> surrogate
    back = eng.deanonymize(safe)          # surrogate -> real (tolerant)
"""

from .engine import Engine
from .config import Settings

__all__ = ["Engine", "Settings"]
__version__ = "0.1.0"
