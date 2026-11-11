"""Configuration, read once from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default


def _str(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v is not None and v.strip() != "" else default


# Map a language code to a PostgreSQL text search configuration.
_TS_CONFIGS = {
    "de": "german", "en": "english", "fr": "french", "nl": "dutch", "it": "italian",
    "es": "spanish", "pt": "portuguese", "da": "danish", "sv": "swedish", "no": "norwegian",
    "fi": "finnish", "ru": "russian", "hu": "hungarian", "ro": "romanian", "tr": "turkish",
}


@dataclass
class Settings:
    # Public URL under which the wiki is reachable (used for setup and result links)
    public_url: str = field(default_factory=lambda: _public_url())

    # Database (shared PostGIS instance; Wiki.js uses schema "public", we use "pkdb")
    db_host: str = field(default_factory=lambda: _str("POSTGRES_HOST", "db"))
    db_port: int = field(default_factory=lambda: _int("POSTGRES_PORT", 5432))
    db_name: str = field(default_factory=lambda: _str("POSTGRES_DB", "pkdb"))
    db_user: str = field(default_factory=lambda: _str("POSTGRES_USER", "pkdb"))
    db_password: str = field(default_factory=lambda: _str("POSTGRES_PASSWORD", "pkdb"))

    # Wiki.js
    wiki_internal_url: str = field(default_factory=lambda: _str("WIKI_INTERNAL_URL", "http://wiki:3000").rstrip("/"))
    wiki_admin_email: str = field(default_factory=lambda: _str("WIKI_ADMIN_EMAIL", "admin@example.com"))
    wiki_admin_password: str = field(default_factory=lambda: _str("WIKI_ADMIN_PASSWORD", ""))
    wiki_title: str = field(default_factory=lambda: _str("WIKI_TITLE", "Knowledge Base"))
    wiki_public: bool = field(default_factory=lambda: _bool("WIKI_PUBLIC", False))
    wiki_locale: str = field(default_factory=lambda: _str("WIKI_LOCALE", "en"))
    wiki_language: str = field(default_factory=lambda: _str("WIKI_LANGUAGE", "en"))
    wiki_media_folder: str = field(default_factory=lambda: _str("WIKI_MEDIA_FOLDER", "pkdb-media"))

    # Auth
    auth_mode: str = field(default_factory=lambda: _str("AUTH_MODE", "builtin").lower())
    static_api_tokens: list[str] = field(
        default_factory=lambda: [t.strip() for t in _str("PKDB_API_TOKENS").split(",") if t.strip()]
    )

    # LLM
    llm_provider: str = field(default_factory=lambda: _str("LLM_PROVIDER", "openai").lower())
    llm_base_url: str = field(default_factory=lambda: _str("LLM_BASE_URL", "").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: _str("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _str("LLM_MODEL", "gpt-4o-mini"))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.2))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 8000))
    llm_timeout: int = field(default_factory=lambda: _int("LLM_TIMEOUT", 600))
    llm_json_mode: bool = field(default_factory=lambda: _bool("LLM_JSON_MODE", True))
    llm_context_chars: int = field(default_factory=lambda: _int("LLM_CONTEXT_CHARS", 12000))
    llm_max_related_pages: int = field(default_factory=lambda: _int("LLM_MAX_RELATED_PAGES", 5))
    llm_extra_instructions: str = field(default_factory=lambda: _str("LLM_EXTRA_INSTRUCTIONS", ""))
    llm_apply_mode: str = field(default_factory=lambda: _str("LLM_APPLY_MODE", "review").lower())

    # Search
    search_min_match: float = field(default_factory=lambda: _float("SEARCH_MIN_MATCH", 0.5))
    search_max_results: int = field(default_factory=lambda: _int("SEARCH_MAX_RESULTS", 5))

    # Storage & limits
    data_dir: str = field(default_factory=lambda: _str("DATA_DIR", "/data"))
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 200))
    max_features_per_layer: int = field(default_factory=lambda: _int("MAX_FEATURES_PER_LAYER", 50000))

    @property
    def ts_config(self) -> str:
        return _TS_CONFIGS.get(self.wiki_language.lower()[:2], "simple")

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"
        )


def _public_url() -> str:
    url = _str("PUBLIC_URL")
    if not url:
        host = _str("PKDB_HOST")
        if host:
            url = f"https://{host}"
        else:
            port = _str("HTTP_PORT", "80")
            url = "http://localhost" + ("" if port == "80" else f":{port}")
    return url.rstrip("/")


settings = Settings()
