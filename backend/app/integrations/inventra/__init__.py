"""Inventra integration (HTTP client).

Scope (verified Inventra API, September 2026): typed access to products,
categories and inventory — reads plus exactly ONE write, the sales-agent
purchase deduction (``POST /api/inventory/{id}/stock-out``), which is sent as
a single explicit attempt and never retried. No other write exists here:
stock-in/adjust/threshold stay admin-only, and product/inventory truth stays
in Inventra; this package talks HTTP only (never Inventra's database).

Structure:
- ``client``   — ``InventraClient`` (httpx; the only outbound path)
- ``schemas``  — Pydantic response contracts (data-transfer only, NOT ORM)
- ``auth``     — replaceable ``InventraAuthProvider`` (currently static bearer)
- ``errors``   — typed integration errors for graceful degradation
"""

from app.config.inventra import InventraSettings, get_inventra_settings
from app.integrations.inventra.auth import (
    InventraAuthProvider,
    StaticBearerTokenProvider,
    build_auth_provider,
)
from app.integrations.inventra.client import InventraClient
from app.integrations.inventra.errors import (
    InventraAuthError,
    InventraConfigError,
    InventraConnectionError,
    InventraError,
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
    MovementType,
    Product,
    ProductListResponse,
    StockMovement,
    StockMovementListResponse,
    StockOutResult,
    StockStatus,
)

__all__ = [
    "Category",
    "InventoryItem",
    "InventraAuthProvider",
    "InventraDashboardStats",
    "InventraAuthError",
    "InventraClient",
    "InventraConfigError",
    "InventraConnectionError",
    "InventraError",
    "InventraNotFoundError",
    "InventraRateLimitedError",
    "InventraResponseError",
    "InventraServerError",
    "InventraSettings",
    "InventraTimeoutError",
    "InventraValidationError",
    "MovementType",
    "Product",
    "ProductListResponse",
    "StaticBearerTokenProvider",
    "StockMovement",
    "StockMovementListResponse",
    "StockOutResult",
    "StockStatus",
    "build_auth_provider",
    "get_inventra_settings",
]
