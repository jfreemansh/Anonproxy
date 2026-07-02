"""Configuration — environment driven, sensible defaults for engagements."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    """All runtime configuration.  Every field has an env override."""

    # --- Engagement isolation ------------------------------------------------
    engagement_id: str = field(
        default_factory=lambda: os.environ.get("ENGAGEMENT_ID", "default")
    )
    vault_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "ANONPROXY_VAULT_DIR",
                str(Path.home() / ".anonproxy" / "vaults"),
            )
        )
    )
    # Keep the vault only in memory (nothing persisted to disk after exit).
    ephemeral: bool = field(default_factory=lambda: _env_bool("ANONPROXY_EPHEMERAL", False))

    # --- Detection -----------------------------------------------------------
    # Ordered backend chain. "regex" is the deterministic floor; the rest are
    # contextual. Out-of-box default needs no extra dependencies. Colleagues who
    # want more recall can set ANONPROXY_DETECTORS=regex,gliner or add piiranha /
    # anonymizer-slm. Unknown or unavailable backends are skipped with a warning.
    detectors: list[str] = field(
        default_factory=lambda: _env_list("ANONPROXY_DETECTORS", ["regex", "ollama"])
    )

    # Engagement scope seed: known client identifiers (domains, bare hostnames,
    # org names, IPs/CIDRs) that must ALWAYS be anonymized — even bare names regex
    # can't infer. Provide inline (comma list) and/or via a file (one per line,
    # optional "value=TYPE"). These run as part of the deterministic floor.
    scope_terms: list[str] = field(default_factory=lambda: _env_list("ANONPROXY_SCOPE", []))
    scope_file: str = field(default_factory=lambda: os.environ.get("ANONPROXY_SCOPE_FILE", ""))

    llm_enabled: bool = field(default_factory=lambda: _env_bool("LLM_ENABLED", True))
    ollama_host: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen3:4b")
    )
    ollama_timeout: int = field(default_factory=lambda: _env_int("OLLAMA_TIMEOUT", 60))
    llm_chunk_size: int = field(default_factory=lambda: _env_int("LLM_CHUNK_SIZE", 1500))
    llm_chunk_overlap: int = field(default_factory=lambda: _env_int("LLM_CHUNK_OVERLAP", 200))

    # Optional backends (only used if named in `detectors`).
    gliner_model: str = field(
        default_factory=lambda: os.environ.get("GLINER_MODEL", "urchade/gliner_multi_pii-v1")
    )
    gliner_threshold: float = field(default_factory=lambda: _env_float("GLINER_THRESHOLD", 0.5))
    # GLiNER2-PII (Fastino) — current SOTA recall; uses the separate `gliner2` lib.
    gliner2_model: str = field(
        default_factory=lambda: os.environ.get(
            "GLINER2_MODEL", "fastino/gliner2-privacy-filter-PII-multi")
    )
    gliner2_threshold: float = field(default_factory=lambda: _env_float("GLINER2_THRESHOLD", 0.5))
    piiranha_model: str = field(
        default_factory=lambda: os.environ.get(
            "PIIRANHA_MODEL", "iiiorg/piiranha-v1-detect-personal-information")
    )
    # OpenAI Privacy Filter — official org is exactly "openai"; beware typosquats.
    openai_pii_model: str = field(
        default_factory=lambda: os.environ.get("OPENAI_PII_MODEL", "openai/privacy-filter")
    )
    # Anonymizer SLM served via Ollama (import the GGUF with the provided Modelfile).
    anonymizer_slm_model: str = field(
        default_factory=lambda: os.environ.get("ANONYMIZER_SLM_MODEL", "anonymizer-slm")
    )

    # --- Restoration tolerance ----------------------------------------------
    # How permissive the deanonymizer is about characters injected *between*
    # surrogate characters (markdown emphasis, zero-width spaces, backticks).
    restore_tolerant: bool = field(default_factory=lambda: _env_bool("ANONPROXY_TOLERANT", True))

    # --- Proxy ---------------------------------------------------------------
    port: int = field(default_factory=lambda: _env_int("PORT", 8080))
    host: str = field(default_factory=lambda: os.environ.get("HOST", "127.0.0.1"))
    anthropic_upstream: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
    )
    openai_upstream: str = field(
        default_factory=lambda: os.environ.get("OPENAI_UPSTREAM", "https://api.openai.com")
    )
    # Local engine HTTP API the Burp extension talks to (anonymize/deanonymize).
    engine_api_token: str = field(
        default_factory=lambda: os.environ.get("ANONPROXY_API_TOKEN", "")
    )

    # --- Audit dashboard -----------------------------------------------------
    audit_enabled: bool = field(default_factory=lambda: _env_bool("ANONPROXY_AUDIT", True))

    # Fail closed instead of forwarding raw bytes when a request body can't be
    # parsed (so it can't be anonymized). Off by default so odd/legacy clients
    # keep working; turn on if you want a hard guarantee over convenience.
    strict_mode: bool = field(default_factory=lambda: _env_bool("ANONPROXY_STRICT", False))

    def __post_init__(self):
        # Normalise the Ollama endpoint. People commonly export
        # OLLAMA_HOST=0.0.0.0:11434 to make the *server* listen on all
        # interfaces, but a *client* must dial a real address and needs a
        # scheme. Fix both so detection doesn't silently fall back to regex.
        h = (self.ollama_host or "").strip()
        if h and "://" not in h:
            h = "http://" + h
        h = h.replace("://0.0.0.0", "://127.0.0.1")
        self.ollama_host = h.rstrip("/")

    def vault_path(self) -> Path:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        # one sqlite file per engagement keeps mappings isolated
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.engagement_id)
        return self.vault_dir / f"{safe}.sqlite"
