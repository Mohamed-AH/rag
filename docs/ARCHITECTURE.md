# Architecture & Design Decisions

This document explains *why* the system is built the way it is. The project began as a
retrieval-augmented Q&A service and was **pivoted into a Packet Auditor**: ingest a packet
of documents, check it against a checklist, return a structured Gap Report. The auditor is
the primary product; the Q&A ("Ask") flow survives as a secondary feature. This document
covers the auditor first, then records the design history of the Ask flow that still
underpins it.

---

## Part I — The Packet Auditor

### The shape of the problem

Auditing is **extract-and-verify against a rubric**, not retrieve-and-answer. That single
distinction drives every decision below: there is no similarity search on the audit path,
the output is a typed report rather than prose, and correctness is measured by
precision/recall against gold packets rather than answer quality.

### 1. Four report states — never a confident-sounding wrong answer

`FindingStatus` is `present | missing | deficient | needs_review`. The fourth state is the
important one: a low-confidence read must **never** be emitted as a false `missing` or
`deficient`. Every `Finding` carries a confidence score and a `SourcePointer`
(`doc_id`, page, snippet), so a reviewer can always trace a verdict back to the pixels it
came from. The `GapReport`'s four buckets are *derived views* over a single flat list of
findings, so there is exactly one source of truth.

### 2. Two-layer checklist — presence, then rules

A checklist separates **Layer 1** (which document types must be present) from **Layer 2**
(per-field and cross-document rules). Layer 2 is where the value is, and Layer 2 rules are
**gated** on their documents being confidently present — so a rule never fires against a
document the packet doesn't actually contain, which would manufacture false `deficient`s.

### 3. Verticals are declarative YAML, compiled to a checklist — not code, not a DSL

**Decision:** A vertical is a `manifests/*.yaml` file. It is validated by Pydantic
(discriminated union on rule `type`) and compiled to a `Checklist` by `compile_manifest`.

**Why:** The alternatives were hand-written Python per vertical (doesn't scale, every
vertical can break the engine) or a string-expression DSL (an interpreter to write, debug,
and secure). Instead, Layer-2 rules are composed from a **fixed vocabulary of check
primitives** in `checks.py` — `regex_match`, `numeric_match`, `cross_match` (text/entity),
`date_valid`, `numeric_threshold`, `quantity_matches` — each a parameterized, individually
tested function returning a `RuleCheck`. The manifest only *selects and parameterizes*
primitives; it can't express arbitrary logic, so a malformed manifest fails validation at
load rather than misbehaving at runtime.

**Payoff:** Adding a vertical is dropping in a YAML file — **zero engine/Python changes**.
Customs itself is loaded from `manifests/customs.yaml`, so the green test suite (including
the precision/recall eval) doubles as proof that the manifest path compiles to exactly the
intended checklist.

**Trade-off:** A genuinely novel rule shape needs a new primitive (Python) before a
manifest can use it — a deliberate seam. The primitive vocabulary grows slowly and on
evidence (e.g. `quantity_matches` was added only after a real under-declaration case).

### 4. No pgvector on the audit path

**Decision:** The auditor makes **direct structured Gemini calls**; there is no embedding,
no vector store, no retrieval on this path.

**Why:** The documents to check are *given* in the request — there is nothing to retrieve.
Chunking a commercial invoice and doing similarity search would only lose the structure
(which field is on which page) that the audit depends on. pgvector stays for the secondary
Ask flow (Part II) and remains available for future large-record verticals.

### 5. Single-pass analyzer that returns a *list* of documents

**Decision:** `Analyzer` is a protocol; `GeminiAnalyzer.analyze(file)` does classification
**and** extraction in one structured call per file and returns a **list** of
`ClassifiedDocument`.

**Why (one pass):** Two calls per file (classify, then extract) doubled cost and latency
and burned scarce free-tier quota. One structured call does both.

**Why (a list):** A real packet is often a single combined multi-page PDF containing every
document, in unpredictable page order, with individual documents spanning non-contiguous
pages. Assuming one file = one document silently dropped the rest. The analyzer's whole-
file view lets it identify *every* document, group pages by shared identifiers
(invoice/BoL/container numbers) rather than by order, and return one `ClassifiedDocument`
per detected document (each with its `pages` list) — still one model call per file.

### 6. A provider fallback ladder, not a single vendor

**Decision:** The analyzer is given an **ordered list** of chat models (`select_models`),
not one. It tries the primary (Gemini); on a quota/rate/transient error it retries the
*same* structured call on the next model. `rag/providers.py` builds the ladder from
`AUDIT_MODEL_ORDER`, including a provider only when its key is configured (so the app ships
working on Gemini alone), with a separate multimodal ladder for the scan path.

