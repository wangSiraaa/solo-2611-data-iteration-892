"""Stable error codes used by the catalog, operations and citation repository."""

from __future__ import annotations

from typing import Any


class CitationSystemError(Exception):
    """An expected, explainable failure.

    ``code`` is part of the machine-readable interface; callers should not have
    to parse the human message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# Catalog / operation errors
VOLUME_NOT_FOUND = "volume_not_found"
PAGE_NOT_FOUND = "page_not_found"
DUPLICATE_PAGE_ID = "duplicate_page_id"
ANCHOR_NOT_FOUND = "anchor_not_found"
CROSS_VOLUME_MOVE = "cross_volume_move"
CROSS_VOLUME_ANCHOR = "cross_volume_anchor"
INVALID_OPERATION = "invalid_operation"
INVALID_PAGE = "invalid_page"
UNSUPPORTED_OPERATION = "unsupported_operation"

# Resolution errors
RESOLUTION_NOT_FOUND = "resolution_not_found"
RESOLUTION_AMBIGUOUS = "resolution_ambiguous"

# Citation repository errors
CITATION_EXISTS = "citation_exists"
CITATION_NOT_FOUND = "citation_not_found"
INVALID_CITATION = "invalid_citation"
DUPLICATE_PINNED_MEMBER = "duplicate_pinned_member"
CROSS_VOLUME_REFERENCE = "cross_volume_reference"


class OperationRejected(CitationSystemError):
    """A whole operation request (batch) was rejected before any write."""


class ReferenceCreationError(CitationSystemError):
    """A citation could not be created; no citation was written."""
