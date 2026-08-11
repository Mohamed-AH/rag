"""Tests for the Customs checklist definition and the checklist registry."""

from __future__ import annotations

import pytest

from ragchat.audit.checklist import CUSTOMS_CHECKLIST, get_checklist
from ragchat.errors import UnknownChecklistError


def test_customs_checklist_shape() -> None:
    assert CUSTOMS_CHECKLIST.id == "customs"
    assert CUSTOMS_CHECKLIST.doc_type_ids() == frozenset(
        {"commercial_invoice", "packing_list", "bill_of_lading", "certificate_of_origin"}
    )
    assert len(CUSTOMS_CHECKLIST.document_requirements) == 4
    assert len(CUSTOMS_CHECKLIST.field_rules) == 6


def test_requirements_reference_known_doc_types() -> None:
    known = CUSTOMS_CHECKLIST.doc_type_ids()
    assert all(req.doc_type in known for req in CUSTOMS_CHECKLIST.document_requirements)


def test_field_rules_only_reference_known_doc_types() -> None:
    known = CUSTOMS_CHECKLIST.doc_type_ids()
    for rule in CUSTOMS_CHECKLIST.field_rules:
        assert set(rule.doc_types) <= known, rule.id


def test_requirement_and_rule_ids_are_unique() -> None:
    req_ids = [r.id for r in CUSTOMS_CHECKLIST.document_requirements]
    rule_ids = [r.id for r in CUSTOMS_CHECKLIST.field_rules]
    assert len(set(req_ids)) == len(req_ids)
    assert len(set(rule_ids)) == len(rule_ids)


def test_get_checklist_returns_registered_checklist() -> None:
    assert get_checklist("customs") is CUSTOMS_CHECKLIST


def test_get_checklist_rejects_unknown_id() -> None:
    with pytest.raises(UnknownChecklistError):
        get_checklist("does-not-exist")
