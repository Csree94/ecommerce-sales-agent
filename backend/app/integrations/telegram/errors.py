"""Integration-level exceptions for the Telegram integration.

Mirrors the Inventra/LLM integration error style: typed, safe to log, and
never carrying credential material. The webhook route catches these to
degrade gracefully — a Telegram-side failure must never crash the endpoint
and must never leak the bot token into any surface.
"""

from __future__ import annotations


class TelegramError(Exception):
    """Base error for every Telegram integration failure."""


class TelegramConfigError(TelegramError):
    """The Telegram integration is misconfigured (e.g. missing bot token)."""


class TelegramAuthError(TelegramError):
    """Telegram rejected the bot token (HTTP 401). Never includes the token."""


class TelegramRateLimitedError(TelegramError):
    """Telegram rate-limited the Bot API call (HTTP 429)."""


class TelegramServerError(TelegramError):
    """Telegram answered with a server error (HTTP 5xx)."""


class TelegramTimeoutError(TelegramError):
    """A request to the Bot API timed out (connect or read)."""


class TelegramConnectionError(TelegramError):
    """The Bot API could not be reached (DNS, refused, reset, TLS)."""


class TelegramResponseError(TelegramError):
    """Telegram answered with a malformed/unexpected response body."""
