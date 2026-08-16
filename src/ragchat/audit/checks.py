"""Reusable, parameterized check primitives — the vocabulary manifests compose.

A vertical's rules are expressed declaratively in a manifest (see ``manifest.py``); each
manifest rule names one of these primitives and supplies parameters. A primitive is a
factory that returns a :data:`~ragchat.audit.checklist.RuleCheck` closure, so the pure gap
engine runs manifest-driven rules exactly like the hand-written ones it replaced.

Keeping the set small and structured (no string-expression DSL) is deliberate: each
primitive is type-checked, and new verticals reuse these rather than adding engine code.
Shared field/number/entity helpers live here too, since both the primitives and any
bespoke rule need them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from ragchat.audit.checklist import RuleCheck
from ragchat.audit.evidence import ExtractedField, PacketEvidence, RuleContext, RuleResult
from ragchat.audit.report import FindingStatus, SourcePointer

FieldRef = tuple[str, str]  # (doc_type, field)


# --- Shared helpers -------------------------------------------------------


class _ShortCircuit(Exception):
    """Abandon a rule early with a ready-made result."""

    def __init__(self, result: RuleResult) -> None:
        super().__init__(result.summary)
        self.result = result


def _ptr(field: ExtractedField | None, doc_id: str) -> tuple[SourcePointer, ...]:
    if field is not None and field.source is not None:
        return (field.source,)
    return (SourcePointer(doc_id=doc_id),)


def _require(
    evidence: PacketEvidence, ctx: RuleContext, doc_type: str, name: str
) -> tuple[ExtractedField, str]:
    """Resolve a required field or short-circuit: missing -> deficient, low-conf -> review."""
    doc = evidence.first(doc_type)
    if doc is None:  # pragma: no cover - engine gates rules on presence
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
                _ptr(field, doc.doc_id),
            )
        )
    return field, doc.doc_id


def _norm_text(value: object) -> str:
    return " ".join(str(value).strip().lower().split())


_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def as_float(value: object) -> float | None:
    """Pull the leading numeric token, tolerating units/currency/thousands separators.

    ``"6,800.00 KG"``/``"80 Crates"``/``"USD 10,000.00"`` -> the number, so consistency
    rules compare quantities, not strings.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    match = _NUM_RE.search(str(value))
    if match is None:
        return None
    try:
        return float(match.group().replace(",", ""))
    except ValueError:  # pragma: no cover - regex guarantees a numeric shape
        return None


def _close(a: float, b: float, tolerance: float) -> bool:
    scale = max(abs(a), abs(b))
    return True if scale == 0.0 else abs(a - b) / scale <= tolerance


_ENTITY_STOPWORDS = frozenset(
    {
        # corporate forms
        "ltd",
        "limited",
        "inc",
        "incorporated",
        "llc",
        "co",
        "company",
        "corp",
        "corporation",
        "plc",
        "gmbh",
        "sa",
        "bv",
        "pvt",
        "the",
        # party/role labels
        "exporter",
        "shipper",
        "seller",
        "consignee",
        "buyer",
        "importer",
        "to",
        "from",
        # personal honorifics & suffixes (so "Dr. Alex Rivera" == "Alex Rivera, MD")
        "dr",
        "mr",
        "mrs",
        "ms",
        "mx",
        "prof",
        "professor",
        "md",
        "do",
        "phd",
        "esq",
        "jr",
        "sr",
        "ii",
        "iii",
        "iv",
    }
)


def _entity_tokens(value: object) -> frozenset[str]:
    return frozenset(
        t
        for t in re.split(r"[^a-z0-9]+", _norm_text(value))
        if len(t) > 1 and t not in _ENTITY_STOPWORDS
    )


def _same_entity(a: object, b: object, threshold: float = 0.6) -> bool:
    """Whether two party strings name the same entity, tolerating granularity."""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = _entity_tokens(a), _entity_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= threshold


_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y")


def _parse_date(value: object) -> date | None:
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _wrap(fn: RuleCheck) -> RuleCheck:
    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        try:
            return fn(evidence, ctx)
        except _ShortCircuit as sc:
            return sc.result

    return check


# --- Primitives -----------------------------------------------------------


def regex_match(
    *, doc_type: str, field: str, pattern: str, strip: str | None, label: str
) -> RuleCheck:
    """Format check: a field on one document matches ``pattern`` (after optional stripping)."""
    rx = re.compile(pattern)

    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        f, doc_id = _require(evidence, ctx, doc_type, field)
        candidate = re.sub(strip, "", str(f.value)) if strip else str(f.value)
        if rx.fullmatch(candidate):
            return RuleResult(
                FindingStatus.PRESENT,
                f"{label} {f.value} present and well-formed",
                f.confidence,
                _ptr(f, doc_id),
            )
        return RuleResult(
            FindingStatus.DEFICIENT,
            f"{label} '{f.value}' is malformed",
            f.confidence,
            _ptr(f, doc_id),
        )

    return _wrap(check)


