"""Context gathering node: dispatches intents to existing read-only tools.

This node is the architectural proof of the required dependency direction:
graph → tools → InventraClient → HTTP. The graph itself contains no HTTP, no
URLs, no auth, and no database code — it only calls the typed tools from
``app.tools.inventra`` and records structured outcomes in state. Tool failures
(ToolError) become structured state (ToolFailure), never exceptions and never
silent empty results.
"""

from __future__ import annotations

from typing import Any, cast

from app.agents.state import AgentState, ToolErrorCode, ToolFailure
from app.tools.errors import ToolError
from app.tools.inventra import (
    InventoryMovementParams,
    InventorySearchParams,
    ProductSearchParams,
    check_inventory,
    get_inventory_movements,
    get_product_details,
    list_categories,
    search_products,
)

# ToolFailure.code valid values (mirrored from the tool error hierarchy).
_VALID_CODES = (
    "not_found",
    "invalid_input",
    "unavailable",
    "timeout",
    "rate_limited",
    "auth",
    "upstream_error",
    "unexpected_response",
    "unexpected_error",
)


def _failure(tool: str, exc: ToolError) -> dict[str, Any]:
    """Build the state update for a failed tool call (structured, not raised)."""
    code = _coerce_code(exc.code)
    return {"tool_errors": {tool: ToolFailure(tool=tool, code=code, message=str(exc))}}


def _coerce_code(raw: str) -> ToolErrorCode:
    """Map an arbitrary tool-error code onto the graph-state code vocabulary."""
    if raw in _VALID_CODES:
        return cast(ToolErrorCode, raw)
    return "unexpected_error"


def _extract_id(message: str) -> int | None:
    """Temporary heuristic: last integer token in the message is an entity id.

    Replaced by proper entity extraction when the LLM gateway arrives.
    """
    digits = [tok for tok in message.split() if tok.isdigit()]
    return int(digits[-1]) if digits else None


async def gather_context(state: AgentState, client: Any) -> dict[str, Any]:
    """Node: call the tool matching the classified intent.

    ``client`` is the injected ``InventraClient`` (typed loosely here only to
    avoid a module-scope import cycle; tests inject a mock at this seam).
    """
    intent = state.intent
    message = state.customer_message

    try:
        if intent == "product_search":
            result = await search_products(
                client,
                ProductSearchParams(search=message.strip() or None, per_page=5),
            )
            return {"tool_results": {"products": result.model_dump(mode="json")}}
        if intent == "product_details":
            product_id = _extract_id(message)
            if product_id is None:
                return _failure(
                    "gather_context",
                    ToolError("No product id found in message", code="invalid_input"),
                )
            product = await get_product_details(client, product_id)
            return {"tool_results": {"product": product.model_dump(mode="json")}}
        if intent == "category_browse":
            categories = await list_categories(client)
            return {"tool_results": {"categories": [c.model_dump(mode="json") for c in categories]}}
        if intent == "inventory_check":
            items = await check_inventory(
                client,
                InventorySearchParams(search=message.strip() or None),
            )
            return {"tool_results": {"inventory": [i.model_dump(mode="json") for i in items]}}
        if intent == "inventory_movements":
            product_id = _extract_id(message)
            history = await get_inventory_movements(
                client, InventoryMovementParams(product_id=product_id)
            )
            return {"tool_results": {"movements": history.model_dump(mode="json")}}
        return {"tool_results": {}}  # unknown intent: nothing to gather
    except ToolError as exc:
        return _failure("gather_context", exc)
