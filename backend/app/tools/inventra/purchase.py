"""Purchase agent tool: the ONLY write in the tool layer (milestone 3B).

A single typed wrapper over ``InventraClient.stock_out`` — the verified
``POST /api/inventory/{product_id}/stock-out`` endpoint. The client method is
a single explicit attempt (no retry), so a timeout can never cause a second
deduction; this tool preserves that property by translating errors without
re-invoking anything.

Failures surface as typed tool errors (same hierarchy as the read tools) so
the graph can carry them as structured state. ``notes`` is capped at 500
characters (upstream ``StockAdjustment`` schema limit).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.integrations.inventra import InventraClient
from app.integrations.inventra.errors import InventraError
from app.integrations.inventra.schemas import StockOutResult
from app.tools.inventra.base import translate_inventra_error

# Upstream StockAdjustment.notes cap (inventra schemas/inventory.py).
MAX_NOTES_LENGTH = 500


class StockOutParams(BaseModel):
    """Typed input for the purchase/stock-out tool (verified contract only)."""

    model_config = ConfigDict(frozen=True)

    product_id: int = Field(..., ge=1)
    quantity: int = Field(..., gt=0)
    notes: str | None = Field(default=None, max_length=MAX_NOTES_LENGTH)


async def stock_out_product(client: InventraClient, params: StockOutParams) -> StockOutResult:
    """Deduct stock via ``POST /api/inventory/{product_id}/stock-out`` (once)."""
    try:
        return await client.stock_out(
            params.product_id,
            quantity=params.quantity,
            notes=params.notes,
        )
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises
