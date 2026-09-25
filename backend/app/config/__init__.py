"""Configuration container for the backend (pydantic-settings)."""

from app.config.llm import FallbackLlmSettings, GeminiSettings
from app.config.settings import Settings, get_settings

__all__ = [
    "FallbackLlmSettings",
    "GeminiSettings",
    "Settings",
    "get_settings",
]
