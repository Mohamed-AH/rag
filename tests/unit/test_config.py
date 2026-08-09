"""Tests for typed configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ragchat.config import Settings


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "DATABASE_URL": "postgresql://u:p@localhost:5432/db",
        "COHERE_API_KEY": "cohere-key",
        "GOOGLE_API_KEY": "google-key",
    }
    base.update(overrides)
    return base


def test_loads_required_settings() -> None:
    settings = Settings(_env_file=None, **_env())  # type: ignore[arg-type]
    assert settings.cohere_api_key.get_secret_value() == "cohere-key"
    assert settings.retriever_k == 3  # default


def test_missing_required_var_raises() -> None:
    with pytest.raises(ValidationError):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            DATABASE_URL="postgresql://u:p@localhost/db",
            COHERE_API_KEY="c",
            # GOOGLE_API_KEY intentionally omitted
        )


def test_rejects_non_postgres_url() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_env(DATABASE_URL="mysql://u:p@localhost/db"))  # type: ignore[arg-type]


def test_sqlalchemy_url_normalises_plain_scheme() -> None:
    settings = Settings(_env_file=None, **_env())  # type: ignore[arg-type]
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@localhost:5432/db"


def test_sqlalchemy_url_preserves_explicit_driver() -> None:
    url = "postgresql+psycopg://u:p@localhost:5432/db"
    settings = Settings(_env_file=None, **_env(DATABASE_URL=url))  # type: ignore[arg-type]
    assert settings.sqlalchemy_url == url


def test_sqlalchemy_url_upgrades_legacy_postgres_alias() -> None:
    settings = Settings(_env_file=None, **_env(DATABASE_URL="postgres://u:p@h/db"))  # type: ignore[arg-type]
    assert settings.sqlalchemy_url == "postgresql+psycopg://u:p@h/db"


def test_retriever_k_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **_env(RETRIEVER_K="0"))  # type: ignore[arg-type]
