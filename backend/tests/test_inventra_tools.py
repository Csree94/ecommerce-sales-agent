"""Tests for the read-only Inventra agent tools.

Mocks are placed at the ``InventraClient`` boundary (the tools' only
dependency): no real HTTP, no real Inventra server. Coverage: correct client
method + parameter pass-through, typed results reused from the client layer,
structured error translation (failures never become empty results), input
validation, and scope guardrails (strictly read-only surface, no DB access,
no HTTP code inside tools).
"""

import asyncio
import inspect
from collections.abc import Coroutine
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.integrations.inventra import InventraClient
from app.integrations.inventra.errors import (
    InventraAuthError,
    InventraConfigError,
    InventraConnectionError,
    InventraNotFoundError,
    InventraRateLimitedError,
    InventraResponseError,
    InventraServerError,
    InventraTimeoutError,
    InventraValidationError,
)
from app.integrations.inventra.schemas import (
    Category,
    InventoryItem,
    Product,
    ProductListResponse,
    StockMovementListResponse,
)
from app.tools.errors import ToolError, ToolInputError, ToolNotFoundError, ToolUnavailableError
from app.tools.inventra import (
    InventoryMovementParams,
    InventorySearchParams,
    ProductSearchParams,
    check_inventory,
    get_category_details,
    get_inventory_movements,
    get_product_details,
    list_categories,
    search_products,
)


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Execute a tool coroutine (tools are async because the client is)."""
    return asyncio.run(coro)


# Fixtures mirroring the verified Inventra response shapes.
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
    "product_sku": "TS-001",
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
def client() -> InventraClient:
    """Mock at the client boundary — tool tests never touch HTTP."""
    return MagicMock(spec=InventraClient)


# --- Product tools ---------------------------------------------------------


def test_search_products_passes_verified_params(client: InventraClient) -> None:
    client.list_products.return_value = ProductListResponse.model_validate(PRODUCT_LIST)
    params = ProductSearchParams(
        page=2, per_page=5, search="shoe", category_id=3, is_active=True, stock_status="low_stock"
    )

    result = run(search_products(client, params))

    client.list_products.assert_called_once_with(
        page=2, per_page=5, search="shoe", category_id=3, is_active=True, stock_status="low_stock"
    )
    assert isinstance(result, ProductListResponse)
    assert result.total == 1
    product = result.products[0]
    assert isinstance(product, Product)
    assert product.sku == "TS-001"


def test_search_products_defaults(client: InventraClient) -> None:
    client.list_products.return_value = ProductListResponse.model_validate(PRODUCT_LIST)

    run(search_products(client, ProductSearchParams()))

    client.list_products.assert_called_once_with(
        page=1, per_page=20, search=None, category_id=None, is_active=None, stock_status=None
    )


def test_product_search_params_reject_unsupported_page_size(client: InventraClient) -> None:
    with pytest.raises(PydanticValidationError):
        ProductSearchParams(per_page=101)
    client.list_products.assert_not_called()


def test_get_product_details_passes_id_and_returns_typed_product(
    client: InventraClient,
) -> None:
    client.get_product.return_value = Product.model_validate(PRODUCT)

    product = run(get_product_details(client, 7))

    client.get_product.assert_called_once_with(7)
    assert isinstance(product, Product)
    assert product.id == 7


# --- Category tools --------------------------------------------------------


def test_list_categories_returns_typed_list(client: InventraClient) -> None:
    client.list_categories.return_value = [Category.model_validate(CATEGORY)]

    categories = run(list_categories(client))

    client.list_categories.assert_called_once_with()
    assert [c.id for c in categories] == [3]
    assert isinstance(categories[0], Category)


def test_get_category_details_passes_id(client: InventraClient) -> None:
    client.get_category.return_value = Category.model_validate(CATEGORY)

    category = run(get_category_details(client, 3))

    client.get_category.assert_called_once_with(3)
    assert category.name == "Footwear"


# --- Inventory tools -------------------------------------------------------


def test_check_inventory_passes_verified_filters(client: InventraClient) -> None:
    client.list_inventory.return_value = [InventoryItem.model_validate(INVENTORY_ITEM)]
    params = InventorySearchParams(search="shoe", stock_status="in_stock", low_stock_only=True)

    items = run(check_inventory(client, params))

    client.list_inventory.assert_called_once_with(
        search="shoe", stock_status="in_stock", low_stock_only=True
    )
    assert isinstance(items[0], InventoryItem)
    assert items[0].quantity == 4


def test_check_inventory_single_product_uses_search(client: InventraClient) -> None:
    """Per-product stock goes through the list endpoint's search — no invented endpoint."""
    client.list_inventory.return_value = []

    run(check_inventory(client, InventorySearchParams(search="TS-001")))

    client.list_inventory.assert_called_once_with(
        search="TS-001", stock_status=None, low_stock_only=False
    )


def test_inventory_search_params_reject_low_stock_status_value() -> None:
    """Upstream silently ignores stock_status="low_stock" — the tool blocks it."""
    with pytest.raises(PydanticValidationError):
        InventorySearchParams(stock_status="low_stock")


def test_get_inventory_movements_passes_verified_params(client: InventraClient) -> None:
    client.list_inventory_movements.return_value = StockMovementListResponse.model_validate(
        MOVEMENT_LIST
    )
    params = InventoryMovementParams(product_id=7, movement_type="STOCK_IN", page=2, per_page=10)

    result = run(get_inventory_movements(client, params))

    client.list_inventory_movements.assert_called_once_with(
        product_id=7, movement_type="STOCK_IN", page=2, per_page=10
    )
    assert isinstance(result, StockMovementListResponse)
    assert result.movements[0].movement_type == "STOCK_IN"


