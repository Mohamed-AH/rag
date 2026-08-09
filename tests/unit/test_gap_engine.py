"""Tests for the pure gap-analysis engine, driven by hand-built evidence (no AI, no DB).

Each test isolates one status path: a complete packet is clear; a dropped document is
``missing`` and its rules are skipped; a low-confidence classification is ``needs_review``;
a broken field rule is ``deficient``; an unrecognized file is ``needs_review``.
"""

from __future__ import annotations

from typing import Any

from ragchat.audit import evaluate
from ragchat.audit.checklist import CUSTOMS_CHECKLIST
from ragchat.audit.evidence import ClassifiedDocument, ExtractedField, PacketEvidence
from ragchat.audit.report import Finding, FindingStatus, GapReport


def _f(name: str, value: Any, confidence: float = 1.0) -> ExtractedField:
    return ExtractedField(name=name, value=value, confidence=confidence)


def _doc(
    doc_id: str, doc_type: str | None, confidence: float = 0.95, **fields: Any
) -> ClassifiedDocument:
    built = {k: (v if isinstance(v, ExtractedField) else _f(k, v)) for k, v in fields.items()}
    return ClassifiedDocument(doc_id=doc_id, doc_type=doc_type, confidence=confidence, fields=built)


def _complete_docs() -> dict[str, ClassifiedDocument]:
    """A fully consistent, confidently classified Customs packet, keyed by doc id."""
    return {
        "inv": _doc(
            "inv",
            "commercial_invoice",
            hts_code="8471.30.01",
            country_of_origin="China",
            currency="USD",
            total_value="10000",
            exporter="Acme Ltd",
            consignee="Globex Inc",
        ),
        "pl": _doc(
            "pl",
            "packing_list",
            total_value="10000",
            net_weight="500",
            total_cartons="20",
            exporter="Acme Ltd",
            consignee="Globex Inc",
        ),
        "bl": _doc(
            "bl",
            "bill_of_lading",
            net_weight="500",
            total_cartons="20",
            exporter="Acme Ltd",
            consignee="Globex Inc",
        ),
        "coo": _doc("coo", "certificate_of_origin", country_of_origin="China"),
    }


def _evaluate(docs: dict[str, ClassifiedDocument]) -> GapReport:
    return evaluate(CUSTOMS_CHECKLIST, PacketEvidence(tuple(docs.values())))


def _by_id(report: GapReport, requirement_id: str) -> Finding | None:
    return next((f for f in report.findings if f.requirement_id == requirement_id), None)


# --- The happy path -------------------------------------------------------


def test_complete_packet_is_clear() -> None:
    report = _evaluate(_complete_docs())
    assert report.is_clear is True
    assert not report.missing and not report.deficient and not report.needs_review
    # 4 document requirements + 5 field rules, all satisfied.
    assert len(report.present) == 9


# --- Layer 1: presence ----------------------------------------------------


def test_missing_document_is_reported_and_its_rules_are_skipped() -> None:
    docs = _complete_docs()
    del docs["pl"]  # drop the packing list
    report = _evaluate(docs)

    missing = _by_id(report, "doc.packing_list")
    assert missing is not None and missing.status is FindingStatus.MISSING
    # Rules that need the packing list must not fire on absent evidence...
    assert _by_id(report, "rule.value_matches") is None
    assert _by_id(report, "rule.weight_count") is None
    assert _by_id(report, "rule.parties_aligned") is None
    # ...but rules that don't need it still run.
    assert _by_id(report, "rule.hts_code").status is FindingStatus.PRESENT  # type: ignore[union-attr]


def test_low_confidence_classification_needs_review() -> None:
    docs = _complete_docs()
    docs["bl"] = _doc("bl", "bill_of_lading", confidence=0.30, net_weight="500", total_cartons="20")
    report = _evaluate(docs)

    finding = _by_id(report, "doc.bill_of_lading")
    assert finding is not None and finding.status is FindingStatus.NEEDS_REVIEW
    # A doc that isn't confidently present doesn't gate rules into running.
    assert _by_id(report, "rule.weight_count") is None


def test_unrecognized_document_needs_review() -> None:
    docs = _complete_docs()
    docs["mystery"] = _doc("mystery", None, confidence=0.4)
    docs["other"] = _doc("other", "not_a_customs_doc", confidence=0.9)
    report = _evaluate(docs)

    reviews = {f.requirement_id for f in report.needs_review}
    assert "doc.unrecognized:mystery" in reviews
    assert "doc.unrecognized:other" in reviews


# --- Layer 2: field rules -------------------------------------------------


def test_malformed_hts_code_is_deficient() -> None:
    docs = _complete_docs()
    docs["inv"].fields["hts_code"] = _f("hts_code", "12")  # too short
    report = _evaluate(docs)
    assert _by_id(report, "rule.hts_code").status is FindingStatus.DEFICIENT  # type: ignore[union-attr]


def test_missing_required_field_is_deficient() -> None:
    docs = _complete_docs()
    del docs["inv"].fields["hts_code"]
    report = _evaluate(docs)
    finding = _by_id(report, "rule.hts_code")
    assert finding is not None and finding.status is FindingStatus.DEFICIENT
    assert "missing" in finding.summary.lower()


def test_low_confidence_field_needs_review() -> None:
    docs = _complete_docs()
    docs["inv"].fields["hts_code"] = _f("hts_code", "8471.30.01", confidence=0.2)
    report = _evaluate(docs)
    assert _by_id(report, "rule.hts_code").status is FindingStatus.NEEDS_REVIEW  # type: ignore[union-attr]


def test_origin_mismatch_is_deficient() -> None:
    docs = _complete_docs()
    docs["coo"].fields["country_of_origin"] = _f("country_of_origin", "Vietnam")
    report = _evaluate(docs)
    assert _by_id(report, "rule.origin_consistent").status is FindingStatus.DEFICIENT  # type: ignore[union-attr]


def test_value_mismatch_is_deficient() -> None:
    docs = _complete_docs()
    docs["pl"].fields["total_value"] = _f("total_value", "20000")
    report = _evaluate(docs)
    assert _by_id(report, "rule.value_matches").status is FindingStatus.DEFICIENT  # type: ignore[union-attr]


def test_value_within_tolerance_is_present() -> None:
    docs = _complete_docs()
    docs["pl"].fields["total_value"] = _f("total_value", "10050")  # 0.5% off, within 1%
    report = _evaluate(docs)
    assert _by_id(report, "rule.value_matches").status is FindingStatus.PRESENT  # type: ignore[union-attr]


def test_carton_count_mismatch_is_deficient() -> None:
    docs = _complete_docs()
    docs["bl"].fields["total_cartons"] = _f("total_cartons", "25")
    report = _evaluate(docs)
    assert _by_id(report, "rule.weight_count").status is FindingStatus.DEFICIENT  # type: ignore[union-attr]


def test_party_mismatch_is_deficient() -> None:
    docs = _complete_docs()
    docs["bl"].fields["consignee"] = _f("consignee", "Someone Else Inc")
    report = _evaluate(docs)
    finding = _by_id(report, "rule.parties_aligned")
    assert finding is not None and finding.status is FindingStatus.DEFICIENT
    assert "consignee" in finding.summary