def numeric_match(
    *,
    groups: Sequence[Sequence[FieldRef]],
    requires: Sequence[FieldRef] = (),
    tolerance: float | None = None,
    missing: str = "needs_review",
    label: str,
) -> RuleCheck:
    """Cross-document numeric equality (within tolerance) for one or more field groups.

    ``requires`` names fields that must merely be present (e.g. currency). ``missing`` picks
    the outcome when a compared field is absent: ``deficient`` (the field was expected) or
    ``needs_review`` (we simply can't confirm)."""

    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        tol = ctx.value_tolerance if tolerance is None else tolerance
        sources: tuple[SourcePointer, ...] = ()
        for dt, fld in requires:
            f, doc_id = _require(evidence, ctx, dt, fld)
            sources += _ptr(f, doc_id)

        resolved: list[list[ExtractedField]] = []
        for group in groups:
            fields: list[ExtractedField] = []
            for dt, fld in group:
                doc = evidence.first(dt)
                fv = doc.get(fld) if doc is not None else None
                if fv is None or doc is None:
                    if missing == "deficient":
                        extra = (SourcePointer(doc.doc_id),) if doc is not None else ()
                        raise _ShortCircuit(
                            RuleResult(
                                FindingStatus.DEFICIENT,
                                f"'{fld}' is missing from the {dt}",
                                0.9,
                                (*sources, *extra),
                            )
                        )
                    return RuleResult(
                        FindingStatus.NEEDS_REVIEW,
                        f"{label} is not stated on all documents — verify manually",
                        0.6,
                        sources,
                    )
                if fv.confidence < ctx.min_field_confidence:
                    return RuleResult(
                        FindingStatus.NEEDS_REVIEW,
                        f"{label} read with low confidence — verify manually",
                        fv.confidence,
                        (*sources, *_ptr(fv, doc.doc_id)),
                    )
                sources = (*sources, *_ptr(fv, doc.doc_id))
                fields.append(fv)
            resolved.append(fields)

        confs = [f.confidence for g in resolved for f in g]
        conf = min(confs) if confs else 1.0
        mismatches: list[str] = []
        for fields in resolved:
            nums = [as_float(f.value) for f in fields]
            if any(n is None for n in nums):
                return RuleResult(
                    FindingStatus.NEEDS_REVIEW,
                    f"{label} not numeric — verify manually",
                    conf,
                    sources,
                )
            base = nums[0]
            assert base is not None
            if not all(n is not None and _close(base, n, tol) for n in nums[1:]):
                mismatches.append(" vs ".join(str(f.value) for f in fields))
        if mismatches:
            return RuleResult(
                FindingStatus.DEFICIENT, f"{label} mismatch: {'; '.join(mismatches)}", conf, sources
            )
        return RuleResult(FindingStatus.PRESENT, f"{label} consistent", conf, sources)

    return _wrap(check)


def cross_match(
    *, fields: Sequence[str], docs: Sequence[str], comparator: str, threshold: float, label: str
) -> RuleCheck:
    """Cross-document field agreement, by exact text or tolerant entity match."""

    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        mismatched: list[str] = []
        confidences: list[float] = []
        sources: tuple[SourcePointer, ...] = ()
        for fld in fields:
            values: list[object] = []
            for dt in docs:
                f, doc_id = _require(evidence, ctx, dt, fld)
                confidences.append(f.confidence)
                sources += _ptr(f, doc_id)
                values.append(f.value)
            if not _all_agree(values, comparator, threshold):
                mismatched.append(fld)
        conf = min(confidences) if confidences else 1.0
        if mismatched:
            return RuleResult(
                FindingStatus.DEFICIENT,
                f"{label} mismatch across documents: {', '.join(mismatched)}",
                conf,
                sources,
            )
        return RuleResult(
            FindingStatus.PRESENT, f"{label} consistent across documents", conf, sources
        )

    return _wrap(check)


def _all_agree(values: Sequence[object], comparator: str, threshold: float) -> bool:
    if comparator == "entity":
        return all(
            _same_entity(values[i], values[j], threshold)
            for i in range(len(values))
            for j in range(i + 1, len(values))
        )
    return len({_norm_text(v) for v in values}) <= 1


def date_valid(
    *, doc_type: str, field: str, max_age_years: int | None, not_expired: bool, label: str
) -> RuleCheck:
    """Recency/expiry check on a date field (e.g. a test score younger than 2 years)."""

    def check(evidence: PacketEvidence, ctx: RuleContext) -> RuleResult:
        f, doc_id = _require(evidence, ctx, doc_type, field)
        parsed = _parse_date(f.value)
        if parsed is None:
            return RuleResult(
                FindingStatus.NEEDS_REVIEW,
                f"{label} '{f.value}' is not a readable date — verify manually",
                f.confidence,
                _ptr(f, doc_id),
            )
        today = date.today()
        if max_age_years is not None and parsed < today - timedelta(days=365 * max_age_years):
            return RuleResult(
                FindingStatus.DEFICIENT,
                f"{label} is older than {max_age_years} years ({f.value})",
                f.confidence,
                _ptr(f, doc_id),
            )
        if not_expired and parsed < today:
            return RuleResult(
                FindingStatus.DEFICIENT,
                f"{label} has expired ({f.value})",
                f.confidence,
                _ptr(f, doc_id),
            )
        return RuleResult(
            FindingStatus.PRESENT,
            f"{label} within validity ({f.value})",
            f.confidence,
            _ptr(f, doc_id),
        )

    return _wrap(check)
