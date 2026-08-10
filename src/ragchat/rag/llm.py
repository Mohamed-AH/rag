"""Chat-model (LLM) factory.

Isolated so the concrete provider (Google Gemini today) stays an implementation detail,
and so a per-request key can be supplied for bring-your-own-keys requests.
"""

from __future__ import annotations

import base64
from typing import Any

from ragchat.config import Settings, get_settings
from ragchat.ingestion.router import DocumentContent


def build_llm(
    settings: Settings | None = None,
    *,
    google_key: str | None = None,
    max_retries: int | None = None,
) -> Any:
    """Construct the Gemini chat model.

    ``google_key`` overrides the configured shared key (used for bring-your-own-keys
    requests); it is never persisted or logged. ``max_retries`` bounds provider retries so
    a quota/rate error fails fast instead of blocking a worker. Returns ``Any`` because the
    provider SDK is imported lazily.
    """
    settings = settings or get_settings()
    key = google_key or settings.google_api_key.get_secret_value()

    from langchain_google_genai import ChatGoogleGenerativeAI

    extra = {} if max_retries is None else {"max_retries": max_retries}
    return ChatGoogleGenerativeAI(
        model=settings.llm_model,
        google_api_key=key,
        temperature=settings.llm_temperature,
        **extra,
    )


def build_vision_llm(
    settings: Settings | None = None,
    *,
    google_key: str | None = None,
    max_retries: int | None = None,
) -> Any:
    """Construct the multimodal Gemini model used for the scanned-document path.

    Same provider as :func:`build_llm` but on the multimodal tier (``vision_model``) so it
    can read page images and PDFs, not just text. ``max_retries`` bounds provider retries.
    """
    settings = settings or get_settings()
    key = google_key or settings.google_api_key.get_secret_value()

    from langchain_google_genai import ChatGoogleGenerativeAI

    extra = {} if max_retries is None else {"max_retries": max_retries}
    return ChatGoogleGenerativeAI(
        model=settings.vision_model,
        google_api_key=key,
        temperature=settings.llm_temperature,
        **extra,
    )


def build_document_message(instruction: str, content: DocumentContent) -> Any:
    """Build a LangChain human message from prepared :class:`DocumentContent`.

    Text-path content becomes a plain string message; image-path content becomes a
    multimodal message with the instruction plus inline ``data:`` parts. Returns ``Any``
    because the message type comes from the lazily imported provider stack.
    """
    from langchain_core.messages import HumanMessage

    if content.mode != "image":
        return HumanMessage(content=f"{instruction}\n\n{content.text or ''}")

    parts: list[str | dict[str, Any]] = [{"type": "text", "text": instruction}]
    for part in content.media:
        encoded = base64.b64encode(part.data).decode("ascii")
        parts.append({"type": "image_url", "image_url": f"data:{part.mime_type};base64,{encoded}"})
    return HumanMessage(content=parts)
