"""Declarative vertical manifests: YAML in, a compiled :class:`Checklist` out.

A vertical is a ``*.yaml`` file under ``ragchat/manifests/`` describing its document types
(and the fields to extract from each) and its rules. Each rule names one of the structured
check primitives in ``checks.py`` and supplies parameters — no string-expression DSL, so the
whole manifest is validated by Pydantic at load time. The compiler turns a manifest into the
same :class:`Checklist` the pure engine already consumes, so **adding a vertical is dropping
in a YAML file** — zero engine or Python changes.

Field references are ``"doc_type.field"`` strings (e.g. ``"commercial_invoice.total_value"``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field

from ragchat.audit import checks
from ragchat.audit.checklist import (
    Checklist,
    DocType,
    DocumentRequirement,
    FieldRule,
    FieldSpec,
    RuleCheck,
)
from ragchat.errors import UnknownChecklistError

_MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"


# --- Manifest schema (validated at load time) -----------------------------


class ManifestField(BaseModel):
    name: str
    description: str


class ManifestDocType(BaseModel):
    id: str
    name: str
    required: bool = True
    fields: list[ManifestField] = Field(default_factory=list)


class _RegexRule(BaseModel):
    id: str
    description: str = ""
    type: Literal["regex_match"]
    doc: str
    field: str
    pattern: str
    strip: str | None = None
    label: str | None = None


class _NumericRule(BaseModel):
    id: str
    description: str = ""
    type: Literal["numeric_match"]
    groups: list[list[str]]
    requires: list[str] = Field(default_factory=list)
    tolerance: float | None = None
    missing: Literal["deficient", "needs_review"] = "needs_review"
    label: str


class _CrossRule(BaseModel):
    id: str
    description: str = ""
    type: Literal["cross_match"]
    fields: list[str]
    docs: list[str]
    comparator: Literal["text", "entity"] = "text"
    threshold: float = 0.6
    label: str


class _DateRule(BaseModel):
    id: str
    description: str = ""
    type: Literal["date_valid"]
    doc: str
    field: str
    max_age_years: int | None = None
    not_expired: bool = False
    label: str


_ManifestRule = Annotated[
    _RegexRule | _NumericRule | _CrossRule | _DateRule, Field(discriminator="type")
]


class Manifest(BaseModel):
    vertical_id: str
    name: str
    documents: list[ManifestDocType]
    rules: list[_ManifestRule] = Field(default_factory=list)


# --- Compilation: Manifest -> Checklist -----------------------------------


def _ref(spec: str) -> checks.FieldRef:
    doc_type, _, field = spec.partition(".")
    if not doc_type or not field:
        raise ValueError(f"Field reference must be 'doc_type.field', got '{spec}'")
    return doc_type, field


def _compile_rule(rule: _ManifestRule) -> FieldRule:
    if isinstance(rule, _RegexRule):
        check: RuleCheck = checks.regex_match(
            doc_type=rule.doc,
            field=rule.field,
            pattern=rule.pattern,
            strip=rule.strip,
            label=rule.label or rule.field,
        )
        doc_types: tuple[str, ...] = (rule.doc,)
    elif isinstance(rule, _NumericRule):
        groups = [[_ref(s) for s in group] for group in rule.groups]
        requires = [_ref(s) for s in rule.requires]
        check = checks.numeric_match(
            groups=groups,
            requires=requires,
            tolerance=rule.tolerance,
            missing=rule.missing,
            label=rule.label,
        )
        seen = {dt for group in groups for dt, _ in group} | {dt for dt, _ in requires}
        doc_types = tuple(sorted(seen))
    elif isinstance(rule, _CrossRule):
        check = checks.cross_match(
            fields=rule.fields,
            docs=rule.docs,
            comparator=rule.comparator,
            threshold=rule.threshold,
            label=rule.label,
        )
        doc_types = tuple(rule.docs)
    else:  # _DateRule
        check = checks.date_valid(
            doc_type=rule.doc,
            field=rule.field,
            max_age_years=rule.max_age_years,
            not_expired=rule.not_expired,
            label=rule.label,
        )
        doc_types = (rule.doc,)
    return FieldRule(id=rule.id, description=rule.description, doc_types=doc_types, check=check)


def compile_manifest(manifest: Manifest) -> Checklist:
    """Compile a validated :class:`Manifest` into a :class:`Checklist`."""
    doc_types = tuple(
        DocType(
            id=d.id,
            name=d.name,
            fields=tuple(FieldSpec(f.name, f.description) for f in d.fields),
        )
        for d in manifest.documents
    )
    requirements = tuple(
        DocumentRequirement(f"doc.{d.id}", d.id, d.name) for d in manifest.documents if d.required
    )
    rules = tuple(_compile_rule(r) for r in manifest.rules)
    return Checklist(
        id=manifest.vertical_id,
        name=manifest.name,
        doc_types=doc_types,
        document_requirements=requirements,
        field_rules=rules,
    )


def load_manifest(source: str | Path) -> Checklist:
    """Parse and compile a single manifest from a YAML string or a file path."""
    text = Path(source).read_text(encoding="utf-8") if _looks_like_path(source) else str(source)
    manifest = Manifest.model_validate(yaml.safe_load(text))
    return compile_manifest(manifest)


def _looks_like_path(source: str | Path) -> bool:
    return isinstance(source, Path) or "\n" not in source


# --- Registry -------------------------------------------------------------


@lru_cache(maxsize=1)
def _registry() -> dict[str, Checklist]:
    """Compile every manifest in the manifests directory once per process."""
    registry: dict[str, Checklist] = {}
    for path in sorted(_MANIFEST_DIR.glob("*.yaml")):
        checklist = load_manifest(path)
        registry[checklist.id] = checklist
    return registry


def available_checklists() -> list[str]:
    """Ids of every vertical whose manifest is installed (drives the UI vertical picker)."""
    return sorted(_registry())


def get_checklist(checklist_id: str) -> Checklist:
    """Return the compiled checklist for ``checklist_id``.

    Raises :class:`~ragchat.errors.UnknownChecklistError` for an unregistered id.
    """
    try:
        return _registry()[checklist_id]
    except KeyError as exc:
        known = ", ".join(available_checklists()) or "(none)"
        raise UnknownChecklistError(
            f"Unknown checklist '{checklist_id}'. Available: {known}."
        ) from exc


# Convenience handle for the first vertical, kept for imports/tests.
CUSTOMS_CHECKLIST = get_checklist("customs")
