"""Tests for the LangGraph persist node.

No live database: sessions are mocked at the existing ``Database`` boundary
(``database.session()``), matching the project's no-real-infrastructure test
convention. Coverage: successful turn persistence (AgentRun + customer/agent
Messages in one transaction, shared correlation_id, last_message_at bump),
persistence when a tool fails but the graph still replies, correct
associations, LLM attribution recording (model_used/fallback_used/token_usage
from state), fail-safe paths (no database, invalid/unknown conversation),
persistence errors rolling back, graph completion, and no secrets stored.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from app.agents import AgentState, ToolFailure
from app.models import AgentRun, AgentRunStatus, Conversation, Message, MessageRole


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_database(conversation: Conversation | None) -> tuple[MagicMock, MagicMock]:
    """Database mock whose session.get returns the given conversation."""
    database = MagicMock()
    session = MagicMock()
    session.get.return_value = conversation
    database.session.return_value = session
    return database, session


def make_conversation() -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        last_message_at=None,
    )


def make_state(**overrides: Any) -> AgentState:
    values: dict[str, Any] = {
        "conversation_id": str(uuid.uuid4()),
        "customer_message": "I'm looking for hiking shoes",
        "intent": "product_search",
        "tool_results": {
            "products": {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 1}
        },
        "draft_response": "Here's what I found.",
        "model_used": "models/gemini-2.5-flash",
        "fallback_used": False,
        "token_usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
        "metadata": {"turn_started_at": datetime.now(UTC).timestamp() - 1.5},
    }
    values.update(overrides)
    return AgentState(**values)


def persist(state: AgentState, database: MagicMock) -> dict[str, Any]:
    from app.agents.persist import persist_turn

    return persist_turn(state, database)


# --- Successful persistence -----------------------------------------------


def test_successful_turn_persists_agent_run_and_two_messages() -> None:
    conversation = make_conversation()
    database, session = make_database(conversation)

    result = persist(make_state(), database)

    assert result["metadata"]["persisted"] is True
    added = session.add_all.call_args[0][0]
    assert len(added) == 3
    agent_run, customer_msg, agent_msg = added
    assert isinstance(agent_run, AgentRun)
    assert isinstance(customer_msg, Message)
    assert isinstance(agent_msg, Message)
    session.commit.assert_called_once()
    session.close.assert_called_once()


def test_agent_run_records_status_timing_and_tool_evidence() -> None:
    database, session = make_database(make_conversation())

    persist(make_state(), database)

    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.status == AgentRunStatus.SUCCEEDED
    assert agent_run.started_at is not None
    assert agent_run.finished_at is not None
    # LLM attribution comes from state now (set by compose via the gateway).
    assert agent_run.model_used == "models/gemini-2.5-flash"
    assert agent_run.fallback_used is False
    assert agent_run.token_usage == {
        "prompt_tokens": 42,
        "completion_tokens": 17,
        "total_tokens": 59,
    }
    assert agent_run.error is None
    assert agent_run.latency_ms is not None and agent_run.latency_ms >= 0
    assert agent_run.tool_calls["intent"] == "product_search"
    assert "products" in agent_run.tool_calls["tool_result_keys"]
    assert agent_run.tool_calls["tool_errors"] == []


def test_agent_run_attribution_reflects_fallback_and_deterministic_paths() -> None:
    database, session = make_database(make_conversation())
    persist(
        make_state(model_used="nvidia/llama-3.1-nemotron-70b-instruct", fallback_used=True),
        database,
    )
    agent_run = session.add_all.call_args[0][0][0]
    assert agent_run.model_used == "nvidia/llama-3.1-nemotron-70b-instruct"
    assert agent_run.fallback_used is True

    # Deterministic safety net: attribution unset.
    database2, session2 = make_database(make_conversation())
    persist(make_state(model_used=None, fallback_used=False, token_usage=None), database2)
    agent_run2 = session2.add_all.call_args[0][0][0]
    assert agent_run2.model_used is None
    assert agent_run2.fallback_used is False
    assert agent_run2.token_usage is None


def test_customer_and_agent_messages_associated_correctly() -> None:
    state = make_state()
    database, session = make_database(make_conversation())

    persist(state, database)

    _, customer_msg, agent_msg = session.add_all.call_args[0][0]
    assert customer_msg.role == MessageRole.CUSTOMER
    assert customer_msg.content_text == "I'm looking for hiking shoes"
    assert agent_msg.role == MessageRole.AGENT
    assert agent_msg.content_text == "Here's what I found."
    assert agent_msg.content_metadata["intent"] == "product_search"


def test_messages_share_one_correlation_id_per_turn() -> None:
    database, session = make_database(make_conversation())

    persist(make_state(), database)

    _, customer_msg, agent_msg = session.add_all.call_args[0][0]
    assert customer_msg.correlation_id is not None
    assert customer_msg.correlation_id == agent_msg.correlation_id


def test_last_message_at_is_bumped_on_conversation() -> None:
    conversation = make_conversation()
    before = conversation.last_message_at
    database, session = make_database(conversation)

    persist(make_state(), database)

    assert conversation.last_message_at is not None
    assert conversation.last_message_at != before


def test_conversation_id_from_state_is_used() -> None:
    conversation = make_conversation()
    database, session = make_database(conversation)

    persist(make_state(conversation_id=str(conversation.id)), database)

    session.get.assert_called_once_with(Conversation, conversation.id)


# --- Tool-failure turn (graph still replies and persists) -------------------


def test_tool_failure_turn_still_persists_with_error_evidence() -> None:
    database, session = make_database(make_conversation())
    state = make_state(
        intent="inventory_check",
        tool_results={},
        tool_errors={
            "gather_context": ToolFailure(
                tool="gather_context", code="rate_limited", message="Inventra rate limit hit"
            )
        },
        draft_response="Sorry — I can't reach the catalog right now (rate_limited).",
    )

    result = persist(state, database)

    assert result["metadata"]["persisted"] is True
    agent_run, _, agent_msg = session.add_all.call_args[0][0]
    # The turn itself completed with a reply; the failure is evidence, not a crash.
    assert agent_run.status == AgentRunStatus.SUCCEEDED
    assert agent_run.tool_calls["tool_errors"] == [
        {"tool": "gather_context", "code": "rate_limited", "message": "Inventra rate limit hit"}
    ]
    assert "rate_limited" in agent_msg.content_metadata["tool_error_codes"]


# --- Fail-safe paths --------------------------------------------------------


def test_missing_database_skips_persistence_cleanly() -> None:
    result = persist(make_state(), None)

    assert result["metadata"]["persisted"] is False
    assert result["metadata"]["persist_reason"] == "no_database"


def test_invalid_conversation_id_skips_persistence() -> None:
    database, session = make_database(make_conversation())

    result = persist(make_state(conversation_id="not-a-uuid"), database)

    assert result["metadata"]["persisted"] is False
    assert result["metadata"]["persist_reason"] == "invalid_conversation_id"
    session.get.assert_not_called()
    session.add_all.assert_not_called()


def test_unknown_conversation_is_never_invented() -> None:
    database, session = make_database(None)  # no Conversation row exists

    result = persist(make_state(), database)

    assert result["metadata"]["persisted"] is False
    assert result["metadata"]["persist_reason"] == "conversation_not_found"
    session.add_all.assert_not_called()
    session.commit.assert_not_called()
    session.rollback.assert_called_once()


def test_persistence_error_rolls_back_and_never_raises() -> None:
    database, session = make_database(make_conversation())
    session.commit.side_effect = RuntimeError("db down")

    result = persist(make_state(), database)

    assert result["metadata"]["persisted"] is False
    assert result["metadata"]["persist_reason"] == "persistence_error"
    session.rollback.assert_called_once()
    session.close.assert_called_once()


# --- Graph integration ------------------------------------------------------


def test_graph_completes_and_reports_persisted_metadata() -> None:
    from app.agents.graph import build_graph

    conversation = make_conversation()
    database, session = make_database(conversation)
    client = MagicMock()
    from app.integrations.inventra.schemas import ProductListResponse

    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))

    graph = build_graph(client, database)
    final = run(
        graph.ainvoke(
            make_state(),
            config={"configurable": {"thread_id": "conv-x"}},
        )
    )

    assert final["metadata"]["persisted"] is True
    session.commit.assert_called_once()


def test_run_turn_without_database_skips_persistence() -> None:
    from app.agents.graph import run_turn as _run_turn

    state = run(
        _run_turn(
            MagicMock(),  # client never called for unknown intent below
            customer_message="hello there",
            conversation_id=None,
        )
    )

    assert state.metadata.get("persisted") is False
    assert state.draft_response  # graph still completes


def test_no_credentials_or_secrets_are_persisted() -> None:
    database, session = make_database(make_conversation())
    state = make_state(
        metadata={
            "turn_started_at": datetime.now(UTC).timestamp(),
            "bearer_token": "should-never-be-here",
        }
    )

    persist(state, database)

    added = session.add_all.call_args[0][0]
    agent_run, customer_msg, agent_msg = added
    import json

    def _no_secrets(obj: Any) -> bool:
        payload = json.dumps(obj, default=str).lower()
        return "bearer" not in payload and "should-never-be-here" not in payload

    assert _no_secrets(agent_run.tool_calls)
    assert _no_secrets(customer_msg.content_metadata)
    assert _no_secrets(agent_msg.content_metadata)
    # metadata snapshot is never written to any row:
    for row in added:
        assert "bearer_token" not in json.dumps(getattr(row, "__dict__", {}), default=str).lower()


# --- helpers -----------------------------------------------------------------


def make_async(value: Any) -> Any:
    async def _inner() -> Any:
        return value

    return _inner()


PRODUCT_LIST = {
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
    "per_page": 20,
    "pages": 1,
}
