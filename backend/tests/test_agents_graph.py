"""Tests for the LangGraph foundation layer.

Mocks are placed at the ``InventraClient`` and ``LLMGateway`` boundaries — no
real HTTP. Coverage: state reducers, deterministic classification, per-intent
tool dispatch, tool failures entering state as structured ToolFailure (never
exceptions), safe unknown-intent handling, LLM-driven composition (with
deterministic degradation), full graph runs reaching a final response, and
guardrails (no HTTP/DB/provider code inside the agents package).
"""

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.agents import (
    AgentState,
    ToolFailure,
    build_graph,
    classify_intent,
    compose_reply,
    gather_context,
    run_turn,
)
from app.integrations.inventra.schemas import (
    Category,
    InventoryItem,
    Product,
    ProductListResponse,
    StockMovementListResponse,
)
from app.integrations.llm.errors import LLMError
from app.integrations.llm.schemas import GenerationRequest, GenerationResponse, TokenUsage
from app.tools.errors import ToolError, ToolInputError, ToolNotFoundError, ToolUnavailableError


def make_async(value: Any) -> Any:
    """Wrap a value in a resolved coroutine (mock client methods are async)."""

    async def _inner() -> Any:
        return value

    return _inner()


def make_tool_error(code: str) -> ToolError:
    """Build the tool error for a given stable code (as the tool layer raises)."""
    if code == "not_found":
        return ToolNotFoundError("nf")
    if code == "invalid_input":
        return ToolInputError("ii")
    return ToolUnavailableError(code, code=code)


# Verified-shape fixtures (mirrors of Inventra response contracts).
PRODUCT = {
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
CATEGORY = {
    "id": 3,
    "name": "Footwear",
    "description": "Shoes and boots",
    "created_at": "2026-01-10T09:00:00Z",
    "updated_at": "2026-01-10T09:00:00Z",
}
INVENTORY_ITEM = {
    "id": 11,
    "product_id": 7,
    "product_name": "Trail Shoes",
    "product_sku": "TS-01...",
    "quantity": 4,
    "low_stock_threshold": 10,
    "is_low_stock": True,
    "updated_at": "2026-02-01T12:30:00Z",
}
MOVEMENT_LIST = {
    "movements": [
        {
            "id": 21,
            "product_id": 7,
            "product_name": "Trail Shoes",
            "user_id": 1,
            "username": "admin",
            "movement_type": "STOCK_IN",
            "quantity": 5,
            "notes": "restock",
            "created_at": "2026-02-01T12:30:00Z",
        }
    ],
    "total": 1,
    "page": 1,
    "per_page": 50,
}
PRODUCT_LIST = {"products": [PRODUCT], "total": 1, "page": 1, "per_page": 20, "pages": 1}

# A second product for name→id resolution tests (T6).
SAMSUNG_PRODUCT = {
    "id": 12,
    "name": "Samsung Galaxy S26",
    "sku": "SGS26-256",
    "description": "Flagship smartphone, 256 GB",
    "category_id": 2,
    "category_name": "Phones",
    "price": 1099.0,
    "is_active": True,
    "created_at": "2026-03-01T09:00:00Z",
    "updated_at": "2026-03-01T09:00:00Z",
}


@pytest.fixture()
def client() -> MagicMock:
    return MagicMock()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- State schema ----------------------------------------------------------


def test_state_reducers_merge_instead_of_overwrite() -> None:
    left = AgentState(tool_results={"a": 1})
    update = {
        "tool_results": {"b": 2},
        "tool_errors": {"t": ToolFailure(tool="t", code="timeout", message="m")},
    }

    reducer = AgentState.model_fields["tool_results"].metadata[0]

    assert reducer(left.tool_results, update["tool_results"]) == {"a": 1, "b": 2}


def test_state_defaults_are_safe() -> None:
    state = AgentState()
    assert state.intent == "unknown"
    assert state.tool_results == {}
    assert state.tool_errors == {}
    assert state.draft_response == ""
    assert state.model_used is None
    assert state.fallback_used is False
    assert state.token_usage is None


def test_state_rejects_invalid_intent() -> None:
    with pytest.raises(PydanticValidationError):
        AgentState(intent="nonsense")


# --- classify_intent node ---------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("I'm looking for hiking shoes", "product_search"),
        ("tell me about product 7", "product_details"),
        ("what categories do you have", "category_browse"),
        ("is the trail shoes in stock", "inventory_check"),
        ("show me the stock history for product 7", "inventory_movements"),
        ("hello there", "unknown"),
        ("", "unknown"),
    ],
)
def test_classification_produces_expected_intents(message: str, expected_intent: str) -> None:
    result = classify_intent(AgentState(customer_message=message))
    assert result == {"intent": expected_intent}


