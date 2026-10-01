"""Tests for the read-only Inventra client.

All HTTP traffic is mocked via ``httpx.MockTransport`` (injected through the
client's ``transport`` seam) — the real Inventra server is never contacted.
Coverage: request paths/params for every verified endpoint, parsing into the
typed response contracts, HTTP 4xx/5xx handling, timeout/connection handling,
auth-header handling without credential leakage, absence of write methods, and
absence of Product/Inventory SQLAlchemy models in the Sales Agent database.
"""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.inventra import InventraClient, InventraSettings
from app.integrations.inventra.auth import StaticBearerTokenProvider
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
from app.integrations.inventra.schemas import InventoryItem, Product

# Fake test credential only — never a real token (see pyproject per-file-ignores).
TEST_TOKEN = "test-token-123"

# Verified Inventra response shapes (from Inventra's schemas/services).
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
PRODUCT_LIST = {"products": [PRODUCT], "total": 1, "page": 1, "per_page": 20, "pages": 1}
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
MOVEMENT = {
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
MOVEMENT_LIST = {"movements": [MOVEMENT], "total": 1, "page": 1, "per_page": 50}


def make_settings(**overrides: Any) -> InventraSettings:
    """Hermetic settings (init kwargs override env vars; no .env is read)."""
    values: dict[str, Any] = {
        "_env_file": None,
        "INVENTRA_BASE_URL": "https://inventra.test",
        "INVENTRA_BEARER_TOKEN": TEST_TOKEN,
        "INVENTRA_TIMEOUT_SECONDS": 1.0,
        "INVENTRA_MAX_RETRIES": 0,
        "INVENTRA_RETRY_BACKOFF_SECONDS": 0.0,
    }
    values.update(overrides)
    return InventraSettings(**values)


@contextmanager
def inventra_client(
    settings: InventraSettings, handler: Callable[[httpx.Request], httpx.Response]
) -> Iterator[InventraClient]:
    """Client bound to a mock transport; the pool is closed on exit."""
    client = InventraClient(settings, transport=httpx.MockTransport(handler))
    try:
        yield client
    finally:
        asyncio.run(client.aclose())


# --- Endpoint request paths, parameters, and parsing ----------------------


def test_list_products_request_path_and_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=PRODUCT_LIST)

    with inventra_client(make_settings(), handler) as client:
        result = asyncio.run(
            client.list_products(
                page=2,
                per_page=5,
                search="shoe",
                category_id=3,
                is_active=True,
                stock_status="low_stock",
            )
        )

    assert seen["path"] == "/api/products"
    assert seen["params"] == {
        "page": "2",
        "per_page": "5",
        "search": "shoe",
        "category_id": "3",
        "is_active": "true",
        "stock_status": "low_stock",
    }
    assert result.total == 1
    product: Product = result.products[0]
    assert product.sku == "TS-001"
    assert product.price == 129.99
    assert product.category_name == "Footwear"


def test_list_products_default_call_sends_only_paging_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=PRODUCT_LIST)

    with inventra_client(make_settings(), handler) as client:
        asyncio.run(client.list_products())

    # Unset filters must NOT be sent (verified params only).
    assert seen["params"] == {"page": "1", "per_page": "20"}


def test_get_product_request_path_and_parsing() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=PRODUCT)

    with inventra_client(make_settings(), handler) as client:
        product = asyncio.run(client.get_product(7))

    assert seen["path"] == "/api/products/7"
    assert product.id == 7
    assert product.name == "Trail Shoes"


def test_list_categories_parses_bare_array() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[CATEGORY])

    with inventra_client(make_settings(), handler) as client:
        categories = asyncio.run(client.list_categories())

    assert seen["path"] == "/api/categories"
    assert seen["params"] == {}  # no query parameters are defined upstream
    assert [c.name for c in categories] == ["Footwear"]


def test_list_inventory_filters_and_parsing() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[INVENTORY_ITEM])

    with inventra_client(make_settings(), handler) as client:
        items = asyncio.run(
            client.list_inventory(search="shoe", stock_status="in_stock", low_stock_only=True)
        )

    assert seen["path"] == "/api/inventory"
    assert seen["params"] == {
        "search": "shoe",
        "stock_status": "in_stock",
        "low_stock_only": "true",
    }
    item: InventoryItem = items[0]
    assert item.product_id == 7
    assert item.quantity == 4
    assert item.is_low_stock is True


def test_list_inventory_movements_params_and_parsing() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=MOVEMENT_LIST)

    with inventra_client(make_settings(), handler) as client:
        result = asyncio.run(
            client.list_inventory_movements(product_id=7, movement_type="STOCK_IN")
        )

    assert seen["path"] == "/api/inventory/movements"
    assert seen["params"] == {
        "product_id": "7",
        "movement_type": "STOCK_IN",
        "page": "1",
        "per_page": "50",
    }
    assert result.total == 1
    assert result.movements[0].movement_type == "STOCK_IN"
    assert result.movements[0].username == "admin"


