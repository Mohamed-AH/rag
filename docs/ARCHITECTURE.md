# Architecture & Design Decisions

This document explains *why* the system is built the way it is. It starts from a working
proof-of-concept and records the engineering decisions made to turn it into a
maintainable, testable service.

## Starting point

The original POC was two scripts: one parsed a markdown file into PostgreSQL, the other
loaded that content, embedded it with Cohere, pushed the vectors to **Pinecone**, and ran
a `RetrievalQA` chain against **Gemini** through a CLI `input()` loop. It worked, but it
had no tests, no input validation, minimal error handling, global side effects at import
time, and it required a Pinecone account to run at all.

## Key decisions

### 1. Consolidate to a single datastore (pgvector)

**Decision:** Drop Pinecone; store vectors in PostgreSQL via the `pgvector` extension,
alongside the relational source content.

**Why:** The POC ran two stateful systems for one job. Since the content already lived in
PostgreSQL, moving vectors there too removes an external SaaS dependency, its cost, and a
class of "the two stores disagree" bugs. It also makes the whole system runnable offline
with one `docker compose up`, which is what makes hermetic testing and easy evaluation
possible.

**Trade-off:** pgvector at very large scale needs index tuning (IVFFlat/HNSW) that a
dedicated vector DB gives you out of the box. For a knowledge base of this size, a single
well-understood datastore is the right call; the `VectorStore` seam (see below) keeps the
door open to swap back if scale ever demands it.

### 2. Relational table as the source of truth; vectors are derived

`knowledge_base` (SQLAlchemy) holds the canonical text. The pgvector collection is a
*derived* index that can always be rebuilt from the table. This separation means
re-embedding (e.g. after changing models) is a rebuild, not a migration.

**Consistency without distributed transactions.** Ingestion writes the relational rows
but does **not commit** until the vector index has been rebuilt successfully. If embedding
fails, the relational write is rolled back and any partial vectors are cleared
(`RAGService.ingest_sections`). The two stores therefore never drift into a state where
rows exist without matching embeddings.

### 3. Dependency injection everywhere → keyless, hermetic tests

`RAGService` receives its session factory, vector store, and RAG chain as constructor
arguments; the FastAPI route resolves the service through a `Depends` provider that tests
override. Nothing reaches for a global client.

The payoff is the [testing strategy](#testing-strategy): the entire suite runs with **no
API keys and no live database**. Provider SDKs (Cohere, Gemini, pgvector) are imported
*lazily* inside their factories, so importing the app never requires them to be installed
or configured.

### 4. Modern LangChain (LCEL) instead of `RetrievalQA`

`RetrievalQA` is deprecated. The pipeline is an explicit LCEL graph
(`retrieve → format context → grounded prompt → Gemini → parse`) that returns the answer
*and* its source documents. It's transparent, composable, and — because it accepts any
retriever and any chat model — trivially driven by fakes in tests.

### 5. Typed configuration, validated at startup

`pydantic-settings` centralises all configuration. Missing or malformed values fail fast
with a clear message instead of surfacing deep inside a request. Two subtleties are
handled here rather than left as runtime landmines:

- **Driver normalisation.** Users set a plain `postgresql://` URL; `Settings.sqlalchemy_url`
  rewrites it to the `postgresql+psycopg://` (psycopg3) form that both SQLAlchemy and
  `langchain-postgres` require, so a driver mismatch can't happen.
- **Embedding-dimension lock.** The vector column dimension is pinned from config and
  cross-checked against the chosen model (`validate_embedding_dimension`), so switching to
  a model of a different dimension is rejected up front rather than corrupting the index.

### 6. Two interfaces over one service

The FastAPI app and the Typer CLI are thin adapters over the same `RAGService`. No business
logic lives in a route handler or a CLI command, so both interfaces behave identically and
neither is a place bugs can hide.

- `/health` is a real readiness probe (`SELECT 1`) returning **503** when the database is
  down — the pattern container orchestrators expect — not a static `{"ok": true}`.
- The API uses the modern `lifespan` context manager (not deprecated `on_event` hooks) to
  build shared resources once and dispose the DB engine on shutdown.

## Testing strategy

| Scope | What it covers | How it stays hermetic |
|-------|----------------|-----------------------|
| Unit | Parser, config/URL normalisation, dimension validation, service orchestration | Pure functions; SQLite in-memory for the DB |
| Integration | Full HTTP request → routing → validation → serialization | Real FastAPI app; service dependency overridden with fakes |

- **Relational layer** is tested for real against SQLite in-memory, so models and the
  repository are genuinely exercised.
- **Embeddings / LLM / pgvector** are replaced with deterministic fakes
  (`FakeListChatModel`, a fake retriever, a fake vector store), keeping tests fast and
  free of network, secrets, and flakiness.
- An **opt-in** integration test against a live pgvector container can be added behind the
  `integration` pytest marker — deselected by default and run in a dedicated CI job that
  stands up a `pgvector` service container (a real ingest → similarity search → purge).

Schema is managed by **Alembic** with an auto-bootstrap in `init_db()`: a fresh or legacy
`create_all` database is created/adopted and stamped at head; subsequent migrations upgrade
in place. New revisions are authored with `alembic revision --autogenerate`.

## Possible next steps

- Real authentication (accounts / magic-link) for hard per-user limits beyond the
  best-effort hashed-IP + cookie allowance.
- Streaming token responses from the `/ask` endpoint.
- A retrieval-quality evaluation harness (golden Q&A set) wired into CI.
- pgvector HNSW index tuning if the corpus grows by orders of magnitude.
