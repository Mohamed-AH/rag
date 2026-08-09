"""Embedding-model factory.

Isolated behind a single function so the concrete provider (Cohere today) is an
implementation detail. Tests substitute a deterministic fake via the service layer's
dependency injection, so no network or API key is needed to exercise retrieval logic.
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.embeddings import Embeddings

from ragchat.config import Settings, get_settings


def build_embeddings(
    settings: Settings | None = None, *, cohere_key: str | None = None
) -> Embeddings:
    """Construct the Cohere embedding model.

    ``cohere_key`` overrides the configured shared key (used for bring-your-own-keys
    requests); it is never persisted or logged.
    """
    settings = settings or get_settings()
    key = cohere_key or settings.cohere_api_key.get_secret_value()
    # Imported lazily so importing this module (e.g. in unit tests) never requires the
    # optional provider SDK to be installed or configured.
    from langchain_cohere import CohereEmbeddings

    # Referenced through an ``Any`` binding: CohereEmbeddings populates its ``client`` /
    # ``async_client`` fields in a validator, but pydantic's mypy plugin reports them as
    # required positional args. Constructing via ``Any`` keeps the call valid whether or
    # not the provider SDK is installed, without an ignore that goes stale either way.
    embeddings_cls: Any = CohereEmbeddings
    return cast(
        Embeddings,
        embeddings_cls(cohere_api_key=key, model=settings.embedding_model),
    )
