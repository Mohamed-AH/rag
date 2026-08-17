"""Tests for the audit model fallback ladder (composition only — no SDKs, no network).

Provider builders are injected as a fake registry, so these exercise the ordering / skip /
multimodal-filter logic without importing any provider SDK or touching a key.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ragchat.rag.providers import _Provider, build_audit_ladder, is_retryable_provider_error


def _prov(pid: str, multimodal: bool, key_attr: str) -> _Provider:
    return _Provider(
        id=pid,
        multimodal=multimodal,
        available=lambda s, k=key_attr: bool(getattr(s, k, False)),
        build_text=lambda s, r, p=pid: f"{p}:text",
        build_vision=lambda s, r, p=pid: f"{p}:vision",
    )


_REGISTRY = {
    "gemini": _prov("gemini", True, "has_gemini"),
    "mistral": _prov("mistral", True, "has_mistral"),
    "groq": _prov("groq", True, "has_groq"),
    "textonly": _prov("textonly", False, "has_textonly"),
}


def _settings(order: str, **flags: Any) -> SimpleNamespace:
    return SimpleNamespace(audit_model_order=order, **flags)


def test_full_ladder_in_order() -> None:
    settings = _settings("gemini,mistral,groq", has_gemini=True, has_mistral=True, has_groq=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert text == ["gemini:text", "mistral:text", "groq:text"]
    assert vision == ["gemini:vision", "mistral:vision", "groq:vision"]


def test_missing_keys_are_skipped() -> None:
    # Only Gemini configured (its key is required config, so it's always available).
    settings = _settings("gemini,mistral,groq", has_gemini=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert text == ["gemini:text"]
    assert vision == ["gemini:vision"]


def test_order_is_respected() -> None:
    settings = _settings("groq,gemini", has_gemini=True, has_groq=True)
    text, _vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert text == ["groq:text", "gemini:text"]


def test_unknown_provider_id_is_ignored() -> None:
    settings = _settings("gemini,nope,mistral", has_gemini=True, has_mistral=True)
    text, _vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert text == ["gemini:text", "mistral:text"]


def test_vision_ladder_drops_non_multimodal_providers() -> None:
    settings = _settings("gemini,textonly", has_gemini=True, has_textonly=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert text == ["gemini:text", "textonly:text"]  # text path keeps both
    assert vision == ["gemini:vision"]  # scan path drops the text-only provider


def test_byo_google_key_builds_gemini_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bring-your-own key must NOT spill onto the operator's other providers.
    import ragchat.rag.llm as llm_mod

    monkeypatch.setattr(
        llm_mod, "build_llm", lambda s, google_key=None, max_retries=None: f"g-text:{google_key}"
    )
    monkeypatch.setattr(
        llm_mod,
        "build_vision_llm",
        lambda s, google_key=None, max_retries=None: f"g-vision:{google_key}",
    )
    settings = _settings("gemini,mistral,groq", has_gemini=True, has_mistral=True, has_groq=True)
    text, vision = build_audit_ladder(settings, google_key="BYO", registry=_REGISTRY)
    assert text == ["g-text:BYO"]
    assert vision == ["g-vision:BYO"]


def test_real_registry_capability_flags() -> None:
    # All default rungs are multimodal (Ministral-3B is a hybrid LLM+ViT; Groq has Qwen-VL).
    from ragchat.rag.providers import _REGISTRY

    assert _REGISTRY["gemini"].multimodal is True
    assert _REGISTRY["mistral"].multimodal is True
    assert _REGISTRY["groq"].multimodal is True


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("RESOURCE_EXHAUSTED: quota"), True),
        (RuntimeError("429 Too Many Requests"), True),
        (RuntimeError("503 Service Unavailable"), True),
        (RuntimeError("model is overloaded, try again"), True),
        (ValueError("schema validation failed"), False),
        (KeyError("hts_code"), False),
    ],
)
def test_retryable_error_detection(exc: Exception, expected: bool) -> None:
    assert is_retryable_provider_error(exc) is expected
