"""Reply composition node — LLM generation through the gateway (audit §F).

The node builds a provider-agnostic prompt from the gathered context and asks
the injected ``LLMGateway`` to generate the customer-facing reply. Provider
HTTP/SDK details live exclusively in ``app.integrations.llm`` — this module
contains no URLs, no headers, no SDK imports.

Failure philosophy (audit §E/§F): if every configured provider fails, the node
degrades to the previous deterministic template reply and leaves LLM
attribution unset (``model_used`` stays None) so persistence records the
degradation honestly. The turn never crashes.

Prompt hygiene: only what is needed to answer the customer — message, intent,
tool results, structured tool failure codes/messages. Never credentials
(keys/tokens never enter state), never internal metadata (turn_started_at,
persisted, agent_run_id, correlation_id).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.agents.state import AgentState
from app.integrations.llm.errors import LLMError
from app.integrations.llm.schemas import GenerationRequest

if TYPE_CHECKING:
    from app.integrations.llm.gateway import LLMGateway

# Internal metadata keys that must never reach a prompt (defense in depth —
# they are filtered even if future state changes add them).
_EXCLUDED_METADATA_KEYS = frozenset(
    {
        "turn_started_at",
        "persisted",
        "persist_reason",
        "agent_run_id",
        "correlation_id",
    }
)

_SYSTEM_PROMPT = (
    "You are the shopping assistant of an online store. Answer the customer "
    "briefly, accurately and helpfully, using ONLY the provided catalog data. "
    "If the data is missing or a tool failed, say so honestly instead of "
    "inventing products, prices or stock levels. Never mention internal "
    "systems, tools, identifiers or error mechanics to the customer."
)

# Human-readable intent labels, shared by the prompt and the deterministic
# safety-net templates below.
_INTENT_LABELS = {
    "product_search": "product search",
    "product_details": "product details",
    "category_browse": "category listing",
    "inventory_check": "stock check",
    "inventory_movements": "stock movements",
    "purchase": "purchase request",
    "unknown": "request",
}


async def compose_reply(state: AgentState, gateway: LLMGateway) -> dict[str, Any]:
    """Node: generate the reply via the gateway; degrade deterministically.

    ``gateway`` is injected by the graph (tests may inject fakes). Returns the
    state update with ``draft_response`` plus LLM attribution — or the
    deterministic template reply with attribution unset when all providers
    fail.
    """
    request = GenerationRequest(prompt=build_prompt(state), system=_SYSTEM_PROMPT)
    try:
        response = await gateway.generate(request)
    except LLMError:
        # Every provider failed (or the gateway itself failed) — deterministic
        # safety net, attribution unset so persistence records the degradation.
        return {"draft_response": _deterministic_reply(state)}

    return {
        "draft_response": response.text,
        "model_used": response.model_used,
        "fallback_used": response.fallback_used,
        "token_usage": (
            response.token_usage.model_dump() if response.token_usage is not None else None
        ),
    }


def _deterministic_reply(state: AgentState) -> str:
    """Previous-phase template composition (used only when all LLMs fail)."""
    label = _INTENT_LABELS.get(state.intent, "request")
    parts: list[str] = []

    # Purchase flow outcomes take priority: the customer must get an honest,
    # self-contained confirmation or failure — never a generic template.
    if state.purchase_requested and state.purchase_message:
        parts.append(state.purchase_message)
        if state.purchase_status.startswith("failed_") and state.tool_errors:
            parts.append("(The catalog could not be reached to complete it.)")
        return " ".join(parts)

    if state.tool_errors:
        first = next(iter(state.tool_errors.values()))
        if first.code in ("not_found", "invalid_input"):
            parts.append(
                f"I couldn't process your {label} request ({first.code}). "
                "Could you rephrase or add more detail?"
            )
        else:
            parts.append(
                f"Sorry — I can't reach the catalog right now "
                f"({first.code}). Please try again shortly."
            )
        return " ".join(parts)

    if "products" in state.tool_results:
        products = state.tool_results["products"].get("products", [])
        if products:
            names = ", ".join(p["name"] for p in products[:5])
            parts.append(f"Here's what I found: {names}.")
        else:
            parts.append("I couldn't find matching products.")
    elif "product" in state.tool_results:
        p = state.tool_results["product"]
        parts.append(f"{p['name']} ({p['sku']}): {p['price']:.2f}.")
    elif "categories" in state.tool_results:
        names = ", ".join(c["name"] for c in state.tool_results["categories"][:10]) or "none yet"
        parts.append(f"Available categories: {names}.")
    elif "inventory" in state.tool_results:
        items = state.tool_results["inventory"]
        if items:
            lines = "; ".join(f"{i['product_name']}: {i['quantity']} in stock" for i in items[:5])
            parts.append(f"Stock status — {lines}.")
        else:
            parts.append("I couldn't find stock information for that.")
    elif "movements" in state.tool_results:
        movements = state.tool_results["movements"].get("movements", [])
        if movements:
            parts.append(f"Found {state.tool_results['movements']['total']} stock movement(s).")
        else:
            parts.append("No stock movements recorded for that product.")
    else:
        parts.append(
            "I'm not sure how to help with that yet. "
            "Try asking about products, categories, or stock."
        )

    return " ".join(parts)


def build_prompt(state: AgentState) -> str:
    """Build the provider-agnostic prompt from safe state fields only."""
    sections: list[str] = [f'Customer message: "{state.customer_message}"']

    intent_label = _INTENT_LABELS.get(state.intent, state.intent)
    sections.append(f"Classified intent: {intent_label}")

    # Purchase outcome guidance: the composer must reflect the ACTUAL result
    # (deduction done, rejected, or failed) — never invent a confirmation.
    if state.purchase_requested:
        lines = [f"Purchase status: {state.purchase_status}"]
        if state.purchase_quantity is not None:
            lines.append(f"Requested quantity: {state.purchase_quantity}")
        if state.purchase_message:
            lines.append(f"Outcome: {state.purchase_message}")
        sections.append("\n".join(lines))

    if state.tool_results:
        results = {
            key: value
            for key, value in state.tool_results.items()
            if key not in _EXCLUDED_METADATA_KEYS
        }
        sections.append("Catalog data (JSON):")
        sections.append(json.dumps(results, ensure_ascii=False, default=str))

    if state.tool_errors:
        failures = [
            {"tool": failure.tool, "code": failure.code, "message": failure.message}
            for failure in state.tool_errors.values()
        ]
        sections.append("Tool failures (data unavailable for these):")
        sections.append(json.dumps(failures, ensure_ascii=False))

    return "\n".join(sections)
