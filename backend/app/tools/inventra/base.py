"""Translation from Inventra client errors to the tool error hierarchy.

Single place where client exception types map onto tool error types/codes, so
tool modules stay thin and the mapping stays consistent (never swallowed,
never converted into empty results).
"""

from __future__ import annotations

from typing import NoReturn

from app.integrations.inventra.errors import (
    InventraAuthError,
    InventraConfigError,
    InventraConnectionError,
    InventraError,
    InventraNotFoundError,
    InventraRateLimitedError,
    InventraResponseError,
    InventraServerError,
    InventraTimeoutError,
    InventraValidationError,
)
from app.tools.errors import ToolError, ToolInputError, ToolNotFoundError, ToolUnavailableError

# Client error type -> (tool error type, stable code).
_TRANSLATIONS: dict[type[InventraError], tuple[type[ToolError], str]] = {
    InventraNotFoundError: (ToolNotFoundError, "not_found"),
    InventraValidationError: (ToolInputError, "invalid_input"),
    InventraTimeoutError: (ToolUnavailableError, "timeout"),
    InventraConnectionError: (ToolUnavailableError, "unavailable"),
    InventraServerError: (ToolUnavailableError, "upstream_error"),
    InventraRateLimitedError: (ToolUnavailableError, "rate_limited"),
    InventraAuthError: (ToolUnavailableError, "auth"),
    InventraConfigError: (ToolUnavailableError, "config"),
    InventraResponseError: (ToolUnavailableError, "unexpected_response"),
}


def translate_inventra_error(exc: InventraError) -> NoReturn:
    """Re-raise a client error as the matching tool error (original chained)."""
    error_cls, code = _TRANSLATIONS.get(type(exc), (ToolUnavailableError, "unexpected_error"))
    raise error_cls(f"Inventra call failed: {exc}", code=code) from exc
