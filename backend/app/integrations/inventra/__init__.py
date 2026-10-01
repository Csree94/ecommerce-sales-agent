"""Inventra integration (read-only HTTP client).

Scope (verified Inventra API, September 2026): typed read-only access to
products, categories and inventory. No write operations exist here — the
Sales Agent never mutates Inventra data. Product/inventory truth stays in
Inventra; this package talks HTTP only (never Inventra's database).

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
    "StockStatus",
    "build_auth_provider",
    "get_inventra_settings",
]
