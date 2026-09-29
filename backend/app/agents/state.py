"""Typed LangGraph state for the sales agent (audit §K).

Minimal but forward-compatible: designed to eventually carry
conversation/thread identity, detected intent, tool results, structured tool
errors, the composed reply, and observability metadata. Tool failures become
*state* (audit §E/§K) — a failing tool never crashes the turn and is never
silently converted into an empty result.

Two parallel collections are kept on purpose:
- ``tool_results``: successful structured results (tool name → payload);
- ``tool_errors``: ``ToolFailure`` entries (tool name → failure), so the
  composer can mention problems while still producing a reply.

LLM attribution (``model_used``/``fallback_used``/``token_usage``) is set by
the composer from the gateway response so persistence can record it.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Deterministic intents recognized by the classifier node (no LLM yet).
IntentName = Literal[
    "product_search",
    "product_details",
    "category_browse",
    "inventory_check",
    "inventory_movements",
    "unknown",
]

# Stable failure codes mirrored from ``app.tools.errors`` for graph-state use.
ToolErrorCode = Literal[
    "not_found",
    "invalid_input",
    "unavailable",
    "timeout",
    "rate_limited",
    "auth",
    "upstream_error",
    "unexpected_response",
    "unexpected_error",
]


class ToolFailure(BaseModel):
    """Structured tool failure carried in graph state (never an exception)."""

    model_config = ConfigDict(frozen=True)

    tool: str
    code: ToolErrorCode
    message: str
    started_at: datetime | None = None


class AgentState(BaseModel):
    """Graph state flowing through every node.

    Reducers: ``tool_results``/``tool_errors`` accumulate across nodes via
    dict-merge instead of overwrite, so several tools could contribute in one
    turn later without losing earlier data.
    """

    # Identity (thread id will equal conversation id — audit §I/§K).
    conversation_id: str | None = None
    thread_id: str | None = None

    # Turn input.
    customer_message: str = ""

    # Classification output.
    intent: IntentName = "unknown"

    # Gathered context (reduced: merged across nodes).
    tool_results: Annotated[dict[str, Any], operator.or_] = Field(default_factory=dict)
    tool_errors: Annotated[dict[str, ToolFailure], operator.or_] = Field(default_factory=dict)

    # Composition output.
    draft_response: str = ""

    # LLM attribution (filled by compose_reply via the LLM gateway; stays None
    # when the deterministic safety net produced the reply).
    model_used: str | None = None
    fallback_used: bool = False
    token_usage: dict[str, int | None] | None = None

    # Observability metadata (bounded; no secrets, no raw customer PII beyond
    # the message itself which lives above).
    metadata: dict[str, Any] = Field(default_factory=dict)
