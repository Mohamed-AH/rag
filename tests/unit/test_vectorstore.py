"""Tests for embedding-dimension validation (guards model/dimension drift)."""

from __future__ import annotations

import pytest

from ragchat.config import Settings
from ragchat.rag.vectorstore import validate_embedding_dimension


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "COHERE_API_KEY": "c",
        "GOOGLE_API_KEY": "g",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_matching_dimension_is_accepted() -> None:
    validate_embedding_dimension(
        _settings(EMBEDDING_MODEL="embed-english-v3.0", EMBEDDING_DIMENSION=1024)
    )


def test_mismatched_dimension_is_rejected() -> None:
    with pytest.raises(ValueError, match="1024-dim"):
        validate_embedding_dimension(
            _settings(EMBEDDING_MODEL="embed-english-v3.0", EMBEDDING_DIMENSION=384)
        )


def test_unknown_model_is_not_second_guessed() -> None:
    # Unknown models can't be validated against a known dimension, so we allow them.
    validate_embedding_dimension(
        _settings(EMBEDDING_MODEL="some-future-model", EMBEDDING_DIMENSION=99)
    )
