"""Checklists as data — the domain rubric a packet is audited against.

A checklist is two layers:

* **Layer 1 (presence)** — which document *types* must be in the packet
  (:class:`DocumentRequirement`).
* **Layer 2 (field rules)** — per-type field extraction plus cross-document consistency
  checks (:class:`FieldRule`). This layer is where the ROI lives: not "is there an
  invoice" but "does the invoice's declared value match the packing list".

Rules are plain data (a predicate over :class:`~ragchat.audit.evidence.PacketEvidence`),
so adding the next vertical means adding a checklist here — not changing engine code. The
only checklist wired for v1 is :data:`CUSTOMS_CHECKLIST`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ragchat.audit.evidence import (
    ClassifiedDocument,
    ExtractedField,
    PacketEvidence,
    RuleContext,
    RuleResult,
)
from ragchat.audit.report import FindingStatus, SourcePointer
from ragchat.errors import UnknownChecklistError

RuleCheck = Callable[[PacketEvidence, RuleContext], RuleResult]


# --- Checklist structure --------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocType:
    """A document type a checklist knows about."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class DocumentRequirement:
    """Layer 1: a document type that must be present in the packet."""

    id: str
    doc_type: str
    description: str


@dataclass(frozen=True, slots=True)
class FieldRule:
    """Layer 2: a check over extracted fields. ``doc_types`` lists the types that must be
    confidently present for the rule to run — otherwise Layer 1 already reports the gap."""

    id: str
    description: str
    doc_types: tuple[str, ...]
    check: RuleCheck


@dataclass(frozen=True, slots=True)
class Checklist:
    """A named rubric: the document types, the presence requirements, and the field rules."""

    id: str
    name: str
    doc_types: tuple[DocType, ...]
    document_requirements: tuple[DocumentRequirement, ...]
    field_rules: tuple[FieldRule, ...]

    def doc_type_ids(self) -> frozenset[str]:
        return frozenset(dt.id for dt in self.doc_types)


# --- Rule helpers ---------------------------------------------------------


class _ShortCircuit(Exception):
    """Internal control flow: abandon a rule early with a ready-made result."""

    def __init__(self, result: RuleResult) -> None:
        super().__init__(result.summary)
        self.result = result


def _ptr(field: ExtractedField | None, doc: ClassifiedDocument) -> tuple[SourcePointer, ...]:
    """A source pointer for a field, falling back to the document if the field lacks one."""
    if field is not None and field.source is not None:
        return (field.source,)
    return (SourcePointer(doc_id=doc.doc_id),)


def _field(
    evidence: PacketEvidence, ctx: RuleContext, doc_type: str, name: str
) -> tuple[ExtractedField, ClassifiedDocument]:
    """Resolve a required field, short-circuiting the rule if it can't be read cleanly.

    * document absent (should not happen — the engine gates on presence) -> ``needs_review``
    * field absent on a present document -> ``deficient``
    * field present but low-confidence -> ``needs_review``
    """
    doc = evidence.first(doc_type)
    if doc is None:  # pragma: no cover - engine only runs a rule when its docs are present
        raise _ShortCircuit(
            RuleResult(FindingStatus.NEEDS_REVIEW, f"{doc_type} not available", 0.5)
        )
    field = doc.get(name)
    if field is None:
        raise _ShortCircuit(
            RuleResult(
                FindingStatus.DEFICIENT,
                f"'{name}' is missing from the {doc_type}",
                0.9,
                (SourcePointer(doc_id=doc.doc_id),),
            )
        )
    if field.confidence < ctx.min_field_confidence:
        raise _ShortCircuit(
            RuleResult(
                FindingStatus.NEEDS_REVIEW,
                f"'{name}' on the {doc_type} was read with low confidence — verify manually",
                field.confidence,
                _ptr(field, doc),
            )
        )
    return field, doc


def _norm_text(value: object) -> str:
    return " ".join(str(value).strip().lower().split())


def _as_float(value: object) -> float | None:
    try:
        return float(re.sub(r"[,$\s]", "", str(value)))
    except (TypeError, ValueError):
        return None


def _close(a: float, b: float, tolerance: float) -> bool:
    scale = max(abs(a), abs(b))
    if scale == 0.0:
        return True
    return abs(a - b) / scale <= tolerance


_HTS_RE = re.compile(r"^\d{6,10}$")