def test_movement_params_reject_upstream_page_cap() -> None:
    with pytest.raises(PydanticValidationError):
        InventoryMovementParams(per_page=101)


# --- Error translation (failures never become empty results) ---------------


@pytest.mark.parametrize(
    ("client_error", "expected_tool_error", "expected_code"),
    [
        (InventraNotFoundError("nf"), ToolNotFoundError, "not_found"),
        (InventraValidationError("bad"), ToolInputError, "invalid_input"),
        (InventraTimeoutError("t/o"), ToolUnavailableError, "timeout"),
        (InventraConnectionError("conn"), ToolUnavailableError, "unavailable"),
        (InventraServerError("5xx"), ToolUnavailableError, "upstream_error"),
        (InventraRateLimitedError("429"), ToolUnavailableError, "rate_limited"),
        (InventraAuthError("401"), ToolUnavailableError, "auth"),
        (InventraConfigError("cfg"), ToolUnavailableError, "config"),
        (InventraResponseError("shape"), ToolUnavailableError, "unexpected_response"),
    ],
)
def test_client_errors_translate_to_typed_tool_errors(
    client: InventraClient,
    client_error: Exception,
    expected_tool_error: type[ToolError],
    expected_code: str,
) -> None:
    client.list_products.side_effect = client_error

    with pytest.raises(expected_tool_error) as excinfo:
        run(search_products(client, ProductSearchParams()))

    assert excinfo.value.code == expected_code
    assert isinstance(excinfo.value.__cause__, type(client_error))


def test_unavailable_error_covers_all_reachable_failures(client: InventraClient) -> None:
    """The graph-degradable bucket: every non-input/non-notfound failure."""
    for exc in (
        InventraTimeoutError("t/o"),
        InventraConnectionError("conn"),
        InventraServerError("5xx"),
        InventraRateLimitedError("429"),
        InventraAuthError("401"),
        InventraConfigError("cfg"),
        InventraResponseError("shape"),
    ):
        client.list_inventory.side_effect = exc
        with pytest.raises(ToolUnavailableError):
            run(check_inventory(client, InventorySearchParams()))


def test_tool_errors_do_not_leak_credentials(client: InventraClient) -> None:
    """Error messages carry no tokens even when the client message did."""
    client.get_product.side_effect = InventraAuthError("Inventra rejected credentials (401)")

    with pytest.raises(ToolUnavailableError) as excinfo:
        run(get_product_details(client, 1))

    assert "Bearer" not in str(excinfo.value)
    assert "Authorization" not in str(excinfo.value)


# --- Scope guardrails (strictly read-only, no DB, no HTTP in tools) --------


def test_tool_surface_is_the_six_read_operations_plus_the_purchase_write() -> None:
    """The six read functions plus the single purchase write (milestone 3B).

    ``stock_out_product`` is the ONLY write tool — one verified Inventra
    endpoint, added deliberately (sales-agent purchase deduction).
    """
    import app.tools.inventra as pkg

    public_callables = {
        name
        for name, member in vars(pkg).items()
        if callable(member) and not name.startswith("_") and not inspect.isclass(member)
    }
    assert public_callables == {
        "search_products",
        "get_product_details",
        "list_categories",
        "get_category_details",
        "check_inventory",
        "get_inventory_movements",
        "stock_out_product",
    }


def test_no_write_functionality_beyond_the_single_purchase_tool() -> None:
    """No create/update/delete/stock-change operations may exist anywhere.

    ``stock_out_product`` is the deliberate single exception (purchase flow,
    milestone 3B); every other write — including admin stock-in/adjust —
    must stay absent.
    """
    import app.tools.inventra as pkg

    forbidden_fragments = (
        "create_product",
        "update_product",
        "delete_product",
        "deactivate_product",
        "stock_in",
        "adjust_stock",
        "update_threshold",
    )
    modules = [m for m in vars(pkg).values() if inspect.ismodule(m)]
    for module in modules:
        for fragment in forbidden_fragments:
            assert not hasattr(module, fragment), f"{module.__name__}.{fragment} must not exist"


def test_tools_are_async_and_depend_only_on_the_client() -> None:
    """Tools are coroutine functions taking the client — ready for LangGraph."""
    for tool in (
        search_products,
        get_product_details,
        list_categories,
        get_category_details,
        check_inventory,
        get_inventory_movements,
    ):
        assert inspect.iscoroutinefunction(tool)
        params = list(inspect.signature(tool).parameters.values())
        # Annotations are strings under ``from __future__ import annotations``.
        assert params[0].annotation in (InventraClient, "InventraClient")


def test_tools_have_no_http_or_database_knowledge() -> None:
    """Tools must not import httpx or the DB layer — the client is the boundary."""
    import app.tools.inventra.categories as categories_mod
    import app.tools.inventra.inventory as inventory_mod
    import app.tools.inventra.products as products_mod

    for module in (products_mod, categories_mod, inventory_mod):
        assert "httpx" not in vars(module)
        assert not any(
            name.startswith("app.db") or name.startswith("app.models") for name in vars(module)
        )


def test_tool_input_models_are_frozen_pydantic() -> None:
    for model in (ProductSearchParams, InventorySearchParams, InventoryMovementParams):
        assert model.model_config.get("frozen") is True
        instance = model()
        with pytest.raises(PydanticValidationError):
            instance.page = 2  # type: ignore[attr-defined]  # frozen models reject mutation
