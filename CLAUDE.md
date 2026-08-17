# CLAUDE.md — project memory & work state

> Read this first. It captures where the pivot stands, the decisions already made (don't
> relitigate them), and how to resume cleanly. Full design rationale lives in `PLAN.md`.

## What this project is now

Originally `ragchat`, a retrieval-augmented **Q&A** service. We are pivoting it into a
**Packet Auditor**: ingest a packet of documents → check it against a checklist → return a
structured **Gap Report** of what's missing/deficient. First vertical: **Customs
Pre-Clearance**. The old chat survives as a secondary feature (Ask via API/CLI — the web
app is now auditor-only; see the 2026-08-16 UI overhaul log below).

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
- `checklist.py` — **types only**: `DocType`(+`FieldSpec`), `DocumentRequirement` (Layer 1),
  `FieldRule` (Layer 2), `Checklist`. The compiled shape a vertical takes.
- `checks.py` — reusable, parameterized **check primitives** (`regex_match`, `numeric_match`,
  `cross_match` text/entity, `date_valid`, `numeric_threshold` min/max) + shared helpers
  (`as_float`, `_same_entity`, …). The vocabulary manifests compose. **No string-expression DSL.**
- `manifest.py` — Pydantic manifest schema (discriminated on rule `type`) + `compile_manifest`
  (manifest → `Checklist`) + registry (`get_checklist`, `available_checklists`,
  `CUSTOMS_CHECKLIST`). Loads every `manifests/*.yaml` once.
- `../manifests/*.yaml` — **verticals as declarative YAML** (`customs`, `education`,
  `procurement`, `healthcare`, `study_visa`). Adding a vertical = dropping in a file; **zero
  engine/Python changes**. `procurement`/`healthcare` exercise `date_valid`/`required:false`;
  `study_visa` exercises `numeric_threshold`; entity matching strips honorifics/suffixes
  (`Dr.`/`MD`) and corporate forms.
- `engine.py` — `evaluate(checklist, evidence)`; pure; Layer 1 presence + unrecognized-doc
  handling + Layer 2 rules gated on their docs being confidently present.
- `export.py` — `render_request(report, checklist_name)`: the Phase-3 **missing-items
  request** (client-ready email text with page-cited evidence). Reads only the unified
  `GapReport`, so it works for every vertical; surfaced as `AuditResponse.request_summary`.
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
- `review.py` — **reviewer overlay** (pure): `ReviewAction` (accept/override), `Review`,
  `ReviewedFinding` (`effective_status`), `normalize_review` (override must specify AND
  change status; accept pins to machine verdict), `effective_report` (re-buckets by
  effective status). Persistence/HTTP build on it; the engine stays untouched.
- `db/models.py` — `Packet`, `PacketDocument`, **`PacketFinding`** (machine verdict written
  once + nullable `review_*` columns; cascade from `sessions`); migrations `0002` (packets/
  documents) + `0003_packet_findings.py`.
- `service.py` — also `AuditReviewService` (relational only, no keys): `list_audits`
  (effective counts), `get_audit` (re-open), `review_finding` (accept/override); wired by
  `build_audit_review_service()`. `AuditService.audit_packet` now persists findings too.
- `api/routes.py` — `POST /audit` (multipart, optional `checklist_id` form field →
  vertical), `GET /checklists` (available verticals + names), **`GET /audits`** (history),
  **`GET /audits/{id}`** (re-open), **`POST /audits/{id}/findings/{rid}/review`**;
  `api/schemas.py` — `GapReportSchema` (`.from_report` + `.from_reviewed`), `FindingSchema`
  (+`machine_status`, `review`), `AuditSummarySchema`, `StoredAuditSchema`, `ReviewRequest`,
  `ChecklistOption`, `AuditResponse.request_summary` (rendered by `export.render_request`).
  `build_audit_service(checklist_id=...)` selects the manifest (defaults to
  `ACTIVE_CHECKLIST`).
- `cli.py` — `ragchat audit <files...>`. `api/static/index.html` — **auditor-only SPA**
  (rebuilt from scratch 2026-08-16; no more Ask/Audit toggle) with light/dark + off-canvas
  drawer + a11y, a **vertical picker dropdown** (populated from `/checklists`, sent as
  `checklist_id`), drag-and-drop upload, **server-backed audit history** (`/audits`),
  **per-finding accept/override review** controls, and **Copy request / Download / Print**
  on the report (`@media print` hides chrome for Save-as-PDF).

Tests: `tests/unit/test_{report,checklist,gap_engine,router,audit_service,audit_api,manifest,review,audit_review,review_api}.py`;
hermetic eval `tests/eval/` (gold Customs packets, precision/recall gate at 1.0). Fake
`FakeAnalyzer`/`FakeAnalysis` live in `tests/conftest.py`. **Customs is now loaded from
`manifests/customs.yaml`, so the full green suite is the manifest-parity proof.**

## Locked decisions (do not re-open without reason)