def _hts_wellformed(value: object) -> bool:
    """HTS/HS codes are 6-10 digits; punctuation (dots, spaces, dashes) is cosmetic."""
    return bool(_HTS_RE.match(re.sub(r"[.\s-]", "", str(value))))


def _rule(fn: RuleCheck) -> RuleCheck:
    """Wrap a rule body so any ``_ShortCircuit`` becomes its ready-made result."""

    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        try:
            return fn(evidence, ctx)
        except _ShortCircuit as sc:
            return sc.result

    return check


# --- Customs rules (Layer 2) ----------------------------------------------


@_rule
def _hts_code_present(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
    field, doc = _field(evidence, ctx, "commercial_invoice", "hts_code")
    if _hts_wellformed(field.value):
        return RuleResult(
            FindingStatus.PRESENT,
            f"HTS code {field.value} present and well-formed",
            field.confidence,
            _ptr(field, doc),
        )
    return RuleResult(
        FindingStatus.DEFICIENT,
        f"HTS code '{field.value}' is malformed (expected 6-10 digits)",
        field.confidence,
        _ptr(field, doc),
    )


@_rule
def _origin_consistent(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
    inv_f, inv = _field(evidence, ctx, "commercial_invoice", "country_of_origin")
    coo_f, coo = _field(evidence, ctx, "certificate_of_origin", "country_of_origin")
    conf = min(inv_f.confidence, coo_f.confidence)
    sources = _ptr(inv_f, inv) + _ptr(coo_f, coo)
    if _norm_text(inv_f.value) == _norm_text(coo_f.value):
        return RuleResult(
            FindingStatus.PRESENT, f"Country of origin consistent ({inv_f.value})", conf, sources
        )
    return RuleResult(
        FindingStatus.DEFICIENT,
        f"Country of origin mismatch: invoice says '{inv_f.value}', "
        f"certificate says '{coo_f.value}'",
        conf,
        sources,
    )


@_rule
def _value_matches_packing_list(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
    _field(evidence, ctx, "commercial_invoice", "currency")  # currency must be declared
    inv_f, inv = _field(evidence, ctx, "commercial_invoice", "total_value")
    pl_f, pl = _field(evidence, ctx, "packing_list", "total_value")
    conf = min(inv_f.confidence, pl_f.confidence)
    sources = _ptr(inv_f, inv) + _ptr(pl_f, pl)
    inv_val, pl_val = _as_float(inv_f.value), _as_float(pl_f.value)
    if inv_val is None or pl_val is None:
        return RuleResult(
            FindingStatus.NEEDS_REVIEW,
            "Declared values are not numeric — verify manually",
            conf,
            sources,
        )
    if _close(inv_val, pl_val, ctx.value_tolerance):
        return RuleResult(
            FindingStatus.PRESENT,
            f"Declared value matches packing list ({inv_f.value})",
            conf,
            sources,
        )
    return RuleResult(
        FindingStatus.DEFICIENT,
        f"Declared value mismatch: invoice {inv_f.value} vs packing list {pl_f.value}",
        conf,
        sources,
    )


@_rule
def _weight_count_consistent(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
    pl_w, pl = _field(evidence, ctx, "packing_list", "net_weight")
    bl_w, bl = _field(evidence, ctx, "bill_of_lading", "net_weight")
    pl_c, _ = _field(evidence, ctx, "packing_list", "total_cartons")
    bl_c, _ = _field(evidence, ctx, "bill_of_lading", "total_cartons")
    conf = min(pl_w.confidence, bl_w.confidence, pl_c.confidence, bl_c.confidence)
    sources = _ptr(pl_w, pl) + _ptr(bl_w, bl)

    w1, w2 = _as_float(pl_w.value), _as_float(bl_w.value)
    c1, c2 = _as_float(pl_c.value), _as_float(bl_c.value)
    if None in (w1, w2, c1, c2):
        return RuleResult(
            FindingStatus.NEEDS_REVIEW,
            "Weight/carton counts are not numeric — verify manually",
            conf,
            sources,
        )
    assert w1 is not None and w2 is not None and c1 is not None and c2 is not None
    weight_ok = _close(w1, w2, ctx.value_tolerance)
    count_ok = c1 == c2
    if weight_ok and count_ok:
        return RuleResult(
            FindingStatus.PRESENT,
            "Net weight and carton count agree across packing list and bill of lading",
            conf,
            sources,
        )
    problems = []
    if not weight_ok:
        problems.append(f"net weight {pl_w.value} vs {bl_w.value}")
    if not count_ok:
        problems.append(f"carton count {pl_c.value} vs {bl_c.value}")
    return RuleResult(
        FindingStatus.DEFICIENT,
        "Manifest inconsistency: " + "; ".join(problems),
        conf,
        sources,
    )


@_rule
def _parties_aligned(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
    mismatched: list[str] = []
    confidences: list[float] = []
    sources: tuple[SourcePointer, ...] = ()
    for party in ("exporter", "consignee"):
        inv_f, inv = _field(evidence, ctx, "commercial_invoice", party)
        pl_f, pl = _field(evidence, ctx, "packing_list", party)
        bl_f, bl = _field(evidence, ctx, "bill_of_lading", party)
        confidences += [inv_f.confidence, pl_f.confidence, bl_f.confidence]
        sources += _ptr(inv_f, inv) + _ptr(pl_f, pl) + _ptr(bl_f, bl)
        values = {_norm_text(inv_f.value), _norm_text(pl_f.value), _norm_text(bl_f.value)}
        if len(values) > 1:
            mismatched.append(party)
    conf = min(confidences) if confidences else 1.0
    if not mismatched:
        return RuleResult(
            FindingStatus.PRESENT,
            "Exporter and consignee match across invoice, packing list, and bill of lading",
            conf,
            sources,
        )
    return RuleResult(
        FindingStatus.DEFICIENT,
        f"Party mismatch across documents: {', '.join(mismatched)}",
        conf,
        sources,
    )


# --- Customs checklist ----------------------------------------------------

CUSTOMS_CHECKLIST = Checklist(
    id="customs",
    name="Customs Pre-Clearance",
    doc_types=(
        DocType(id="commercial_invoice", name="Commercial Invoice"),
        DocType(id="packing_list", name="Packing List"),
        DocType(id="bill_of_lading", name="Bill of Lading / Air Waybill"),
        DocType(id="certificate_of_origin", name="Certificate of Origin"),
    ),
    document_requirements=(
        DocumentRequirement("doc.commercial_invoice", "commercial_invoice", "Commercial Invoice"),
        DocumentRequirement("doc.packing_list", "packing_list", "Packing List"),
        DocumentRequirement("doc.bill_of_lading", "bill_of_lading", "Bill of Lading / Air Waybill"),
        DocumentRequirement(
            "doc.certificate_of_origin", "certificate_of_origin", "Certificate of Origin"
        ),
    ),
    field_rules=(
        FieldRule(
            "rule.hts_code",
            "HTS/HS classification code present and well-formed on the commercial invoice",
            ("commercial_invoice",),
            _hts_code_present,
        ),
        FieldRule(
            "rule.origin_consistent",
            "Country of origin agrees between commercial invoice and certificate of origin",
            ("commercial_invoice", "certificate_of_origin"),
            _origin_consistent,
        ),
        FieldRule(
            "rule.value_matches",
            "Declared currency present and value matches the packing list",
            ("commercial_invoice", "packing_list"),
            _value_matches_packing_list,
        ),
        FieldRule(
            "rule.weight_count",
            "Net weight and carton count agree across packing list and bill of lading",
            ("packing_list", "bill_of_lading"),
            _weight_count_consistent,
        ),
        FieldRule(
            "rule.parties_aligned",
            "Exporter and consignee align across invoice, packing list, and bill of lading",
            ("commercial_invoice", "packing_list", "bill_of_lading"),
            _parties_aligned,
        ),
    ),
)


# --- Registry -------------------------------------------------------------

_CHECKLISTS: dict[str, Checklist] = {CUSTOMS_CHECKLIST.id: CUSTOMS_CHECKLIST}


def get_checklist(checklist_id: str) -> Checklist:
    """Return the checklist registered under ``checklist_id``.

    Raises :class:`~ragchat.errors.UnknownChecklistError` for an unregistered id.
    """
    try:
        return _CHECKLISTS[checklist_id]
    except KeyError as exc:
        known = ", ".join(sorted(_CHECKLISTS)) or "(none)"
        raise UnknownChecklistError(
            f"Unknown checklist '{checklist_id}'. Available: {known}."
        ) from exc
