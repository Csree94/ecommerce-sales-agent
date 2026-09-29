"""Tests for compose_reply via the LLM gateway + persistence attribution.

Uses a deterministic fake gateway at the ``LLMGateway`` seam (never a real
provider call). Coverage:

- compose uses the gateway and records model attribution in state
- fallback attribution flows through compose and persistence
- token usage flows through compose and persistence
- all-providers-fail → deterministic safety net, attribution unset
- prompt hygiene: intent + tool results included; internal metadata,
  credentials excluded; guardrail that compose/classify stay provider-free
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from app.agents.compose import build_prompt, compose_reply
from app.agents.state import AgentState, ToolFailure
from app.integrations.llm.errors import LLMError
from app.integrations.llm.schemas import GenerationRequest, GenerationResponse, TokenUsage


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeGateway:
    """Scriptable LLMGateway fake: canned responses or errors per turn."""

    def __init__(
        self,
        *,
        response: GenerationResponse | None = None,
        error: LLMError | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        return None


def make_state(**overrides: Any) -> AgentState:
    values: dict[str, Any] = {
        "conversation_id": str(uuid.uuid4()),
        "customer_message": "I'm looking for hiking shoes",
        "intent": "product_search",
        "tool_results": {
            "products": {
                "products": [
                    {
                        "id": 7,
                        "name": "Trail Shoes",
                        "sku": "TS-001",
                        "description": "Waterproof hiking shoes",
                        "category_id": 3,
                        "category_name": "Footwear",
                        "price": 129.99,
                        "is_active": True,
                        "created_at": "2026-01-15T10:00:00Z",
                        "updated_at": "2026-02-01T12:30:00Z",
                    }
                ],
                "total": 1,
                "page": 1,
                "per_page": 5,
                "pages": 1,
            }
        },
        "metadata": {"turn_started_at": datetime.now(UTC).timestamp() - 1.5},
    }
    values.update(overrides)
    return AgentState(**values)


def make_response(
    text: str = "Great news — the Trail Shoes are available!",
    model: str = "models/gemini-2.5-flash",
    *,
    fallback_used: bool = False,
    usage: TokenUsage | None = None,
) -> GenerationResponse:
    return GenerationResponse(
        text=text, model_used=model, fallback_used=fallback_used, token_usage=usage
    )


# --- compose_reply via the gateway -------------------------------------------


def test_compose_uses_gateway_and_records_attribution() -> None:
    gateway = FakeGateway(response=make_response(model="models/gemini-2.5-flash"))
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]

    assert result["draft_response"] == "Great news — the Trail Shoes are available!"
    assert result["model_used"] == "models/gemini-2.5-flash"
    assert result["fallback_used"] is False
    assert len(gateway.requests) == 1


def test_compose_records_token_usage_when_available() -> None:
    usage = TokenUsage(prompt_tokens=42, completion_tokens=17, total_tokens=59)
    gateway = FakeGateway(response=make_response(usage=usage))
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]

    assert result["token_usage"] == {
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
    }


def test_compose_without_token_usage_sets_none() -> None:
    gateway = FakeGateway(response=make_response(usage=None))
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]
    assert result["token_usage"] is None


def test_compose_attribution_from_fallback_response() -> None:
    gateway = FakeGateway(
        response=make_response(model="nvidia/llama-3.1-nemotron-70b-instruct", fallback_used=True)
    )
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]

    assert result["model_used"] == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert result["fallback_used"] is True


def test_compose_degrades_deterministically_when_all_providers_fail() -> None:
    gateway = FakeGateway(error=LLMError("all failed"))
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]

    assert "Trail Shoes" in result["draft_response"]  # deterministic template
    assert "model_used" not in result
    assert "fallback_used" not in result
    assert "token_usage" not in result


def test_compose_failure_response_shape_matches_defaults() -> None:
    gateway = FakeGateway(error=LLMError("all failed"))
    state = make_state()

    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]

    merged = AgentState.model_validate({**state.model_dump(), **result})
    assert merged.model_used is None
    assert merged.fallback_used is False
    assert merged.token_usage is None


def test_compose_prompt_contains_message_intent_and_results() -> None:
    gateway = FakeGateway(response=make_response())
    state = make_state()

    run(compose_reply(state, gateway))  # type: ignore[arg-type]

    prompt = gateway.requests[0].prompt
    assert "I'm looking for hiking shoes" in prompt
    assert "product search" in prompt
    assert "Trail Shoes" in prompt
    assert "129.99" in prompt


def test_compose_prompt_includes_tool_failures() -> None:
    gateway = FakeGateway(response=make_response())
    state = make_state(
        tool_results={},
        tool_errors={
            "gather_context": ToolFailure(
                tool="gather_context", code="rate_limited", message="429 too many"
            )
        },
    )

    run(compose_reply(state, gateway))  # type: ignore[arg-type]

    prompt = gateway.requests[0].prompt
    assert "rate_limited" in prompt
    assert "429 too many" in prompt


def test_compose_prompt_excludes_internal_metadata() -> None:
    gateway = FakeGateway(response=make_response())
    state = make_state(
        metadata={
            "turn_started_at": 123.0,
            "persisted": False,
            "agent_run_id": "should-not-appear",
            "correlation_id": "nor-this",
        }
    )

    run(compose_reply(state, gateway))  # type: ignore[arg-type]

    prompt = gateway.requests[0].prompt
    assert "should-not-appear" not in prompt
    assert "nor-this" not in prompt
    assert "turn_started_at" not in prompt
    assert "persisted" not in prompt


def test_build_prompt_is_pure_and_safe() -> None:
    state = make_state(metadata={"agent_run_id": "x", "correlation_id": "y", "persisted": True})
    prompt = build_prompt(state)
    assert "agent_run_id" not in prompt
    assert "correlation_id" not in prompt
    assert '"persisted"' not in prompt


# --- Guardrails: provider isolation -------------------------------------------


def test_compose_and_classify_contain_no_provider_http_logic() -> None:
    """compose/classify must not hold URLs, keys, or provider transports."""
    import app.agents.classify as classify_mod
    import app.agents.compose as compose_mod

    for module in (classify_mod, compose_mod):
        source = inspect.getsource(module)
        assert "httpx" not in source
        assert "generativelanguage" not in source
        assert "integrate.api.nvidia" not in source
        assert "x-goog-api-key" not in source
        assert "Authorization" not in source
        assert "GEMINI_API_KEY" not in source
        assert "sessionmaker" not in source
        assert "create_engine" not in source


def test_compose_signature_takes_gateway_not_provider_details() -> None:
    signature = inspect.signature(compose_reply)
    params = list(signature.parameters)
    assert params == ["state", "gateway"]


# --- Persistence records real attribution --------------------------------------


def make_database(conversation: Any) -> tuple[MagicMock, MagicMock]:
    database = MagicMock()
    session = MagicMock()
    session.get.return_value = conversation
    database.session.return_value = session
    return database, session


def make_conversation() -> Any:
    from app.models import Conversation

    return Conversation(id=uuid.uuid4(), customer_id=uuid.uuid4(), last_message_at=None)


def test_persistence_records_model_attribution_and_token_usage() -> None:
    from app.agents.persist import persist_turn

    state = make_state(
        draft_response="LLM reply",
        model_used="models/gemini-2.5-flash",
        fallback_used=False,
        token_usage={"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
    )
    database, session = make_database(make_conversation())

    persist_turn(state, database)  # type: ignore[arg-type]

    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.model_used == "models/gemini-2.5-flash"
    assert agent_run.fallback_used is False
    assert agent_run.token_usage == {
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
    }


def test_persistence_records_fallback_usage() -> None:
    from app.agents.persist import persist_turn

    state = make_state(
        draft_response="Fallback reply",
        model_used="nvidia/llama-3.1-nemotron-70b-instruct",
        fallback_used=True,
        token_usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )
    database, session = make_database(make_conversation())

    persist_turn(state, database)  # type: ignore[arg-type]

    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.model_used == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert agent_run.fallback_used is True


def test_persistence_leaves_attribution_unset_for_deterministic_reply() -> None:
    from app.agents.persist import persist_turn

    state = make_state(draft_response="template reply")
    database, session = make_database(make_conversation())

    persist_turn(state, database)  # type: ignore[arg-type]

    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.model_used is None
    assert agent_run.fallback_used is False
    assert agent_run.token_usage is None


# --- Full graph integration ------------------------------------------------------


def test_full_graph_completes_with_gateway_and_persists_attribution() -> None:
    from unittest.mock import MagicMock

    from app.agents.graph import build_graph
    from app.integrations.inventra.schemas import Category

    category = {
        "id": 3,
        "name": "Footwear",
        "description": "Shoes and boots",
        "created_at": "2026-01-10T09:00:00Z",
        "updated_at": "2026-01-10T09:00:00Z",
    }
    client = MagicMock()
    client.list_categories.return_value = _async([Category.model_validate(category)])

    conversation = make_conversation()
    database, session = make_database(conversation)
    gateway = FakeGateway(response=make_response(text="We offer Footwear!"))

    graph = build_graph(client, database, gateway)  # type: ignore[arg-type]
    final = run(
        graph.ainvoke(
            AgentState(
                customer_message="what categories do you have",
                conversation_id=str(conversation.id),
            ),
            config={"configurable": {"thread_id": "conv-llm"}},
        )
    )

    assert final["draft_response"] == "We offer Footwear!"
    assert final["model_used"] == "models/gemini-2.5-flash"
    assert final["fallback_used"] is False
    session.commit.assert_called_once()
    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.model_used == "models/gemini-2.5-flash"


def _async(value: Any) -> Any:
    async def _inner() -> Any:
        return value

    return _inner()


def test_run_turn_uses_injected_gateway() -> None:
    from app.agents.graph import run_turn

    gateway = FakeGateway(response=make_response(text="Hi there!"))
    client = MagicMock()

    state = run(
        run_turn(
            client,
            customer_message="hello there",
            conversation_id="conv-rt",
            gateway=gateway,  # type: ignore[arg-type]
        )
    )

    assert isinstance(state, AgentState)
    assert state.draft_response == "Hi there!"
    assert state.model_used == "models/gemini-2.5-flash"
    assert state.fallback_used is False


def test_full_graph_degrades_when_gateway_always_fails() -> None:
    from unittest.mock import MagicMock

    from app.agents.graph import build_graph
    from app.tools.errors import ToolUnavailableError

    client = MagicMock()
    client.list_categories.side_effect = ToolUnavailableError("inventra down", code="unavailable")

    gateway = FakeGateway(error=LLMError("providers down"))

    graph = build_graph(client, None, gateway)  # type: ignore[arg-type]
    final = run(
        graph.ainvoke(
            AgentState(customer_message="what categories do you have"),
            config={"configurable": {"thread_id": "conv-degrade"}},
        )
    )

    assert final["draft_response"]  # deterministic safety net produced a reply
    assert "model_used" not in final or final.get("model_used") is None
    assert final["tool_errors"]["gather_context"].code == "unavailable"