1. **Four report states** incl. `needs_review` — never emit a false `missing`/`deficient`
   from a low-confidence read; route uncertainty to `needs_review`. Every finding carries
   confidence + source pointer.
2. **Two-layer checklist**: presence (Layer 1) then per-field/cross-doc rules (Layer 2).
   Layer 2 is the value; keep investing there. **Verticals are declarative YAML manifests**
   compiled to `Checklist` via structured check primitives — NOT a string-expression DSL,
   NOT hand-written Python per vertical. Add a vertical = add a `manifests/*.yaml` file.
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
- UI *was* an Ask/Audit **mode toggle**; as of the 2026-08-16 overhaul the web app is
  **auditor-only** and Ask is reachable only via API/CLI.

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

### UI overhaul + docs refresh (2026-08-16)

The web app began as an "Ask your PDF" chat with an Audit mode bolted on; the UI had
become a mess. Rebuilt `api/static/index.html` **from scratch** as an auditor-only SPA
(the Ask flow stays reachable via API/CLI, not the web UI). Still a dependency-free
single static file served by FastAPI (so blob download / `window.print` work — not a
claude.ai artifact). Delivered as planned Phase 1 of a phased overhaul:

- **Genuine light/dark** — system default via `@media (prefers-color-scheme)` guarded by
  `:root:not([data-theme="light"])`, plus an explicit toggle persisted to `localStorage`
  (`[data-theme="dark"]` wins both ways). Design-token palette for both.
- **Full responsiveness** — sidebar collapses to an off-canvas drawer (hamburger + scrim +
  Escape) at ≤880px.
- **Accessibility** — skip-link, ARIA roles/labels, `aria-live` status regions,
  focus-visible rings, keyboard-operable dropzone (Enter/Space), `prefers-reduced-motion`.
- Real drag-and-drop upload + file chips; vertical picker from `/checklists`; Google-only
  BYO key (`sessionStorage`, `X-Google-Api-Key`); four-bucket report with colour-coded
  findings + expandable source pointers; Copy/Download/Print (Phase-3 export) with a print
  stylesheet; recent-audits history in the sidebar (`sessionStorage`, 12 max).
- JS syntax-checked with `node --check`; served-UI integration test updated to assert the
  new "Packet Auditor" title. 141 tests green, ruff/mypy clean.

**Docs refreshed (Phase 2 of the overhaul).** `README.md` and `docs/ARCHITECTURE.md`
rewritten around the auditor (Ask demoted to a clearly-marked secondary section). README
leads with the four-state model, the "verticals are YAML not code" story, and the audit
pipeline mermaid; ARCHITECTURE gained a Part I (auditor design decisions) ahead of the
preserved Part II (Ask-flow history the auditor's DI/testing foundations still rest on).

**Next: Reviewer workflow** (Phase 3 of the pivot proper) — accept/override per finding +
server-backed audit history/re-audit. Explicitly requested to follow the UI+docs work.

### Reviewer workflow (2026-08-17) — DONE

Audits were computed and returned but never stored, so a packet couldn't be re-opened or
corrected. Added a full accept/override review layer, backend + UI + docs:

- **Pure domain** `audit/review.py` — `ReviewAction`/`Review`/`ReviewedFinding`
  (`effective_status`), `normalize_review` (override must specify AND change the status;
  accept pins to the machine verdict), `effective_report` (re-buckets by effective status).
  The engine is untouched; reviews are an overlay applied at read time.
- **Persistence** — new `packet_findings` table (machine verdict written once at audit time;
  nullable `review_*` columns hold the decision), migration `0003`, cascades from `packets`.
  `AuditService.audit_packet` now persists findings; `AuditReviewService` (relational only,
  no keys) does `list_audits`/`get_audit`/`review_finding`, all session-scoped.
- **API** — `GET /audits`, `GET /audits/{id}`, `POST /audits/{id}/findings/{rid}/review`.
  `FindingSchema` gained `machine_status` + `review`; `GapReportSchema.from_reviewed` buckets
  by effective status; stored audits re-render the missing-items request from the post-review
  report. Original machine verdict always retained for the audit trail.
- **UI** — sidebar history is now server-backed (`/audits`, not sessionStorage); clicking
  re-opens via `/audits/{id}`. Every finding has Accept / Override (status select excluding
  the machine status + optional note) posting to the review endpoint; the report re-renders
  from the effective state (overridden finding changes bucket, verdict/counts update live).
  Reviewed findings show a ✓ + Accepted/Overridden badge; history shows a reviewed count.
  Validated headless with Chromium (load → open → override → moves to Present, verdict CLEAR).
- **Tests** — `test_review.py` (pure), `test_audit_review.py` (service over SQLite),
  `test_review_api.py` (HTTP). 160 pass; ruff + mypy --strict clean.

The pivot's Phase 3 (reviewer workflow) is complete. Natural follow-ups: threshold
calibration against a larger real-document eval set; budget-metering the audit path;
weight↔quantity reconciliation; more verticals.
