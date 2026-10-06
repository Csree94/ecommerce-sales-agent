"""Context gathering node: dispatches intents to existing read-only tools.

This node is the architectural proof of the required dependency direction:
graph → tools → InventraClient → HTTP. The graph itself contains no HTTP, no
URLs, no auth, and no database code — it only calls the typed tools from
``app.tools.inventra`` and records structured outcomes in state. Tool failures
(ToolError) become structured state (ToolFailure), never exceptions and never
silent empty results. An optional read-through cache (milestone 4) travels
the same path — the node never talks to Redis itself.
"""

from __future__ import annotations

from typing import Any, cast

from app.agents.state import AgentState, ToolErrorCode, ToolFailure
from app.integrations.cache import InventraCache
from app.integrations.inventra.schemas import Product, ProductListResponse
from app.tools.errors import ToolError, ToolNotFoundError
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
# Includes the small Spanish set used by the T9 multilingual fallback — the
# SAME extraction pipeline serves both languages (no duplicated logic).
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
    # Spanish framing phrases (longest first).
    "estás buscando",
    "estas buscando",
    "estás vendiendo",
    "estas vendiendo",
    "dónde puedo comprar",
    "donde puedo comprar",
    "muéstrame",
    "muestrame",
    "recomiéndame",
    "recomiendame",
    "tienen",
    "tienes",
    "quiero",
    "quieres",
    "necesito",
    "busco",
    "buscando",
    "vendes",
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
        # Spanish function words (T9 multilingual fallback).
        "un", "una", "unos", "unas", "el", "la", "los", "las",
        "de", "del", "al", "en", "con", "por", "para", "que", "qué",
        "y", "o", "es", "son", "hay", "sí", "si", "tienes", "tienen",
        "quiero", "necesito", "busco", "algún", "algun", "alguna",
    }
)

_PUNCTUATION = ".,!?;:'\"()[]{}—–-¿¡"

# Tiny deterministic alias/category table (T7): a handful of common customer
# words Inventra's whole-phrase substring search cannot match directly —
# either because the customer uses a different surface form (plural "laptops"
# vs the matching singular "laptop") or a generic category term ("phones",
# "tablets"). Values are either a literal search term or a catalog category
# name. Consulted ONLY after the normal ladder returns zero results, so it
# never affects successful searches. No LLM, no new search architecture.
# Deliberately narrow: broad aliases would create many false positives.
_SEARCH_ALIASES: dict[str, str] = {
    # Plural → matching singular ("laptop" appears in product descriptions).
    "laptops": "laptop",
    # Generic category term → catalog category name (resolved to a live id).
    "phones": "Mobile phones",
    "headphones": "Headphones",
    "tablets": "Tablets",
    "laptop": "Laptops",
    "notebook": "Laptops",
}

# Spanish → English alias terms (T7/T9 companion): stripped Spanish product
# words whose catalog match only exists in English ("auriculares" appears in
# no product name/description). Mapped to the SAME alias table — no separate
# Spanish search path.
_SPANISH_ALIASES: dict[str, str] = {
    "auriculares": "headphones",
    "portatil": "laptops",
    "portátil": "laptops",
    "ordenador": "laptops",
    "movil": "phones",
    "móvil": "phones",
    "telefono": "phones",
    "teléfono": "phones",
}


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


async def _category_id_for_name(
    client: Any, category_name: str, cache: InventraCache | None = None
) -> int | None:
    """Resolve a catalog category name to its id via the existing tool.

    Deterministic, bounded (one GET /api/categories through the existing tool
    layer). Resolved at runtime — no hardcoded ids, no catalog assumptions.
    Returns ``None`` on any failure; the caller then degrades to the honest
    zero-result result it would have produced anyway (never raises, never
    invents products).
    """
    try:
        categories = await list_categories(client, cache=cache)
    except ToolError:
        return None
    wanted = category_name.strip().lower()
    for category in categories:
        if category.name.strip().lower() == wanted:
            return category.id
    return None


def _alias_value_for(token: str) -> str | None:
    """Alias value for one token: ``category:<Name>`` or a literal search term.

    Two-level resolution: the Spanish table maps onto keys of the main table
    (``auriculares`` → ``headphones`` → the Headphones category), so Spanish
    reuses the SAME alias set — no separate Spanish search path. Uppercased
    values are catalog category names; lowercase values are search terms.
    """
    value = _SPANISH_ALIASES.get(token)
    if value is not None:
        token = value  # resolve one level further through the main table
    value = _SEARCH_ALIASES.get(token)
    if value is None:
        return None
    if value[:1].isupper():
        return f"category:{value}"
    return value


def _literal_alias_value(token: str) -> str | None:
    """Literal (lowercase) search-term alias for a token, or ``None``.

    Category-valued aliases are excluded — they are tried after the ladder as
    a resolved ``category_id`` filter, never substituted into the search term.
    """
    value = _alias_value_for(token)
    if value is not None and not value.startswith("category:"):
        return value
    return None


def _canonicalize_term(term: str | None) -> str | None:
    """Swap alias tokens for their literal catalog search form (deterministic).

    ``laptops`` → ``laptop`` before the ladder starts: the plural form can
    match unrelated products whose descriptions mention it (e.g. a charger
    "for laptops"), while the singular matches the actual laptops. Category-
    valued aliases leave the term untouched. No-op for everything else.
    """
    if not term:
        return term
    tokens = [_literal_alias_value(tok) or tok for tok in term.split()]
    return " ".join(tokens)


