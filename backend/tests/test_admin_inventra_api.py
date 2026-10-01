"""Tests for the admin Inventra proxy endpoints (Milestone 2 — Products).

No live database and no live Inventra server (project convention): admin
credentials/JWT material come from monkeypatched env vars (same helpers as
``test_admin_api.py``), and the Inventra client is faked at the exact seam the
routes use — ``get_inventra_client`` is overridden with an
``AsyncMock``-backed ``MagicMock(spec=InventraClient)``.

Coverage:
1. unauthenticated request              → 401 (auth checked before integration status)
2. admin authentication works           → 200 with valid JWT
3. integration unavailable (state None) → 503
4. query parameters forwarded           → client called with the exact filters
5. product list response shape          → envelope fields surface unchanged
6. product detail                       → 200 single product; invalid id → 422
7. upstream error mapping               → typed client errors → 503/502/404
Plus: invalid query bounds → 422, and no credential material in any response.
"""

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_inventra_client
from app.integrations.inventra import InventraClient
from app.integrations.inventra.errors import (
    InventraAuthError,
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
    InventraDashboardStats,
    Product,
    ProductListResponse,
    StockMovementListResponse,
)

TEST_PASSWORD = "correct-horse-battery"
TEST_SECRET = "unit-test-jwt-secret"


