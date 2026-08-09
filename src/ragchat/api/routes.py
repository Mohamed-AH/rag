"""HTTP routes, session resolution, and dependency providers."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from ragchat.api.guards import Guards
from ragchat.api.schemas import (
    AskRequest,
    AskResponse,
    HealthResponse,
    IngestResponse,
    SourceSchema,
)
from ragchat.db import repository
from ragchat.errors import (
    EmptyDocumentError,
    FileTooLargeError,
    TooManySectionsError,
    UnsupportedFileTypeError,
)
from ragchat.service import RAGService, build_session_service

logger = logging.getLogger(__name__)

router = APIRouter()

SESSION_COOKIE = "sid"
_SESSION_TTL_SECONDS = 24 * 60 * 60
# Session ids become part of a pgvector collection name, so only accept the exact
# shape we mint (a uuid4 hex). Anything else is replaced with a fresh id.
_VALID_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")

# Bring-your-own-keys are sent per request in these headers and never stored server-side.
COHERE_KEY_HEADER = "X-Cohere-Api-Key"
GOOGLE_KEY_HEADER = "X-Google-Api-Key"


def get_session_id(request: Request, response: Response) -> str:
    """Resolve the caller's session id from a cookie, minting one if absent/invalid."""
    sid = request.cookies.get(SESSION_COOKIE)
    if sid is None or not _VALID_SESSION_ID.match(sid):
        sid = uuid4().hex
        response.set_cookie(
            SESSION_COOKIE,
            sid,
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
        )
    return sid


def _byo_keys(request: Request) -> tuple[str | None, str | None]:
    """Extract bring-your-own Cohere/Google keys from request headers, if both present."""
    cohere = (request.headers.get(COHERE_KEY_HEADER) or "").strip()
    google = (request.headers.get(GOOGLE_KEY_HEADER) or "").strip()
    if cohere and google:
        return cohere, google
    return None, None


def get_service(request: Request, session_id: str = Depends(get_session_id)) -> RAGService:
    """Build a session-scoped service, honoring per-request BYO keys if supplied.

    Overridden in tests to inject a fake.
    """
    cohere_key, google_key = _byo_keys(request)
    return build_session_service(session_id, cohere_key=cohere_key, google_key=google_key)


def get_db_session_factory() -> Callable[[], DbSession]:
    """Provide the DB session factory used by the readiness probe and usage metering.

    Kept separate from :func:`get_service` so neither readiness nor metering depends on
    the model layer. Overridden in tests to point at SQLite.
    """
    from ragchat.db.engine import get_session_factory

    return get_session_factory()


def _get_guards(request: Request) -> Guards:
    """Return the instance's guardrails, building them from settings on first use."""
    guards: Guards | None = getattr(request.app.state, "guards", None)
    if guards is None:
        from ragchat.config import get_settings

        guards = Guards.from_settings(get_settings())
        request.app.state.guards = guards
    return guards


def _too_many(retry_after: int, detail: object) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


