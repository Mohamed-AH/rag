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
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from ragchat.config import Settings

# Which Settings field holds each provider's key — used to overlay a caller's bring-your-own
# key onto a copy of the settings so the ladder builds on the caller's quota, not the
# operator's. Only these three providers are offered for BYO (openai_compat is operator-only).
_BYO_KEY_FIELD: dict[str, str] = {
    "gemini": "google_api_key",
    "mistral": "mistral_api_key",
    "groq": "groq_api_key",
}

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


def _no_models(settings: Settings) -> dict[str, str | None]:
    return {"text": None, "vision": None}


@dataclass(frozen=True)
class _Provider:
    """One rung of the ladder: how to tell if it's configured, and how to build its models."""

    id: str
    multimodal: bool
    available: Callable[[Settings], bool]
    build_text: Callable[[Settings, int | None], Any]
    build_vision: Callable[[Settings, int | None], Any]
    # Non-secret model ids this rung would use, for the /providers diagnostic (no keys).
    describe: Callable[[Settings], dict[str, str | None]] = field(default=_no_models)


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
        describe=lambda s: {"text": s.llm_model, "vision": s.vision_model},
    ),
    "mistral": _Provider(
        id="mistral",
        multimodal=True,  # Ministral-3B is a hybrid multimodal model (LLM + ViT): text + scans
        available=lambda s: s.mistral_api_key is not None,
        build_text=lambda s, r: _build_mistral(s, s.mistral_model, r),
        build_vision=lambda s, r: _build_mistral(s, s.mistral_model, r),
        describe=lambda s: {"text": s.mistral_model, "vision": s.mistral_model},
    ),
    "groq": _Provider(
        id="groq",
        multimodal=True,  # gpt-oss-120b (text) + Qwen-VL (scans)
        available=lambda s: s.groq_api_key is not None,
        build_text=lambda s, r: _build_groq(s, s.groq_model, r),
        build_vision=lambda s, r: _build_groq(s, s.groq_vision_model, r),
        describe=lambda s: {"text": s.groq_model, "vision": s.groq_vision_model},
    ),
    "openai_compat": _Provider(
        id="openai_compat",
        multimodal=True,  # depends on the configured model (should be a VL model for scans)
        available=lambda s: s.openai_compat_api_key is not None and bool(s.openai_compat_base_url),
        build_text=lambda s, r: _build_openai_compat(s, r),
        build_vision=lambda s, r: _build_openai_compat(s, r),
        describe=lambda s: {"text": s.openai_compat_model, "vision": s.openai_compat_model},
    ),
}


def _order(settings: Settings) -> list[str]:
    return [pid.strip() for pid in settings.audit_model_order.split(",") if pid.strip()]


def build_audit_ladder(
    settings: Settings,
    *,
    byo_keys: dict[str, str] | None = None,
    max_retries: int | None = 2,
    registry: dict[str, _Provider] | None = None,
) -> tuple[list[Any], list[Any]]:
    """Build ``(text_models, vision_models)`` — the ordered fallback ladders for the audit.

    **Bring-your-own keys** (``byo_keys`` maps a provider id — ``gemini``/``mistral``/``groq`` —
    to the caller's key) restrict the ladder to *only* those providers, built on the caller's
    keys: a BYO caller spends their own quota, so the ladder must never fall back onto the
    operator's other-provider keys. Otherwise the ladder follows ``settings.audit_model_order``
    over the operator's configured providers. Either way, a provider is included only when
    available, and the vision ladder drops non-multimodal providers. ``registry`` is injectable
    for tests.
    """
    reg = registry if registry is not None else _REGISTRY

    if byo_keys:
        # Overlay the caller's keys onto a settings copy, and restrict the order to just the
        # providers they supplied (in the configured order). No operator keys are spent.
        overlay = {
            _BYO_KEY_FIELD[pid]: SecretStr(key)
            for pid, key in byo_keys.items()
            if pid in _BYO_KEY_FIELD and key
        }
        settings = settings.model_copy(update=overlay)
        supplied = {pid for pid in byo_keys if pid in _BYO_KEY_FIELD and byo_keys[pid]}
        order = [pid for pid in _order(settings) if pid in supplied]
        # Preserve any supplied provider not named in AUDIT_MODEL_ORDER (append in given order).
        order += [pid for pid in supplied if pid not in order]
    else:
        order = _order(settings)

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


def describe_audit_ladder(
    settings: Settings, *, registry: dict[str, _Provider] | None = None
) -> list[dict[str, Any]]:
    """Non-secret snapshot of the configured ladder for the ``/providers`` diagnostic.

    Reports, per provider in ``AUDIT_MODEL_ORDER``, whether it's configured (key present — the
    boolean only, never the key), whether it's multimodal, and the resolved text/scan model
    ids. Builds nothing and imports no SDK.
    """
    reg = registry if registry is not None else _REGISTRY
    out: list[dict[str, Any]] = []
    for pid in _order(settings):
        provider = reg.get(pid)
        if provider is None:
            out.append(
                {
                    "id": pid,
                    "known": False,
                    "configured": False,
                    "multimodal": False,
                    "text_model": None,
                    "vision_model": None,
                }
            )
            continue
        models = provider.describe(settings)
        out.append(
            {
                "id": pid,
                "known": True,
                "configured": provider.available(settings),
                "multimodal": provider.multimodal,
                "text_model": models.get("text"),
                "vision_model": models.get("vision") if provider.multimodal else None,
            }
        )
    return out
