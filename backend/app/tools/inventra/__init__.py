"""Inventra agent tools.

Thin typed wrappers over ``app.integrations.inventra.InventraClient`` used by
the LangGraph graph:

    LangGraph → agent tools (this package) → InventraClient → HTTP

Rules enforced here (architecture audit §E):
- no HTTP code, no URLs, no auth details, no database access, no LLM calls,
  no recommendation logic inside tools;
- response contracts are reused from the client layer — tools add typed
  *input* models only;
- client failures are translated into the stable tool error hierarchy
  (``app.tools.errors``) and never silently become empty results;
- the surface is read-only plus exactly ONE write: ``stock_out_product`` (the
  purchase deduction, single attempt by client contract). Stock-in/adjust/
  threshold remain deliberately absent — they are admin operations, not
  customer actions.
"""

from app.integrations.inventra import InventraClient
from app.tools.errors import ToolError, ToolInputError, ToolNotFoundError, ToolUnavailableError
from app.tools.inventra.categories import get_category_details, list_categories
from app.tools.inventra.inventory import (
    InventoryMovementParams,
    InventorySearchParams,
    check_inventory,
    get_inventory_movements,
)
from app.tools.inventra.products import (
    MAX_PER_PAGE,
    ProductSearchParams,
    get_product_details,
    search_products,
)
from app.tools.inventra.purchase import StockOutParams, stock_out_product

__all__ = [
    "MAX_PER_PAGE",
    "InventoryMovementParams",
    "InventorySearchParams",
    "InventraClient",
    "ProductSearchParams",
    "StockOutParams",
    "ToolError",
    "ToolInputError",
    "ToolNotFoundError",
    "ToolUnavailableError",
    "check_inventory",
    "get_category_details",
    "get_inventory_movements",
    "get_product_details",
    "list_categories",
    "search_products",
    "stock_out_product",
]
