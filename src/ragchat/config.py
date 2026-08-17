"""Typed, environment-driven application configuration.

All runtime configuration is centralised here so the rest of the codebase never
reaches into ``os.environ`` directly. Values are read from the environment (and a
local ``.env`` file, if present) and validated at load time — a missing or malformed
setting fails fast with a clear error rather than surfacing deep inside a request.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Datastore ---------------------------------------------------------
    database_url: str = Field(
        ...,
        description="PostgreSQL connection URL, e.g. postgresql://user:pass@host:5432/db",
    )

    # --- Providers ---------------------------------------------------------
    cohere_api_key: SecretStr = Field(..., description="API key for Cohere embeddings.")
    google_api_key: SecretStr = Field(..., description="API key for Google Gemini.")

    # --- Model / retrieval tunables ---------------------------------------
    embedding_model: str = Field(
        default="embed-english-v3.0",
        description="Cohere embedding model. Must match embedding_dimension.",
    )
    embedding_dimension: int = Field(
        default=1024,
        description="Vector dimension of the embedding model (embed-english-v3.0 -> 1024).",
    )
    llm_model: str = Field(
        default="gemini-flash-lite-latest",
        description="Gemini chat model. Defaults to the 'flash-lite-latest' alias: the "
        "lite tier has the most generous free quota and the alias tracks a current model "
        "as Google rotates versions; override with LLM_MODEL.",
    )
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    retriever_k: int = Field(default=3, ge=1, le=20, description="Documents to retrieve.")
    collection_name: str = Field(
        default="qa_knowledge_base",
        description="Default pgvector collection (used by the CLI 'default' session).",
    )

    # --- Multi-tenancy / lifecycle ----------------------------------------
    session_ttl_hours: int = Field(
        default=24,
        ge=1,
        description="Hours a hosted session's uploaded data is retained before cleanup.",
    )

    # --- Upload limits (guardrails; env-tunable without a redeploy) --------
    max_upload_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        description="Maximum accepted upload size in bytes (default 2 MiB).",
    )
    max_sections_per_upload: int = Field(
        default=150,
        ge=1,
        description="Maximum sections/chunks a single upload may produce.",
    )
    chunk_max_chars: int = Field(
        default=1200, ge=100, description="Target chunk size for non-markdown documents."
    )
    chunk_overlap_chars: int = Field(
        default=150, ge=0, description="Overlap between adjacent chunks."
    )

    # --- Packet auditor ----------------------------------------------------
    active_checklist: str = Field(
        default="customs",
        description="Id of the checklist a packet is audited against (see "
        "ragchat.audit.checklist). v1 ships 'customs'.",
    )
    max_files_per_packet: int = Field(
        default=25,
        ge=1,
        description="Maximum number of files accepted in a single audited packet.",
    )
    vision_model: str = Field(
        default="gemini-flash-lite-latest",
        description="Gemini model for the scanned-document/image path. Flash-Lite is "
        "natively multimodal, so the same high-free-quota lite tier reads scans too; "
        "override with a heavier multimodal model only if real scans need more OCR accuracy.",
    )

    # --- Audit model fallback ladder --------------------------------------
    # When the primary (Gemini) is quota-exhausted, the audit analyzer transparently retries
    # the same structured call on the next available provider. Each provider is used only if
    # its key (and, for openai_compat, its base_url) is set — otherwise it is skipped — so
    # the app ships working on Gemini alone and each rung is opt-in via a dashboard secret.
    # Order and model ids are env-tunable so a churned free model can be swapped without a
    # redeploy. All fallbacks are OpenAI/LangChain-compatible and multimodal-capable.
    audit_model_order: str = Field(
        default="gemini,mistral,groq",
        description="Comma-separated provider ids forming the audit fallback ladder, primary "
        "first. Providers without a configured key/URL are skipped. Ids: gemini, mistral, "
        "groq, openai_compat (openai_compat is available but off by default — add it to the "
        "order and set its key/URL to enable, e.g. an OpenRouter endpoint).",
    )
    mistral_api_key: SecretStr | None = Field(
        default=None, description="API key for Mistral (free tier). Enables the mistral rung."
    )
    mistral_model: str = Field(default="mistral-small-latest")
    mistral_vision_model: str = Field(default="pixtral-12b-latest")
    groq_api_key: SecretStr | None = Field(
        default=None, description="API key for Groq (free tier). Enables the groq rung."
    )
    # gpt-oss-120b is a strong text reasoning model on Groq's free tier, but text-only, so
    # the scan path keeps a separate multimodal Groq model (Llama-4 Scout).
    groq_model: str = Field(default="openai/gpt-oss-120b")
    groq_vision_model: str = Field(default="meta-llama/llama-4-scout-17b-16e-instruct")
    openai_compat_api_key: SecretStr | None = Field(
        default=None,
        description="API key for any OpenAI-compatible endpoint (e.g. OpenRouter). Enables "
        "the openai_compat rung.",
    )
    openai_compat_base_url: str | None = Field(
        default=None,
        description="Base URL for the OpenAI-compatible endpoint, e.g. "
        "https://openrouter.ai/api/v1. Required for the openai_compat rung.",
    )
    openai_compat_model: str = Field(
        default="qwen/qwen2.5-vl-72b-instruct:free",
        description="Model id served by the OpenAI-compatible endpoint. On OpenRouter the "
        "':free' Qwen2.5-VL variant is multimodal; free model ids rotate, so keep this tunable.",
    )

    # --- Rate limiting & cost control (in-memory; per instance) ------------
    rate_limit_asks_per_minute: int = Field(
        default=10,
        ge=1,
        description="Max /ask calls per session per minute (kept under the Gemini "
        "free-tier RPM so the shared key isn't throttled).",
    )
    rate_limit_ingests_per_hour: int = Field(
        default=20, ge=1, description="Max uploads per session per hour."
    )
    daily_request_budget: int = Field(
        default=1000,
        ge=0,
        description="Max shared-key asks/day across the whole instance (0 = unlimited). "
        "The absolute cost ceiling that keeps usage within the provider free tier.",
    )
    daily_free_allowance: int = Field(
        default=10,
        ge=0,
        description="Free shared-key asks/day per user before they must supply their own "
        "keys (0 = unlimited). Enforced per hashed-IP + cookie.",
    )
    daily_audit_free_allowance: int = Field(
        default=15,
        ge=0,
        description="Free shared-key audits/day per user (hashed-IP) before they must supply "
        "their own Google key (0 = unlimited). One audit = one model call per file, so this "
        "is metered separately from /ask and keyed on IP (not the client-set cookie).",
    )
    daily_audit_budget: int = Field(
        default=150,
        ge=0,
        description="Max shared-key audits/day across the whole instance (0 = unlimited). "
        "The absolute ceiling that keeps audit usage within the provider free tier even if "
        "the per-user limit is evaded.",
    )
    usage_hash_salt: SecretStr = Field(
        default=SecretStr("change-me-in-prod"),
        description="Salt for hashing client IPs before storing usage counters (privacy). "
        "Set a real secret in production.",
    )
    trusted_proxy_hops: int = Field(
        default=1,
        ge=1,
        description="Number of trusted reverse-proxy hops in front of the app (Render = 1). "
        "Used to read the real client IP from the right of X-Forwarded-For, resisting "
        "client-spoofed values.",
    )

    # --- Observability -----------------------------------------------------
    log_level: str = Field(default="INFO")

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
            raise ValueError(
                "database_url must be a PostgreSQL URL "
                "(postgresql://... or postgresql+psycopg://...)"
            )
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """Connection URL normalised to the psycopg (v3) driver.

        ``langchain-postgres`` and our SQLAlchemy engine both require the psycopg3
        driver. Users are free to set a plain ``postgresql://`` URL in ``.env``;
        we rewrite the scheme so a driver mismatch can never happen at runtime.
        """
        url = self.database_url
        if url.startswith("postgresql+psycopg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        # postgres:// (legacy alias)
        return url.replace("postgres://", "postgresql+psycopg://", 1)


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance.

    Cached so configuration is parsed once per process. Tests clear the cache via
    ``get_settings.cache_clear()`` when they need to inject a different environment.
    """
    return Settings()  # values are populated from the environment / .env
