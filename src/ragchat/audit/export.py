"""Render a Gap Report into a client-ready "missing-items request".

The day-1 deliverable from the pitch: turn an audit's ``missing`` / ``deficient`` /
``needs_review`` findings into a plain-text message an ops user can paste into an email or
hand back to the submitter — with page-cited evidence so the recipient can fix each item.

It reads only the unified :class:`~ragchat.audit.report.GapReport`, so it works for **every**
vertical with no per-vertical code. Pure and deterministic → unit-testable.
"""

from __future__ import annotations

from ragchat.audit.report import Finding, GapReport


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _cite(finding: Finding) -> str:
    """Trailing ``(doc p.N; doc2 p.M)`` citation for a finding's sources, or ``""``."""
    cites = []
    for s in finding.sources:
        cites.append(s.doc_id + (f" p.{s.page}" if s.page else ""))
    return f"  ({'; '.join(cites)})" if cites else ""


def render_request(report: GapReport, *, checklist_name: str) -> str:
    """Render ``report`` as a copy-paste request message for the submitter."""
    if report.is_clear:
        return (
            f"No action required — {checklist_name}\n\n"
            "Your submission is complete: all required documents are present and every "
            "check passed."
        )

    missing, deficient, review = report.missing, report.deficient, report.needs_review
    counts = []
    if missing:
        counts.append(_plural(len(missing), "missing document", "missing documents"))
    if deficient:
        counts.append(_plural(len(deficient), "discrepancy", "discrepancies"))
    if review:
        counts.append(_plural(len(review), "item to verify", "items to verify"))

    lines = [
        f"Action Required — {checklist_name}",
        "",
        "Your submission was reviewed and needs attention before it can proceed: "
        f"{', '.join(counts)}.",
    ]
    if missing:
        lines += ["", f"MISSING ({len(missing)}) — please provide:"]
        lines += [f"  - {f.summary}" for f in missing]
    if deficient:
        lines += ["", f"DISCREPANCIES ({len(deficient)}) — please correct:"]
        lines += [f"  - {f.summary}{_cite(f)}" for f in deficient]
    if review:
        lines += ["", f"TO VERIFY ({len(review)}):"]
        lines += [f"  - {f.summary}{_cite(f)}" for f in review]
    lines += ["", "Once updated, please resubmit the complete packet. Thank you."]
    return "\n".join(lines)