**Why:** A free-tier key is a single point of failure — one busy day exhausts it and every
audit 429s. Because the analyzer already consumed any LangChain chat model via
`.with_structured_output(...).invoke(...)`, a fallback needed no engine, schema, or analyzer-
logic change — just: return a list, and loop. The fallbacks (Mistral Ministral-3B, a hybrid
multimodal model; Groq gpt-oss-120b for text + Qwen-VL for scans; or any OpenAI-compatible
endpoint such as OpenRouter → Qwen2.5-VL) are all free-tier, so resilience costs nothing. A
provider's `multimodal` flag controls whether it also joins the scan ladder, so a text-only
rung would simply be skipped there.

**Design choices worth noting:** the fail-over *trigger* is a provider-agnostic predicate on
the exception text (quota/rate/transient), so no provider SDK exception class needs importing
and a genuine error still surfaces instead of silently burning every rung; a bring-your-own
Google key builds a Gemini-only ladder (a BYO user must never spend the operator's other
keys); and every provider integration is imported **lazily**, so the app and the hermetic
test suite never require the fallback SDKs to be installed. Model ids and order are env-
tunable because free model names churn.

### 7. Intake router wraps, not replaces, the existing extractors

`ingestion/router.py` picks the **text path** (wrapping the existing `extractors.py` for
files with a real text layer) or the **multimodal path** (scanned PDFs and images, handed
to Gemini vision). It makes no model calls itself and is fully unit-tested. Because
Flash-Lite is natively multimodal, per-path model *routing* (a `select_llm(content)`
callable) lets text-path docs and scans share one generous lite-tier quota, while leaving
the door open to point scans at a heavier OCR model if real documents ever demand it.

### 8. The engine is pure; everything model-facing is injected

`engine.evaluate(checklist, evidence)` is a pure function — no I/O, no clock, no network —
so it is exhaustively and instantly testable. The one component that must call a model
(the analyzer) sits behind a protocol and is injected, so the **entire pipeline is
hermetically testable** with a `FakeAnalyzer`: the CI suite needs no keys and no network.

### 9. Reviewer overlay: the machine never has the last word

**Decision:** An audit's Gap Report is **persisted** (table `packet_findings`), and a human
reviewer can **accept** a finding (confirm the machine verdict) or **override** it (set a
different status, with a note). The reviewer decision lives in nullable `review_*` columns
*beside* the machine verdict, which is written once and never mutated.

**Why (persist findings):** The original design computed the report and returned it without
storing it, so a packet could not be re-opened or reviewed. Persisting the findings is what
turns a one-shot audit into a durable, revisitable record with a server-backed history.

**Why (an overlay, not a mutation):** Keeping the machine verdict and the human decision as
separate columns preserves the audit trail — you can always see what the model said *and*
what the reviewer decided. The *effective* status (override if overridden, else machine) is
computed by the pure `audit/review.py` module (`ReviewedFinding.effective_status`,
`effective_report`), which re-buckets a report by effective status so `is_clear`, the four
buckets, and the re-rendered missing-items request all reflect human decisions. Validation
lives there too: an override must specify a status *and change it* (accepting is how you
confirm the machine), so every stored decision is meaningful.

**Testability:** `AuditReviewService` is relational-only — no model calls, no keys — so the
whole workflow (list history, re-open, accept/override, session scoping) is tested against
SQLite with no network, same as the rest of the suite.

### 10. v1 scope: completeness & consistency only

The engine checks that a packet is *complete* and *internally consistent*. It does **not**
attempt authenticity or fraud detection (is this invoice forged?) in v1. Cross-document
consistency rules do, as a side effect, catch some fraud patterns — `quantity_matches`
surfaced a real under-declaration where the invoice said 1,000 units and the packing list
said 2,000 — but that is consistency-checking, not authenticity verification.

### Audit module map

Pure domain package `src/ragchat/audit/` (a clean DAG, no I/O):

| Module | Responsibility |
|--------|----------------|
| `report.py` | `FindingStatus`, `SourcePointer`, `Finding`, `GapReport` (buckets are derived views) |
| `evidence.py` | Engine input contract: `ExtractedField`, `ClassifiedDocument`, `PacketEvidence`, `RuleContext`, `RuleResult` |
| `checklist.py` | Types only: `DocType`/`FieldSpec`, `DocumentRequirement` (L1), `FieldRule` (L2), `Checklist` |
| `checks.py` | Reusable check primitives + shared helpers (`as_float`, `_same_entity`, date parsing) |
| `manifest.py` | Pydantic manifest schema + `compile_manifest` + registry (`get_checklist`, `available_checklists`) |
| `../manifests/*.yaml` | Verticals as declarative YAML |
| `engine.py` | `evaluate(checklist, evidence)` — pure; L1 presence + gated L2 rules |
| `export.py` | `render_request(report, checklist_name)` — client-ready missing-items email from any report |
| `review.py` | Reviewer overlay: `ReviewAction`, `Review`, `ReviewedFinding` (effective status), `effective_report` |
| `analyzer.py` | `Analyzer` protocol + `StructuredAnalyzer` (single-pass classify+extract → list of documents; retries the ladder on quota/transient errors) |
| `../rag/providers.py` | Builds the audit fallback ladder (`build_audit_ladder`) from `AUDIT_MODEL_ORDER`; providers imported lazily |

