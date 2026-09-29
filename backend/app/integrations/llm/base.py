"""Provider contract for the LLM gateway.

``LLMProvider`` is the minimal async interface every backend implements
(Gemini native REST, Nemotron via the OpenAI-compatible NVIDIA endpoint, and
test fakes). Providers translate transport/HTTP failures into the typed
``app.integrations.llm.errors`` hierarchy and never raise raw HTTP errors.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.integrations.llm.schemas import GenerationRequest, GenerationResponse


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal async interface for one LLM backend."""

    @property
    def model_name(self) -> str:
        """Stable model identifier recorded as ``model_used`` (never "latest")."""
        ...

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Produce one completion; raise a typed LLMError on failure."""
        ...

    async def aclose(self) -> None:
        """Release underlying HTTP resources (application shutdown)."""
        ...
