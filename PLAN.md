# Pivot Plan: from RAG Q&A to a Customs Pre-Clearance Packet Auditor

**Status:** approved direction, pre-implementation. No application code has changed yet.
**Branch:** `claude/rag-chat-pivot-plan-uufnkm`

---

## 1. The reframe

`ragchat` today is a *retrieve-and-answer* service: upload documents → chunk → embed →
ask a question → get a grounded answer. The unit of work is **one question**; the output
is **prose**.

We are pivoting to a *extract-and-verify* service — a **Packet Auditor**. The unit of
work becomes **one submission packet** (several files reviewed together); the output
becomes a structured **Gap Report** that says, on day one, what is present, what is
missing, what is deficient, and what a human needs to check.

The engine is domain-agnostic. Every vertical (immigration, insurance, tax, veterans,
customs) is the *same* engine with a *different checklist*. The first checklist we ship
is **Customs Pre-Clearance (#1)** — chosen because it carries **no consumer PII** (only
corporate trade data), has the **shortest and most stable checklist**, and is the **most
tabular** domain (the best proving ground for layout-aware extraction).

### What stays the same
The whole non-AI backbone is reused unchanged: FastAPI/Typer scaffold, per-session
tenant isolation, TTL cleanup, upload size/rate guards, daily budget metering,
bring-your-own-keys, and the fakes-based hermetic test strategy.

### What is new
1. **Checklist-as-data** — the requirements rubric for a document set.
2. **Intake router** — digital-PDF text path vs. scanned-image multimodal path.
3. **Document classification** — "which of the required doc types is this file?"
4. **Field extraction** — pull specific values (HTS code, totals, weights, addresses).
5. **Gap-analysis engine** — checklist × extracted evidence → findings.
6. **Gap Report** — the new structured output, replacing the prose answer as the primary
   deliverable. Chat survives as a secondary "ask about this packet" feature.

---

## 2. Core design decisions (locked)

### 2.1 Report shape — four states, not three
```
GapReport {
  present:      Finding[]   # requirement satisfied
  missing:      Finding[]   # required document/field absent
  deficient:    Finding[]   # present but fails a rule (e.g. HTS code malformed)
  needs_review: Finding[]   # low-confidence extraction / classification — human triage
}
```
`needs_review` is the safety valve. Ops managers reject tools that lie with confidence;
when the model can't classify a file or read a field confidently, the honest output is
"look here," **not** a false "missing." Every `Finding` carries a **confidence score** and
a **source pointer** (document id + page + text snippet or image crop) so a reviewer
verifies in one glance.

### 2.2 Two-layer checklist
- **Layer 1 — presence:** which document *types* must exist in the packet.
- **Layer 2 — field rules:** per-type extracted fields + cross-document rules. This is
  where the ROI lives (e.g. invoice line-item total must match packing-list total; net
  weight consistent across manifest and packing list). Layer 1 alone is a glorified file
  checklist; Layer 2 is the product.

### 2.3 Intake router (do not rip out `extractors.py` — wrap it)
```
for each file / page:
  if PDF has a usable text layer (pypdf returns clean text)  -> text path  (current code, ~free)
  else (scanned / image / low-text)                          -> Gemini multimodal path
```
For the multimodal path, "parse the document" and "extract the fields" **collapse into a
single call**: hand the page image + the extraction schema to Gemini and get structured
JSON back. No separate OCR step, no new vendor. A dedicated layout parser
(Azure Document Intelligence / LlamaParse) is an *upgrade bought only if measured table
fidelity is insufficient* — and it would widen the data-processing surface, which cuts
against the low-PII rationale for choosing Customs.

