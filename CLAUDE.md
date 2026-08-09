# CLAUDE.md — project memory & work state

> Read this first. It captures where the pivot stands, the decisions already made (don't
> relitigate them), and how to resume cleanly. Full design rationale lives in `PLAN.md`.

## What this project is now

Originally `ragchat`, a retrieval-augmented **Q&A** service. We are pivoting it into a
**Packet Auditor**: ingest a packet of documents → check it against a checklist → return a
structured **Gap Report** of what's missing/deficient. First vertical: **Customs
Pre-Clearance**. The old chat survives as a secondary feature (an "Ask" tab).

Every vertical is the *same engine* with a *different checklist*. Adding one = adding a
checklist, not changing engine code.

## Status (as of Phase 1)

- **Phase 0 — DONE, merged to `main`.** Pure domain model + gap engine + Customs checklist.
- **Phase 1 — DONE, merged to `main`.** Real files → Gap Report: intake router, Gemini
  classifier/extractor, `AuditService`, `POST /audit`, CLI `audit`, web UI Audit tab,
  hermetic eval.
- **Phase 2 — NOT STARTED.** Held while Phase 1 is deployed to Render and tested live with
  real sample packets. **Do not start Phase 2 until the user confirms live testing is
  done** and shares findings.

Feature branch for this work: `claude/rag-chat-pivot-plan-uufnkm` (Phase 0+1 already pulled
into `main`). When resuming, branch fresh from latest `main`.

## Architecture map (audit feature)

Pure domain package `src/ragchat/audit/` — clean DAG, no I/O:
- `report.py` — `FindingStatus` (present/missing/deficient/**needs_review**), `SourcePointer`,
  `Finding`, `GapReport` (buckets are derived views over `findings`).
- `evidence.py` — engine input contract: `ExtractedField`, `ClassifiedDocument`,
  `PacketEvidence`, `RuleContext`, `RuleResult`.
- `checklist.py` — two-layer checklist-as-data: `DocType`(+`FieldSpec` extraction schema),
  `DocumentRequirement` (Layer 1 presence), `FieldRule` (Layer 2 cross-doc rules),
  `CUSTOMS_CHECKLIST`, `get_checklist`. **Rules are data.**
- `engine.py` — `evaluate(checklist, evidence)`; pure; Layer 1 presence + unrecognized-doc
  handling + Layer 2 rules gated on their docs being confidently present.
- `classifier.py` / `extractor.py` — `Classifier`/`Extractor` **protocols** + `Gemini*`
  adapters (the only model-facing code; injected so tests use fakes).

Pipeline & surfaces:
- `ingestion/router.py` — `route()` picks text path (wraps existing `extractors.py`) vs
  multimodal path (scanned PDFs/images). No model calls; fully tested.
- `rag/llm.py` — `build_vision_llm`, `build_document_message` (text or inline image parts).
- `service.py` — `AuditService.audit_packet()` composes router→classify→extract→engine,
  enforces file cap before any model call, persists atomically (session-scoped).
  `build_audit_service()` wires production deps.
- `db/models.py` — `Packet`, `PacketDocument` (cascade from `sessions`); migration
  `migrations/versions/0002_packet_auditor_schema.py`.
- `api/routes.py` — `POST /audit` (multipart); `api/schemas.py` — `GapReportSchema` (4
  buckets + `is_clear`) `.from_report`.
- `cli.py` — `ragchat audit <files...>`. `api/static/index.html` — Ask/Audit mode toggle.

Tests: `tests/unit/test_{report,checklist,gap_engine,router,audit_service,audit_api}.py`;
hermetic eval `tests/eval/` (gold Customs packets, precision/recall gate at 1.0). Fakes
`FakeClassifier`/`FakeExtractor` live in `tests/conftest.py`.

## Locked decisions (do not re-open without reason)

1. **Four report states** incl. `needs_review` — never emit a false `missing`/`deficient`
   from a low-confidence read; route uncertainty to `needs_review`. Every finding carries
   confidence + source pointer.
2. **Two-layer checklist**: presence (Layer 1) then per-field/cross-doc rules (Layer 2).
   Layer 2 is the value; keep investing there.
3. **Intake router wraps, not replaces, `extractors.py`.** Scanned path = one multimodal
   call (parse+extract together). No new parser vendor unless measured.
4. **No pgvector on the audit path** — direct structured Gemini calls. pgvector stays only
   for the secondary chat + future large-record verticals.
5. **v1 = completeness/consistency only. NO authenticity/fraud detection.**
6. **Model-facing steps are injected behind protocols** so the whole pipeline stays
   hermetically testable (no keys/network in CI).

## Deviations from PLAN.md (intentional)

- Added `audit/evidence.py` (plan folded input types into `engine.py`) to keep imports a DAG.
- Added `FieldSpec` + `Checklist.fields_for` to the checklist (extraction schema as data).
- `/audit` is gated by the **ingest** burst limiter and is **not** counted against
  `DAILY_REQUEST_BUDGET` (that only meters `/ask`). Revisit metering in Phase 2/3.
- UI is an Ask/Audit **mode toggle**, not a separate page.

## Known limitations to address (Phase 2+ candidates)

- **Live Gemini multimodal path is wired but UNVALIDATED** — never exercised in CI (LLM is
  faked, same as the rest of the repo). Real scanned-PDF/image behavior is unproven until
  live testing. This is the #1 thing live testing must confirm.
- Confidence thresholds are constants (`min_classification_confidence=0.5`,
  `RuleContext.min_field_confidence=0.5`, `value_tolerance=0.01`) — untuned against real docs.
- Audit calls aren't budget-metered (cost exposure under load).
- One packet = one audit; no re-audit/history surfacing beyond persistence.
- Extraction schema returns `value` as string; numeric rules parse leniently.

## Phase 2 scope (when unblocked)

Deepen Layer-2 rules + **calibrate confidence thresholds against a larger, real-document
eval set** (expand `tests/eval/` with packets derived from live-test samples). Add
tolerances. THEN Phase 3: reviewer accept/override workflow + "missing-items request"
export. Regulated verticals (insurance/veterans) come after the engine is proven.

**Resume checklist:** (1) `git fetch && git checkout -B <branch> origin/main`;
(2) re-read this file + `PLAN.md`; (3) fold live-test findings into `tests/eval/`;
(4) proceed with threshold calibration.

## Dev commands

- `make check` — ruff (lint+format), `mypy --strict`, pytest. **Run before every commit.**
  (In this environment mypy must be invoked as `python -m mypy` so it sees `pydantic`.)
- `make test` / `python -m pytest -q` — unit + eval (integration deselected by default).
- Gates must stay green: ruff clean, mypy clean, all tests pass, eval precision/recall = 1.0.

## Live testing (Phase 1) — record findings here

Deploy = Render Blueprint (`render.yaml`) + Neon; secrets `DATABASE_URL`,
`COHERE_API_KEY`, `GOOGLE_API_KEY`. Audit uses `GOOGLE_API_KEY` + `VISION_MODEL`
(default `gemini-flash-latest`); tune via env `MAX_FILES_PER_PACKET`, `ACTIVE_CHECKLIST`.

<!-- Paste live-test observations here before starting Phase 2:
     - which doc types classified well / poorly
     - extraction accuracy per field
     - false missing/deficient (should be ~0) and any that leaked
     - scanned-PDF/image path behavior
     - latency/cost per packet
-->
