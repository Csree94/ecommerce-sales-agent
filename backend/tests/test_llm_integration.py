"""Tests for the LLM integration package (gateway + providers).

No real network: Gemini/Nemotron HTTP behavior is exercised through
``httpx.MockTransport``; gateway failover is exercised with deterministic
fakes at the ``LLMProvider`` seam. Coverage:

- Gemini REST request shape (model pin, system instruction, key in headers)
- Gemini typed error mapping (401/429/5xx, timeouts) and response parsing
  (text, ``usageMetadata`` → TokenUsage, blocked/empty payload refusals)
- Nemotron OpenAI-compatible request shape and response parsing
- Gateway per-turn failover (primary fail → fallback serve → attribution),
  both-fail aggregate error, primary-only shortcut
- Secret hygiene: API keys/bearer tokens never appear in errors or results
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.config.llm import FallbackLlmSettings, GeminiSettings
from app.integrations.llm import (
    GeminiProvider,
    GenerationRequest,
    GenerationResponse,
    LLMAllProvidersFailedError,
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMGateway,
    LLMRateLimitedError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
    NemotronProvider,
    TokenUsage,
    UnconfiguredProvider,
)
from app.integrations.llm.base import LLMProvider


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_gemini(
    *, handler: Any, model: str = "models/gemini-2.5-flash", retries: int = 0
) -> GeminiProvider:
    settings = GeminiSettings(
        _env_file=None,
        GEMINI_API_KEY="test-gemini-key-123",
        GEMINI_MODEL=model,
        GEMINI_TIMEOUT_SECONDS=1.0,
        GEMINI_MAX_RETRIES=retries,
    )
    return GeminiProvider(settings, transport=httpx.MockTransport(handler))


def make_nemotron(
    *, handler: Any, model: str = "nvidia/llama-3.1-nemotron-70b-instruct", retries: int = 0
) -> NemotronProvider:
    settings = FallbackLlmSettings(
        _env_file=None,
        FALLBACK_LLM_API_KEY="nvapi-test-fallback-key-456",
        FALLBACK_LLM_BASE_URL="https://integrate.api.nvidia.com/v1",
        FALLBACK_LLM_MODEL=model,
        FALLBACK_LLM_TIMEOUT_SECONDS=1.0,
        FALLBACK_LLM_MAX_RETRIES=retries,
    )
    return NemotronProvider(settings, transport=httpx.MockTransport(handler))


def gemini_ok_payload(text: str = "Hello! How can I help?", with_usage: bool = True) -> dict:
    payload: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]
    }
    if with_usage:
        payload["usageMetadata"] = {
            "promptTokenCount": 21,
            "candidatesTokenCount": 9,
            "totalTokenCount": 30,
        }
    return payload


def openai_ok_payload(text: str = "Fallback answer", with_usage: bool = True) -> dict:
    payload: dict[str, Any] = {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]
    }
    if with_usage:
        payload["usage"] = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
    return payload


# --- GeminiProvider: request shape -------------------------------------------


def test_gemini_sends_model_pin_system_and_key_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key_header"] = request.headers.get("x-goog-api-key")
        captured["auth_header"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=gemini_ok_payload())

    provider = make_gemini(handler=handler, model="models/gemini-2.5-pro")
    response = run(provider.generate(GenerationRequest(prompt="hi", system="be brief")))

    assert "models/gemini-2.5-pro:generateContent" in captured["url"]
    assert captured["key_header"] == "test-gemini-key-123"
    assert captured["auth_header"] is None  # no bearer auth on Gemini
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "hi"
    assert captured["body"]["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert response.text == "Hello! How can I help?"
    assert response.model_used == "models/gemini-2.5-pro"
    assert response.fallback_used is False


def test_gemini_parses_usage_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=gemini_ok_payload())

    provider = make_gemini(handler=handler)
    response = run(provider.generate(GenerationRequest(prompt="hi")))

    assert response.token_usage == TokenUsage(
        prompt_tokens=21, completion_tokens=9, total_tokens=30
    )


def test_gemini_without_usage_metadata_returns_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=gemini_ok_payload(with_usage=False))

    provider = make_gemini(handler=handler)
    response = run(provider.generate(GenerationRequest(prompt="hi")))
    assert response.token_usage is None


# --- GeminiProvider: error mapping -------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (401, LLMAuthError),
        (429, LLMRateLimitedError),
        (500, LLMServerError),
        (503, LLMServerError),
    ],
)
def test_gemini_maps_http_errors(status: int, expected_type: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "boom"}})

    provider = make_gemini(handler=handler)
    with pytest.raises(expected_type):
        run(provider.generate(GenerationRequest(prompt="hi")))


def test_gemini_timeout_maps_to_typed_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    provider = make_gemini(handler=handler)
    with pytest.raises(LLMTimeoutError):
        run(provider.generate(GenerationRequest(prompt="hi")))


def test_gemini_blocked_content_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )

    provider = make_gemini(handler=handler)
    with pytest.raises(LLMResponseError):
        run(provider.generate(GenerationRequest(prompt="hi")))


def test_gemini_empty_completion_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": ""}]}}]})

    provider = make_gemini(handler=handler)
    with pytest.raises(LLMResponseError):
        run(provider.generate(GenerationRequest(prompt="hi")))


def test_gemini_error_messages_never_contain_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = make_gemini(handler=handler)
    with pytest.raises(LLMError) as excinfo:
        run(provider.generate(GenerationRequest(prompt="hi")))
    assert "test-gemini-key-123" not in str(excinfo.value)


# --- NemotronProvider ---------------------------------------------------------


def test_nemotron_sends_openai_shape_and_bearer_header() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=openai_ok_payload())

    provider = make_nemotron(handler=handler, model="nvidia/llama-3.1-nemotron-70b-instruct")
    response = run(provider.generate(GenerationRequest(prompt="hi", system="be brief")))

    assert captured["url"].startswith("https://integrate.api.nvidia.com/v1/chat/completions")
    assert captured["auth"] == "Bearer nvapi-test-fallback-key-456"
    assert captured["body"]["model"] == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert response.text == "Fallback answer"
    assert response.model_used == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert response.token_usage == TokenUsage(
        prompt_tokens=11, completion_tokens=5, total_tokens=16
    )


def test_nemotron_maps_rate_limit_and_server_errors() -> None:
    def handler_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "slow down"})

    provider = make_nemotron(handler=handler_429)
    with pytest.raises(LLMRateLimitedError):
        run(provider.generate(GenerationRequest(prompt="hi")))

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    provider2 = make_nemotron(handler=handler_500)
    with pytest.raises(LLMServerError):
        run(provider2.generate(GenerationRequest(prompt="hi")))


def test_nemotron_error_never_contains_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    provider = make_nemotron(handler=handler)
    with pytest.raises(LLMError) as excinfo:
        run(provider.generate(GenerationRequest(prompt="hi")))
    assert "nvapi-test-fallback-key-456" not in str(excinfo.value)


def test_providers_require_configured_keys() -> None:
    with pytest.raises(LLMConfigError):
        GeminiProvider(GeminiSettings(_env_file=None, GEMINI_API_KEY="", GEMINI_MODEL="models/m"))
    with pytest.raises(LLMConfigError):
        NemotronProvider(FallbackLlmSettings(_env_file=None, FALLBACK_LLM_API_KEY=""))


# --- UnconfiguredProvider ------------------------------------------------------


def test_unconfigured_provider_fails_fast_with_config_error() -> None:
    provider = UnconfiguredProvider()
    with pytest.raises(LLMConfigError):
        run(provider.generate(GenerationRequest(prompt="hi")))


# --- LLMGateway: failover -------------------------------------------------------


class FakeProvider:
    """Deterministic LLMProvider fake (no I/O) for gateway behavior tests."""

    def __init__(
        self,
        name: str,
        *,
        response: GenerationResponse | None = None,
        error: LLMError | None = None,
    ) -> None:
        self._name = name
        self._response = response
        self._error = error
        self.calls: list[GenerationRequest] = []

    @property
    def model_name(self) -> str:
        return self._name

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        return None


def make_response(
    text: str = "ok", model: str = "fake-model", *, usage: TokenUsage | None = None
) -> GenerationResponse:
    return GenerationResponse(text=text, model_used=model, token_usage=usage)


def test_gateway_primary_success_skips_fallback() -> None:
    primary = FakeProvider("gemini-primary", response=make_response("primary text", "models/g"))
    fallback = FakeProvider("nemotron-fallback", response=make_response("fallback text", "nv"))
    gateway = LLMGateway(primary, fallback)  # type: ignore[arg-type]

    result = run(gateway.generate(GenerationRequest(prompt="hi")))

    assert result.text == "primary text"
    assert result.model_used == "models/g"
    assert result.fallback_used is False
    assert fallback.calls == []


def test_gateway_falls_back_when_primary_fails_and_attributes_it() -> None:
    primary = FakeProvider("gemini-primary", error=LLMRateLimitedError("429"))
    fallback = FakeProvider("nemotron-fallback", response=make_response("fallback text", "nv/m"))
    gateway = LLMGateway(primary, fallback)  # type: ignore[arg-type]

    result = run(gateway.generate(GenerationRequest(prompt="hi")))

    assert result.text == "fallback text"
    assert result.model_used == "nv/m"
    assert result.fallback_used is True  # gateway owns the flag
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    assert fallback.calls[0].prompt == primary.calls[0].prompt  # same request retried


def test_gateway_raises_aggregate_error_when_both_fail() -> None:
    primary = FakeProvider("gemini-primary", error=LLMTimeoutError("slow"))
    fallback = FakeProvider("nemotron-fallback", error=LLMServerError("5xx"))
    gateway = LLMGateway(primary, fallback)  # type: ignore[arg-type]

    with pytest.raises(LLMAllProvidersFailedError) as excinfo:
        run(gateway.generate(GenerationRequest(prompt="hi")))

    assert excinfo.value.codes == ["timeout", "upstream_error"]
    assert "nvapi" not in str(excinfo.value)
    assert "test-gemini-key" not in str(excinfo.value)


def test_gateway_without_fallback_raises_immediately() -> None:
    primary = FakeProvider("gemini-primary", error=LLMRateLimitedError("429"))
    gateway = LLMGateway(primary)  # type: ignore[arg-type]

    with pytest.raises(LLMAllProvidersFailedError) as excinfo:
        run(gateway.generate(GenerationRequest(prompt="hi")))
    assert excinfo.value.codes == ["rate_limited"]


def test_gateway_satisfies_provider_protocol_shape() -> None:
    """Providers and the gateway expose the protocol surface used by compose."""

    class _ProtoCheck:
        pass

    # The gateway duck-types LLMProvider (generate/aclose/model_name).
    gateway = LLMGateway(FakeProvider("p", response=make_response()))  # type: ignore[arg-type]
    assert callable(gateway.generate)
    assert callable(gateway.aclose)
    assert LLMProvider is not None  # protocol imported for typing seams
