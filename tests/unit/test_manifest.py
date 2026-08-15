"""Tests for the declarative manifest layer: loading, validation, and plug-and-play.

Customs parity is already proven by the full engine/eval suites (customs is now loaded
from ``manifests/customs.yaml``). These tests cover the loader itself and prove a second
vertical audits end to end with no engine changes.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from ragchat.audit import evaluate
from ragchat.audit.evidence import ClassifiedDocument, ExtractedField, PacketEvidence
from ragchat.audit.manifest import available_checklists, get_checklist, load_manifest
from ragchat.audit.report import FindingStatus
from ragchat.errors import UnknownChecklistError


def _f(name: str, value: object, confidence: float = 0.95) -> ExtractedField:
    return ExtractedField(name=name, value=value, confidence=confidence)


def _doc(doc_id: str, doc_type: str, **fields: object) -> ClassifiedDocument:
    return ClassifiedDocument(
        doc_id=doc_id,
        doc_type=doc_type,
        confidence=0.95,
        fields={k: _f(k, v) for k, v in fields.items()},
    )


# --- Registry / loader ----------------------------------------------------


def test_installed_verticals_are_available() -> None:
    available = available_checklists()
    assert "customs" in available
    assert "education_admissions" in available


def test_unknown_checklist_raises() -> None:
    with pytest.raises(UnknownChecklistError):
        get_checklist("does_not_exist")


def test_customs_manifest_compiles_to_expected_rules() -> None:
    customs = get_checklist("customs")
    assert {r.id for r in customs.field_rules} == {
        "rule.hts_code",
        "rule.origin_consistent",
        "rule.value_matches",
        "rule.quantity_matches",
        "rule.weight_count",
        "rule.parties_aligned",
    }


def test_unknown_rule_type_is_rejected_at_load() -> None:
    bad = """
vertical_id: broken
name: Broken
documents:
  - id: doc_a
    name: Doc A
    fields: []
rules:
  - id: rule.bad
    type: not_a_real_primitive
    label: Bad
"""
    with pytest.raises(ValidationError):
        load_manifest(bad)


def test_bad_field_reference_is_rejected() -> None:
    bad = """
vertical_id: broken
name: Broken
documents:
  - id: doc_a
    name: Doc A
    fields:
      - {name: amount, description: an amount}
rules:
  - id: rule.bad
    type: numeric_match
    label: Amount
    groups:
      - [amount]          # missing the doc_type. prefix
"""
    with pytest.raises(ValueError):
        load_manifest(bad)


# --- Second vertical audits end to end (zero engine code) -----------------


def _education_docs(name: str = "Jane Q Applicant", test_date: str | None = None) -> PacketEvidence:
    recent = (date.today() - timedelta(days=30)).isoformat()
    future = (date.today() + timedelta(days=365)).isoformat()
    return PacketEvidence(
        (
            _doc(
                "passport",
                "passport",
                applicant_name=name,
                passport_number="X123",
                expiry_date=future,
            ),
            _doc(
                "transcript",
                "transcript",
                applicant_name="Jane Applicant",
                institution_name="State U",
            ),
            _doc(
                "language_score",
                "language_score",
                applicant_name="Jane Q. Applicant",
                overall_score="7.5",
                test_date=test_date or recent,
            ),
        )
    )


def test_education_complete_packet_is_clear() -> None:
    report = evaluate(get_checklist("education_admissions"), _education_docs())
    assert report.is_clear is True


def test_education_name_mismatch_is_deficient() -> None:
    evidence = _education_docs(name="Completely Different Person")
    report = evaluate(get_checklist("education_admissions"), evidence)
    finding = next(f for f in report.deficient if f.requirement_id == "rule.name_consistent")
    assert finding.status is FindingStatus.DEFICIENT


def test_education_expired_score_is_deficient() -> None:
    old = (date.today() - timedelta(days=365 * 3)).isoformat()  # 3 years ago
    report = evaluate(get_checklist("education_admissions"), _education_docs(test_date=old))
    finding = next(f for f in report.deficient if f.requirement_id == "rule.language_score_recent")
    assert finding.status is FindingStatus.DEFICIENT


def test_education_missing_document_is_reported() -> None:
    # Drop the language scorecard entirely.
    evidence = PacketEvidence(
        (
            _doc("passport", "passport", applicant_name="Jane", expiry_date="2030-01-01"),
            _doc("transcript", "transcript", applicant_name="Jane", institution_name="State U"),
        )
    )
    report = evaluate(get_checklist("education_admissions"), evidence)
    assert any(f.requirement_id == "doc.language_score" for f in report.missing)
