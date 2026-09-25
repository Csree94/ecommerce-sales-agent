"""Application configuration (pydantic-settings).

All runtime configuration is sourced from environment variables (optionally a
local ``.env`` file, gitignored) and validated at startup. Secret values use
``SecretStr`` so they are never leaked through logs or ``repr``.
"""

from functools import lru_cache

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.environment import Environment


class Settings(BaseSettings):
    """Central application settings, loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- App ------------------------------------------------------------
    app_name: str = "ecommerce-sales-agent"
    app_env: Environment = Environment.LOCAL
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    api_v1_prefix: str = "/api/v1"

    # --- Database (PostgreSQL / Neon) -------------------------------------
    # Use the Neon *pooled* connection string in deployment (serverless Postgres,
    # short-lived connections — see §L of the architecture audit).
    database_url: SecretStr = Field(..., validation_alias="DATABASE_URL")
    db_echo: bool = Field(default=False, validation_alias="DB_ECHO")
    db_pool_size: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")
    db_pool_recycle_seconds: int = Field(default=1800, validation_alias="DB_POOL_RECYCLE_SECONDS")
    db_connect_timeout_seconds: int = Field(default=10, validation_alias="DB_CONNECT_TIMEOUT")
    # SQLAlchemy callables are not thread-safe; disable pool sharing across loops.
    db_use_null_pool: bool = Field(default=True, validation_alias="DB_USE_NULL_POOL")

    # --- Cache (Redis) ----------------------------------------------------
    redis_url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"), validation_alias="REDIS_URL"
    )
    redis_connect_timeout_seconds: float = Field(
        default=5.0, validation_alias="REDIS_CONNECT_TIMEOUT"
    )
    redis_socket_timeout_seconds: float = Field(
        default=5.0, validation_alias="REDIS_SOCKET_TIMEOUT"
    )

    # --- Shopify (integration NOT implemented in this phase) ---------------
    shopify_shop_url: str = Field(default="", validation_alias="SHOPIFY_SHOP_URL")
    shopify_api_version: str = Field(default="", validation_alias="SHOPIFY_API_VERSION")
    shopify_access_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="SHOPIFY_ACCESS_TOKEN"
    )
    shopify_webhook_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="SHOPIFY_WEBHOOK_SECRET"
    )

    # --- Telegram (integration NOT implemented in this phase) --------------
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_webhook_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias="TELEGRAM_WEBHOOK_SECRET"
    )

    # --- LLM (provider clients NOT implemented in this phase) --------------
    gemini_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="models/gemini-2.5-flash", validation_alias="GEMINI_MODEL")
    fallback_llm_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="FALLBACK_LLM_API_KEY"
    )
    fallback_llm_base_url: str = Field(
        default="https://integrate.api.nvidia.com/v1", validation_alias="FALLBACK_LLM_BASE_URL"
    )
    fallback_llm_model: str = Field(
        default="nvidia/llama-3.1-nemotron-70b-instruct", validation_alias="FALLBACK_LLM_MODEL"
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        """Database URL as a plain string for the SQLAlchemy engine factory."""
        return self.database_url.get_secret_value()

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.app_env is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (import-safe, created once per process)."""
    return Settings()  # type: ignore[call-arg]
