"""LLM Gateway — the single provider-agnostic entry point for generation.

Hides Gemini/Nemotron transport details from the agent. The graph and compose
see only ``generate()`` + ``GenerationResponse`` (text, ``model_used``,
``fallback_used``, optional ``token_usage``).

Failover (audit §F): per-turn. Gemini is attempted with its configured retry
policy; on any typed failure (or misconfiguration) the gateway retries the
same request once against Nemotron. No cross-turn stickiness, no cross-turn
memory. When both fail, the gateway raises ``LLMAllProvidersFailedError``
carrying the safe provider error codes — never secrets — so compose can fall
back to the deterministic safety net.

Secrets: API keys never leave the provider layer (they exist only inside
provider HTTP headers); error messages, logs and results never contain them.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.integrations.llm.base import LLMProvider
from app.integrations.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitedError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from app.integrations.llm.schemas import GenerationRequest, GenerationResponse

logger = get_logger(__name__)


class LLMAllProvidersFailedError(LLMError):
    """Every configured provider failed for one generation request.

    Carries provider error codes only (e.g. ``"timeout"``, ``"rate_limited"``)
    — never provider messages, bodies or credentials.
    """

    def __init__(self, codes: list[str]) -> None:
        super().__init__("all LLM providers failed: " + ", ".join(codes))
        self.codes = codes


def _error_code(exc: LLMError) -> str:
    """Collapse a typed LLM error onto a short, safe code string."""
    if isinstance(exc, LLMAuthError):
        return "auth"
    if isinstance(exc, LLMConfigError):
        return "config"
    if isinstance(exc, LLMRateLimitedError):
        return "rate_limited"
    if isinstance(exc, LLMTimeoutError):
        return "timeout"
    if isinstance(exc, LLMConnectionError):
        return "connection"
    if isinstance(exc, LLMServerError):
        return "upstream_error"
    if isinstance(exc, LLMResponseError):
        return "unexpected_response"
    return "unexpected_error"


class LLMGateway:
    """Primary/fallback generation facade over injected ``LLMProvider``s."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
    ) -> None:
        """Store the provider chain (order is the failover order).

        ``fallback`` is optional so a deployment can run primary-only.
        """
        self._primary = primary
        self._fallback = fallback

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Generate via primary; on failure, once via fallback (per turn).

        Raises ``LLMAllProvidersFailedError`` when every configured provider
        fails (or when no fallback is configured and the primary fails).
        """
        codes: list[str] = []
        try:
            return await self._primary.generate(request)
        except LLMError as exc:
            codes.append(_error_code(exc))
            logger.warning(
                "llm_primary_failed",
                provider=type(self._primary).__name__,
                error_code=codes[0],
            )

        if self._fallback is None:
            raise LLMAllProvidersFailedError(codes)

        try:
            response = await self._fallback.generate(request)
        except LLMError as exc:
            codes.append(_error_code(exc))
            logger.warning(
                "llm_fallback_failed",
                provider=type(self._fallback).__name__,
                error_code=codes[-1],
            )
            raise LLMAllProvidersFailedError(codes) from exc

        # Attribution is a gateway concern: serving from the fallback is
        # exactly what ``fallback_used`` means for the turn.
        response = response.model_copy(update={"fallback_used": True})
        logger.info(
            "llm_fallback_used",
            primary_model=self._primary.model_name,
            fallback_model=response.model_used,
            primary_error_code=codes[0],
        )
        return response

    async def aclose(self) -> None:
        """Release provider HTTP resources (application shutdown)."""
        await self._primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()
