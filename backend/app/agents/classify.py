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
    ("product_details", ("details", "detail", "more about", "tell me about", "info about",
                          # Price/cost questions: the price lives on the product, so they
                          # retrieve product details (Step 3C: previously fell to "unknown"
                          # and the agent answered without catalog data). Spanish price
                          # words ride the same rule — T9 fallback table stays untouched.
                          "price", "how much", "cost", "cuánto", "cuesta")),
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

# Small deterministic multilingual fallback (T9): a handful of common Spanish
# shopping/product-intent patterns, consulted ONLY after the English rules
# above miss, so ordinary English messages keep the same fast path. Mapped to
# EXISTING intents — no new intent, no LLM call, no translation service.
# Longest/most specific phrases first (first match wins).
_MULTILINGUAL_FALLBACK_RULES: list[tuple[IntentName, tuple[str, ...]]] = [
    (
        "inventory_movements",
        ("historial de stock", "historial de inventario", "historial de movimientos"),
    ),
    (
        "inventory_check",
        (
            "hay stock",
            "hay existencias",
            "queda stock",
            "quedan existencias",
            "en stock",
            "disponible",
            "disponibilidad",
        ),
    ),
    (
        "product_details",
        ("dime más sobre", "dime mas sobre", "detalles de", "más información sobre"),
    ),
    ("category_browse", ("qué categorías", "que categorías", "ver categorías", "categorías")),
    (
        "product_search",
        (
            "estás buscando",
            "estás vendiendo",
            "tienen",
            "tienes",
            "quiero",
            "quieres",
            "necesito",
            "busco",
            "buscando",
            "vendes",
            "muéstrame",
            "muestrame",
            "recomienda",
            "recomiéndame",
            "recomiendame",
            "sugiere",
            "sugiéreme",
            "sugereme",
            "dónde puedo comprar",
            "donde puedo comprar",
        ),
    ),
]


def classify_intent(state: AgentState) -> dict[str, Any]:
    """Node: pick an intent from the customer message (deterministic).

    English rules run first (fast path, unchanged). If none match, a small
    deterministic Spanish fallback table is consulted — no LLM call for
    ordinary classification (T9). Returns a partial state update (LangGraph
    merges it into the state).
    """
    message = state.customer_message.lower()
    for intent, keywords in _INTENT_RULES:
        if any(kw in message for kw in keywords):
            return {"intent": intent}
    for intent, keywords in _MULTILINGUAL_FALLBACK_RULES:
        if any(kw in message for kw in keywords):
            return {"intent": intent}
    return {"intent": _UNKNOWN_INTENT}
