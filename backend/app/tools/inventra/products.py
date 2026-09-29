"""Product agent tools (read-only wrappers over ``InventraClient``).

Thin, typed boundary between a future LangGraph graph and the Inventra
integration: no HTTP code, no URLs, no auth details, no database, no LLM, no
recommendation logic. Failures surface as typed tool errors — never as empty
results. Response contracts are reused from the client layer (no duplication).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.inventra import InventraClient, StockStatus
from app.integrations.inventra.errors import InventraError
from app.integrations.inventra.schemas import Product, ProductListResponse
from app.tools.inventra.base import translate_inventra_error

# Upstream caps per_page at 100 (Query(20, ge=1, le=100)).
MAX_PER_PAGE = 100


class ProductSearchParams(BaseModel):
    """Typed input for the product search/list tool (verified filters only)."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=MAX_PER_PAGE)
    search: str | None = Field(default=None, max_length=200)
    category_id: int | None = Field(default=None, ge=1)
    is_active: bool | None = None
    stock_status: StockStatus | None = None


async def search_products(
    client: InventraClient, params: ProductSearchParams
) -> ProductListResponse:
    """Search/list products via the verified ``GET /api/products`` filters."""
    try:
        return await client.list_products(
            page=params.page,
            per_page=params.per_page,
            search=params.search,
            category_id=params.category_id,
            is_active=params.is_active,
            stock_status=params.stock_status,
        )
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises


async def get_product_details(client: InventraClient, product_id: int) -> Product:
    """Fetch one product via ``GET /api/products/{product_id}``."""
    try:
        return await client.get_product(product_id)
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises
