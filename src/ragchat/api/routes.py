"""HTTP routes, session resolution, and dependency providers."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession
from starlette.concurrency import run_in_threadpool

from ragchat.api.guards import Guards
from ragchat.api.schemas import (
    AskRequest,
    AskResponse,
    AuditResponse,
    AuditSummarySchema,
    ChecklistOption,
    GapReportSchema,
    HealthResponse,
    IngestResponse,
    ProviderStatus,
    ReviewRequest,
    SourceSchema,
    StoredAuditSchema,
)
from ragchat.db import repository
from ragchat.errors import (
    EmptyDocumentError,
    FileTooLargeError,
    TooManyFilesError,
    TooManySectionsError,
    UnknownChecklistError,
    UnsupportedFileTypeError,
)
from ragchat.service import (
    AuditReviewService,
    AuditService,
    RAGService,
    build_audit_review_service,
    build_audit_service,
    build_session_service,
)

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
# Audit BYO: a caller may supply a key for any of the three audit providers, spending their
# own quota. Header -> provider id used to build the ladder.
_AUDIT_KEY_HEADERS: dict[str, str] = {
    "X-Google-Api-Key": "gemini",
    "X-Mistral-Api-Key": "mistral",
    "X-Groq-Api-Key": "groq",
}


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


def _byo_audit_keys(request: Request) -> dict[str, str]:
    """Extract any bring-your-own audit keys from headers → ``{provider_id: key}``.

    The audit path uses only chat models (no Cohere embeddings), so a caller can supply a key
    for any of the three providers (Gemini/Mistral/Groq) to run on their own quota. Keys are
    read per request and never stored or logged.
    """
    keys: dict[str, str] = {}
    for header, provider in _AUDIT_KEY_HEADERS.items():
        value = (request.headers.get(header) or "").strip()
        if value:
            keys[provider] = value
    return keys


def get_service(request: Request, session_id: str = Depends(get_session_id)) -> RAGService:
    """Build a session-scoped service, honoring per-request BYO keys if supplied.

    Overridden in tests to inject a fake.
    """
    cohere_key, google_key = _byo_keys(request)
    return build_session_service(session_id, cohere_key=cohere_key, google_key=google_key)


def get_audit_service(
    request: Request,
    session_id: str = Depends(get_session_id),
    checklist_id: str | None = Form(default=None),
) -> AuditService:
    """Build a session-scoped audit service for the selected vertical.

    Honors a per-request BYO Google key and an optional ``checklist_id`` form field
    (defaults to the configured vertical). Overridden in tests to inject a fake.
    """
    try:
        return build_audit_service(
            session_id, byo_keys=_byo_audit_keys(request), checklist_id=checklist_id
        )
    except UnknownChecklistError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def get_audit_review_service(
    session_id: str = Depends(get_session_id),
) -> AuditReviewService:
    """Build a session-scoped review service (relational only; no keys).

    Overridden in tests to inject a fake.
    """
    return build_audit_review_service(session_id)


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


_SECRETISH = re.compile(r"(AIza[\w-]{8,}|gsk_[\w-]{8,}|sk-[\w-]{8,}|[A-Za-z0-9_-]{32,})")


def _scrub(message: str) -> str:
    """Redact anything that looks like an API key before showing a provider message."""
    return _SECRETISH.sub("[redacted]", message)


def _map_provider_error(exc: Exception, action: str) -> HTTPException:
    """Turn a provider failure into a clean HTTP error the UI can display.

    The provider's own message is surfaced (scrubbed + truncated) so a rate-limit, capacity,
    or invalid-model error can be told apart from a genuine quota exhaustion — essential when
    debugging a specific fallback rung.
    """
    hint = _scrub(str(exc))[:200]
    logger.exception("%s failed for session", action)
    if _is_provider_quota_error(str(exc)):
        return _too_many(
            60,
            "The model is rate-limited or out of quota right now — retry shortly, or add "
            f"your own API key. (provider said: {hint})",
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"The model request failed: {hint}",
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


def guard_audit(
    request: Request,
    response: Response,
    session_factory: Callable[[], DbSession] = Depends(get_db_session_factory),
) -> None:
    """Guard the audit endpoint: IP-keyed burst limit + durable daily allowance/budget.

    A bring-your-own key for **any** audit provider bypasses every shared-key limit (the
    caller spends their own quota). Otherwise the limits are keyed on the **hashed client
    IP**, not the client-set session cookie — an audit is one model call per file, the most
    expensive path, so it must not be evadable by simply dropping the cookie to mint a fresh
    session. Counters are durable (survive restarts).
    """
    if _byo_audit_keys(request):
        return

    guards = _get_guards(request)
    ip = _ip_scope(request, guards)
    # Burst limit, IP-keyed (namespaced so it can't collide with ingest's session keys).
    if not guards.ingest_limiter.allow("audit:" + ip):
        raise _too_many(3600, "Too many audits; slow down, or add your own Google API key.")

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with session_factory() as db:
        remaining = -1
        if guards.daily_audit_allowance > 0:
            ip_count = repository.bump_usage(db, "audit_" + ip, day)
            if ip_count > guards.daily_audit_allowance:
                db.rollback()
                raise _too_many(
                    3600,
                    {
                        "message": "You've used your free audits for today. Add your own "
                        "Google API key to keep going.",
                        "byok_required": True,
                    },
                )
            remaining = guards.daily_audit_allowance - ip_count
        if guards.daily_audit_budget > 0:
            total = repository.bump_usage(db, "audit_global", day)
            if total > guards.daily_audit_budget:
                db.rollback()
                raise _too_many(
                    3600,
                    "The demo's daily audit budget is exhausted. Please try again tomorrow, "
                    "or add your own Google API key.",
                )
        db.commit()
    if remaining >= 0:
        response.headers["X-Audit-Free-Remaining"] = str(remaining)


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


@router.get("/checklists", response_model=list[ChecklistOption], tags=["audit"])
def list_checklists() -> list[ChecklistOption]:
    """The audit verticals available to pick, each with its display name."""
    from ragchat.audit.manifest import available_checklists, get_checklist

    return [ChecklistOption(id=cid, name=get_checklist(cid).name) for cid in available_checklists()]


@router.get("/providers", response_model=list[ProviderStatus], tags=["ops"])
def list_providers() -> list[ProviderStatus]:
    """Diagnostic: the audit fallback ladder as configured — which rungs are active and the
    resolved model ids. Reports no secrets (only whether each provider's key is present)."""
    from ragchat.config import get_settings
    from ragchat.rag.providers import describe_audit_ladder

    return [ProviderStatus(**rung) for rung in describe_audit_ladder(get_settings())]


@router.post(
    "/audit",
    response_model=AuditResponse,
    tags=["audit"],
    dependencies=[Depends(guard_audit)],
)
async def audit_packet(
    files: list[UploadFile] = File(...),
    service: AuditService = Depends(get_audit_service),
) -> AuditResponse:
    """Audit a submission packet (several files) against the active checklist.

    Reads each file bounded by the per-file size cap and enforces the packet's file-count
    cap before any model call, so an oversized or over-large packet never incurs cost.
    """
    limit = service.max_upload_bytes
    if len(files) > service.max_files:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Packet exceeds the {service.max_files}-file limit.",
        )

    payload: list[tuple[str, bytes]] = []
    for upload in files:
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"File '{upload.filename}' exceeds the {limit}-byte limit.",
            )
        payload.append((upload.filename or "upload", data))

    try:
        # Offload the blocking pipeline (model calls + DB) to a worker thread so a slow or
        # retrying provider call can never starve the event loop and fail health checks.
        result = await run_in_threadpool(service.audit_packet, payload)
    except TooManyFilesError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except UnsupportedFileTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except (EmptyDocumentError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise _map_provider_error(exc, "audit") from exc

    from ragchat.audit.export import render_request
    from ragchat.audit.manifest import get_checklist

    checklist_name = get_checklist(result.checklist_id).name
    return AuditResponse(
        packet_id=result.packet_id,
        checklist_id=result.checklist_id,
        report=GapReportSchema.from_report(result.report),
        request_summary=render_request(result.report, checklist_name=checklist_name),
    )


def _checklist_name(checklist_id: str) -> str:
    """Best-effort display name for a checklist id (falls back to the id itself)."""
    from ragchat.audit.manifest import get_checklist

    try:
        return get_checklist(checklist_id).name
    except UnknownChecklistError:
        return checklist_id


@router.get("/audits", response_model=list[AuditSummarySchema], tags=["audit"])
def list_audits(
    service: AuditReviewService = Depends(get_audit_review_service),
) -> list[AuditSummarySchema]:
    """This session's recent audits (newest first), with effective, post-review counts."""
    return [
        AuditSummarySchema.from_summary(s, checklist_name=_checklist_name(s.checklist_id))
        for s in service.list_audits()
    ]


@router.get("/audits/{packet_id}", response_model=StoredAuditSchema, tags=["audit"])
def get_audit(
    packet_id: str,
    service: AuditReviewService = Depends(get_audit_review_service),
) -> StoredAuditSchema:
    """Re-open one past audit with any reviewer decisions applied (404 if not this session's)."""
    from ragchat.audit.export import render_request

    stored = service.get_audit(packet_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit not found.")
    checklist_name = _checklist_name(stored.checklist_id)
    return StoredAuditSchema.from_stored(
        stored,
        checklist_name=checklist_name,
        request_summary=render_request(stored.report, checklist_name=checklist_name),
    )


@router.post(
    "/audits/{packet_id}/findings/{requirement_id}/review",
    response_model=StoredAuditSchema,
    tags=["audit"],
)
def review_finding(
    packet_id: str,
    requirement_id: str,
    body: ReviewRequest,
    service: AuditReviewService = Depends(get_audit_review_service),
) -> StoredAuditSchema:
    """Accept or override one finding's verdict; returns the refreshed audit.

    Accepting confirms the machine; overriding sets a different status (and must change it).
    Scoped to the session, so only your own audits can be reviewed.
    """
    from ragchat.audit.export import render_request
    from ragchat.audit.report import FindingStatus
    from ragchat.audit.review import ReviewAction

    try:
        action = ReviewAction(body.action)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown review action {body.action!r}.",
        ) from exc
    review_status: FindingStatus | None = None
    if body.status is not None:
        try:
            review_status = FindingStatus(body.status)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown status {body.status!r}.",
            ) from exc

    try:
        stored = service.review_finding(
            packet_id, requirement_id, action=action, status=review_status, note=body.note
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found."
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    checklist_name = _checklist_name(stored.checklist_id)
    return StoredAuditSchema.from_stored(
        stored,
        checklist_name=checklist_name,
        request_summary=render_request(stored.report, checklist_name=checklist_name),
    )
