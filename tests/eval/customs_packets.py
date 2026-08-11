"""Gold-standard Customs packets for the hermetic audit eval.

Each :class:`EvalPacket` is a small, labelled packet: the files, the (faked) classification
and extraction the model would produce for them, and the set of requirement ids we expect
the audit to flag as gaps. The eval feeds these through the *real* router, gap engine, and
persistence with a *scripted* model, so a checklist or engine regression that invents or
drops a gap is caught before it ever reaches a live demo.

To add coverage, add an :class:`EvalPacket` here — no engine changes required.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Enough readable text that the intake router takes its (free) text path.
DOC_TEXT = b"A customs trade document containing enough readable text for the text path."


@dataclass(frozen=True)
class EvalDoc:
    """One labelled file: how the model would classify it and what it would extract."""

    filename: str
    doc_type: str | None
    confidence: float
    fields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalPacket:
    """A labelled packet plus the requirement ids expected to be flagged as gaps."""

    name: str
    docs: list[EvalDoc]
    expected_gaps: set[str]


def _complete_docs() -> list[EvalDoc]:
    """A fully consistent, confidently classified packet — the baseline to mutate."""
    return [
        EvalDoc(
            "invoice.txt",
            "commercial_invoice",
            0.97,
            {
                "hts_code": "8471.30.01",
                "country_of_origin": "China",
                "currency": "USD",
                "total_value": "10000",
                "total_quantity": "200",
                "exporter": "Acme Ltd",
                "consignee": "Globex Inc",
            },
        ),
        EvalDoc(
            "packing.txt",
            "packing_list",
            0.96,
            {
                "total_value": "10000",
                "total_quantity": "200",
                "net_weight": "500",
                "total_cartons": "20",
                "exporter": "Acme Ltd",
                "consignee": "Globex Inc",
            },
        ),
        EvalDoc(
            "bol.txt",
            "bill_of_lading",
            0.95,
            {
                "net_weight": "500",
                "total_cartons": "20",
                "exporter": "Acme Ltd",
                "consignee": "Globex Inc",
            },
        ),
        EvalDoc("origin.txt", "certificate_of_origin", 0.94, {"country_of_origin": "China"}),
    ]


def _without(filename: str) -> list[EvalDoc]:
    return [d for d in _complete_docs() if d.filename != filename]


def _mutated(filename: str, **overrides: object) -> list[EvalDoc]:
    docs = _complete_docs()
    for i, doc in enumerate(docs):
        if doc.filename == filename:
            fields = dict(doc.fields)
            new_conf = doc.confidence
            for key, value in overrides.items():
                if key == "confidence":
                    new_conf = float(value)  # type: ignore[arg-type]
                else:
                    fields[key] = str(value)
            docs[i] = EvalDoc(doc.filename, doc.doc_type, new_conf, fields)
    return docs


PACKETS: list[EvalPacket] = [
    EvalPacket("clean_complete", _complete_docs(), set()),
    EvalPacket("missing_certificate", _without("origin.txt"), {"doc.certificate_of_origin"}),
    EvalPacket("malformed_hts", _mutated("invoice.txt", hts_code="12"), {"rule.hts_code"}),
    EvalPacket(
        "value_mismatch", _mutated("packing.txt", total_value="20000"), {"rule.value_matches"}
    ),
    EvalPacket(
        "origin_mismatch",
        _mutated("origin.txt", country_of_origin="Vietnam"),
        {"rule.origin_consistent"},
    ),
    EvalPacket("low_confidence_bol", _mutated("bol.txt", confidence=0.20), {"doc.bill_of_lading"}),
    # Scenario 2 (live test 2026-08-11): invoice declares 200, packing list says 400.
    EvalPacket(
        "quantity_mismatch",
        _mutated("packing.txt", total_quantity="400"),
        {"rule.quantity_matches"},
    ),
    # Unit-laden numeric strings must parse, not fall to needs_review.
    EvalPacket(
        "units_in_weight",
        _mutated("packing.txt", net_weight="500 kg", total_cartons="20 crates"),
        set(),
    ),
    # Same entity at different granularity across docs must not flag a party mismatch.
    EvalPacket(
        "party_granularity",
        _mutated("packing.txt", exporter="Exporter: Acme Ltd, Shenzhen, China"),
        set(),
    ),
]
