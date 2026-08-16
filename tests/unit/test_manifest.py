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
    assert "procurement" in available
    assert "healthcare_credentialing" in available
    assert "study_visa_funds" in available


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


def test_threshold_without_a_bound_is_rejected() -> None:
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
    type: numeric_threshold
    doc: doc_a
    field: amount
    label: Amount
"""
    with pytest.raises(ValidationError):
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


# --- Third vertical: procurement onboarding (also zero engine code) --------


def _procurement_docs(
    *,
    entity: str = "Northwind Traders LLC",
    coi_expiry: str | None = None,
    signed: str = "2026-01-15",
) -> PacketEvidence:
    future = (date.today() + timedelta(days=365)).isoformat()
    return PacketEvidence(
        (
            _doc("w9", "w9", legal_entity_name=entity, ein="12-3456789"),
            _doc(
                "coi",
                "coi",
                legal_entity_name="Northwind Traders LLC",
                coverage_type="General Liability",
                policy_expiry=coi_expiry or future,
                coverage_amount="2,000,000",
            ),
            _doc(
                "nda",
                "nda",
                legal_entity_name="Northwind Traders LLC",
                signed_date=signed,
                effective_date="2026-01-15",
            ),
        )
    )


def test_procurement_complete_packet_is_clear() -> None:
    # SOC 2 is optional (required: false) — its absence must not be flagged.
    report = evaluate(get_checklist("procurement"), _procurement_docs())
    assert report.is_clear is True


def test_procurement_expired_insurance_is_deficient() -> None:
    past = (date.today() - timedelta(days=10)).isoformat()
    report = evaluate(get_checklist("procurement"), _procurement_docs(coi_expiry=past))
    finding = next(f for f in report.deficient if f.requirement_id == "rule.coi_not_expired")
    assert finding.status is FindingStatus.DEFICIENT


def test_procurement_entity_mismatch_is_deficient() -> None:
    report = evaluate(get_checklist("procurement"), _procurement_docs(entity="Different Corp"))
    finding = next(f for f in report.deficient if f.requirement_id == "rule.entity_name_consistent")
    assert finding.status is FindingStatus.DEFICIENT


def test_procurement_unsigned_nda_is_deficient() -> None:
    # No signed_date at all -> the NDA-signature check reports it missing.
    evidence = PacketEvidence(
        (
            _doc("w9", "w9", legal_entity_name="Northwind Traders LLC", ein="12-3456789"),
            _doc(
                "coi",
                "coi",
                legal_entity_name="Northwind Traders LLC",
                coverage_type="General Liability",
                policy_expiry="2030-01-01",
                coverage_amount="2,000,000",
            ),
            _doc(
                "nda", "nda", legal_entity_name="Northwind Traders LLC", effective_date="2026-01-15"
            ),
        )
    )
    report = evaluate(get_checklist("procurement"), evidence)
    finding = next(f for f in report.deficient if f.requirement_id == "rule.nda_signed")
    assert finding.status is FindingStatus.DEFICIENT


# --- Fourth vertical: healthcare credentialing (expiry-heavy, zero code) ----


def _credentialing_docs(
    *, name: str = "Dr. Alex Rivera", npi: str = "1234567893", license_expiry: str | None = None
) -> PacketEvidence:
    future = (date.today() + timedelta(days=365)).isoformat()
    return PacketEvidence(
        (
            _doc(
                "medical_license",
                "medical_license",
                practitioner_name=name,
                license_number="MD-556677",
                expiry_date=license_expiry or future,
            ),
            _doc(
                "dea_registration",
                "dea_registration",
                practitioner_name="Alex Rivera",
                dea_number="BR1234563",
                expiry_date=future,
            ),
            _doc(
                "board_certification",
                "board_certification",
                practitioner_name="Alex Rivera, MD",
                specialty="Internal Medicine",
                expiry_date=future,
            ),
            _doc("npi_record", "npi_record", practitioner_name="Alex Rivera", npi=npi),
        )
    )


def test_credentialing_complete_packet_is_clear() -> None:
    report = evaluate(get_checklist("healthcare_credentialing"), _credentialing_docs())
    assert report.is_clear is True


def test_credentialing_expired_license_is_deficient() -> None:
    past = (date.today() - timedelta(days=5)).isoformat()
    report = evaluate(
        get_checklist("healthcare_credentialing"), _credentialing_docs(license_expiry=past)
    )
    finding = next(f for f in report.deficient if f.requirement_id == "rule.license_not_expired")
    assert finding.status is FindingStatus.DEFICIENT


def test_credentialing_malformed_npi_is_deficient() -> None:
    report = evaluate(get_checklist("healthcare_credentialing"), _credentialing_docs(npi="123"))
    finding = next(f for f in report.deficient if f.requirement_id == "rule.npi_format")
    assert finding.status is FindingStatus.DEFICIENT


def test_credentialing_name_mismatch_is_deficient() -> None:
    report = evaluate(
        get_checklist("healthcare_credentialing"), _credentialing_docs(name="Dr. Someone Else")
    )
    finding = next(f for f in report.deficient if f.requirement_id == "rule.name_consistent")
    assert finding.status is FindingStatus.DEFICIENT


# --- Fifth vertical: study-visa funds (numeric_threshold primitive) --------


def _study_visa_docs(*, balance: str = "USD 30,000", sponsored: str = "40000") -> PacketEvidence:
    recent = (date.today() - timedelta(days=60)).isoformat()
    return PacketEvidence(
        (
            _doc(
                "bank_statement",
                "bank_statement",
                applicant_name="Priya Nair",
                closing_balance=balance,
                statement_date=recent,
            ),
            _doc(
                "admission_letter",
                "admission_letter",
                applicant_name="Priya Nair",
                institution_name="State University",
                program="M.Sc. Data Science",
            ),
            _doc(
                "sponsorship_affidavit",
                "sponsorship_affidavit",
                sponsor_name="Rajesh Nair",
                sponsored_amount=sponsored,
                signed_date="2026-02-01",
            ),
        )
    )


def test_study_visa_complete_packet_is_clear() -> None:
    report = evaluate(get_checklist("study_visa_funds"), _study_visa_docs())
    assert report.is_clear is True


def test_study_visa_insufficient_balance_is_deficient() -> None:
    # Below the 25,000 minimum -> the threshold check fails (unit-laden value still parses).
    report = evaluate(get_checklist("study_visa_funds"), _study_visa_docs(balance="USD 8,500"))
    finding = next(f for f in report.deficient if f.requirement_id == "rule.min_bank_balance")
    assert finding.status is FindingStatus.DEFICIENT


def test_study_visa_sufficient_balance_is_present() -> None:
    report = evaluate(get_checklist("study_visa_funds"), _study_visa_docs(balance="26,000.00"))
    finding = next(f for f in report.present if f.requirement_id == "rule.min_bank_balance")
    assert finding.status is FindingStatus.PRESENT
