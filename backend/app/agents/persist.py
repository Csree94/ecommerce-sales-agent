"""Persistence node: records one completed agent turn (audit §I/§L).

Uses ONLY the existing application database infrastructure: the ``Database``
holder is injected into the graph (same pattern as the Inventra client) and
the node opens one short-lived session — commit on success, rollback on
failure, close always. No engine is ever created here, and no Inventra data
is stored (product/inventory results live in the ``agent_runs.tool_calls``
JSONB as turn evidence only).

Fail-safe by design: the customer-facing reply is already composed when this
node runs, so a persistence problem must not crash the turn — it is recorded
in ``metadata`` (``persisted: false`` + reason) and logged. Nothing is
invented: a turn whose ``conversation_id`` is missing/invalid or does not
match an existing Conversation row is simply not persisted.

What is stored per turn (one transaction):
- ``AgentRun``: status/timing/latency + ``tool_calls`` JSONB evidence
  (intent, result keys, structured tool errors). ``model_used``,
  ``fallback_used`` and ``token_usage`` come from the LLM attribution in
  state (set by compose via the gateway); unset when the deterministic
  safety net produced the reply.
- ``Message(role=customer)``: the customer's text.
- ``Message(role=agent)``: the composed reply + ``content_metadata``
  (intent, tool error codes).
- Both messages share one ``correlation_id`` (one agent turn).
- ``Conversation.last_message_at`` is bumped for dashboard ordering.

Secrets: nothing here touches credentials — the client's auth lives in
headers only (never in results/state/errors, test-enforced upstream), so the
persisted payload contains turn data exclusively.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.agents.state import AgentState
from app.core.logging import get_logger
from app.db.session import Database
from app.models import AgentRun, AgentRunStatus, Conversation, Message, MessageRole

logger = get_logger(__name__)


def _to_uuid(raw: str | None) -> uuid.UUID | None:
    """Convert the state's string conversation id to UUID (None if invalid)."""
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


def _turn_started_at(state: AgentState) -> datetime | None:
    """Recover the turn start time from metadata (set by ``run_turn``)."""
    started = state.metadata.get("turn_started_at")
    if isinstance(started, (int, float)):
        return datetime.fromtimestamp(started, tz=UTC)
    return None


def persist_turn(state: AgentState, database: Database | None) -> dict[str, Any]:
    """Node body: persist the turn; returns a ``metadata`` update, never raises."""
    metadata = dict(state.metadata)  # preserve keys like ``turn_started_at``

    if database is None:
        metadata.update({"persisted": False, "persist_reason": "no_database"})
        return {"metadata": metadata}

    conversation_id = _to_uuid(state.conversation_id)
    if conversation_id is None:
        metadata.update({"persisted": False, "persist_reason": "invalid_conversation_id"})
        return {"metadata": metadata}

    session = database.session()
    try:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            # Fail safely: never invent a Conversation (audit §I ownership).
            session.rollback()
            metadata.update({"persisted": False, "persist_reason": "conversation_not_found"})
            return {"metadata": metadata}

        now = datetime.now(UTC)
        started_at = _turn_started_at(state) or now
        correlation_id = uuid.uuid4()
        tool_errors = [
            {"tool": failure.tool, "code": failure.code, "message": failure.message}
            for failure in state.tool_errors.values()
        ]

        agent_run = AgentRun(
            conversation_id=conversation_id,
            # The turn itself completed with a reply; tool problems are
            # recorded as evidence below, not as a run-level failure.
            status=AgentRunStatus.SUCCEEDED,
            started_at=started_at,
            finished_at=now,
            # LLM attribution from state (compose/gateway); None/False when
            # the deterministic safety net produced the reply.
            model_used=state.model_used,
            fallback_used=state.fallback_used,
            tool_calls={
                "intent": state.intent,
                "tool_result_keys": sorted(state.tool_results),
                "tool_errors": tool_errors,
            },
            token_usage=state.token_usage,
            error=None,
            latency_ms=_latency_ms(state, now),
        )
        customer_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.CUSTOMER,
            content_text=state.customer_message,
            correlation_id=correlation_id,
        )
        agent_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.AGENT,
            content_text=state.draft_response,
            correlation_id=correlation_id,
            content_metadata={
                "intent": state.intent,
                "tool_error_codes": sorted({code for code in _error_codes(state)}),
            },
        )

        conversation.last_message_at = now  # dashboard ordering (audit §I)

        session.add_all([agent_run, customer_message, agent_message])
        session.commit()

        metadata.update(
            {
                "persisted": True,
                "agent_run_id": str(agent_run.id),
                "correlation_id": str(correlation_id),
            }
        )
        return {"metadata": metadata}
    except Exception:  # noqa: BLE001 — persistence must not crash the turn
        session.rollback()
        logger.exception("agent_turn_persist_failed")
        metadata.update({"persisted": False, "persist_reason": "persistence_error"})
        return {"metadata": metadata}
    finally:
        session.close()


def _error_codes(state: AgentState) -> list[str]:
    return [failure.code for failure in state.tool_errors.values()]


def _latency_ms(state: AgentState, finished_at: datetime) -> int | None:
    """Wall-clock latency when the turn start time is known, else None."""
    started_at = _turn_started_at(state)
    if started_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))