def _client_ip(request: Request, trusted_hops: int = 1) -> str:
    """Best-effort client IP that resists a client-spoofed ``X-Forwarded-For``.

    Proxies *append* to X-Forwarded-For, so the trustworthy client IP is the entry our
    trusted proxy added — counted from the right, ``trusted_hops`` in. Any values a client
    prepends sit to the left and are ignored.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - trusted_hops)]
    return request.client.host if request.client else "unknown"


def _ip_scope(request: Request, guards: Guards) -> str:
    """A privacy-preserving, salted hash of the client IP for usage counting."""
    ip = _client_ip(request, guards.trusted_proxy_hops)
    digest = hashlib.sha256(f"{guards.hash_salt}:{ip}".encode()).hexdigest()[:32]
    return f"ip:{digest}"


def _is_provider_quota_error(message: str) -> bool:
    """Heuristic: does a provider exception indicate an upstream rate/quota limit?"""
    upper = message.upper()
    return "RESOURCE_EXHAUSTED" in upper or "429" in upper or "QUOTA" in upper


def _map_provider_error(exc: Exception, action: str) -> HTTPException:
    """Turn a provider failure into a clean HTTP error the UI can display."""
    message = str(exc)
    logger.exception("%s failed for session", action)
    if _is_provider_quota_error(message):
        return _too_many(
            60,
            "The shared model quota is exhausted right now — please try again shortly. "
            "(This demo runs on a limited free-tier key.)",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"The model request failed: {message}"[:400],
    )


def guard_ask(
    request: Request,
    response: Response,
    session_id: str = Depends(get_session_id),
    session_factory: Callable[[], DbSession] = Depends(get_db_session_factory),
) -> None:
    """Guard the ask endpoint: burst limit + durable daily allowance/budget.

    Bring-your-own-keys requests bypass every shared-key limit (they spend their own
    quota). Otherwise: a per-session burst limit, then a per-user daily free allowance
    (hashed-IP), then the instance-wide daily budget — all enforced against durable DB
    counters so limits survive restarts. Exhausting the allowance returns a 429 flagged
    ``byok_required`` so the UI can prompt for the user's own keys.
    """
    if _byo_keys(request) != (None, None):
        return

    guards = _get_guards(request)
    if not guards.ask_limiter.allow(session_id):
        raise _too_many(60, "Too many questions; slow down and try again shortly.")

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with session_factory() as db:
        remaining = -1
        if guards.daily_free_allowance > 0:
            ip_count = repository.bump_usage(db, _ip_scope(request, guards), day)
            if ip_count > guards.daily_free_allowance:
                db.rollback()
                raise _too_many(
                    3600,
                    {
                        "message": "You've used your free questions for today. Add your own "
                        "Cohere and Gemini API keys to keep going.",
                        "byok_required": True,
                    },
                )
            remaining = guards.daily_free_allowance - ip_count
        if guards.daily_budget > 0:
            total = repository.bump_usage(db, "global", day)
            if total > guards.daily_budget:
                db.rollback()
                raise _too_many(
                    3600,
                    "The demo's daily request budget is exhausted. Please try again "
                    "tomorrow, or add your own API keys.",
                )
        db.commit()
    if remaining >= 0:
        response.headers["X-Free-Remaining"] = str(remaining)


def guard_ingest(request: Request, session_id: str = Depends(get_session_id)) -> None:
    """Guard uploads with a per-session burst limit (BYO-key requests bypass it)."""
    if _byo_keys(request) != (None, None):
        return
    guards = _get_guards(request)
    if not guards.ingest_limiter.allow(session_id):
        raise _too_many(3600, "Too many uploads; try again later.")


@router.get("/health", response_model=HealthResponse, tags=["ops"])
def health(
    session_factory: Callable[[], DbSession] = Depends(get_db_session_factory),
) -> HealthResponse:
    """Readiness probe: verifies the database is reachable with ``SELECT 1``.

    Returns 503 (not 200) while the database is unavailable, so orchestrators can gate
    traffic until the app is genuinely ready.
    """
    try:
        db = session_factory()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not reachable",
        ) from exc
    return HealthResponse(status="ok")


@router.post(
    "/ask",
    response_model=AskResponse,
    tags=["qa"],
    dependencies=[Depends(guard_ask)],
)
def ask(payload: AskRequest, service: RAGService = Depends(get_service)) -> AskResponse:
    """Answer a question using retrieval-augmented generation over the caller's session."""
    try:
        result = service.ask(payload.question)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        # Provider/model failures must surface as clean JSON (not a raw 500): quota/rate
        # limits become a friendly 429, everything else a 502 with the real message.
        raise _map_provider_error(exc, "ask") from exc
    return AskResponse(
        answer=result.answer,
        sources=[SourceSchema(content=s.content, metadata=s.metadata) for s in result.sources],
    )


@router.post(
    "/ingest/file",
    response_model=IngestResponse,
    tags=["ingest"],
    dependencies=[Depends(guard_ingest)],
)
async def ingest_file(
    file: UploadFile = File(...),
    service: RAGService = Depends(get_service),
) -> IngestResponse:
    """Upload a file (.md/.txt/.pdf/.docx) and (re)build the caller's knowledge base.

    The read is bounded by the configured size cap, and all caps are enforced before any
    embedding work happens, so an oversized or unsupported upload never incurs cost.
    """
    limit = service.max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {limit}-byte limit.",
        )
    try:
        result = service.ingest_upload(file.filename or "upload", data)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except FileTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except (TooManySectionsError, EmptyDocumentError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        # Embedding/provider failures return clean JSON (429 for quota, else 502).
        raise _map_provider_error(exc, "ingest") from exc
    return IngestResponse(sections_written=result.sections_written)