def test_classification_is_deterministic() -> None:
    a = classify_intent(AgentState(customer_message="stock movements for 7"))
    b = classify_intent(AgentState(customer_message="stock movements for 7"))
    assert a == b


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("do you have samsung phones?", "product_search"),
        ("do you sell laptops", "product_search"),
        ("i want headphones", "product_search"),
        ("i need a dell laptop", "product_search"),
    ],
)
def test_classification_recognizes_shopping_phrases(
    message: str, expected_intent: str
) -> None:
    """Common shopping phrasings reach product_search (regression: were unknown)."""
    assert classify_intent(AgentState(customer_message=message)) == {"intent": expected_intent}


# --- classify_intent: deterministic multilingual fallback (T9) -----------------


def test_spanish_product_availability_reaches_product_search(client: MagicMock) -> None:
    """T9: '¿Tienes auriculares Sony?' routes to the EXISTING product_search intent."""
    assert classify_intent(
        AgentState(customer_message="¿Tienes auriculares Sony?")
    ) == {"intent": "product_search"}


@pytest.mark.parametrize(
    ("message", "expected_intent"),
    [
        ("¿Tienen laptops HP?", "product_search"),
        ("Necesito un teléfono", "product_search"),
        ("Busco zapatillas Nike", "product_search"),
        ("Quiero auriculares Sony", "product_search"),
        ("¿Hay stock de la TS-001?", "inventory_check"),
        ("¿Qué categorías hay?", "category_browse"),
        ("Dime más sobre Samsung Galaxy S26", "product_details"),
    ],
)
def test_multilingual_fallback_maps_to_existing_intents(
    message: str, expected_intent: str
) -> None:
    """Spanish patterns reuse existing intents — no new intent is created."""
    assert classify_intent(AgentState(customer_message=message)) == {"intent": expected_intent}


@pytest.mark.parametrize(
    "message",
    [
        "Show me Sony headphones",
        "Do you have Samsung phones?",
        "I need a phone.",
        "I'm looking for hiking shoes",
        "tell me about product 7",
        "what categories do you have",
        "is the trail shoes in stock",
    ],
)
def test_english_classification_unchanged_by_multilingual_fallback(message: str) -> None:
    """Regression: English messages classify exactly as before the fallback."""
    expected = {
        "Show me Sony headphones": "product_search",
        "Do you have Samsung phones?": "product_search",
        "I need a phone.": "product_search",
        "I'm looking for hiking shoes": "product_search",
        "tell me about product 7": "product_details",
        "what categories do you have": "category_browse",
        "is the trail shoes in stock": "inventory_check",
    }
    assert classify_intent(AgentState(customer_message=message)) == {"intent": expected[message]}


def test_greeting_stays_unknown_with_multilingual_fallback() -> None:
    """Greetings must not be swallowed by the fallback table."""
    assert classify_intent(AgentState(customer_message="hi")) == {"intent": "unknown"}
    assert classify_intent(AgentState(customer_message="hola")) == {"intent": "unknown"}


@pytest.mark.parametrize(
    "message",
    [
        "¿Cómo llego a la tienda?",
        "mi gato es negro",
        "random unrelated text",
    ],
)
def test_unrelated_requests_stay_unknown_with_multilingual_fallback(message: str) -> None:
    """Unknown/unrelated requests remain unknown — fallback is narrow, not greedy."""
    assert classify_intent(AgentState(customer_message=message)) == {"intent": "unknown"}


def test_classification_multilingual_fallback_is_deterministic() -> None:
    """The Spanish path is pure keyword matching: no LLM, no randomness."""
    a = classify_intent(AgentState(customer_message="¿Tienes auriculares Sony?"))
    b = classify_intent(AgentState(customer_message="¿Tienes auriculares Sony?"))
    assert a == b == {"intent": "product_search"}


