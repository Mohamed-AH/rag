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


def test_multipage_prompt_instructs_reading_every_page() -> None:
    from ragchat.audit.analyzer import _prompt
    from ragchat.audit.manifest import CUSTOMS_CHECKLIST

    single = _prompt(CUSTOMS_CHECKLIST)
    assert "page images" not in single  # no page-count nudge for the text path

    multi = _prompt(CUSTOMS_CHECKLIST, page_count=4)
    assert "4 page images" in multi
    assert "examine ALL 4 pages" in multi.replace("  ", " ")
    assert "Never stop after the first page" in multi


def test_rung_image_cap_packs_pages_before_invoke() -> None:
    # A rung with max_images=3 must receive at most 3 image parts even for a 5-page scan —
    # pages are packed into composites, never dropped.
    import io

    import pypdfium2 as pdfium
    from pypdf import PdfWriter

    from ragchat.ingestion.router import IMAGE, DocumentContent, MediaPart
    from ragchat.rag.providers import Rung

    # Build 5 real PNG page images.
    writer = PdfWriter()
    for _ in range(5):
        writer.add_blank_page(width=200, height=260)
    buf = io.BytesIO()
    writer.write(buf)
    doc = pdfium.PdfDocument(buf.getvalue())
    pngs = []
    for i in range(5):
        b = io.BytesIO()
        doc[i].render(scale=1.0).to_pil().convert("RGB").save(b, "PNG")
        pngs.append(b.getvalue())
    content = DocumentContent(
        filename="scan.pdf", mode=IMAGE, media=tuple(MediaPart("image/png", p) for p in pngs)
    )

    seen_counts: list[int] = []
    one_doc = _AnalysisResult(
        documents=[_DocResult(doc_type="commercial_invoice", confidence=0.9, pages=[1])]
    )

    class _Struct:
        def invoke(self, messages: Any) -> _AnalysisResult:
            blocks = [b for b in messages[0].content if b["type"] == "image_url"]
            seen_counts.append(len(blocks))  # 1 text block + N image blocks
            return one_doc

    class _Recorder:
        def with_structured_output(self, _schema: Any) -> _Struct:
            return _Struct()

    StructuredAnalyzer(lambda _c: [Rung(_Recorder(), max_images=3)]).analyze(
        "scan.pdf", content, CUSTOMS_CHECKLIST
    )
    assert seen_counts == [3]  # 5 pages packed into exactly 3 images, none dropped


def test_rung_structured_method_is_passed_through() -> None:
    from ragchat.rag.providers import Rung

    seen: list[Any] = []

    class _Model:
        def with_structured_output(self, _schema: Any, **kwargs: Any) -> _FakeStructured:
            seen.append(kwargs.get("method", "DEFAULT"))
            return _FakeStructured(
                _AnalysisResult(
                    documents=[_DocResult(doc_type="commercial_invoice", confidence=0.9, pages=[1])]
                ),
                None,
            )

    StructuredAnalyzer(lambda _c: [Rung(_Model(), structured_method="json_schema")]).analyze(
        "invoice.txt", _content(), CUSTOMS_CHECKLIST
    )
    assert seen == ["json_schema"]


def test_tool_use_failed_falls_over() -> None:
    # Groq's 'tool_use_failed' (a vision model that can't tool-call) should fall to the next rung.
    primary = _FakeModel(exc=RuntimeError("400 tool_use_failed: Failed to call a function"))
    out = _analyzer([primary, _good()]).analyze("invoice.txt", _content(), CUSTOMS_CHECKLIST)
    assert [d.doc_type for d in out] == ["commercial_invoice"]
