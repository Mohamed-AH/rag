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
- **Phase 2 — IN PROGRESS.** Live testing confirmed the multimodal + combined-PDF paths;
  three false-positive/robustness findings folded in so far (see log below):
  robust numeric parsing (`_as_float` strips units/currency), tolerant party matching
  (`_same_entity` containment/token-overlap + name-only extraction), and a new
  cross-document **quantity** rule (`rule.quantity_matches`) catching under-declaration
  fraud. Remaining Phase-2 work: broader financial↔physical reconciliation, confidence-
  threshold calibration against *real* documents, more verticals.

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
- `analyzer.py` — `Analyzer` **protocol** + `GeminiAnalyzer`: **single-pass** classify+extract
  in one structured call per **file**, returning a **list** of documents so a combined
  multi-page packet (one PDF, many docs) is split into all its documents (the only
  model-facing code; injected so tests use a fake). Takes a `select_llm(content)` callable
  for per-path model routing.

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
hermetic eval `tests/eval/` (gold Customs packets, precision/recall gate at 1.0). Fake
`FakeAnalyzer`/`FakeAnalysis` live in `tests/conftest.py`.

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
`COHERE_API_KEY`, `GOOGLE_API_KEY`. Audit uses `GOOGLE_API_KEY`; both `LLM_MODEL` (text)
and `VISION_MODEL` (scans) default to `gemini-flash-lite-latest` — Flash-Lite is natively
multimodal, so one high-free-quota lite tier reads both. Bump `VISION_MODEL` to a heavier
multimodal model only if real scans need more OCR. Tune `MAX_FILES_PER_PACKET`,
`ACTIVE_CHECKLIST`.

### First live deploy (2026-08-10) — findings + fixes applied

Deploy to Render + Neon succeeded (migration `0002` applied). First real audit surfaced
three coupled issues, all now fixed on the feature branch:

1. **Free-tier quota exhaustion.** `VISION_MODEL=gemini-flash-latest` → `gemini-3.6-flash`,
   free tier **20 req/day**; each file = 2 calls (classify+extract). Fix: **per-path model
   routing** — text-path docs use `LLM_MODEL` (lite, higher free quota) and scans/images use
   `VISION_MODEL` via a `select_llm(content)` callable. Classify+extract were later **merged
   into one call** (`audit/analyzer.py`, `GeminiAnalyzer`) — halves calls/latency (2N → N).
   Later still, since **Flash-Lite is natively multimodal**, `VISION_MODEL` was defaulted to
   the same `gemini-flash-lite-latest` so scans also run on the generous lite quota (the
   routing stays, letting VISION_MODEL be overridden to a heavier model if needed).
2. **Health-check crash / worker starvation.** `/audit` is async but did blocking model
   I/O; a quota-retry storm tied up the single free-tier worker → `/health` timed out →
   Render restarted the instance. Fix: `await run_in_threadpool(service.audit_packet, …)`
   so blocking work never starves the event loop. Also bounded provider retries
   (`max_retries=2`) so quota errors fail fast.
