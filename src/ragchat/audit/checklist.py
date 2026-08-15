"""Checklist domain types — the compiled shape a vertical audit takes.

A checklist is two layers:

* **Layer 1 (presence)** — which document *types* must be in the packet
  (:class:`DocumentRequirement`).
* **Layer 2 (field rules)** — per-type field extraction (:class:`FieldSpec`) plus
  cross-document checks (:class:`FieldRule`), where the ROI lives.

These are plain data. Verticals are authored as declarative manifests (``manifest.py``)
that **compile** to a :class:`Checklist` using the primitives in ``checks.py`` — so adding a
vertical is dropping in a YAML file, never changing engine code. This module holds only the
types; the loader and the registry (``get_checklist``) live in ``manifest.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ragchat.audit.evidence import PacketEvidence, RuleContext, RuleResult

RuleCheck = Callable[[PacketEvidence, RuleContext], RuleResult]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A field the analyzer should pull from a document type. The description guides the
    model; the name is the key the Layer-2 rules read."""

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class DocType:
    """A document type a checklist knows about, plus the fields to extract from it."""

    id: str
    name: str
    fields: tuple[FieldSpec, ...] = ()


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

    def doc_type(self, doc_type_id: str) -> DocType | None:
        return next((dt for dt in self.doc_types if dt.id == doc_type_id), None)

    def fields_for(self, doc_type_id: str) -> tuple[FieldSpec, ...]:
        """The fields the analyzer should pull for ``doc_type_id`` (empty if unknown)."""
        dt = self.doc_type(doc_type_id)
        return dt.fields if dt is not None else ()