def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a fully-configured admin auth environment (mirrors test_admin_api)."""
    from app.api.admin_deps import hash_password

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(TEST_PASSWORD))
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("ADMIN_JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")


@contextmanager
def make_client(monkeypatch: pytest.MonkeyPatch):
    """Fresh app + entered TestClient with admin env applied (cache reset)."""
    from app.config.settings import get_settings
    from app.main import create_app

    admin_env(monkeypatch)
    get_settings.cache_clear()  # env changes must reach the freshly built app
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def login_token(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# Verified Inventra response shapes (same fixtures as test_inventra_client.py).
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
CATEGORY = {
    "id": 3,
    "name": "Footwear",
    "description": "Shoes and boots",
    "created_at": "2026-01-10T09:00:00Z",
    "updated_at": "2026-01-10T09:00:00Z",
}
DASH_STATS = {
    "total_products": 1,
    "active_products": 1,
    "total_stock": 4,
    "low_stock_count": 1,
    "out_of_stock_count": 0,
    "total_stock_in": 5,
    "total_stock_out": 1,
    "recent_movements": [
        {
            "id": 21,
            "product_name": "Trail Shoes",
            "movement_type": "STOCK_IN",
            "quantity": 5,
            "username": "admin",
            "notes": "restock",
            "created_at": "2026-02-01T12:30:00Z",
        }
    ],
    "low_stock_products": [
        {
            "product_id": 7,
            "product_name": "Trail Shoes",
            "product_sku": "TS-001",
            "quantity": 4,
            "threshold": 10,
        }
    ],
}


def make_inventra_mock() -> MagicMock:
    """InventraClient double: async methods mocked at the client boundary."""
    client = MagicMock(spec=InventraClient)
    client.list_products = AsyncMock(
        return_value=ProductListResponse.model_validate(PRODUCT_LIST)
    )
    client.get_product = AsyncMock(return_value=Product.model_validate(PRODUCT))
    client.list_inventory = AsyncMock(
        return_value=[InventoryItem.model_validate(INVENTORY_ITEM)]
    )
    client.list_inventory_movements = AsyncMock(
        return_value=StockMovementListResponse.model_validate(MOVEMENT_LIST)
    )
    client.list_categories = AsyncMock(return_value=[Category.model_validate(CATEGORY)])
    client.get_dashboard_stats = AsyncMock(
        return_value=InventraDashboardStats.model_validate(DASH_STATS)
    )
    return client


@pytest.fixture()
def inventra_mock() -> MagicMock:
    return make_inventra_mock()


@pytest.fixture()
def authed_client(monkeypatch: pytest.MonkeyPatch, inventra_mock: MagicMock) -> TestClient:
    """App with admin env applied and the Inventra client dependency faked."""
    with make_client(monkeypatch) as test_client:
        test_client.app.dependency_overrides[get_inventra_client] = lambda: inventra_mock
        yield test_client


# --- 1. Unauthenticated requests (auth must precede integration status) -------


def test_products_without_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    with make_client(monkeypatch) as test_client:
        test_client.app.state.inventra_client = None  # integration ALSO absent
        resp = test_client.get("/api/v1/admin/products")
        assert resp.status_code == 401  # 401 wins over 503 — auth checked first
        assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_products_with_garbage_token_is_401(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/v1/admin/products", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


# --- 2. Admin authentication works ----------------------------------------------


def test_products_with_valid_token_returns_200(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get("/api/v1/admin/products", headers=login_token(authed_client))

    assert resp.status_code == 200
    inventra_mock.list_products.assert_awaited_once()


# --- 3. Integration unavailable → 503 --------------------------------------------


def test_products_when_inventra_not_configured_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with make_client(monkeypatch) as test_client:
        test_client.app.state.inventra_client = None
        resp = test_client.get("/api/v1/admin/products", headers=login_token(test_client))
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Inventra integration is not configured"


# --- 4. Query parameters are forwarded exactly ------------------------------------


def test_products_forward_all_filters(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get(
        "/api/v1/admin/products",
        params={
            "page": 2,
            "per_page": 5,
            "search": "shoe",
            "category_id": 3,
            "is_active": "true",
            "stock_status": "low_stock",
        },
        headers=login_token(authed_client),
    )

    assert resp.status_code == 200
    inventra_mock.list_products.assert_awaited_once_with(
        page=2,
        per_page=5,
        search="shoe",
        category_id=3,
        is_active=True,
        stock_status="low_stock",
    )


def test_products_defaults_forward_none_filters(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get("/api/v1/admin/products", headers=login_token(authed_client))

    assert resp.status_code == 200
    inventra_mock.list_products.assert_awaited_once_with(
        page=1,
        per_page=20,
        search=None,
        category_id=None,
        is_active=None,
        stock_status=None,
    )


def test_products_reject_out_of_bounds_params(authed_client: TestClient) -> None:
    headers = login_token(authed_client)
    assert (
        authed_client.get(
            "/api/v1/admin/products", params={"page": 0}, headers=headers
        ).status_code
        == 422
    )
    assert (
        authed_client.get(
            "/api/v1/admin/products", params={"per_page": 101}, headers=headers
        ).status_code
        == 422
    )
    assert (
        authed_client.get(
            "/api/v1/admin/products", params={"stock_status": "bogus"}, headers=headers
        ).status_code
        == 422
    )


# --- 5. Product list response shape ------------------------------------------------


def test_products_response_shape(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/v1/admin/products", headers=login_token(authed_client))

    body: dict[str, Any] = resp.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["per_page"] == 20
    assert body["pages"] == 1
    product = body["products"][0]
    assert product["id"] == 7
    assert product["name"] == "Trail Shoes"
    assert product["sku"] == "TS-001"
    assert product["category_name"] == "Footwear"
    assert product["price"] == 129.99
    assert product["is_active"] is True


# --- 6. Product detail ---------------------------------------------------------------


def test_product_detail_returns_product(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get("/api/v1/admin/products/7", headers=login_token(authed_client))

    assert resp.status_code == 200
    inventra_mock.get_product.assert_awaited_once_with(7)
    body = resp.json()
    assert body["id"] == 7
    assert body["name"] == "Trail Shoes"
    assert body["sku"] == "TS-001"


def test_product_detail_rejects_invalid_id(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/v1/admin/products/0", headers=login_token(authed_client))
    assert resp.status_code == 422


# --- 7. Upstream error mapping --------------------------------------------------------


@pytest.mark.parametrize(
    ("client_error", "expected_status"),
    [
        (InventraNotFoundError("nf"), 404),
        (InventraValidationError("bad"), 502),
        (InventraResponseError("shape"), 502),
        (InventraTimeoutError("t/o"), 503),
        (InventraConnectionError("conn"), 503),
        (InventraServerError("5xx"), 503),
        (InventraRateLimitedError("429"), 503),
        (InventraAuthError("401"), 503),
    ],
)
def test_product_detail_maps_upstream_errors(
    authed_client: TestClient,
    inventra_mock: MagicMock,
    client_error: Exception,
    expected_status: int,
) -> None:
    inventra_mock.get_product.side_effect = client_error

    resp = authed_client.get("/api/v1/admin/products/7", headers=login_token(authed_client))

    assert resp.status_code == expected_status
    assert "Bearer" not in resp.text
    assert "Authorization" not in resp.text


def test_products_list_maps_upstream_errors_to_503(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    inventra_mock.list_products.side_effect = InventraTimeoutError("t/o")

    resp = authed_client.get("/api/v1/admin/products", headers=login_token(authed_client))

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Inventra is unavailable"


# --- Inventory / movements / categories / dashboard (Milestone 2, Parts 2-5) ---

_NEW_ENDPOINTS = (
    "/api/v1/admin/inventory",
    "/api/v1/admin/inventory/movements",
    "/api/v1/admin/categories",
    "/api/v1/admin/dashboard/stats",
)


def test_new_endpoints_without_token_are_401(monkeypatch: pytest.MonkeyPatch) -> None:
    with make_client(monkeypatch) as test_client:
        test_client.app.state.inventra_client = None
        for endpoint in _NEW_ENDPOINTS:
            resp = test_client.get(endpoint)
            assert resp.status_code == 401, endpoint
            assert resp.headers["WWW-Authenticate"] == "Bearer", endpoint


def test_new_endpoints_when_inventra_not_configured_are_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with make_client(monkeypatch) as test_client:
        test_client.app.state.inventra_client = None
        headers = login_token(test_client)
        for endpoint in _NEW_ENDPOINTS:
            resp = test_client.get(endpoint, headers=headers)
            assert resp.status_code == 503, endpoint
            assert resp.json()["detail"] == "Inventra integration is not configured", endpoint


def test_inventory_forwards_filters_and_shape(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get(
        "/api/v1/admin/inventory",
        params={"search": "shoe", "stock_status": "in_stock", "low_stock_only": "true"},
        headers=login_token(authed_client),
    )

    assert resp.status_code == 200
    inventra_mock.list_inventory.assert_awaited_once_with(
        search="shoe", stock_status="in_stock", low_stock_only=True
    )
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1
    item = body[0]
    assert item["product_id"] == 7
    assert item["product_name"] == "Trail Shoes"
    assert item["product_sku"] == "TS-001"
    assert item["quantity"] == 4
    assert item["low_stock_threshold"] == 10
    assert item["is_low_stock"] is True


def test_inventory_rejects_invalid_stock_status(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/v1/admin/inventory",
        params={"stock_status": "low_stock"},
        headers=login_token(authed_client),
    )
    assert resp.status_code == 422


def test_movements_forward_params_and_shape(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get(
        "/api/v1/admin/inventory/movements",
        params={"product_id": 7, "movement_type": "STOCK_IN", "page": 2, "per_page": 10},
        headers=login_token(authed_client),
    )

    assert resp.status_code == 200
    inventra_mock.list_inventory_movements.assert_awaited_once_with(
        product_id=7, movement_type="STOCK_IN", page=2, per_page=10
    )
    body = resp.json()
    assert body["total"] == 1 and body["page"] == 1
    movement = body["movements"][0]
    assert movement["product_name"] == "Trail Shoes"
    assert movement["movement_type"] == "STOCK_IN"
    assert movement["quantity"] == 5
    assert movement["username"] == "admin"


def test_movements_reject_invalid_params(authed_client: TestClient) -> None:
    headers = login_token(authed_client)
    assert (
        authed_client.get(
            "/api/v1/admin/inventory/movements", params={"per_page": 101}, headers=headers
        ).status_code
        == 422
    )
    assert (
        authed_client.get(
            "/api/v1/admin/inventory/movements",
            params={"movement_type": "BOGUS"},
            headers=headers,
        ).status_code
        == 422
    )


def test_categories_forward_no_params_and_shape(
    authed_client: TestClient, inventra_mock: MagicMock
) -> None:
    resp = authed_client.get("/api/v1/admin/categories", headers=login_token(authed_client))

    assert resp.status_code == 200
    inventra_mock.list_categories.assert_awaited_once_with()
    body = resp.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["id"] == 3
    assert body[0]["name"] == "Footwear"
    assert body[0]["description"] == "Shoes and boots"


def test_dashboard_stats_shape(authed_client: TestClient, inventra_mock: MagicMock) -> None:
    resp = authed_client.get(
        "/api/v1/admin/dashboard/stats", headers=login_token(authed_client)
    )

    assert resp.status_code == 200
    inventra_mock.get_dashboard_stats.assert_awaited_once_with()
    body = resp.json()
    assert body["total_products"] == 1
    assert body["total_stock"] == 4
    assert body["low_stock_count"] == 1
    assert body["out_of_stock_count"] == 0
    assert body["total_stock_in"] == 5
    assert body["total_stock_out"] == 1
    assert body["recent_movements"][0]["product_name"] == "Trail Shoes"
    assert body["low_stock_products"][0]["product_sku"] == "TS-001"


@pytest.mark.parametrize(
    ("endpoint", "client_attr", "client_error", "expected_status"),
    [
        ("/api/v1/admin/inventory", "list_inventory", InventraNotFoundError("nf"), 404),
        ("/api/v1/admin/inventory", "list_inventory", InventraServerError("5xx"), 503),
        (
            "/api/v1/admin/inventory/movements",
            "list_inventory_movements",
            InventraRateLimitedError("429"),
            503,
        ),
        (
            "/api/v1/admin/categories",
            "list_categories",
            InventraResponseError("shape"),
            502,
        ),
        (
            "/api/v1/admin/dashboard/stats",
            "get_dashboard_stats",
            InventraAuthError("401"),
            503,
        ),
        (
            "/api/v1/admin/dashboard/stats",
            "get_dashboard_stats",
            InventraConnectionError("conn"),
            503,
        ),
    ],
)
def test_new_endpoints_map_upstream_errors(
    authed_client: TestClient,
    inventra_mock: MagicMock,
    endpoint: str,
    client_attr: str,
    client_error: Exception,
    expected_status: int,
) -> None:
    getattr(inventra_mock, client_attr).side_effect = client_error

    resp = authed_client.get(endpoint, headers=login_token(authed_client))

    assert resp.status_code == expected_status, endpoint
    assert "Bearer" not in resp.text
    assert "Authorization" not in resp.text
