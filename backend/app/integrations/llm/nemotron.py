"""NVIDIA Nemotron provider (fallback LLM) — OpenAI-compatible REST via httpx.

Uses the verified OpenAI-compatible surface (`docs/architecture-audit.md` §F):

- ``POST {base_url}/chat/completions`` (base URL from ``FALLBACK_LLM_BASE_URL``)
- Bearer key in the ``Authorization`` header (never logged, never in errors)
- Response carries ``usage`` when the provider exposes token counts

The provider is failover-only in this phase: the gateway calls it after the
primary fails. Retry policy mirrors Gemini's — bounded retries with exponential
backoff on transient failures only (timeouts, connection errors, 429, 5xx).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config.llm import FallbackLlmSettings
from app.core.logging import get_logger
from app.integrations.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMConnectionError,
    LLMRateLimitedError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from app.integrations.llm.schemas import GenerationRequest, GenerationResponse, TokenUsage

logger = get_logger(__name__)

# Retried HTTP statuses: transient/rate-limit conditions per audit §F.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_RETRY_BACKOFF_SECONDS = 0.25


class NemotronProvider:
    """LLMProvider backed by NVIDIA's OpenAI-compatible chat completions API."""

    def __init__(
        self,
        settings: FallbackLlmSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the provider.

        ``transport`` is an injection seam for tests (``httpx.MockTransport``);
        production code never passes it.
        """
        if not settings.api_key.get_secret_value():
            raise LLMConfigError(
                "Nemotron integration is not configured (set FALLBACK_LLM_API_KEY)"
            )
        if not settings.base_url:
            raise LLMConfigError(
                "Nemotron integration is not configured (set FALLBACK_LLM_BASE_URL)"
            )
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    @property
    def model_name(self) -> str:
        """Exact model checkpoint pin from configuration."""
        return self._settings.model

    async def aclose(self) -> None:
        """Release the underlying httpx pool."""
        await self._client.aclose()

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Generate one completion via ``/chat/completions``."""
        payload = self._build_payload(request)
        data = await self._request_json("/chat/completions", payload)
        text, usage = self._extract(data)
        return GenerationResponse(
            text=text,
            model_used=self.model_name,
            fallback_used=True,  # this provider is only reached as failover
            token_usage=usage,
        )

    # ------------------------------------------------------------------
    # Request/response details
    # ------------------------------------------------------------------

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        """Translate the provider-agnostic request into the OpenAI chat shape."""
        messages: list[dict[str, str]] = []
        if request.system is not None:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        return {
            "model": self._settings.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
        }

    async def _request_json(self, path: str, payload: dict[str, Any]) -> Any:
        """Perform one bearer-authenticated POST with bounded retries."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}",
        }
        retries_left = self._settings.max_retries
        backoff = _RETRY_BACKOFF_SECONDS

        while True:
            try:
                response = await self._client.post(path, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise LLMTimeoutError("Nemotron request timed out") from exc
            except httpx.TransportError as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise LLMConnectionError("Could not reach Nemotron") from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and retries_left > 0:
                retries_left -= 1
                logger.warning(
                    "nemotron_request_retry",
                    status_code=response.status_code,
                    retries_left=retries_left,
                )
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            self._raise_for_status(response)
            try:
                return response.json()
            except ValueError as exc:
                raise LLMResponseError("Nemotron returned a non-JSON body") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map an HTTP error response onto a typed LLM error (no body secrets)."""
        if response.is_success:
            return
        status_code = response.status_code
        if status_code in (401, 403):
            raise LLMAuthError(f"Nemotron rejected credentials (HTTP {status_code})")
        if status_code == 429:
            raise LLMRateLimitedError("Nemotron rate limit hit (HTTP 429)")
        if status_code >= 500:
            raise LLMServerError(f"Nemotron server error (HTTP {status_code})")
        raise LLMResponseError(f"Unexpected Nemotron response (HTTP {status_code})")

    def _extract(self, data: Any) -> tuple[str, TokenUsage | None]:
        """Extract text + usage from the OpenAI chat completion shape."""
        if not isinstance(data, dict):
            raise LLMResponseError("Nemotron returned a non-object payload")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("Nemotron returned no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("Nemotron choice shape was unexpected")
        message = first.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise LLMResponseError("Nemotron returned an empty completion")
        return text.strip(), self._extract_usage(data.get("usage"))

    @staticmethod
    def _extract_usage(raw: Any) -> TokenUsage | None:
        """Map the OpenAI ``usage`` object onto ``TokenUsage`` (None when absent)."""
        if not isinstance(raw, dict):
            return None
        return TokenUsage(
            prompt_tokens=raw.get("prompt_tokens"),
            completion_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
        )
