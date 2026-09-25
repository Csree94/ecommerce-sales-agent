"""LLM provider settings.

Kept in a separate module so the LLM gateway (§F of the architecture audit)
can be added without touching the core ``Settings`` class. These settings are
*not* loaded at runtime yet — no LLM calls are made in this phase.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class GeminiSettings(BaseSettings):
    """Google Gemini (primary LLM)."""

    model: str = Field(default="models/gemini-2.5-flash", validation_alias="GEMINI_MODEL")
    api_key: SecretStr = Field(..., validation_alias="GEMINI_API_KEY")
    timeout_seconds: float = Field(default=20.0, validation_alias="GEMINI_TIMEOUT_SECONDS")
    max_retries: int = Field(default=2, validation_alias="GEMINI_MAX_RETRIES")


class FallbackLlmSettings(BaseSettings):
    """Nemotron via NVIDIA's OpenAI-compatible endpoint (fallback LLM)."""

    model: str = Field(
        default="nvidia/llama-3.1-nemotron-70b-instruct",
        validation_alias="FALLBACK_LLM_MODEL",
    )
    base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1",
        validation_alias="FALLBACK_LLM_BASE_URL",
    )
    api_key: SecretStr = Field(..., validation_alias="FALLBACK_LLM_API_KEY")
    timeout_seconds: float = Field(default=20.0, validation_alias="FALLBACK_LLM_TIMEOUT_SECONDS")
    max_retries: int = Field(default=2, validation_alias="FALLBACK_LLM_MAX_RETRIES")


@lru_cache
def get_gemini_settings() -> GeminiSettings:
    """Cached accessor for Gemini settings."""
    return GeminiSettings()  # type: ignore[call-arg]


@lru_cache
def get_fallback_llm_settings() -> FallbackLlmSettings:
    """Cached accessor for fallback LLM settings."""
    return FallbackLlmSettings()  # type: ignore[call-arg]