def test_classification_uses_no_llm_for_multilingual_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T9 guard: ordinary classification never calls an LLM.

    Any construction of the LLM gateway (or a gateway.generate call) inside the
    classify path would fail this test — classify_intent is pure/deterministic.
    """
    import app.integrations.llm as llm_pkg

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("classification must not build or use an LLM gateway")

    # build_graph resolves the gateway lazily via ``from app.integrations.llm
    # import get_llm_gateway`` — patch the package attribute (the real seam).
    monkeypatch.setattr(llm_pkg, "get_llm_gateway", _boom)
    from app.agents.classify import classify_intent as classify_direct

    assert classify_direct(AgentState(customer_message="¿Tienes auriculares Sony?")) == {
        "intent": "product_search"
    }
    assert classify_direct(AgentState(customer_message="hi")) == {"intent": "unknown"}


# --- gather_context: Spanish search-term extraction (T9) -----------------------


@pytest.mark.parametrize(
    ("message", "expected_term"),
    [
        # Opening ¿ and trailing ? are stripped; framing verbs removed.
        ("¿Tienes auriculares Sony?", "auriculares sony"),
        ("¿Tienen laptops HP?", "laptops hp"),
        ("Necesito un teléfono", "teléfono"),
        ("Busco zapatillas Nike", "zapatillas nike"),
        ("Quiero auriculares Sony", "auriculares sony"),
        ("Muéstrame las zapatillas", "zapatillas"),
    ],
)
def test_extract_search_term_spanish(message: str, expected_term: str) -> None:
    """Spanish queries strip framing through the SAME extraction pipeline."""
    from app.agents.gather import _extract_search_term

    assert _extract_search_term(message) == expected_term


# --- gather_context: alias/category fallback (T7) ------------------------------

# Real-catalog-shaped laptop products (mirrors the live Inventra catalog:
# "laptop" occurs in their descriptions, never as a bare name token).
LAPTOPS = {
    "products": [
        dict(PRODUCT, id=31, name="Dell XPS 15", sku="DXPS-15", category_name="Laptops"),
        dict(PRODUCT, id=32, name="HP Pavilion 15", sku="HPP-15", category_name="Laptops"),
        dict(PRODUCT, id=33, name="Lenovo IdeaPad Slim 5", sku="LIP-5", category_name="Laptops"),
    ],
    "total": 3,
    "page": 1,
    "per_page": 5,
    "pages": 1,
}
EMPTY_LIST = {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
LAPTOP_CATEGORY = dict(CATEGORY, id=2, name="Laptops")
PHONE_CATEGORY = dict(CATEGORY, id=1, name="Mobile phones")
HEADPHONES_CATEGORY = dict(CATEGORY, id=4, name="Headphones")


def test_laptop_query_alias_finds_real_products(client: MagicMock) -> None:
    """T7: 'What laptops do you have?' → canonicalized 'laptop' hits immediately.

    The plural term is swapped for its literal singular alias BEFORE the first
    search (the plural can match unrelated products mentioning "laptops" in
    their description). Same search_products tool, one call, no invented data.
    """
    hit = ProductListResponse.model_validate(LAPTOPS)
    # Exactly one call expected: the canonicalized term hits on the first try
    # (StopAsyncIteration on any extra call keeps this strict).
    client.list_products.side_effect = [make_async(hit)]

    result = run(
        gather_context(
            AgentState(customer_message="What laptops do you have?", intent="product_search"),
            client,
        )
    )

    searches = [c.kwargs["search"] for c in client.list_products.call_args_list]
    assert searches == ["laptop"]
    client.list_categories.assert_not_called()  # literal alias needs no category lookup
    assert result["tool_results"]["products"]["total"] == 3


def test_category_alias_resolves_live_category_id(client: MagicMock) -> None:
    """Generic category term → category_id resolved live via the existing tool.

    'phones' has no literal alias, so its category alias (Mobile phones) is
    resolved through GET /api/categories and used as a category_id filter.
    """
    empty = ProductListResponse.model_validate(EMPTY_LIST)
    hit = ProductListResponse.model_validate(PRODUCT_LIST)
    client.list_products.side_effect = [make_async(empty), make_async(empty), make_async(hit)]
    client.list_categories.return_value = make_async([Category.model_validate(PHONE_CATEGORY)])

    result = run(
        gather_context(
            AgentState(customer_message="Do you have phones?", intent="product_search"),
            client,
        )
    )

    # Final attempt is a category_id filter — no search term invented.
    assert client.list_products.call_args.kwargs.get("category_id") == 1
    assert client.list_products.call_args.kwargs.get("search") is None
    client.list_categories.assert_called_once()
    assert result["tool_results"]["products"]["total"] == 1


def test_alias_ladder_is_bounded_and_fails_honestly(client: MagicMock) -> None:
    """Alias adds at most its fixed entries; all-zero stays an honest empty."""
    empty = ProductListResponse.model_validate(EMPTY_LIST)
    client.list_products.side_effect = lambda *a, **kw: make_async(empty)
    # Live catalog HAS a Laptops category → the category alias is resolved and
    # tried once more after the literal 'laptop' alias failed (bounded).
    client.list_categories.return_value = make_async([Category.model_validate(LAPTOP_CATEGORY)])

    result = run(
        gather_context(
            AgentState(customer_message="What laptops do you have?", intent="product_search"),
            client,
        )
    )

    # Canonical term + raw message + one resolved category filter — hard-capped.
    assert client.list_products.call_count == 3
    assert client.list_products.call_args.kwargs.get("category_id") == 2
    assert result["tool_results"]["products"]["total"] == 0
    assert "tool_errors" not in result


def test_category_alias_failure_degrades_without_crashing(client: MagicMock) -> None:
    """Category resolution failure → skip alias, honest zero result (no raise)."""
    empty = ProductListResponse.model_validate(EMPTY_LIST)
    client.list_products.side_effect = lambda *a, **kw: make_async(empty)
    client.list_categories.side_effect = make_tool_error("upstream_error")

    result = run(
        gather_context(
            AgentState(customer_message="Do you have phones?", intent="product_search"),
            client,
        )
    )

    # term + raw ladder only; the category alias was skipped cleanly.
    assert client.list_products.call_count == 2
    assert result["tool_results"]["products"]["total"] == 0
    assert "tool_errors" not in result


def test_english_multiword_search_unaffected_by_alias(client: MagicMock) -> None:
    """'Do you have Samsung phones?' resolves via the existing token ladder."""
    empty = ProductListResponse.model_validate(EMPTY_LIST)
    hit = ProductListResponse.model_validate(PRODUCT_LIST)
    client.list_products.side_effect = [make_async(empty), make_async(empty), make_async(hit)]

    result = run(
        gather_context(
            AgentState(customer_message="Do you have Samsung phones?", intent="product_search"),
            client,
        )
    )

    searches = [c.kwargs["search"] for c in client.list_products.call_args_list]
    assert searches == ["samsung phones", "Do you have Samsung phones?", "samsung"]
    client.list_categories.assert_not_called()  # ladder hit before any alias
    assert result["tool_results"]["products"]["total"] == 1


def test_spanish_auriculares_alias_reaches_headphones_category(client: MagicMock) -> None:
    """T9 enhancement: 'auriculares' maps onto the SAME alias/category path."""
    empty = ProductListResponse.model_validate(EMPTY_LIST)
    sony_hit = ProductListResponse.model_validate(
        {"products": [dict(PRODUCT, id=21, name="Sony WH-1000XM6", sku="SONY-XM6")],
         "total": 1, "page": 1, "per_page": 5, "pages": 1}
    )
    client.list_products.side_effect = [
        make_async(empty),  # 'auriculares sony'
        make_async(empty),  # raw message
        make_async(empty),  # 'auriculares'
        make_async(empty),  # 'sony'
        make_async(sony_hit),  # category_id=Headphones
    ]
    client.list_categories.return_value = make_async(
        [Category.model_validate(HEADPHONES_CATEGORY)]
    )

    result = run(
        gather_context(
            AgentState(customer_message="¿Tienes auriculares Sony?", intent="product_search"),
            client,
        )
    )

    assert client.list_categories.call_count == 1
    assert client.list_products.call_args.kwargs.get("category_id") == 4
    assert result["tool_results"]["products"]["total"] == 1


def test_graph_laptop_query_end_to_end_mentions_real_products(client: MagicMock) -> None:
    """T7, full graph: 'What laptops do you have?' → real catalog product names."""
    hit = ProductListResponse.model_validate(LAPTOPS)
    # Canonicalized 'laptop' search hits on the first Inventra call.
    client.list_products.side_effect = [make_async(hit)]

    graph = build_graph(client, None, failing_gateway())
    final = run(
        graph.ainvoke(
            AgentState(customer_message="What laptops do you have?"),
            config={"configurable": {"thread_id": "conv-t7"}},
        )
    )

    assert final["intent"] == "product_search"
    assert final["tool_results"]["products"]["total"] == 3
    assert not final["tool_errors"]
    # Deterministic safety net names real catalog products — nothing invented.
    assert "Dell XPS 15" in final["draft_response"]
    assert "HP Pavilion 15" in final["draft_response"]
    assert "Lenovo IdeaPad Slim 5" in final["draft_response"]


def test_spanish_query_end_to_end_reaches_existing_product_search(client: MagicMock) -> None:
    """T9, full graph: '¿Tienes auriculares Sony?' → product_search tool path.

    Asserts the EXISTING product-search capability is reused (list_products
    called with the extracted term) — not a new intent or new pipeline.
    """
    sony_list = ProductListResponse.model_validate(
        {
            "products": [dict(PRODUCT, id=21, name="Sony WH-1000XM6", sku="SONY-XM6")],
            "total": 1,
            "page": 1,
            "per_page": 5,
            "pages": 1,
        }
    )
    client.list_products.return_value = make_async(sony_list)

    graph = build_graph(client, None, failing_gateway())
    final = run(
        graph.ainvoke(
            AgentState(customer_message="¿Tienes auriculares Sony?"),
            config={"configurable": {"thread_id": "conv-t9"}},
        )
    )

    assert final["intent"] == "product_search"
    client.list_products.assert_called_once()
    assert client.list_products.call_args.kwargs["search"] == "auriculares sony"
    assert final["tool_results"]["products"]["total"] == 1
    assert not final["tool_errors"]
    # Deterministic safety net lists the found product → turn completes.
    assert "Sony WH-1000XM6" in final["draft_response"]


# --- gather_context node -----------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_term"),
    [
        # Natural-language shopping phrasings → bare product term.
        ("Show me Samsung Galaxy S26", "samsung galaxy s26"),
        ("Do you have Samsung phones?", "samsung phones"),
        ("I'm looking for a Dell laptop", "dell laptop"),
        ("Show me some headphones", "headphones"),
        ("Can you recommend a laptop?", "laptop"),
        # Exact product name passes through (lowercased, unchanged tokens).
        ("Samsung Galaxy S26", "samsung galaxy s26"),
        ("Trail Shoes", "trail shoes"),
        # SKUs / tokens containing digits are never dropped.
        ("TS-001", "ts-001"),
        ("Is the TS-001 in stock?", "ts-001"),
        ("Is Samsung Galaxy S26 in stock?", "samsung galaxy s26"),
        # Plain single word stays as-is.
        ("shoes", "shoes"),
        # Punctuation-only and empty input → None (caller falls back to raw).
        ("???", None),
        ("", None),
        ("   ", None),
    ],
)
def test_extract_search_term(message: str, expected_term: str | None) -> None:
    from app.agents.gather import _extract_search_term

    assert _extract_search_term(message) == expected_term


def test_product_search_uses_extracted_term(client: MagicMock) -> None:
    """The node searches for the extracted term, not the whole sentence."""
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))

    run(
        gather_context(
            AgentState(customer_message="Show me Samsung Galaxy S26", intent="product_search"),
            client,
        )
    )

    assert client.list_products.call_args.kwargs["search"] == "samsung galaxy s26"


def test_product_search_falls_back_to_raw_message_on_zero_results(
    client: MagicMock,
) -> None:
    """Bounded retry ladder on zero results: raw message first."""
    empty = ProductListResponse.model_validate(
        {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
    )
    hit = ProductListResponse.model_validate(PRODUCT_LIST)
    client.list_products.side_effect = [make_async(empty), make_async(hit)]

    result = run(
        gather_context(
            AgentState(customer_message="Show me the xyzzy-model-9", intent="product_search"),
            client,
        )
    )

    assert client.list_products.call_count == 2
    assert client.list_products.call_args_list[0].kwargs["search"] == "xyzzy-model-9"
    assert client.list_products.call_args_list[1].kwargs["search"] == "Show me the xyzzy-model-9"
    assert result["tool_results"]["products"]["total"] == 1


def test_product_search_token_fallback_when_multiword_term_finds_nothing(
    client: MagicMock,
) -> None:
    """Multi-word term 0-hit → raw message → longest significant token (bounded)."""
    empty = ProductListResponse.model_validate(
        {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
    )
    hit = ProductListResponse.model_validate(PRODUCT_LIST)
    client.list_products.side_effect = [make_async(empty), make_async(empty), make_async(hit)]

    result = run(
        gather_context(
            AgentState(customer_message="Show me Sony headphones", intent="product_search"),
            client,
        )
    )

    searches = [c.kwargs["search"] for c in client.list_products.call_args_list]
    assert searches == ["sony headphones", "Show me Sony headphones", "headphones"]
    assert client.list_products.call_count == 3
    assert result["tool_results"]["products"]["total"] == 1


def test_product_search_fallback_ladder_is_bounded(client: MagicMock) -> None:
    """All-zero results → exactly 1 + len(ladder) calls, never unbounded."""
    empty = ProductListResponse.model_validate(
        {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
    )
    # A fresh coroutine per call — the fallback loop awaits several.
    client.list_products.side_effect = lambda *a, **kw: make_async(empty)

    run(
        gather_context(
            AgentState(
                customer_message="Show me Samsung Galaxy S26", intent="product_search"
            ),
            client,
        )
    )

    # 1 initial + raw + at most 2 token fallbacks = hard cap of 4 calls.
    assert client.list_products.call_count <= 4


def test_zero_result_fallback_terms_helper() -> None:
    from app.agents.gather import _zero_result_fallback_terms

    # Raw message first, then up to two longest significant tokens.
    assert _zero_result_fallback_terms("Show me Sony headphones", "sony headphones") == [
        "Show me Sony headphones",
        "headphones",
        "sony",
    ]
    # Term equals raw → no duplicated raw entry; single-token term stays.
    assert _zero_result_fallback_terms("shoes", "shoes") == ["shoes"]
    # Nothing product-bearing and term is None → only the raw message.
    assert _zero_result_fallback_terms("hi", None) == ["hi"]


def test_product_search_single_call_when_first_search_hits(client: MagicMock) -> None:
    """A clean search performs exactly one Inventra call — no speculative retries."""
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))

    run(gather_context(AgentState(customer_message="shoes", intent="product_search"), client))

    assert client.list_products.call_count == 1


def test_inventory_check_uses_extracted_term(client: MagicMock) -> None:
    """Inventory search strips framing the same way (regression: raw sentence)."""
    client.list_inventory.return_value = make_async([InventoryItem.model_validate(INVENTORY_ITEM)])

    run(
        gather_context(
            AgentState(
                customer_message="Is the TS-001 in stock?", intent="inventory_check"
            ),
            client,
        )
    )

    assert client.list_inventory.call_args.kwargs["search"] == "ts-001"


def test_product_search_routes_to_search_products(client: MagicMock) -> None:
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))
    result = run(
        gather_context(AgentState(customer_message="shoes", intent="product_search"), client)
    )

    client.list_products.assert_called_once()
    assert "products" in result["tool_results"]


def test_product_details_routes_to_get_product_details(client: MagicMock) -> None:
    """Numeric-ID details flow is preserved: direct lookup, search never called."""
    client.get_product.return_value = make_async(Product.model_validate(PRODUCT))
    result = run(
        gather_context(
            AgentState(customer_message="details for product 7", intent="product_details"), client
        )
    )

    client.get_product.assert_called_once_with(7)
    client.list_products.assert_not_called()
    assert result["tool_results"]["product"]["id"] == 7


def test_product_details_resolves_name_via_existing_search(client: MagicMock) -> None:
    """T6: a natural-language product name resolves through product search."""
    samsung_list = ProductListResponse.model_validate(
        {
            "products": [SAMSUNG_PRODUCT],
            "total": 1,
            "page": 1,
            "per_page": 5,
            "pages": 1,
        }
    )
    client.list_products.return_value = make_async(samsung_list)
    client.get_product.return_value = make_async(Product.model_validate(SAMSUNG_PRODUCT))

    result = run(
        gather_context(
            AgentState(
                customer_message="Tell me about Samsung Galaxy S26", intent="product_details"
            ),
            client,
        )
    )

    client.list_products.assert_called_once()
    assert client.list_products.call_args.kwargs["search"] == "samsung galaxy s26"
    client.get_product.assert_called_once_with(12)
    assert "tool_errors" not in result
    assert result["tool_results"]["product"]["id"] == 12
    assert result["tool_results"]["product"]["name"] == "Samsung Galaxy S26"


def test_product_details_name_resolution_prefers_exact_match(client: MagicMock) -> None:
    """Several hits → the exact case-insensitive name match wins (no LLM guess)."""
    ultra = dict(SAMSUNG_PRODUCT, id=5, name="Samsung Galaxy S26 Ultra")
    exact = dict(SAMSUNG_PRODUCT, id=9, name="Samsung Galaxy S26")
    both = ProductListResponse.model_validate(
        {"products": [ultra, exact], "total": 2, "page": 1, "per_page": 5, "pages": 1}
    )
    client.list_products.return_value = make_async(both)
    client.get_product.return_value = make_async(Product.model_validate(exact))

    result = run(
        gather_context(
            AgentState(
                customer_message="Tell me about Samsung Galaxy S26",
                intent="product_details",
            ),
            client,
        )
    )

    client.get_product.assert_called_once_with(9)
    assert result["tool_results"]["product"]["id"] == 9


def test_product_details_unknown_name_is_not_found_failure(client: MagicMock) -> None:
    """No search hit at all → honest not_found failure, details never called."""
    empty = ProductListResponse.model_validate(
        {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
    )
    client.list_products.side_effect = lambda *a, **kw: make_async(empty)

    result = run(
        gather_context(
            AgentState(
                customer_message="Tell me about Unobtainium X999",
                intent="product_details",
            ),
            client,
        )
    )

    client.get_product.assert_not_called()
    failure = result["tool_errors"]["gather_context"]
    assert failure.code == "not_found"
    assert failure.tool == "gather_context"


def test_product_details_name_resolution_ladder_is_bounded(client: MagicMock) -> None:
    """Name resolution reuses the bounded search ladder — never unbounded."""
    empty = ProductListResponse.model_validate(
        {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
    )
    client.list_products.side_effect = lambda *a, **kw: make_async(empty)

    run(
        gather_context(
            AgentState(
                customer_message="Tell me about Samsung Galaxy S26", intent="product_details"
            ),
            client,
        )
    )

    # Same hard cap as product_search: initial + raw + at most 2 token fallbacks.
    assert client.list_products.call_count <= 4


def test_category_browse_routes_to_list_categories(client: MagicMock) -> None:
    client.list_categories.return_value = make_async([Category.model_validate(CATEGORY)])
    result = run(
        gather_context(AgentState(customer_message="categories", intent="category_browse"), client)
    )

    client.list_categories.assert_called_once_with()
    assert result["tool_results"]["categories"][0]["name"] == "Footwear"


def test_inventory_check_routes_to_check_inventory(client: MagicMock) -> None:
    client.list_inventory.return_value = make_async([InventoryItem.model_validate(INVENTORY_ITEM)])
    result = run(
        gather_context(AgentState(customer_message="shoes stock", intent="inventory_check"), client)
    )

    client.list_inventory.assert_called_once()
    assert result["tool_results"]["inventory"][0]["quantity"] == 4


def test_inventory_movements_routes_to_get_inventory_movements(client: MagicMock) -> None:
    client.list_inventory_movements.return_value = make_async(
        StockMovementListResponse.model_validate(MOVEMENT_LIST)
    )
    result = run(
        gather_context(
            AgentState(customer_message="movements for product 7", intent="inventory_movements"),
            client,
        )
    )

    client.list_inventory_movements.assert_called_once()
    called_kwargs = client.list_inventory_movements.call_args.kwargs
    assert called_kwargs["product_id"] == 7
    assert result["tool_results"]["movements"]["total"] == 1


def test_unknown_intent_gathers_nothing(client: MagicMock) -> None:
    result = run(gather_context(AgentState(customer_message="hi", intent="unknown"), client))
    assert result == {"tool_results": {}}
    client.list_products.assert_not_called()
    client.get_product.assert_not_called()
    client.list_categories.assert_not_called()
    client.list_inventory.assert_not_called()
    client.list_inventory_movements.assert_not_called()


# --- Tool failures become structured state ------------------------------------


@pytest.mark.parametrize(
    ("tool_error", "expected_code"),
    [
        (make_tool_error("not_found"), "not_found"),
        (make_tool_error("invalid_input"), "invalid_input"),
        (make_tool_error("timeout"), "timeout"),
        (make_tool_error("unavailable"), "unavailable"),
        (make_tool_error("rate_limited"), "rate_limited"),
        (make_tool_error("auth"), "auth"),
        (make_tool_error("upstream_error"), "upstream_error"),
        (make_tool_error("unexpected_response"), "unexpected_response"),
    ],
)
def test_tool_error_becomes_structured_state(tool_error: Any, expected_code: str) -> None:
    failing = MagicMock()
    failing.list_products.side_effect = tool_error

    result = run(
        gather_context(AgentState(customer_message="shoes", intent="product_search"), failing)
    )

    assert "products" not in result.get("tool_results", {})
    failure = result["tool_errors"]["gather_context"]
    assert failure.code == expected_code
    assert failure.tool == "gather_context"
    assert failure.message


def test_failed_tool_does_not_raise_out_of_the_node(client: MagicMock) -> None:
    client.list_inventory.side_effect = make_tool_error("timeout")
    # Must not raise — returns a structured failure update instead.
    result = run(
        gather_context(AgentState(customer_message="stock", intent="inventory_check"), client)
    )
    assert result["tool_errors"]["gather_context"].code == "timeout"


# --- compose_reply node -------------------------------------------------------


class FakeGateway:
    """Scriptable LLMGateway fake: one canned response or error per turn."""

    def __init__(
        self, *, response: GenerationResponse | None = None, error: LLMError | None = None
    ):
        self._response = response
        self._error = error
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        return None


def failing_gateway() -> FakeGateway:
    """Gateway whose providers all fail → deterministic safety net."""
    return FakeGateway(error=LLMError("all providers failed"))


def test_compose_uses_products_context() -> None:
    gateway = FakeGateway(
        response=GenerationResponse(
            text="The Trail Shoes look perfect for you.",
            model_used="models/gemini-2.5-flash",
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
    )
    state = AgentState(
        intent="product_search",
        tool_results={
            "products": {"products": [PRODUCT], "total": 1, "page": 1, "per_page": 20, "pages": 1}
        },
    )
    result = run(compose_reply(state, gateway))  # type: ignore[arg-type]
    assert result["draft_response"] == "The Trail Shoes look perfect for you."
    assert result["model_used"] == "models/gemini-2.5-flash"
    assert result["fallback_used"] is False
    # The LLM saw the gathered products.
    assert "Trail Shoes" in gateway.requests[0].prompt


def test_compose_deterministic_safety_net_uses_products_context() -> None:
    state = AgentState(
        intent="product_search",
        tool_results={
            "products": {"products": [PRODUCT], "total": 1, "page": 1, "per_page": 20, "pages": 1}
        },
    )
    result = run(compose_reply(state, failing_gateway()))  # type: ignore[arg-type]
    assert "Trail Shoes" in result["draft_response"]
    assert "model_used" not in result  # attribution unset for deterministic reply


def test_compose_mentions_failure_codes() -> None:
    state = AgentState(
        intent="inventory_check",
        tool_errors={
            "gather_context": ToolFailure(tool="gather_context", code="rate_limited", message="429")
        },
    )
    result = run(compose_reply(state, failing_gateway()))  # type: ignore[arg-type]
    assert "rate_limited" in result["draft_response"]


def test_compose_handles_unknown_gracefully() -> None:
    result = run(compose_reply(AgentState(intent="unknown"), failing_gateway()))  # type: ignore[arg-type]
    assert "not sure how to help" in result["draft_response"]


# --- Full graph runs -----------------------------------------------------------


def test_graph_constructs_and_reaches_final_response(client: MagicMock) -> None:
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))

    graph = build_graph(client, None, failing_gateway())
    final = run(
        graph.ainvoke(
            AgentState(customer_message="I'm looking for hiking shoes"),
            config={"configurable": {"thread_id": "conv-1"}},
        )
    )

    assert final["intent"] == "product_search"
    assert "Trail Shoes" in final["draft_response"]


def test_graph_t6_name_based_product_details_end_to_end(client: MagicMock) -> None:
    """T6, full graph: 'Tell me about Samsung Galaxy S26' → product details."""
    samsung_list = ProductListResponse.model_validate(
        {"products": [SAMSUNG_PRODUCT], "total": 1, "page": 1, "per_page": 5, "pages": 1}
    )
    client.list_products.return_value = make_async(samsung_list)
    client.get_product.return_value = make_async(Product.model_validate(SAMSUNG_PRODUCT))
    gateway = FakeGateway(
        response=GenerationResponse(
            text="The Samsung Galaxy S26 is our flagship phone at 1099.00.",
            model_used="models/gemini-2.5-flash",
        )
    )

    graph = build_graph(client, None, gateway)
    final = run(
        graph.ainvoke(
            AgentState(customer_message="Tell me about Samsung Galaxy S26"),
            config={"configurable": {"thread_id": "conv-t6"}},
        )
    )

    assert final["intent"] == "product_details"
    assert final["tool_results"]["product"]["id"] == 12
    assert not final["tool_errors"]
    assert "Samsung Galaxy S26" in final["draft_response"]


def test_graph_survives_tool_failure_and_still_replies(client: MagicMock) -> None:
    client.list_products.side_effect = make_tool_error("upstream_error")

    graph = build_graph(client, None, failing_gateway())
    final = run(
        graph.ainvoke(
            AgentState(customer_message="looking for shoes"),
            config={"configurable": {"thread_id": "conv-2"}},
        )
    )

    assert final["tool_errors"]["gather_context"].code == "upstream_error"
    assert final["draft_response"]  # graceful degradation, not a crash


def test_graph_compose_uses_gateway_response(client: MagicMock) -> None:
    """The graph's final reply comes from the injected gateway, not templates."""
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))
    gateway = FakeGateway(
        response=GenerationResponse(
            text="Gateway says: Trail Shoes are great.",
            model_used="models/gemini-2.5-flash",
            fallback_used=False,
        )
    )

    graph = build_graph(client, None, gateway)
    final = run(
        graph.ainvoke(
            AgentState(customer_message="I'm looking for hiking shoes"),
            config={"configurable": {"thread_id": "conv-3"}},
        )
    )

    assert final["draft_response"] == "Gateway says: Trail Shoes are great."
    assert final["model_used"] == "models/gemini-2.5-flash"
    assert final["fallback_used"] is False


def test_run_turn_returns_typed_final_state(client: MagicMock) -> None:
    client.list_categories.return_value = make_async([Category.model_validate(CATEGORY)])

    state = run(
        run_turn(
            client,
            customer_message="what categories do you have",
            conversation_id="conv-9",
            gateway=failing_gateway(),
        )
    )

    assert isinstance(state, AgentState)
    assert state.intent == "category_browse"
    assert state.draft_response
    assert state.conversation_id == "conv-9"


# --- Guardrails -----------------------------------------------------------------


def test_agents_package_has_no_http_or_db_logic() -> None:
    """The graph orchestrates only — no httpx, no URLs, no SQLAlchemy, no models."""
    import app.agents.classify as classify_mod
    import app.agents.compose as compose_mod
    import app.agents.gather as gather_mod
    import app.agents.graph as graph_mod

    for module in (classify_mod, compose_mod, gather_mod, graph_mod):
        source = inspect.getsource(module)
        assert "httpx" not in source
        assert "/api/products" not in source
        assert "generativelanguage" not in source
        assert "integrate.api.nvidia" not in source
        assert "sessionmaker" not in source
        assert "create_engine" not in source
        assert not any(
            name.startswith("app.db") or name.startswith("app.models") for name in vars(module)
        )
