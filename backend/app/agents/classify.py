"""Deterministic intent classification (temporary — audit §K/§F boundary).

Rule-based keyword matching over the customer message. The future LLM gateway
(audit §F) will replace ``classify_intent`` with an LLM call behind the same
node signature; tools and the graph stay unchanged.
"""

from __future__ import annotations

from typing import Any

from app.agents.state import AgentState, IntentName

# Ordered rules: first match wins. Simple, deterministic, explainable.
_INTENT_RULES: list[tuple[IntentName, tuple[str, ...]]] = [
    ("inventory_movements", ("movement", "movement history", "stock history", "stock movement")),
    ("inventory_check", ("in stock", "in-stock", "stock", "availability", "available")),
    ("product_details", ("details", "detail", "more about", "tell me about", "info about")),
    ("category_browse", ("categories", "category", "browse", "catalog sections")),
    (
        "product_search",
        (
            "search",
            "looking for",
            "find",
            "show me",
            "recommend",
            "suggest",
            "do you have",
            "do you sell",
            "i want",
            "i need",
        ),
    ),
]

_UNKNOWN_INTENT = "unknown"


def classify_intent(state: AgentState) -> dict[str, Any]:
    """Node: pick an intent from the customer message (deterministic).

    Returns a partial state update (LangGraph merges it into the state).
    """
    message = state.customer_message.lower()
    for intent, keywords in _INTENT_RULES:
        if any(kw in message for kw in keywords):
            return {"intent": intent}
    return {"intent": _UNKNOWN_INTENT}