def _alias_terms_for(term: str | None) -> list[str]:
    """Ordered alias values for an exhausted search (deterministic, bounded).

    Significant extracted tokens (longest first, at most two) are mapped via
    ``_alias_value_for``; results are deduplicated and capped at two.
    """
    if not term:
        return []
    alias_terms: list[str] = []
    tokens = sorted(
        (tok for tok in term.split() if len(tok) > 2),
        key=len,
        reverse=True,
    )[:2]
    for token in tokens:
        value = _alias_value_for(token)
        if value is not None and value not in alias_terms:
            alias_terms.append(value)
    return alias_terms


async def _search_products_bounded(
    client: Any, message: str, cache: InventraCache | None = None
) -> ProductListResponse:
    """Product search with the bounded zero-result ladder (single implementation).

    Shared by the ``product_search`` intent and by name→product resolution for
    ``product_details`` so the search/retry logic is never duplicated. The
    optional cache (milestone 4) rides the SAME ladder — every attempt is a
    cache-aside read of its own exact parameters.
    """
    raw = message.strip()
    # Canonicalize alias tokens up front (laptops→laptop) so the ladder and
    # the post-ladder alias loop operate on the same canonical term.
    term = _canonicalize_term(_extract_search_term(message))
    first = term or raw or None
    result = await search_products(
        client,
        ProductSearchParams(search=first, per_page=5),
        cache=cache,
    )
    # Bounded zero-result ladder: raw message, then individual significant
    # tokens ( Inventra matches the whole phrase as one substring ). Never
    # unbounded — see _zero_result_fallback_terms.
    if result.total == 0 and first is not None:
        for fallback in _zero_result_fallback_terms(message, term):
            if fallback == first:
                continue
            result = await search_products(
                client,
                ProductSearchParams(search=fallback, per_page=5),
                cache=cache,
            )
            if result.total > 0:
                break
    # T7 alias/category fallback (deterministic, bounded): only when every
    # attempt above found nothing. Reuses the SAME search_products tool —
    # literal aliases as ``search``, category aliases as a resolved
    # ``category_id`` filter (resolved live via the existing categories tool;
    # resolution failure skips the alias, never crashes, never invents data).
    if result.total == 0:
        for alias in _alias_terms_for(term):
            if alias == first or alias in _zero_result_fallback_terms(message, term):
                continue  # already tried verbatim in the ladder above
            if alias.startswith("category:"):
                category_id = await _category_id_for_name(
                    client, alias.removeprefix("category:"), cache=cache
                )
                if category_id is None:
                    continue
                result = await search_products(
                    client,
                    ProductSearchParams(category_id=category_id, per_page=5),
                    cache=cache,
                )
            else:
                result = await search_products(
                    client,
                    ProductSearchParams(search=alias, per_page=5),
                    cache=cache,
                )
            if result.total > 0:
                break
    return result


async def _resolve_product_by_name(
    client: Any, message: str, cache: InventraCache | None = None
) -> Product | None:
    """Resolve a product by name from a natural-language message.

    Deterministic (no LLM): runs the shared bounded search over the message,
    prefers an exact case-insensitive name match and otherwise takes the
    first hit. Returns ``None`` when nothing matches — the caller then emits
    the honest no-result failure instead of inventing a product.
    """
    result = await _search_products_bounded(client, message, cache=cache)
    if result.total == 0:
        return None
    term = _extract_search_term(message)
    wanted = (term or message).strip().lower()
    for candidate in result.products:
        if candidate.name.strip().lower() == wanted:
            return candidate
    return result.products[0]


async def gather_context(
    state: AgentState, client: Any, cache: InventraCache | None = None
) -> dict[str, Any]:
    """Node: call the tool matching the classified intent.

    ``client`` is the injected ``InventraClient`` (typed loosely here only to
    avoid a module-scope import cycle; tests inject a mock at this seam).
    ``cache`` is the optional milestone-4 read-through cache (``None`` keeps
    the historical direct-read path — tests rely on that default).
    """
    intent = state.intent
    message = state.customer_message

    try:
        if intent == "product_search":
            result = await _search_products_bounded(client, message, cache=cache)
            return {"tool_results": {"products": result.model_dump(mode="json")}}
        if intent == "product_details":
            product_id = _extract_id(message)
            if product_id is not None:
                # Numeric-ID flow (unchanged): direct details lookup.
                product = await get_product_details(client, product_id, cache=cache)
                return {"tool_results": {"product": product.model_dump(mode="json")}}
            # No numeric id → resolve the product by name with the existing
            # search capability (no LLM, no new endpoint), then reuse the
            # existing details flow with the resolved id.
            resolved = await _resolve_product_by_name(client, message, cache=cache)
            if resolved is None:
                return _failure(
                    "gather_context",
                    ToolNotFoundError("No catalog product matches the requested name"),
                )
            product = await get_product_details(client, resolved.id, cache=cache)
            return {"tool_results": {"product": product.model_dump(mode="json")}}
        if intent == "category_browse":
            categories = await list_categories(client, cache=cache)
            return {"tool_results": {"categories": [c.model_dump(mode="json") for c in categories]}}
        if intent == "inventory_check":
            raw = message.strip()
            term = _extract_search_term(message)
            first = term or raw or None
            items = await check_inventory(
                client,
                InventorySearchParams(search=first),
                cache=cache,
            )
            if not items and first is not None:
                for fallback in _zero_result_fallback_terms(message, term):
                    if fallback == first:
                        continue
                    items = await check_inventory(
                        client,
                        InventorySearchParams(search=fallback),
                        cache=cache,
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
