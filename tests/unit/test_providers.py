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


def _models(rungs: list[Any]) -> list[Any]:
    return [rung.model for rung in rungs]


def test_full_ladder_in_order() -> None:
    settings = _settings("gemini,mistral,groq", has_gemini=True, has_mistral=True, has_groq=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert _models(text) == ["gemini:text", "mistral:text", "groq:text"]
    assert _models(vision) == ["gemini:vision", "mistral:vision", "groq:vision"]


def test_missing_keys_are_skipped() -> None:
    # Only Gemini configured (its key is required config, so it's always available).
    settings = _settings("gemini,mistral,groq", has_gemini=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert _models(text) == ["gemini:text"]
    assert _models(vision) == ["gemini:vision"]


def test_order_is_respected() -> None:
    settings = _settings("groq,gemini", has_gemini=True, has_groq=True)
    text, _vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert _models(text) == ["groq:text", "gemini:text"]


def test_unknown_provider_id_is_ignored() -> None:
    settings = _settings("gemini,nope,mistral", has_gemini=True, has_mistral=True)
    text, _vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert _models(text) == ["gemini:text", "mistral:text"]


def test_vision_ladder_drops_non_multimodal_providers() -> None:
    settings = _settings("gemini,textonly", has_gemini=True, has_textonly=True)
    text, vision = build_audit_ladder(settings, registry=_REGISTRY)
    assert _models(text) == ["gemini:text", "textonly:text"]  # text path keeps both
    assert _models(vision) == ["gemini:vision"]  # scan path drops the text-only provider


def _real_settings(order: str = "gemini,mistral,groq", **overrides: Any) -> Any:
    """A real Settings (needed for the BYO path's ``model_copy`` overlay)."""
    from ragchat.config import Settings

    kw: dict[str, Any] = {
        "database_url": "postgresql://u:p@localhost:5432/db",  # validated only, never connected
        "cohere_api_key": "c",
        "google_api_key": "g",
        "audit_model_order": order,
    }
    kw.update(overrides)
    return Settings(**kw)


def _field_prov(pid: str) -> _Provider:
    """A fake provider whose availability keys off the real Settings key field (for BYO)."""
    fld = {"gemini": "google_api_key", "mistral": "mistral_api_key", "groq": "groq_api_key"}[pid]
    return _Provider(
        id=pid,
        multimodal=True,
        available=lambda s, f=fld: getattr(s, f) is not None,
        build_text=lambda s, r, p=pid: f"{p}:text",
        build_vision=lambda s, r, p=pid: f"{p}:vision",
    )


_BYO_REGISTRY = {pid: _field_prov(pid) for pid in ("gemini", "mistral", "groq")}


def test_byo_restricts_to_the_supplied_provider() -> None:
    # Operator has only Gemini configured, but a BYO Groq key yields a Groq-only ladder on the
    # caller's key — the operator's Gemini is never used.
    settings = _real_settings()
    text, vision = build_audit_ladder(settings, byo_keys={"groq": "gk"}, registry=_BYO_REGISTRY)
    assert _models(text) == ["groq:text"]
    assert _models(vision) == ["groq:vision"]


def test_byo_multiple_keys_follow_configured_order() -> None:
    settings = _real_settings(order="gemini,mistral,groq")
    text, _vision = build_audit_ladder(
        settings, byo_keys={"groq": "g", "mistral": "m"}, registry=_BYO_REGISTRY
    )
    # AUDIT_MODEL_ORDER order, and the operator's Gemini is absent (not a supplied key).
    assert _models(text) == ["mistral:text", "groq:text"]


def test_real_registry_capability_flags() -> None:
    # All default rungs are multimodal (Ministral-3B is a hybrid LLM+ViT; Groq has Qwen-VL).
    from ragchat.rag.providers import _REGISTRY

    assert _REGISTRY["gemini"].multimodal is True
    assert _REGISTRY["mistral"].multimodal is True
    assert _REGISTRY["groq"].multimodal is True
    # Groq's Qwen-VL caps images per request; the others have no meaningful cap.
    assert _REGISTRY["groq"].max_images == 3
    assert _REGISTRY["gemini"].max_images is None
    assert _REGISTRY["mistral"].max_images is None


def test_ladder_carries_per_provider_image_cap() -> None:
    # A provider's max_images propagates onto its built Rung, so the analyzer can pack pages.
    capped = _Provider(
        id="capped",
        multimodal=True,
        available=lambda s: True,
        build_text=lambda s, r: "capped:text",
        build_vision=lambda s, r: "capped:vision",
        max_images=3,
    )
    registry = {"gemini": _prov("gemini", True, "has_gemini"), "capped": capped}
    settings = _settings("gemini,capped", has_gemini=True)
    _text, vision = build_audit_ladder(settings, registry=registry)
    caps = {rung.model: rung.max_images for rung in vision}
    assert caps == {"gemini:vision": None, "capped:vision": 3}


def test_describe_audit_ladder_is_non_secret_snapshot() -> None:
    from ragchat.rag.providers import describe_audit_ladder

    # groq key set, mistral unset; over the real registry.
    settings = _real_settings(order="gemini,mistral,groq,bogus", groq_api_key="gk")
    by_id = {r["id"]: r for r in describe_audit_ladder(settings)}

    assert by_id["gemini"]["configured"] is True
    assert by_id["gemini"]["text_model"] == settings.llm_model
    assert by_id["groq"]["configured"] is True  # key present
    assert by_id["groq"]["vision_model"] == settings.groq_vision_model
    assert by_id["mistral"]["configured"] is False  # no key
    assert by_id["bogus"]["known"] is False
    # No secret ever leaks — only the boolean and model ids are reported.
    assert "gk" not in str(by_id)


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


def test_groq_rung_uses_json_schema_structured_method() -> None:
    # Groq's vision model can't tool-call; the rung must carry a non-default structured method.
    from ragchat.rag.providers import _normalize_method

    settings = _real_settings(order="groq", groq_api_key="gk")
    # Fake registry mirroring groq's config resolver, so no SDK import is needed.
    groq = _Provider(
        id="groq",
        multimodal=True,
        available=lambda s: s.groq_api_key is not None,
        build_text=lambda s, r: "groq:text",
        build_vision=lambda s, r: "groq:vision",
        structured_method=lambda s: _normalize_method(s.groq_structured_method),
    )
    _text, vision = build_audit_ladder(settings, registry={"groq": groq})
    assert vision[0].structured_method == "json_schema"  # from the default config


def test_normalize_method_maps_defaults_to_none() -> None:
    from ragchat.rag.providers import _normalize_method

    assert _normalize_method("json_schema") == "json_schema"
    assert _normalize_method("json_mode") == "json_mode"
    assert _normalize_method("") is None
    assert _normalize_method("default") is None
    assert _normalize_method("function_calling") is None  # the LangChain default