Pipeline & surfaces: `ingestion/router.py` (path selection) → `analyzer` → `engine`;
`service.py` (`AuditService.audit_packet` composes them, enforces the file cap before any
model call, persists the packet *and its findings* atomically; `AuditReviewService` reads
history and records review decisions); `api/routes.py` (`POST /audit`, `GET /checklists`,
`GET /audits`, `GET /audits/{id}`, `POST /audits/{id}/findings/{rid}/review`); `cli.py`
(`ragchat audit`); `api/static/index.html` (the web app, with server-backed history and
per-finding accept/override).

---

## Part II — The Ask flow (secondary) and shared foundations

The retrieval-augmented Q&A service the project grew from still runs as the "Ask" feature.
Its original design decisions remain in force and several are the foundation the auditor
builds on (dependency injection, typed config, two-interfaces-over-one-service, hermetic
tests). They are recorded here.

### Starting point

The original POC was two scripts: one parsed a markdown file into PostgreSQL, the other
embedded it with Cohere, pushed vectors to **Pinecone**, and ran a `RetrievalQA` chain
against **Gemini** through a CLI `input()` loop. It worked, but had no tests, no
validation, minimal error handling, global import-time side effects, and required a
Pinecone account to run at all.

### 1. Consolidate to a single datastore (pgvector)

Dropped Pinecone; vectors live in PostgreSQL via `pgvector`, alongside the relational
source content. One stateful system instead of two, no external SaaS dependency, no
"the two stores disagree" bugs, and the whole system runs offline with one
`docker compose up` — which is what makes hermetic testing possible. The `VectorStore`
seam keeps the door open to swap back if scale ever demands it.

### 2. Relational table as source of truth; vectors are derived

`knowledge_base` holds the canonical text; the pgvector collection is a derived index that
can always be rebuilt. Ingestion writes the relational rows but does **not commit** until
the vector index rebuilds successfully, so the two stores never drift.

### 3. Dependency injection everywhere → keyless, hermetic tests

Services receive their session factory, vector store, RAG chain (and, for the auditor, the
analyzer) as constructor arguments; FastAPI routes resolve them through `Depends`
providers that tests override. Provider SDKs (Cohere, Gemini, pgvector) are imported
*lazily* inside their factories, so importing the app never requires them. The payoff is
the [testing strategy](#testing-strategy): the entire suite runs with no keys and no live
database.

### 4. Modern LangChain (LCEL) instead of `RetrievalQA`

The Ask pipeline is an explicit LCEL graph
(`retrieve → format context → grounded prompt → Gemini → parse`) that returns the answer
*and* its sources — transparent, composable, and trivially driven by fakes in tests.

### 5. Typed configuration, validated at startup

`pydantic-settings` centralises configuration; missing/malformed values fail fast. Driver
normalisation rewrites a plain `postgresql://` URL to the `postgresql+psycopg://` form;
the embedding-dimension lock rejects a model of the wrong dimension up front rather than
corrupting the index.

### 6. Two interfaces over one service

The FastAPI app and the Typer CLI are thin adapters over the same services — no business
logic in a route handler or CLI command, so both interfaces behave identically. `/health`
is a real readiness probe (`SELECT 1` → **503** when the DB is down). The API uses the
modern `lifespan` context manager to build shared resources once and dispose the engine on
shutdown.

## Testing strategy

| Scope | What it covers | How it stays hermetic |
|-------|----------------|-----------------------|
| Unit | Gap engine, report/checklist types, manifest compilation, intake router, audit service, export, review overlay + workflow, parser, config | Pure functions; `FakeAnalyzer`; SQLite in-memory for the DB |
| Eval | Customs precision/recall against gold packets (gated at 1.0) | Fake analyzer feeds engineered `PacketEvidence`; pure engine |
| Integration | Full HTTP request → routing → validation → serialization | Real FastAPI app; services overridden with fakes |

- **The gap engine and every check primitive are pure** and directly unit-tested.
- **The Gemini analyzer, embeddings, LLM, and pgvector** are replaced with deterministic
  fakes, keeping tests free of network, secrets, and flakiness.
- The **eval** derives its gold packets from real live-test observations (quantity
  mismatch, units-in-weight, party-granularity cases), so it calibrates the engine against
  problems seen in production rather than invented ones.

Schema is managed by **Alembic** with an auto-bootstrap in `init_db()`. Migration `0002`
adds the `packets` / `packet_documents` tables and `0003` adds `packet_findings` (all
cascade from `sessions`).

## Possible next steps

- Confidence-threshold calibration against a larger real-document eval set.
- A shared multi-instance store (Redis) for the rate-limit counters if the app scales past
  one worker (the burst window is currently in-memory per instance).
- Weight↔quantity reconciliation (a derived cross-metric rule) and more verticals.
- Streaming token responses from `/ask`; pgvector HNSW tuning if the Ask corpus grows.
