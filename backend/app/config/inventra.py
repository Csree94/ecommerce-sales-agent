"""Inventra integration settings.

Kept in a separate module so Inventra configuration — and, later, a replaced
authentication mechanism — can evolve without touching the core ``Settings``
class (same pattern as ``app/config/llm.py``).

Verified Inventra reality (September 2026): JWT Bearer tokens issued to *users*
only; there is NO service-to-service auth, API key, or OAuth client-credentials
flow. ``bearer_token`` therefore holds a pre-issued JWT and may be empty. When
Inventra gains proper service authentication, add the new fields here and a
matching provider in ``app/integrations/inventra/auth.py`` — the client
interface and agent tools stay unchanged.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class InventraSettings(BaseSettings):
    """Settings for the read-only Inventra HTTP integration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Base URL only (scheme://host[:port]) — the client adds the /api/... paths.
    base_url: str = Field(default="", validation_alias="INVENTRA_BASE_URL")

    # Pre-issued JWT for Inventra's current user-token mechanism (SecretStr so
    # it never leaks into logs or repr; empty locally is allowed — the client
    # is simply not constructed when base_url is unset).
    bearer_token: SecretStr = Field(default=SecretStr(""), validation_alias="INVENTRA_BEARER_TOKEN")

    # Per-request timeout. The architecture audit (§E) budgets ~3s for Inventra
    # so a whole agent turn stays bounded.
    timeout_seconds: float = Field(default=3.0, validation_alias="INVENTRA_TIMEOUT_SECONDS")

    # Bounded retries for transient failures only (timeouts, connection errors,
    # 5xx). All current operations are reads, so retries are safe.
    max_retries: int = Field(default=2, validation_alias="INVENTRA_MAX_RETRIES")
    retry_backoff_seconds: float = Field(
        default=0.25, validation_alias="INVENTRA_RETRY_BACKOFF_SECONDS"
    )


@lru_cache
def get_inventra_settings() -> InventraSettings:
    """Cached accessor for Inventra settings (import-safe, created once)."""
    return InventraSettings()
