"""Request/response models for the HTTP API.

Pydantic models double as validation and as the source for the generated OpenAPI schema
served at ``/docs``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    """A question to answer against the knowledge base."""

    question: str = Field(..., min_length=1, max_length=2000, examples=["What is a VPC?"])


class SourceSchema(BaseModel):
    """A source document that supported an answer."""

    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AskResponse(BaseModel):
    """An answer plus the sources it was grounded in."""

    answer: str
    sources: list[SourceSchema]


class IngestResponse(BaseModel):
    """Result of an ingestion run."""

    sections_written: int


class HealthResponse(BaseModel):
    """Readiness probe payload."""

    status: str = Field(examples=["ok"])


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    detail: str
