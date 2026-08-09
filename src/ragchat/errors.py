"""Domain errors that map cleanly to HTTP responses.

Defined in one place so the API layer can translate them to status codes without the
service layer importing anything web-related.
"""

from __future__ import annotations


class IngestError(ValueError):
    """Base class for user-correctable ingestion problems."""


class UnsupportedFileTypeError(IngestError):
    """The uploaded file's extension is not supported."""


class FileTooLargeError(IngestError):
    """The uploaded file exceeds the configured size cap."""


class TooManySectionsError(IngestError):
    """The uploaded file produced more sections/chunks than allowed."""


class EmptyDocumentError(IngestError):
    """The uploaded file contained no extractable text."""


class TooManyFilesError(IngestError):
    """A submitted packet contains more files than the configured cap allows."""


class UnclassifiableDocumentError(IngestError):
    """A packet document could not be matched to any known document type."""


class UnknownChecklistError(ValueError):
    """A checklist was requested by an id that is not registered."""
