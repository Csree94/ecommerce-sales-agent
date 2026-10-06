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
    "purchase",
    "unknown",
]

# Outcome of the purchase node (milestone 3B, revised by Step 3C: confirmation
# flow). ``not_requested`` is the steady state for every non-purchase turn;
# ``skipped_*`` values mean the flow bailed out BEFORE any write (no deduction
# ever happened); ``failed_*" values mean the write itself did not succeed
# (never presented as success).
#
# Business rule (Step 3C): an order request NEVER deducts stock — it stores a
# pending confirmation. Only the customer's explicit confirmation (a LATER
# message) triggers the single stock-out; a decline cancels the pending
# purchase without any write.
PurchaseStatus = Literal[
    "not_requested",
    "pending_confirmation",
    "cancelled_by_customer",
    "skipped_product_unresolved",
    "skipped_insufficient_stock",
    "skipped_invalid_quantity",
    "completed",
    "failed_unavailable",
    "failed_auth",
    "failed_unexpected",
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


class PendingPurchase(BaseModel):
    """A purchase awaiting the customer's explicit confirmation (Step 3C).

    Stored as turn evidence on the AGENT message's ``content_metadata``
    (JSONB) under ``pending_purchase`` — no schema change, no new table. The
    confirm/decline branches reload it from the conversation's most recent
    agent message so a pending purchase survives across turns exactly like
    every other piece of conversation evidence.
    """

    model_config = ConfigDict(frozen=True)

    product_id: int
    product_name: str
    quantity: int
    unit_price: float


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

    # Purchase flow (milestone 3B — set by the purchase node between gather
    # and compose; untouched for every non-purchase turn).
    purchase_requested: bool = False
    purchase_quantity: int | None = None
    purchase_status: PurchaseStatus = "not_requested"
    # Customer-facing outcome summary for the composer (product name,
    # remaining stock, or the exact reason no deduction happened).
    purchase_message: str | None = None
    # A purchase awaiting confirmation (Step 3C). Set by the purchase node on
    # an order request; persisted with the turn; consumed (never re-persisted)
    # when the customer confirms or declines.
    pending_purchase: PendingPurchase | None = None

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
