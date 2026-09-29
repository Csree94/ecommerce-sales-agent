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


# --- gather_context node -----------------------------------------------------


def test_product_search_routes_to_search_products(client: MagicMock) -> None:
    client.list_products.return_value = make_async(ProductListResponse.model_validate(PRODUCT_LIST))
    result = run(
        gather_context(AgentState(customer_message="shoes", intent="product_search"), client)
    )

    client.list_products.assert_called_once()
    assert "products" in result["tool_results"]


def test_product_details_routes_to_get_product_details(client: MagicMock) -> None:
    client.get_product.return_value = make_async(Product.model_validate(PRODUCT))
    result = run(
        gather_context(
            AgentState(customer_message="details for product 7", intent="product_details"), client
        )
    )

    client.get_product.assert_called_once_with(7)
    assert result["tool_results"]["product"]["id"] == 7


def test_product_details_without_id_is_invalid_input_failure(client: MagicMock) -> None:
    result = run(
        gather_context(
            AgentState(customer_message="tell me more", intent="product_details"), client
        )
    )

    client.get_product.assert_not_called()
    failure = result["tool_errors"]["gather_context"]
    assert failure.code == "invalid_input"


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