3. **"Unexpected end of JSON input" in the UI.** Frontend assumed every response is JSON; a
   dropped/empty body (from #2) produced a cryptic error. Fix: defensive `readJson()` +
   audit now accepts a **Google-only** BYO key (`_byo_google_key`) so live testing can run
   on a billed key without a Cohere key.

**Still to validate live (pending quota/billing):** real classification accuracy per doc
type, extraction accuracy per field (esp. numbers + whether snippets point at the value),
false missing/deficient rate (target ~0), the scanned-PDF/image multimodal path, and
latency/cost per packet. Paste observations below.

**Sample fixtures for live testing:** `samples/customs/` holds one consistent shipment in
two forms — `digital/*.pdf` (text-layer → text path) and `scanned/*.png` (rasterized →
multimodal path) — plus a README of test scenarios and `generate.py` (fpdf2 + Pillow) to
regenerate. Use the scanned set to exercise the still-unproven multimodal path; fold real
observations into `tests/eval/` for Phase 2 calibration.

### Scanned + digital run (2026-08-10) — multimodal path PROVEN; 2 false positives found

Ran the sample packet both as digital PDFs and scanned PNGs (Flash-Lite vision).

- **Multimodal path works.** All 4 scanned docs classified correctly; HTS `8471.30.0100`
  extracted + well-formed; origin consistent; declared value matched packing list;
  Present = 7. The #1 unknown is resolved.
- **False positive #1 — party matching (DEFICIENT, in *both* PDF and PNG runs).**
  `_parties_aligned` does exact normalized-string equality, but the analyzer returns the
  same entity at different granularity per doc (`"Acme Manufacturing Ltd"` vs
  `"Exporter: Acme Manufacturing Ltd, Shenzhen, China"` vs
  `"Shipper: Acme Manufacturing Ltd, 12 Industrial Road, …"`) → falsely flagged mismatch.
  Fix (Phase 2): extract entity **name only** (analyzer prompt) **and** make the rule
  tolerant (containment / token-overlap) rather than exact equality.
- **False positive #2 — numeric parsing (NEEDS_REVIEW).** `_as_float` can't parse
  `"500 kg"` (unit suffix) → weight/carton rule → needs_review. Same root cause as the
  earlier "declared values not numeric." Fix (Phase 2): pull the leading numeric token,
  strip units/currency. (Value rule passed only because the fixture used a clean `10000.00`.)

Both false positives are **engine brittleness** (identical in PDF and PNG runs), not a
vision problem. These two + numeric tolerances are the first Phase-2 work; fold these
sample packets into `tests/eval/` as the calibration set.

### Combined multi-page PDF (2026-08-11) — one-file-many-docs fixed

A single scanned multi-page PDF (all four docs in one file) was classified as **one**
document (commercial invoice) → other three reported MISSING. Root cause: the pipeline
assumed one file = one document. **Fix:** `Analyzer.analyze` now returns a **list** —
`GeminiAnalyzer` asks Gemini to identify *every* document in the file (leveraging native
multi-page multimodal + 1M context) and returns one `ClassifiedDocument` per detected doc;
`AuditService` persists a row per detected doc; ids stay unique (`_ensure_unique_id`).
Still one model call per file (quota-cheap). Covered by
`test_combined_file_yields_multiple_documents`. Combined fixtures added at
`samples/customs/combined/packet_{digital,scanned}.pdf`.

The analyzer prompt/schema handle **unpredictable page order + non-contiguous multi-page
documents**: `_DocResult` carries a full `pages: list[int]` (+ `grouping_reason`), and the
prompt directs the model to scan every page for anchor headers and group pages by shared
identifiers (invoice/BoL/container numbers) rather than assuming order — the single-call
approach's whole-file view is what makes that possible. Doc ids read `packet.pdf (pages 2, 4)`.

### Scenario 2 (2026-08-11) — combined-PDF split PROVEN; 3 Phase-2 fixes applied

Live-ran an engineered combined PDF (`set2.pdf`: invoice 1,000 units vs packing-list
2,000). Combined-PDF split worked (invoice+PL+BoL all detected from one file). Structural
gaps caught (missing COO, missing PL total_value). Two engine gaps found → fixed:
- **Numeric parsing** — `_as_float` rewritten to pull the leading numeric token, so
  `"6,800.00 KG"`/`"80 Crates"`/`"USD 10,000.00"` parse instead of tripping needs_review.
- **Party matching** — `_same_entity` (containment / ≥0.6 token overlap, corporate-form &
  role stopwords stripped) + exporter/consignee FieldSpecs now say "COMPANY NAME only".
- **New rule `rule.quantity_matches`** — invoice `total_quantity` vs packing-list
  `total_quantity` (absent-on-either → needs_review, not a false pass). Catches the
  under-declaration fraud Scenario 2 engineered.
Eval gained `quantity_mismatch`, `units_in_weight`, `party_granularity` gold packets;
116 tests pass, precision/recall still 1.0. **Still missed (future rule):** weight↔quantity
reconciliation (weight implies 2,000 units while invoice declares 1,000) — a derived
cross-metric check, not yet modeled.

**Re-run confirmed (2026-08-15).** `set2.pdf` audited live with the fixes deployed:
`rule.quantity_matches` → **DEFICIENT "invoice 1,000 vs packing list 2,000" @100%** with
page-cited sources (p.1=1,000, p.2=2,000); the weight/carton needs_review and the party
false positive are gone; page-level source pointers work. Remaining set2 deficients
(`total_value` missing on PL, `net_weight` missing on BoL) are correct structural findings.
Possible future refinement: BoL often states *gross* weight only — consider net↔gross
fallback before flagging `net_weight` missing.

<!-- Paste live-test observations here before starting Phase 2:
     - which doc types classified well / poorly
     - extraction accuracy per field
     - false missing/deficient (should be ~0) and any that leaked
     - scanned-PDF/image path behavior
     - latency/cost per packet
-->
