"""Chat-model (LLM) factory.

Isolated so the concrete provider (Google Gemini today) stays an implementation detail,
and so a per-request key can be supplied for bring-your-own-keys requests.
"""

from __future__ import annotations

from typing import Any

from ragchat.config import Settings, get_settings


def build_llm(settings: Settings | None = None, *, google_key: str | None = None) -> Any:
    """Construct the Gemini chat model.

    ``google_key`` overrides the configured shared key (used for bring-your-own-keys
    requests); it is never persisted or logged. Returns ``Any`` because the provider SDK
    is imported lazily.
    """
    settings = settings or get_settings()
    key = google_key or settings.google_api_key.get_secret_value()

    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        google_api_key=key,
        temperature=settings.llm_temperature,
    )