### 2.4 pgvector is demoted, not deleted
Customs packets fit comfortably in Gemini's context window, so the **core audit uses
direct structured LLM calls (Pydantic schemas), no retrieval.** pgvector + the LCEL chain
are reserved for (a) the secondary "ask about this packet" chat and (b) future
large-record verticals (e.g. Veterans' 500-page service records) where evidence exceeds
the context window. The existing `rag/` module survives intact behind that secondary path.

### 2.5 Eval fixture is the definition of done
Before any live demo: **10–20 gold-standard packets** — some deliberately complete, some
with known, labelled gaps — with expected `GapReport` output. Precision/recall on gap
findings is measured against these. This plugs into the existing fakes-based test
strategy and becomes the CI gate that stops a prompt tweak from silently regressing.

---

## 3. Customs domain (v1 checklist)

**Core document types**
- Commercial Invoice
- Packing List
- Bill of Lading / Air Waybill
- Certificate of Origin

**Layer-1 presence rules:** each of the four types must be present in the packet.

**Layer-2 field & cross-document rules (initial set):**
- HTS/HS code present and well-formed on the Commercial Invoice.
- Country-of-origin declared and consistent between Commercial Invoice and Certificate of
  Origin.
- Currency + declared value present on the Commercial Invoice; total matches the Packing
  List total (within tolerance).
- Net/gross weight and total unit/carton count consistent across Packing List and
  Bill of Lading.
- Exporter (shipper) and consignee names/addresses aligned across Commercial Invoice,
  Packing List, and Bill of Lading.

Rules are **data**, versioned alongside the checklist, so adding the next vertical later
means adding a checklist file — not changing engine code.

---

## 4. Target pipeline

```
upload packet (N files)
      |
      v
[intake router]  text-path  ─┐
                 multimodal ─┤→  per-file raw text / page images
      |                       │
      v                       │
[classifier]  ── each file → one of the required doc types (+ confidence)
      |
      v
[extractor]  ── per doc type, structured JSON of required fields (+ per-field confidence + source pointer)
      |
      v
[gap engine]  ── evaluate checklist (Layer 1 presence + Layer 2 rules) over extracted evidence
      |
      v
GapReport { present, missing, deficient, needs_review }   ──►  reviewer UI / JSON / export
      │
      └─ (secondary) pgvector index of the packet  ──►  "ask about this packet" chat (reuses rag/ + LCEL)
```

---

## 5. Phase 0 — reframe the domain model (file-by-file)

Goal: introduce the Packet Auditor data model and the `GapReport` output *shape* with a
single hard-coded Customs checklist, reusing the existing intake and session machinery.
No new AI capability yet — the classifier/extractor can be stubbed so the pipeline is
wired end-to-end and testable.

| Module | File | Change |
|---|---|---|
| **New: domain checklist** | `src/ragchat/audit/checklist.py` *(new)* | `DocType`, `Requirement`, `FieldRule`, `Checklist` dataclasses; `CUSTOMS_CHECKLIST` constant encoding §3. Pure data + pure predicate functions — trivially unit-testable. |
| **New: findings model** | `src/ragchat/audit/report.py` *(new)* | `Finding` (status, requirement id, confidence, `SourcePointer{doc_id, page, snippet}`) and `GapReport` (the four buckets) as frozen dataclasses. |
| **New: gap engine** | `src/ragchat/audit/engine.py` *(new)* | `evaluate(checklist, classified_docs, extracted_fields) -> GapReport`. Pure function over already-extracted evidence, so it is tested with fixtures and no LLM. |
| **New: package init** | `src/ragchat/audit/__init__.py` *(new)* | Public exports. |
| **DB models** | `src/ragchat/db/models.py` | Add `Packet` (belongs to a `Session`) and `PacketDocument` (one uploaded file: filename, detected `doc_type`, confidence, extracted-fields JSON, raw text). Keep `KnowledgeBase`/`UsageCounter` untouched — the secondary chat still uses `KnowledgeBase`. |
| **Migration** | `src/ragchat/migrations/versions/0002_packet_auditor_schema.py` *(new)* | Alembic revision after `0001_initial_schema.py` creating `packets` and `packet_documents` (both `ON DELETE CASCADE` from `sessions`, matching the existing tenancy invariant). |
| **Repository** | `src/ragchat/db/repository.py` | Add session-scoped helpers: `create_packet`, `add_packet_document`, `get_packet`, `list_packet_documents`, `delete_packet`. Same "never issue an unscoped query" invariant as the existing section helpers. |
| **Response schemas** | `src/ragchat/api/schemas.py` | Add `FindingSchema`, `SourcePointerSchema`, `GapReportSchema`, `AuditResponse`. Leave `Ask*` schemas for the secondary chat. |
| **Config** | `src/ragchat/config.py` | Add `max_files_per_packet`, `active_checklist` (default `"customs"`), and a `vision_model` setting (default a Gemini multimodal alias) — all env-tunable like the existing limits. |
| **Errors** | `src/ragchat/errors.py` | Add `TooManyFilesError`, `UnclassifiableDocumentError`. |
| **Tests** | `tests/unit/test_checklist.py`, `tests/unit/test_gap_engine.py`, `tests/unit/test_report.py` *(new)* | Drive the pure engine with hand-built classified/extracted fixtures → assert bucket placement. No AI, no DB. |

**Phase 0 done when:** a `GapReport` can be produced from *hand-supplied* extracted
evidence for a Customs packet, fully unit-tested, with the DB tables migrated in.

---

## 6. Phase 1 — the thin demo (file-by-file)

Goal: real files in → real Gap Report out. Wire the intake router, classifier, and
extractor to Gemini, expose an audit endpoint, and stand up the eval fixture. This is the
demoable, sellable milestone.

| Module | File | Change |
|---|---|---|
| **Intake router** | `src/ragchat/ingestion/router.py` *(new)* | `route(filename, data) -> list[PageInput]` deciding text-path vs. multimodal-path per §2.3. Text path delegates to existing `extractors.py`; image path yields page images for the multimodal extractor. |
| **Reuse** | `src/ragchat/ingestion/extractors.py` | Unchanged behaviour; now called *by* the router rather than directly by the service. Text-layer detection (is `pypdf` output usable?) lives in the router. |
| **Classifier** | `src/ragchat/audit/classifier.py` *(new)* | `classify(page_inputs, checklist) -> ClassifiedDoc(doc_type, confidence)` via a structured Gemini call. Below a confidence threshold → routes the file to `needs_review`. |
| **Extractor** | `src/ragchat/audit/extractor.py` *(new)* | Per `doc_type`, a Pydantic output schema; one structured Gemini call (text or multimodal) returns fields + per-field confidence + source pointer. **No pgvector.** |
| **LLM plumbing** | `src/ragchat/rag/llm.py` | Add a structured-output / multimodal helper (`with_structured_output` + image parts) reusing existing key handling and BYO-keys. Keep the chat `build_llm` path intact. |
| **Service** | `src/ragchat/service.py` | Add `audit_packet(files) -> GapReport`: router → classifier → extractor → `engine.evaluate`, persisting `Packet`/`PacketDocument` rows and (optionally) indexing the packet into pgvector for the secondary chat. Mirrors the transactional discipline already in `ingest_sections`. Enforce `max_files_per_packet` before any model call (cost guard, same posture as the existing caps). |
| **API route** | `src/ragchat/api/routes.py` | Add `POST /audit` (multipart, N files) → `AuditResponse`; guarded by the existing burst/budget guards. Keep `/ask` and `/ingest/file` for the secondary chat. |
| **Guards** | `src/ragchat/api/guards.py` | Reuse limiters; add a per-packet-file cap check. Vision/extraction calls count against the daily budget like asks do. |
| **CLI** | `src/ragchat/cli.py` | Add `ragchat audit <files...>` printing the Gap Report — the fastest demo/eval surface. |
| **Web UI** | `src/ragchat/api/static/index.html` | Add a "Audit a packet" panel: multi-file upload → render the four buckets, each finding expandable to its source pointer (page + snippet). Existing chat becomes a second tab. |
| **Eval fixture** | `tests/eval/packets/` + `tests/eval/test_customs_eval.py` *(new)* | 10–20 labelled Customs packets (complete + known-gap) with expected reports; a precision/recall harness over gap findings. Runs with a **recorded/faked model** in CI (hermetic) and can be pointed at the live model locally for calibration. |
| **Provider fakes** | `tests/conftest.py` | Extend the existing fakes with a deterministic fake classifier/extractor so `/audit` is testable with no keys and no network — preserving the "tests need no API keys" property. |

**Phase 1 done when:** dropping a real Customs packet into the CLI or web UI produces a
correct four-bucket Gap Report with source pointers, and the eval harness reports
precision/recall on the labelled fixtures in CI.

---

## 7. Later phases (not scoped here)

- **Phase 2 — deeper field rules & calibration:** expand Layer-2 cross-document rules,
  tune confidence thresholds against the eval set, add tolerances.
- **Phase 3 — reviewer workflow & export:** accept/override on findings, a PDF/email
  "missing-items request" export (the day-1 artifact the sales pitch promises), and a
  second checklist (e.g. immigration or a second trade lane).
- **Phase 4 — regulated verticals:** revisit insurance/veterans with the proven engine;
  this is where pgvector retrieval and PHI/PII handling re-enter.

---

## 8. Explicit non-goals for v1

- **No authenticity / fraud detection.** v1 checks *completeness and consistency*, never
  "is this document forged." That is a different product with a very different liability
  profile.
- **No new document-processing vendor** unless measured table fidelity forces it.
- **No removal of the existing chat.** It is demoted to a secondary feature, not deleted.
</content>
</invoke>
