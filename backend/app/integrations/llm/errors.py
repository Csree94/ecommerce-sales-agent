"""Integration-level exceptions for the LLM gateway.

Provider implementations raise these so the gateway can decide failover and
compose can degrade gracefully (deterministic safety net) instead of crashing
a turn (architecture audit §E/§F). Messages never contain API keys, bearer
tokens or request headers — credentials live only inside provider HTTP
headers and never surface in error strings.
"""


class LLMError(Exception):
    """Base error for every LLM integration failure."""


class LLMConfigError(LLMError):
    """The LLM integration is misconfigured (e.g. missing GEMINI_API_KEY)."""


class LLMAuthError(LLMError):
    """The provider rejected the credentials (HTTP 401/403).

    Never includes the API key or any credential material.
    """


class LLMRateLimitedError(LLMError):
    """The provider rate-limited the request (HTTP 429)."""


class LLMServerError(LLMError):
    """The provider answered with a server error (HTTP 5xx)."""


class LLMTimeoutError(LLMError):
    """A request to the provider timed out (connect or read)."""


class LLMConnectionError(LLMError):
    """The provider could not be reached (DNS, refused, reset, TLS)."""


class LLMResponseError(LLMError):
    """The provider answered with a malformed/unexpected response body."""
