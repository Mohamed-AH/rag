# Deploying ragchat (free tier)

The whole demo runs for free on **Render** (compute: web UI + API + a cleanup cron) with
**Neon** as the PostgreSQL + pgvector database. Phase 1 runs on *your* Cohere and Gemini
keys, protected by upload caps, per-session rate limits, a global daily budget, and
auto-expiring session data.

```
Browser ─► Render web service (FastAPI + built-in UI)  ─►  Neon (Postgres + pgvector)
                                                              ▲
          GitHub Actions (hourly) ─ ragchat cleanup ─────────┘  purges expired sessions
```

Render's free tier has no cron jobs, so scheduled cleanup runs as a **GitHub Actions**
workflow that connects straight to Neon (step 3).

## 1. Create the database (Neon)

1. Create a project at <https://neon.com>. pgvector is available on the free plan.
2. Enable the extension once (Neon SQL editor): `CREATE EXTENSION IF NOT EXISTS vector;`
   (The app also attempts this on first use, but doing it here avoids a first-request
   permission surprise.)
3. Copy the **pooled** connection string. It looks like:
   `postgresql://USER:PASSWORD@ep-xxx-pooler.REGION.aws.neon.tech/DB?sslmode=require`
   — the app normalizes the driver and passes `sslmode` straight through to psycopg3.

Notes: the free plan gives ~0.5 GB storage and scales to zero after 5 minutes idle (the
first request after idle takes ~1 s). Session data auto-expires, so storage stays bounded.

## 2. Deploy the app (Render)

1. Push this repo to GitHub (already done for this branch).
2. In Render, **New → Blueprint** and point it at the repo. It reads `render.yaml` and
   creates the `ragchat` web service.
3. Set these **secret** environment variables on the service (marked `sync:false`, so
   Render prompts for them — they are never committed):
   - `DATABASE_URL` — the Neon pooled string from step 1
   - `COHERE_API_KEY`
   - `GOOGLE_API_KEY`
4. Deploy. When it's live, open the service URL: the web UI is at `/`, interactive API
   docs at `/docs`, readiness at `/health`.

The free web service spins down after ~15 minutes idle and takes ~1 minute to wake; the
UI shows a "waking up" banner during that window.

## 3. Schedule cleanup (GitHub Actions)

Render's free tier has no cron, so expired sessions are reclaimed by the
`.github/workflows/cleanup.yml` workflow (hourly + a manual "Run workflow" button). It
connects directly to Neon and needs **only the database URL** — dropping expired
collections computes no embeddings, so your provider keys aren't required.

1. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository
   secret** → add `DATABASE_URL` with the same Neon pooled string.
2. That's it. The workflow runs hourly; you can also trigger it from the **Actions** tab
   (**Cleanup expired sessions → Run workflow**) to verify it.

> Prefer daily instead of hourly? Change the `cron:` line in the workflow to `0 3 * * *`.
> On a **private** repo, Actions minutes are quota-limited — daily keeps usage minimal;
> public repos get unlimited minutes.

## 4. Tuning (no redeploy needed)

All limits are environment variables you can change in the Render dashboard:

| Variable | Default | Purpose |
|---|---|---|
| `LLM_MODEL` | `gemini-flash-lite-latest` | Gemini model. Defaults to the `flash-lite-latest` alias: the lite tier has the most generous free quota and the alias survives Google's version rotation. A `404 NOT_FOUND` means the name isn't available to your key (try `gemini-flash-latest` or `gemini-2.5-flash`); a `429 RESOURCE_EXHAUSTED` with `limit: 0` means that model has no free-tier allocation for your key — switch models or enable billing. List your key's models: `curl "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY"`. |
| `SESSION_TTL_HOURS` | `24` | How long uploaded data is retained |
| `RATE_LIMIT_ASKS_PER_MINUTE` | `10` | Per-session ask limit, kept under the free-tier RPM |
| `MAX_UPLOAD_BYTES` | `2097152` | Max upload size (2 MiB) |
| `MAX_SECTIONS_PER_UPLOAD` | `150` | Max chunks per upload |
| `RATE_LIMIT_INGESTS_PER_HOUR` | `20` | Per-session upload burst limit |
| `DAILY_FREE_ALLOWANCE` | `10` | Free shared-key asks/day **per user** before they're prompted for their own keys (0 = unlimited) |
| `DAILY_REQUEST_BUDGET` | `1000` | Instance-wide shared-key asks/day — the absolute cost ceiling (0 = off) |
| `USAGE_HASH_SALT` | `change-me-in-prod` | Secret salt for hashing client IPs in usage counters; set a real value |
| `TRUSTED_PROXY_HOPS` | `1` | Reverse-proxy hops in front of the app (Render = 1). The real client IP is read that many entries from the right of `X-Forwarded-For`, so client-prepended values can't spoof a fresh allowance. |

## Usage limits & bring-your-own-keys

Each visitor gets `DAILY_FREE_ALLOWANCE` shared-key questions per day (counted per
salted-hashed IP + cookie, stored durably in Postgres so limits survive restarts). When
that's used up, the UI prompts for the visitor's **own** Cohere + Gemini keys, which are
sent per request (headers `X-Cohere-Api-Key` / `X-Google-Api-Key`) and **never stored or
logged**; BYO requests bypass the shared-key limits. `DAILY_REQUEST_BUDGET` is the
instance-wide backstop that guarantees total shared-key usage stays within the provider
free tier no matter what.

## Caveats (by design, for a free demo)

- **Cold starts** on both Render and Neon after idle — expected; surfaced in the UI.
- **Daily limits are durable** (Postgres) so they survive restarts; the short *burst*
  limiter is in-memory (harmless to reset). A multi-instance deployment would move the
  burst limiter to Redis.
- **Best-effort identity.** Client IPs are read from the right of `X-Forwarded-For`
  (`TRUSTED_PROXY_HOPS`), so prepended values can't spoof a new allowance; but without
  accounts the allowance can still be evaded (fresh cookies, IP rotation), which is why
  `DAILY_REQUEST_BUDGET` is the real cost ceiling. Real auth is the next step for stronger
  per-user limits.
- **BYO keys are never logged or stored.** They're read from request headers only and
  passed straight to the provider clients; the app logs no request headers, and configured
  keys are `SecretStr` (masked in reprs).
- **Schema is managed by Alembic**, applied automatically on startup: a fresh (or legacy
  `create_all`) database is created/adopted and stamped; later migrations upgrade in place.
  To author a change after editing the models: `alembic revision --autogenerate -m "..."`,
  review the generated file in `src/ragchat/migrations/versions/`, and commit it — the app
  applies it on the next deploy. No manual step is needed on your existing database.
