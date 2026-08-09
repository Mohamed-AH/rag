"""pgvector-backed vector store construction.

Wraps ``langchain-postgres`` ``PGVector`` so the rest of the app treats vector storage
as just another PostgreSQL table. The embedding dimension is pinned from configuration
(``embed-english-v3.0`` -> 1024); a mismatch between the configured model and dimension
is rejected here rather than surfacing as a cryptic pgvector insert error later.
"""

from __future__ import annotations

from typing import Any

from langchain_core.embeddings import Embeddings

from ragchat.config import Settings, get_settings

# Known embedding dimensions, used to catch model/dimension drift at startup.
_KNOWN_DIMENSIONS: dict[str, int] = {
    "embed-english-v3.0": 1024,
    "embed-multilingual-v3.0": 1024,
    "embed-english-light-v3.0": 384,
    "embed-multilingual-light-v3.0": 384,
}


def validate_embedding_dimension(settings: Settings) -> None:
    """Raise ``ValueError`` if the configured dimension can't be right for the model."""
    expected = _KNOWN_DIMENSIONS.get(settings.embedding_model)
    if expected is not None and expected != settings.embedding_dimension:
        raise ValueError(
            f"embedding_model '{settings.embedding_model}' produces {expected}-dim vectors "
            f"but embedding_dimension is set to {settings.embedding_dimension}. "
            "Update EMBEDDING_DIMENSION to match, or the pgvector column will be wrong."
        )


def session_collection_name(session_id: str) -> str:
    """Return the pgvector collection name for a session (one collection per tenant)."""
    return f"kb_{session_id}"


def build_vector_store(
    embeddings: Embeddings,
    settings: Settings | None = None,
    collection_name: str | None = None,
) -> Any:
    """Construct a ``PGVector`` store bound to ``collection_name``.

    Returns ``Any`` because ``PGVector`` is imported lazily (its SDK is optional at
    import time); callers treat it as a LangChain vector store.

    Passing a per-session ``collection_name`` isolates one tenant's vectors from another's
    (see :func:`session_collection_name`); it defaults to ``settings.collection_name``.
    The collection's vector column dimension is fixed to ``settings.embedding_dimension``,
    so switching embedding models without also updating the dimension is caught up front.
    """
    settings = settings or get_settings()
    validate_embedding_dimension(settings)

    from langchain_postgres import PGVector

    return PGVector(
        embeddings=embeddings,
        collection_name=collection_name or settings.collection_name,
        connection=settings.sqlalchemy_url,
        embedding_length=settings.embedding_dimension,
        use_jsonb=True,
    )
