"""Integration-level exceptions for the Inventra client.

Tool/graph code catches these to degrade gracefully (e.g. "catalog
unavailable") instead of crashing a turn (architecture audit §E/§N). They
deliberately carry no request/response bodies or credentials.
"""

from __future__ import annotations


class InventraError(Exception):
    """Base error for every Inventra integration failure."""


class InventraConfigError(InventraError):
    """The integration is misconfigured (e.g. missing base URL)."""


class InventraAuthError(InventraError):
    """Inventra rejected the credentials (HTTP 401/403).

    Never includes the token or any credential material.
    """


class InventraNotFoundError(InventraError):
    """The requested resource does not exist on Inventra (HTTP 404)."""


class InventraValidationError(InventraError):
    """Inventra rejected the request parameters (HTTP 422/400)."""


class InventraRateLimitedError(InventraError):
    """Inventra rate-limited the request (HTTP 429)."""


class InventraServerError(InventraError):
    """Inventra answered with an unexpected server error (HTTP 5xx)."""


class InventraTimeoutError(InventraError):
    """A request to Inventra timed out (connect or read)."""


class InventraConnectionError(InventraError):
    """Inventra could not be reached (DNS, refused, reset, TLS)."""


class InventraResponseError(InventraError):
    """Inventra answered with a malformed/unexpected response body."""
