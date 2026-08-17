"""Tests for the analyzer's provider fallback loop (no SDKs, no network).

Fake chat models stand in for real providers: each either returns a scripted
``_AnalysisResult`` or raises. These verify the ladder fails over on quota/transient errors
but propagates a genuine (non-retryable) error instead of masking it.
"""

from __future__ import annotations

from typing import Any

import pytest

from ragchat.audit.analyzer import StructuredAnalyzer, _AnalysisResult, _DocResult
from ragchat.audit.manifest import CUSTOMS_CHECKLIST
from ragchat.ingestion.router import TEXT, DocumentContent


class _FakeStructured:
    def __init__(self, result: _AnalysisResult | None, exc: Exception | None) -> None:
        self._result = result
        self._exc = exc

    def invoke(self, _messages: Any) -> _AnalysisResult:
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


class _FakeModel:
    """A stand-in chat model whose ``.with_structured_output(...).invoke(...)`` is scripted."""

    def __init__(
        self, *, result: _AnalysisResult | None = None, exc: Exception | None = None
    ) -> None:
        self._result = result
        self._exc = exc

    def with_structured_output(self, _schema: Any) -> _FakeStructured:
        return _FakeStructured(self._result, self._exc)


def _content() -> DocumentContent:
    return DocumentContent(filename="invoice.txt", mode=TEXT, text="a commercial invoice")


def _good(doc_type: str = "commercial_invoice") -> _FakeModel:
    return _FakeModel(
        result=_AnalysisResult(documents=[_DocResult(doc_type=doc_type, confidence=0.9, pages=[1])])
    )


def _analyzer(models: list[Any]) -> StructuredAnalyzer:
    return StructuredAnalyzer(lambda _content: models)


def test_single_model_success() -> None:
    out = _analyzer([_good()]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
    assert [d.doc_type for d in out] == ["commercial_invoice"]


def test_fails_over_on_quota_error() -> None:
    primary = _FakeModel(exc=RuntimeError("RESOURCE_EXHAUSTED: free quota exhausted"))
    out = _analyzer([primary, _good()]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
    # The secondary's result is used — the audit survives the primary's exhaustion.
    assert [d.doc_type for d in out] == ["commercial_invoice"]


def test_non_retryable_error_propagates_without_fallback() -> None:
    primary = _FakeModel(exc=ValueError("bad code path"))  # our own bug, not a model failure
    secondary = _good()
    with pytest.raises(ValueError, match="bad code path"):
        _analyzer([primary, secondary]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)


def test_output_parser_exception_falls_over() -> None:
    # A weak model returning unparseable output should fall over to a stronger rung.
    from langchain_core.exceptions import OutputParserException

    primary = _FakeModel(exc=OutputParserException("could not parse model output"))
    out = _analyzer([primary, _good()]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
    assert [d.doc_type for d in out] == ["commercial_invoice"]


def test_schema_validation_error_falls_over() -> None:
    # A malformed structured output raises pydantic ValidationError -> try the next provider.
    from pydantic import ValidationError

    try:
        _AnalysisResult.model_validate({"documents": "not-a-list"})
        raise AssertionError("expected a ValidationError")
    except ValidationError as verr:
        bad = _FakeModel(exc=verr)

    out = _analyzer([bad, _good()]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
    assert [d.doc_type for d in out] == ["commercial_invoice"]


def test_last_provider_error_propagates() -> None:
    models = [
        _FakeModel(exc=RuntimeError("429 rate limit")),
        _FakeModel(exc=RuntimeError("503 unavailable")),
    ]
    with pytest.raises(RuntimeError, match="503"):
        _analyzer(models).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)


def test_empty_ladder_raises() -> None:
    with pytest.raises(RuntimeError, match="no audit model providers"):
        _analyzer([]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
