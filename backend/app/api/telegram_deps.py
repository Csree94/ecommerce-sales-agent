"""Request-scoped FastAPI dependencies for the Telegram webhook route."""

from __future__ import annotations

import hmac

from fastapi import Request

from app.integrations.telegram.client import TelegramClient


def get_telegram_client(request: Request) -> TelegramClient:
    """Return the shared Telegram client attached at startup (503 when unconfigured)."""
    client: TelegramClient | None = getattr(request.app.state, "telegram_client", None)
    if client is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram integration is not configured",
        )
    return client


def verify_webhook_secret(request: Request) -> None:
    """Constant-time comparison of Telegram's secret-token header (audit §H).

    Telegram sends the value of ``TELEGRAM_WEBHOOK_SECRET`` in the
    ``X-Telegram-Bot-Api-Secret-Token`` header on every webhook call. When no
    secret is configured, verification is skipped (local/dev convenience) —
    production deployments must set the variable.
    """
    settings = request.app.state.settings
    expected = settings.telegram_webhook_secret.get_secret_value()
    if not expected:
        return
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not hmac.compare_digest(provided or "", expected):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret token",
        )
