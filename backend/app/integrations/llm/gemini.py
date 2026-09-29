"""Google Gemini provider (primary LLM) — native REST over the existing httpx.

Uses the verified ``generativelanguage.googleapis.com`` REST surface:

- ``POST {base}/v1beta/{model}:generateContent``
- API key in the ``x-goog-api-key`` header (never logged, never in errors)
- Response carries ``usageMetadata`` when the provider exposes token counts

Retry/failure policy (audit §F): bounded retries with exponential backoff on
transient failures only — timeouts, connection errors, HTTP 429 and 5xx.
Retries are exhausted → typed ``LLMError`` subclasses propagate to the gateway,
which decides Nemotron failover. Failures are never silently converted into
empty responses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config.llm import GeminiSettings
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

# Gemini REST base + version pin (explicit, never "latest").
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
_GEMINI_API_VERSION = "v1beta"

# Retried HTTP statuses: transient/rate-limit conditions per audit §F.
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

_RETRY_BACKOFF_SECONDS = 0.25


class GeminiProvider:
    """LLMProvider backed by Gemini's native REST API (httpx only, no SDK)."""

    def __init__(
        self,
        settings: GeminiSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Build the provider.

        ``transport`` is an injection seam for tests (``httpx.MockTransport``);
        production code never passes it.
        """
        if not settings.api_key.get_secret_value():
            raise LLMConfigError("Gemini integration is not configured (set GEMINI_API_KEY)")
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=_GEMINI_API_BASE,
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    @property
    def model_name(self) -> str:
        """Exact model pin from configuration (recorded as ``model_used``)."""
        return self._settings.model

    async def aclose(self) -> None:
        """Release the underlying httpx pool."""
        await self._client.aclose()

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Generate one completion via ``:generateContent``."""
        path = f"/{_GEMINI_API_VERSION}/{self._settings.model}:generateContent"
        payload = self._build_payload(request)
        data = await self._request_json(path, payload)
        text, usage = self._extract(data)
        return GenerationResponse(
            text=text,
            model_used=self.model_name,
            fallback_used=False,
            token_usage=usage,
        )

    # ------------------------------------------------------------------
    # Request/response details
    # ------------------------------------------------------------------

    def _build_payload(self, request: GenerationRequest) -> dict[str, Any]:
        """Translate the provider-agnostic request into Gemini's JSON shape."""
        payload: dict[str, Any] = {
            "contents": [{"parts": [{"text": request.prompt}]}],
            "generationConfig": {"maxOutputTokens": request.max_output_tokens},
        }
        if request.system is not None:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        return payload

    async def _request_json(self, path: str, payload: dict[str, Any]) -> Any:
        """Perform one keyed POST with bounded retries; return parsed JSON."""
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._settings.api_key.get_secret_value(),
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
                raise LLMTimeoutError("Gemini request timed out") from exc
            except httpx.TransportError as exc:
                if retries_left > 0:
                    retries_left -= 1
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise LLMConnectionError("Could not reach Gemini") from exc

            if response.status_code in _RETRYABLE_STATUS_CODES and retries_left > 0:
                retries_left -= 1
                logger.warning(
                    "gemini_request_retry",
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
                raise LLMResponseError("Gemini returned a non-JSON body") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Map an HTTP error response onto a typed LLM error (no body secrets)."""
        if response.is_success:
            return
        status_code = response.status_code
        if status_code in (401, 403):
            raise LLMAuthError(f"Gemini rejected credentials (HTTP {status_code})")
        if status_code == 429:
            raise LLMRateLimitedError("Gemini rate limit hit (HTTP 429)")
        if status_code >= 500:
            raise LLMServerError(f"Gemini server error (HTTP {status_code})")
        raise LLMResponseError(f"Unexpected Gemini response (HTTP {status_code})")

    def _extract(self, data: Any) -> tuple[str, TokenUsage | None]:
        """Extract text + token usage; contract violations raise ``LLMResponseError``."""
        if not isinstance(data, dict):
            raise LLMResponseError("Gemini returned a non-object payload")

        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            # Content was blocked — a contract-level refusal, safe to surface.
            raise LLMResponseError("Gemini blocked the request content")

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise LLMResponseError("Gemini returned no candidates")
        first = candidates[0]
        if not isinstance(first, dict):
            raise LLMResponseError("Gemini candidate shape was unexpected")

        content = first.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise LLMResponseError("Gemini candidate content was missing parts")

        text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
        if not text:
            raise LLMResponseError("Gemini returned an empty completion")

        usage = self._extract_usage(data.get("usageMetadata"))
        return text, usage

    @staticmethod
    def _extract_usage(raw: Any) -> TokenUsage | None:
        """Map ``usageMetadata`` onto ``TokenUsage`` (None when absent)."""
        if not isinstance(raw, dict):
            return None
        return TokenUsage(
            prompt_tokens=raw.get("promptTokenCount"),
            completion_tokens=raw.get("candidatesTokenCount"),
            total_tokens=raw.get("totalTokenCount"),
        )
