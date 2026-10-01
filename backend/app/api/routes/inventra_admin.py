"""Admin dashboard proxy over the Inventra API — read-only management views.

The browser never talks to Inventra directly. Requests arrive here with the
standard Project 1 admin JWT (``require_admin``) and are fulfilled through the
shared ``InventraClient`` (``app.state.inventra_client``), so the Inventra
bearer token stays entirely server-side and the two identity systems never
mix. ``require_admin`` is declared before the Inventra dependency on purpose:
an unauthenticated caller gets 401 before integration status is revealed.

Read-only by design: only the verified GET endpoints are proxied, reusing the
client layer's response contracts (no duplication, no new schemas). Inventra
remains the source of truth for product/stock data — no write endpoints exist
here, and writes belong to Inventra's own UI, not the agent side.

Error mapping (stable details, no credential or path material):

- Inventra not configured (no ``INVENTRA_BASE_URL``)            → 503 (deps)
- ``InventraNotFoundError`` (upstream 404)                      → 404
- ``InventraValidationError`` / ``InventraResponseError``       → 502 (contract drift)
- timeout / connection / 5xx / 429 / auth / config              → 503 (upstream unavailable)

Proxied endpoints (all read-only):
- GET /api/v1/admin/products            → upstream GET /api/products
- GET /api/v1/admin/products/{id}       → upstream GET /api/products/{id}
- GET /api/v1/admin/inventory           → upstream GET /api/inventory
- GET /api/v1/admin/inventory/movements → upstream GET /api/inventory/movements
- GET /api/v1/admin/categories          → upstream GET /api/categories
- GET /api/v1/admin/dashboard/stats     → upstream GET /api/dashboard
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.api.admin_deps import AdminContext, require_admin
from app.api.deps import get_inventra_client
from app.integrations.inventra import InventraClient, StockStatus
from app.integrations.inventra.errors import (
    InventraError,
    InventraNotFoundError,
    InventraResponseError,
    InventraValidationError,
)
from app.integrations.inventra.schemas import (
    Category,
    InventoryItem,
    InventraDashboardStats,
    MovementType,
    Product,
    ProductListResponse,
    StockMovementListResponse,
)

# Upstream movements endpoint caps per_page at 100 (Query(50, ge=1, le=100)).
_MAX_MOVEMENTS_PER_PAGE = 100

router = APIRouter(prefix="/admin", tags=["admin-inventra"])

# Upstream caps per_page at 100 (Query(20, ge=1, le=100)).
_MAX_PER_PAGE = 100
_SEARCH_MAX_LENGTH = 200


def _inventra_error_to_http(exc: InventraError) -> HTTPException:
    """Map a typed client error onto a stable, non-leaking HTTP error."""
    if isinstance(exc, InventraNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )
    if isinstance(exc, (InventraValidationError, InventraResponseError)):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Unexpected Inventra response"
        )
    # Timeout, connection failure, 5xx, 429, auth rejection, misconfiguration:
    # all are "the upstream is not usable right now" from the dashboard's view.
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Inventra is unavailable"
    )


@router.get("/products", response_model=ProductListResponse)
async def list_products(
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=_MAX_PER_PAGE),
    search: str | None = Query(default=None, max_length=_SEARCH_MAX_LENGTH),
    category_id: int | None = Query(default=None, ge=1),
    is_active: bool | None = Query(default=None),
    stock_status: StockStatus | None = Query(default=None),
) -> ProductListResponse:
    """Proxy ``GET /api/products`` with the verified Inventra filter set only."""
    try:
        return await inventra.list_products(
            page=page,
            per_page=per_page,
            search=search,
            category_id=category_id,
            is_active=is_active,
            stock_status=stock_status,
        )
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc


@router.get("/products/{product_id}", response_model=Product)
async def get_product(
    product_id: int = Path(ge=1),
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
) -> Product:
    """Proxy ``GET /api/products/{id}`` (404 passes through for unknown ids)."""
    try:
        return await inventra.get_product(product_id)
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc


@router.get("/inventory", response_model=list[InventoryItem])
async def list_inventory(
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
    search: str | None = Query(default=None, max_length=_SEARCH_MAX_LENGTH),
    stock_status: Literal["in_stock", "out_of_stock"] | None = Query(default=None),
    low_stock_only: bool = Query(default=False),
) -> list[InventoryItem]:
    """Proxy ``GET /api/inventory`` (bare array; no per-product route upstream)."""
    try:
        return await inventra.list_inventory(
            search=search,
            stock_status=stock_status,
            low_stock_only=low_stock_only,
        )
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc


@router.get("/inventory/movements", response_model=StockMovementListResponse)
async def list_inventory_movements(
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
    product_id: int | None = Query(default=None, ge=1),
    movement_type: MovementType | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=_MAX_MOVEMENTS_PER_PAGE),
) -> StockMovementListResponse:
    """Proxy ``GET /api/inventory/movements`` (audit history, read-only)."""
    try:
        return await inventra.list_inventory_movements(
            product_id=product_id,
            movement_type=movement_type,
            page=page,
            per_page=per_page,
        )
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc


@router.get("/categories", response_model=list[Category])
async def list_categories(
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
) -> list[Category]:
    """Proxy ``GET /api/categories`` (bare array upstream, no filters)."""
    try:
        return await inventra.list_categories()
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc


@router.get("/dashboard/stats", response_model=InventraDashboardStats)
async def get_dashboard_stats(
    _admin: AdminContext = Depends(require_admin),
    inventra: InventraClient = Depends(get_inventra_client),
) -> InventraDashboardStats:
    """Proxy ``GET /api/dashboard`` (aggregate stats; upstream-cached 120 s)."""
    try:
        return await inventra.get_dashboard_stats()
    except InventraError as exc:
        raise _inventra_error_to_http(exc) from exc
