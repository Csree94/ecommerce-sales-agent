"""Typed Inventra API response contracts (Pydantic — NOT SQLAlchemy models).

These mirror the verified Inventra response shapes exactly (verified against
Inventra's route declarations, Pydantic schemas and service serializers):

- ``GET /api/products``      → ``ProductListResponse`` envelope
- ``GET /api/products/{id}`` → single ``Product``
- ``GET /api/categories``    → bare JSON array of ``Category``
- ``GET /api/inventory``     → bare JSON array of ``InventoryItem``
- ``GET /api/inventory/movements`` → ``StockMovementListResponse`` envelope

These are data-transfer contracts only. Product/inventory truth stays in
Inventra; nothing here is (or ever becomes) a Sales Agent database table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Verified stock_status vocabulary. Products accept all three; the inventory
# list endpoint only honors in_stock/out_of_stock (a "low_stock" value is
# silently ignored there — use low_stock_only=True instead).
StockStatus = Literal["in_stock", "out_of_stock", "low_stock"]

# Verified movement_type vocabulary (Inventra MovementType enum values).
MovementType = Literal["STOCK_IN", "STOCK_OUT", "STOCK_ADJUSTMENT"]


class Product(BaseModel):
    """One product as returned by Inventra (mirrors ProductResponse)."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    sku: str
    description: str | None
    category_id: int | None
    category_name: str | None = None
    price: float
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProductListResponse(BaseModel):
    """Envelope of ``GET /api/products`` (mirrors Inventra's ProductListResponse)."""

    model_config = ConfigDict(frozen=True)

    products: list[Product]
    total: int
    page: int
    per_page: int
    pages: int


class Category(BaseModel):
    """One category as returned by Inventra (mirrors CategoryResponse)."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class InventoryItem(BaseModel):
    """One inventory row as returned by ``GET /api/inventory``.

    Serialized by Inventra's service layer (no response_model upstream), so
    ``product_name``/``product_sku`` are nullable to tolerate changes.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    product_id: int
    product_name: str | None = None
    product_sku: str | None = None
    quantity: int
    low_stock_threshold: int
    is_low_stock: bool
    updated_at: datetime


class StockMovement(BaseModel):
    """One stock movement (mirrors Inventra's StockMovementResponse)."""

    model_config = ConfigDict(frozen=True)

    id: int
    product_id: int
    product_name: str | None = None
    user_id: int
    username: str | None = None
    movement_type: str
    quantity: int
    notes: str | None
    created_at: datetime


class StockMovementListResponse(BaseModel):
    """Envelope of ``GET /api/inventory/movements`` (service-built dict)."""

    model_config = ConfigDict(frozen=True)

    movements: list[StockMovement]
    total: int
    page: int
    per_page: int


class DashboardMovement(BaseModel):
    """One recent-movement row inside the dashboard stats payload.

    Service-built upstream (no response_model there): ``product_name`` and
    "username" fall back to "Unknown" when relations are missing.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    product_name: str
    movement_type: str
    quantity: int
    username: str | None = None
    notes: str | None = None
    created_at: datetime | None = None


class LowStockProduct(BaseModel):
    """One low-stock alert row inside the dashboard stats payload."""

    model_config = ConfigDict(frozen=True)

    product_id: int
    product_name: str
    product_sku: str
    quantity: int
    threshold: int


class InventraDashboardStats(BaseModel):
    """Payload of ``GET /api/dashboard`` (upstream-cached 120 s, written by
    Inventra's ``dashboard_service.get_dashboard_stats``)."""

    model_config = ConfigDict(frozen=True)

    total_products: int
    active_products: int
    total_stock: int
    low_stock_count: int
    out_of_stock_count: int
    total_stock_in: int
    total_stock_out: int
    recent_movements: list[DashboardMovement]
    low_stock_products: list[LowStockProduct]
