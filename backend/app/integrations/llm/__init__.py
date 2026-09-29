"""LLM integration (gateway + Gemini/Nemotron providers).

The ONLY path from agent code to LLM providers. Structure mirrors the Inventra
integration (audit §F):

- ``gateway``  — ``LLMGateway`` (primary/fallback orchestration, per-turn)
- ``gemini``   — ``GeminiProvider`` (native REST over httpx; primary)
- ``nemotron`` — ``NemotronProvider`` (NVIDIA OpenAI-compatible REST; fallback)
- ``schemas``  — Pydantic data-transfer contracts (no credentials inside)
- ``base``     — ``LLMProvider`` protocol (the seam test fakes implement)
- ``errors``   — typed integration errors for graceful degradation

No SDK is used: both providers run over the existing ``httpx`` dependency.
"""

from functools import lru_cache

from app.config.llm import FallbackLlmSettings, GeminiSettings
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
from app.integrations.llm.gateway import LLMAllProvidersFailedError, LLMGateway
from app.integrations.llm.gemini import GeminiProvider
from app.integrations.llm.nemotron import NemotronProvider
from app.integrations.llm.schemas import GenerationRequest, GenerationResponse, TokenUsage


class UnconfiguredProvider:
    """Stand-in primary used when no LLM is configured.

    Fails fast with ``LLMConfigError`` on every generation so the gateway
    reports a clean failure (and compose degrades deterministically) instead
    of any call ever reaching the network.
    """

    @property
    def model_name(self) -> str:
        return "unconfigured"

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        raise LLMConfigError(
            "LLM integration is not configured (set GEMINI_API_KEY, optionally "
            "FALLBACK_LLM_API_KEY)"
        )

    async def aclose(self) -> None:
        return None


__all__ = [
    "FallbackLlmSettings",
    "GenerationRequest",
    "GenerationResponse",
    "GeminiProvider",
    "GeminiSettings",
    "LLMAuthError",
    "LLMAllProvidersFailedError",
    "LLMConfigError",
    "LLMConnectionError",
    "LLMError",
    "LLMGateway",
    "LLMProvider",
    "LLMRateLimitedError",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "NemotronProvider",
    "TokenUsage",
    "UnconfiguredProvider",
    "build_default_gateway",
    "get_llm_gateway",
]


def build_default_gateway() -> LLMGateway:
    """Build the gateway from existing configuration (``config.llm``).

    - ``GEMINI_API_KEY`` set → Gemini primary (+ Nemotron fallback when
      ``FALLBACK_LLM_API_KEY`` is also set).
    - Nothing configured → a gateway over an ``UnconfiguredProvider`` that
      fails fast with ``LLMConfigError`` on generate, so callers degrade to
      the deterministic safety net instead of crashing at import time. This
      keeps tests and ad-hoc usage network-free without a second config
      system.
    """
    from app.config.llm import get_fallback_llm_settings, get_gemini_settings

    gemini_key = get_gemini_settings().api_key.get_secret_value()
    if not gemini_key:
        return LLMGateway(UnconfiguredProvider())

    primary = GeminiProvider(get_gemini_settings())
    fallback: NemotronProvider | None
    if get_fallback_llm_settings().api_key.get_secret_value():
        fallback = NemotronProvider(get_fallback_llm_settings())
    else:
        fallback = None
    return LLMGateway(primary, fallback)


@lru_cache
def get_llm_gateway() -> LLMGateway:
    """Process-wide gateway accessor (built once, cached per process)."""
    return build_default_gateway()
