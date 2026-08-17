"""The audit model **fallback ladder**.

The audit analyzer makes one structured, (optionally) multimodal call per file. When the
primary provider (Gemini) is quota-exhausted or transiently failing, we want the *same*
call to retry on the next available provider instead of failing the audit — the whole
point being resilience when a free tier runs out.

This module builds that ladder as two ordered lists of LangChain chat models — one for the
text path, one for the multimodal/scan path — from :class:`~ragchat.config.Settings`. A
provider is included only when its key (and, for ``openai_compat``, its base URL) is
configured, so the app ships working on Gemini alone and every extra rung is opt-in via a
dashboard secret. Order and model ids are env-tunable, so a churned free model can be
swapped without a redeploy.

Every provider integration is imported **lazily** inside its builder, so importing this
module (and the app, and the test suite) never requires the provider SDKs to be installed.
The fallback *trigger* is a provider-agnostic predicate on the exception
(:func:`is_retryable_provider_error`), so no provider-specific exception class needs to be
importable for the retry logic to work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ragchat.config import Settings

# Substrings (matched against the exception's type name + message, lower-cased) that mark a
# failure worth retrying on the next provider: quota/rate limits and transient upstream
# errors. A non-matching error (e.g. a schema/validation bug) is NOT retried — it surfaces
# immediately rather than silently burning every rung.
_RETRYABLE_MARKERS: tuple[str, ...] = (
    "resource_exhausted",
    "quota",
    "rate limit",
    "ratelimit",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "overloaded",
    "unavailable",
    "temporarily",
    "timeout",
    "timed out",
    "try again",
)


def is_retryable_provider_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a quota/rate/transient provider error worth failing over."""
    blob = f"{type(exc).__name__} {exc}".lower()
    return any(marker in blob for marker in _RETRYABLE_MARKERS)


@dataclass(frozen=True)
class _Provider:
    """One rung of the ladder: how to tell if it's configured, and how to build its models."""

    id: str
    multimodal: bool
    available: Callable[[Settings], bool]
    build_text: Callable[[Settings, int | None], Any]
    build_vision: Callable[[Settings, int | None], Any]


def _retry_kwargs(max_retries: int | None) -> dict[str, int]:
    return {} if max_retries is None else {"max_retries": max_retries}


# --- Provider builders (SDKs imported lazily) -----------------------------


def _build_gemini_text(settings: Settings, max_retries: int | None) -> Any:
    from ragchat.rag.llm import build_llm

    return build_llm(settings, max_retries=max_retries)


def _build_gemini_vision(settings: Settings, max_retries: int | None) -> Any:
    from ragchat.rag.llm import build_vision_llm

    return build_vision_llm(settings, max_retries=max_retries)


def _build_mistral(settings: Settings, model: str, max_retries: int | None) -> Any:
    from langchain_mistralai import ChatMistralAI

    assert settings.mistral_api_key is not None  # guarded by availability
    return ChatMistralAI(
        model=model,
        api_key=settings.mistral_api_key.get_secret_value(),
        temperature=settings.llm_temperature,
        **_retry_kwargs(max_retries),
    )


def _build_groq(settings: Settings, model: str, max_retries: int | None) -> Any:
    from langchain_groq import ChatGroq

    assert settings.groq_api_key is not None
    return ChatGroq(
        model=model,
        api_key=settings.groq_api_key.get_secret_value(),
        temperature=settings.llm_temperature,
        **_retry_kwargs(max_retries),
    )


def _build_openai_compat(settings: Settings, max_retries: int | None) -> Any:
    from langchain_openai import ChatOpenAI

    assert settings.openai_compat_api_key is not None and settings.openai_compat_base_url
    return ChatOpenAI(
        model=settings.openai_compat_model,
        api_key=settings.openai_compat_api_key.get_secret_value(),
        base_url=settings.openai_compat_base_url,
        temperature=settings.llm_temperature,
        **_retry_kwargs(max_retries),
    )


_REGISTRY: dict[str, _Provider] = {
    "gemini": _Provider(
        id="gemini",
        multimodal=True,
        available=lambda s: True,  # google_api_key is required config, so Gemini is always up
        build_text=_build_gemini_text,
        build_vision=_build_gemini_vision,
    ),
    "mistral": _Provider(
        id="mistral",
        multimodal=True,  # Pixtral
        available=lambda s: s.mistral_api_key is not None,
        build_text=lambda s, r: _build_mistral(s, s.mistral_model, r),
        build_vision=lambda s, r: _build_mistral(s, s.mistral_vision_model, r),
    ),
    "groq": _Provider(
        id="groq",
        multimodal=True,  # Llama-4 vision
        available=lambda s: s.groq_api_key is not None,
        build_text=lambda s, r: _build_groq(s, s.groq_model, r),
        build_vision=lambda s, r: _build_groq(s, s.groq_vision_model, r),
    ),
    "openai_compat": _Provider(
        id="openai_compat",
        multimodal=True,  # depends on the configured model (should be a VL model for scans)
        available=lambda s: s.openai_compat_api_key is not None and bool(s.openai_compat_base_url),
        build_text=lambda s, r: _build_openai_compat(s, r),
        build_vision=lambda s, r: _build_openai_compat(s, r),
    ),
}


def build_audit_ladder(
    settings: Settings,
    *,
    google_key: str | None = None,
    max_retries: int | None = 2,
    registry: dict[str, _Provider] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Build ``(text_models, vision_models)`` — the ordered fallback ladders for the audit.

    A **bring-your-own Google key** builds a Gemini-only ladder with no fallback: the caller
    asked to spend *their* quota, so we must not spill onto the operator's other-provider
    keys. Otherwise the ladder follows ``settings.audit_model_order``, including only
    providers whose keys/URL are configured; the vision ladder additionally drops
    non-multimodal providers. ``registry`` is injectable for tests.
    """
    if google_key:
        from ragchat.rag.llm import build_llm, build_vision_llm

        return (
            [build_llm(settings, google_key=google_key, max_retries=max_retries)],
            [build_vision_llm(settings, google_key=google_key, max_retries=max_retries)],
        )

    reg = registry if registry is not None else _REGISTRY
    order = [pid.strip() for pid in settings.audit_model_order.split(",") if pid.strip()]
    text: list[Any] = []
    vision: list[Any] = []
    for pid in order:
        provider = reg.get(pid)
        if provider is None or not provider.available(settings):
            continue
        text.append(provider.build_text(settings, max_retries))
        if provider.multimodal:
            vision.append(provider.build_vision(settings, max_retries))
    return text, vision