# --- HTTP error handling ---------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, InventraAuthError),
        (404, InventraNotFoundError),
        (422, InventraValidationError),
        (429, InventraRateLimitedError),
    ],
)
def test_http_4xx_maps_to_typed_errors(status_code: int, expected_error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "boom"})

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(expected_error):
            asyncio.run(client.get_product(1))


def test_http_5xx_is_retried_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] <= 2:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(200, json=PRODUCT_LIST)

    with inventra_client(make_settings(INVENTRA_MAX_RETRIES=2), handler) as client:
        result = asyncio.run(client.list_products())

    assert calls["count"] == 3  # 2 retries + final success
    assert result.total == 1


def test_http_5xx_exhausted_retries_raise_server_error() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    with inventra_client(make_settings(INVENTRA_MAX_RETRIES=2), handler) as client:
        with pytest.raises(InventraServerError):
            asyncio.run(client.list_products())

    assert calls["count"] == 3  # bounded: initial + max_retries


def test_timeout_is_retried_then_raises() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectTimeout("timed out")

    with inventra_client(make_settings(INVENTRA_MAX_RETRIES=1), handler) as client:
        with pytest.raises(InventraTimeoutError):
            asyncio.run(client.get_product(7))

    assert calls["count"] == 2


def test_connection_error_is_retried_then_raises() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectError("connection refused")

    with inventra_client(make_settings(INVENTRA_MAX_RETRIES=1), handler) as client:
        with pytest.raises(InventraConnectionError):
            asyncio.run(client.list_inventory())

    assert calls["count"] == 2


# --- Malformed / unexpected responses --------------------------------------


def test_malformed_json_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json{{")

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(InventraResponseError):
            asyncio.run(client.list_products())


def test_contract_mismatch_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Envelope fields missing — must not be parsed into a "successful" result.
        return httpx.Response(200, json={"products": [{"id": 1}]})

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(InventraResponseError):
            asyncio.run(client.list_products())


def test_categories_non_array_payload_raises_response_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"categories": []})

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(InventraResponseError):
            asyncio.run(client.list_categories())


# --- Authentication --------------------------------------------------------


def test_authorization_header_is_bearer_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json=PRODUCT_LIST)

    with inventra_client(make_settings(), handler) as client:
        asyncio.run(client.list_products())

    assert seen["auth"] == f"Bearer {TEST_TOKEN}"


def test_auth_error_does_not_expose_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Could not validate credentials"})

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(InventraAuthError) as excinfo:
            asyncio.run(client.get_product(1))

    assert TEST_TOKEN not in str(excinfo.value)


def test_missing_credential_raises_auth_error_without_leaking() -> None:
    provider = StaticBearerTokenProvider(SecretStr(""))
    with pytest.raises(InventraAuthError):
        provider.headers()


def test_missing_base_url_raises_config_error() -> None:
    with pytest.raises(InventraConfigError):
        InventraClient(make_settings(INVENTRA_BASE_URL=""))


# --- Scope guardrails -------------------------------------------------------


def test_client_exposes_only_verified_read_methods() -> None:
    """No write methods, no guessed endpoints — read-only surface only.

    ``aclose`` is the lifecycle method, not an API endpoint.
    """
    methods = {
        name
        for name, member in vars(InventraClient).items()
        if callable(member) and not name.startswith("_")
    }
    assert methods == {
        "aclose",
        "list_products",
        "get_product",
        "list_categories",
        "get_category",
        "list_inventory",
        "list_inventory_movements",
        "get_dashboard_stats",
    }


def test_get_category_request_path_and_parsing() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=CATEGORY)

    with inventra_client(make_settings(), handler) as client:
        category = asyncio.run(client.get_category(3))

    assert seen["path"] == "/api/categories/3"
    assert category.id == 3
    assert category.name == "Footwear"


def test_get_category_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Category not found"})

    with inventra_client(make_settings(), handler) as client:
        with pytest.raises(InventraNotFoundError):
            asyncio.run(client.get_category(999))


def test_no_product_or_inventory_tables_in_sales_agent_db() -> None:
    """Product/inventory truth stays in Inventra — never in our metadata."""
    from app.models import Base

    forbidden = {"products", "inventory"}
    assert not (forbidden & set(Base.metadata.tables))


def test_inventra_schemas_are_pydantic_not_orm() -> None:
    """The Inventra contracts are data-transfer models, not SQLAlchemy models."""
    from pydantic import BaseModel as PydanticBaseModel

    assert issubclass(Product, PydanticBaseModel)
    assert issubclass(InventoryItem, PydanticBaseModel)
    assert not hasattr(Product, "__tablename__")
    assert not hasattr(InventoryItem, "__tablename__")
