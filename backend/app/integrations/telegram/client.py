"""Telegram Bot API client (httpx) — the ONLY outbound path to Telegram.

Implements the minimal verified surface needed for this phase:

- ``POST /bot{token}/sendMessage`` — deliver the agent's reply to the chat
- ``GET  /bot{token}/getMe``        — startup config sanity check (public info)

Design (mirrors the Inventra client):
- The bot token lives in the URL path per Telegram's API convention. It is
  set as the httpx ``base_url`` at construction and therefore never appears
  in logs, error messages, state, prompts, or persistence payloads.
- Explicit per-request timeout; bounded retries with exponential backoff for
  transient failures only (timeouts, connection errors, 429/5xx). Never
  retries 4xx.
- Failures map to typed integration errors; responses that do not match the
  expected contract raise ``TelegramResponseError`` — failures are never
  silently converted into success.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.logging import get_logger
from app.integrations.telegram.errors import (
    TelegramAuthError,
    TelegramConfigError,
    TelegramConnectionError,
    TelegramRateLimitedError,
    TelegramResponseError,
    TelegramServerError,
    TelegramTimeoutError,
)
from app.integrations.telegram.schemas import TelegramBotInfo

logger = get_logger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

# Retried HTTP statuses: transient server-side conditions only.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_RETRY_BACKOFF_SECONDS = 0.25


class TelegramClient:
    """Typed Bot API access for sending replies to customer chats."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str = _TELEGRAM_API_BASE,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the client.

        ``transport`` is an injection seam for tests (``httpx.MockTransport``);
        production code never passes it. Raises ``TelegramConfigError`` when
        the bot token is missing.
        """
        if not bot_token:
            raise TelegramConfigError(
                "Telegram integration is not configured (set TELEGRAM_BOT_TOKEN)"
            )
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=f"{base_url}/bot{bot_token}",
            timeout=httpx.Timeout(timeout_seconds),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        """Release the underlying httpx pool (application shutdown)."""
        await self._client.aclose()

    async def send_message(self, chat_id: int, text: str) -> None:
        """``POST sendMessage`` — deliver the reply (raises typed errors)."""
        payload = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        await self._request_json("POST", "sendMessage", json_payload=payload)

    async def get_me(self) -> TelegramBotInfo:
        """``GET getMe`` — public bot identity (config sanity check)."""
        data = await self._request_json("GET", "getMe")
        return self._parse_bot_info(data)

    # ------------------------------------------------------------------
    # Request core
    # ------------------------------------------------------------------

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        """Perform one Bot API call with bounded retries; return parsed JSON."""
        retries_left = self._max_retries
        backoff = _RETRY_BACKOFF_SECONDS

        while True:
            try:
                response = await self._client.request(method, path, json=json_payload)
            except httpx.TimeoutException as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise TelegramTimeoutError("Telegram request timed out") from exc
            except httpx.TransportError as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise TelegramConnectionError("Could not reach Telegram") from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and retries_left > 0:
                retries_left -= 1
                logger.warning(
                    "telegram_request_retry",
                    path=path,
                    status_code=response.status_code,
                    retries_left=retries_left,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            self._raise_for_status(response, path)
            try:
                body = response.json()
            except ValueError as exc:
                raise TelegramResponseError("Telegram returned a non-JSON body") from exc
            return self._unwrap(body, path)

    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        """Map an HTTP error response onto a typed integration error."""
        if response.is_success:
            return
        status_code = response.status_code
        if status_code in (401, 403):
            raise TelegramAuthError(f"Telegram rejected the bot token ({status_code}) for {path}")
        if status_code == 429:
            raise TelegramRateLimitedError(f"Telegram rate limit hit ({status_code}) for {path}")
        if status_code >= 500:
            raise TelegramServerError(f"Telegram server error ({status_code}) for {path}")
        detail = self._safe_detail(response)
        raise TelegramResponseError(
            f"Unexpected Telegram response ({status_code}) for {path}: {detail}"
        )

    @staticmethod
    def _unwrap(body: Any, path: str) -> Any:
        """Validate Telegram's ``{ok: bool, result: ...}`` envelope."""
        if not isinstance(body, dict) or body.get("ok") is not True:
            description = body.get("description") if isinstance(body, dict) else None
            raise TelegramResponseError(
                f"Telegram Bot API call failed for {path}: {str(description)[:200]}"
            )
        return body.get("result")

    @staticmethod
    def _parse_bot_info(result: Any) -> TelegramBotInfo:
        """Validate ``getMe``'s result against the schema."""
        try:
            return TelegramBotInfo.model_validate(result)
        except Exception as exc:
            raise TelegramResponseError("Telegram getMe returned an unexpected payload") from exc

    @staticmethod
    def _safe_detail(response: httpx.Response) -> str:
        """Extract a short error detail without echoing credential material."""
        try:
            body: Any = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(body, dict):
            return str(body.get("description"))[:200]
        return str(body)[:200]
