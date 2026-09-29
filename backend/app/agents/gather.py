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


# Phrases that frame a shopping request but never appear in product names.
# Longest first so multi-word fillers are stripped before their fragments.
_SEARCH_FILLERS = (
    "i'm looking for",
    "im looking for",
    "do you have",
    "do you sell",
    "show me",
    "search for",
    "find me",
    "looking for",
    "recommend",
    "suggest",
    "i want",
    "i need",
    "please",
    "can you",
    "could you",
    "find",
    "search",
)

# Bare function words that carry no product meaning once fillers are gone.
# Tokens containing digits (SKUs like "TS-001", models like "s26") are never dropped.
_SEARCH_STOPWORDS = frozenset(
    {
        "a", "an", "the", "some", "any", "for", "me", "my", "your",
        "is", "are", "in", "on", "do", "does", "you", "i", "we",
        "have", "has", "was", "were", "there", "that", "this", "it",
        "of", "to", "with", "and", "or", "about", "tell", "what",
        "how", "much", "many", "available", "availability", "stock",
    }
)

_PUNCTUATION = ".,!?;:'\"()[]{}—–-"


def _extract_search_term(message: str) -> str | None:
    """Deterministic shopping-phrase extraction (temporary — LLM replaces later).

    Strips known framing phrases and bare function words from a customer
    message, keeping the product-bearing remainder. Digits-containing tokens
    (SKUs/model numbers) are preserved. Returns ``None`` when nothing product-
    bearing remains (caller falls back to the raw message or a bare listing).
    """
    cleaned = message.lower()
    for filler in _SEARCH_FILLERS:
        cleaned = cleaned.replace(filler, " ")
    cleaned = cleaned.strip(_PUNCTUATION + " ")
    tokens = [
        tok.strip(_PUNCTUATION)
        for tok in cleaned.split()
        if tok.strip(_PUNCTUATION)
    ]
    # Keep tokens carrying digits (SKUs/model numbers) unconditionally;
    # drop bare function words; keep every other product-bearing token.
    kept = [
        tok
        for tok in tokens
        if any(ch.isdigit() for ch in tok) or tok not in _SEARCH_STOPWORDS
    ]
    term = " ".join(kept)
    return term or None


def _zero_result_fallback_terms(message: str, term: str | None) -> list[str]:
    """Ordered, bounded fallback terms for a zero-result first attempt.

    1. the original (unmodified) customer message — covers deliberately
       exact queries the extractor may have mangled;
    2. up to two significant individual tokens (longest first) — Inventra
       matches the whole ``search`` value as one substring, so a multi-word
       term like ``"samsung phones"`` finds nothing even though the product
       exists and ``"samsung"`` alone would match it.

    Hard-capped at three entries: never an unbounded retry loop.
    """
    raw = message.strip()
    ladder: list[str] = []
    if raw and raw.lower() != (term or ""):
        ladder.append(raw)
    if term:
        tokens = sorted(
            (tok for tok in term.split() if len(tok) > 2),
            key=len,
            reverse=True,
        )[:2]
        ladder.extend(tok for tok in tokens if tok.lower() not in [x.lower() for x in ladder])
    return ladder


async def gather_context(state: AgentState, client: Any) -> dict[str, Any]:
    """Node: call the tool matching the classified intent.

    ``client`` is the injected ``InventraClient`` (typed loosely here only to
    avoid a module-scope import cycle; tests inject a mock at this seam).
    """
    intent = state.intent
    message = state.customer_message

    try:
        if intent == "product_search":
            raw = message.strip()
            term = _extract_search_term(message)
            first = term or raw or None
            result = await search_products(
                client,
                ProductSearchParams(search=first, per_page=5),
            )
            # Bounded zero-result ladder: raw message, then individual
            # significant tokens ( Inventra matches the whole phrase as one
            # substring ). Never unbounded — see _zero_result_fallback_terms.
            if result.total == 0 and first is not None:
                for fallback in _zero_result_fallback_terms(message, term):
                    if fallback == first:
                        continue
                    result = await search_products(
                        client,
                        ProductSearchParams(search=fallback, per_page=5),
                    )
                    if result.total > 0:
                        break
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
            raw = message.strip()
            term = _extract_search_term(message)
            first = term or raw or None
            items = await check_inventory(
                client,
                InventorySearchParams(search=first),
            )
            if not items and first is not None:
                for fallback in _zero_result_fallback_terms(message, term):
                    if fallback == first:
                        continue
                    items = await check_inventory(
                        client,
                        InventorySearchParams(search=fallback),
                    )
                    if items:
                        break
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
