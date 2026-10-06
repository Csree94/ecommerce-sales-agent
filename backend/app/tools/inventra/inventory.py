"""Inventory agent tools (read-only wrappers over ``InventraClient``).

There is NO per-product inventory endpoint upstream: checking one product's
stock must go through the verified list endpoint's ``search`` (matches
name/SKU). These tools keep that constraint explicit instead of inventing an
endpoint. Failures surface as typed tool errors — never as empty results.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.cache import TTL_PRODUCTS, InventraCache, inventory_key
from app.integrations.inventra import InventraClient, MovementType
from app.integrations.inventra.errors import InventraError
from app.integrations.inventra.schemas import InventoryItem, StockMovementListResponse
from app.tools.inventra.base import translate_inventra_error


class InventorySearchParams(BaseModel):
    """Typed input for the inventory/stock tool (verified filters only)."""

    model_config = ConfigDict(frozen=True)

    search: str | None = Field(default=None, max_length=200)
    # Upstream inventory list honors only in_stock/out_of_stock; low_stock
    # filtering is done via low_stock_only (a "low_stock" value is ignored).
    stock_status: Literal["in_stock", "out_of_stock"] | None = None
    low_stock_only: bool = False


class InventoryMovementParams(BaseModel):
    """Typed input for the stock-movement history tool (verified filters only)."""

    model_config = ConfigDict(frozen=True)

    product_id: int | None = Field(default=None, ge=1)
    movement_type: MovementType | None = None
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=50, ge=1, le=100)  # upstream cap le=100


async def check_inventory(
    client: InventraClient, params: InventorySearchParams, cache: InventraCache | None = None
) -> list[InventoryItem]:
    """Check stock levels via ``GET /api/inventory`` (verified filters only).

    For a single product, pass ``search`` (product name/SKU match upstream).
    With a ``cache`` (milestone 4), results are served Redis-first with the
    inventory TTL; empty results are negative-cached briefly. ``cache=None``
    keeps the historical direct-read behavior byte-for-byte.
    """
    if cache is not None:
        key = inventory_key(
            search=params.search,
            stock_status=params.stock_status,
            low_stock_only=params.low_stock_only,
        )
        try:
            items = await cache.read_through(
                key,
                list[InventoryItem],
                lambda: client.list_inventory(
                    search=params.search,
                    stock_status=params.stock_status,
                    low_stock_only=params.low_stock_only,
                ),
                ttl_setting=TTL_PRODUCTS,
                negative=True,
            )
        except InventraError as exc:
            translate_inventra_error(exc)  # always raises (same contract as direct path)
        return items if items is not None else []  # negative HIT → empty list
    try:
        return await client.list_inventory(
            search=params.search,
            stock_status=params.stock_status,
            low_stock_only=params.low_stock_only,
        )
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises


async def get_inventory_movements(
    client: InventraClient, params: InventoryMovementParams
) -> StockMovementListResponse:
    """Fetch stock-movement history via ``GET /api/inventory/movements``."""
    try:
        return await client.list_inventory_movements(
            product_id=params.product_id,
            movement_type=params.movement_type,
            page=params.page,
            per_page=params.per_page,
        )
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises
