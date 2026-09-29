"""Data-transfer contracts for the LLM gateway (Pydantic, data only).

These models carry NO credentials and no provider SDK types: agent-facing code
(compose) sees only ``GenerationRequest``/``GenerationResponse``, so a provider
swap never leaks transport details into the graph.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    """Token accounting when a provider exposes it (audit §L)."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class GenerationRequest(BaseModel):
    """One provider-agnostic generation request (no credentials inside)."""

    model_config = ConfigDict(frozen=True)

    prompt: str = Field(..., min_length=1)
    system: str | None = None
    max_output_tokens: int = Field(default=512, gt=0)


class GenerationResponse(BaseModel):
    """One completed generation with explicit model attribution."""

    model_config = ConfigDict(frozen=True)

    text: str
    model_used: str
    fallback_used: bool = False
    token_usage: TokenUsage | None = None
