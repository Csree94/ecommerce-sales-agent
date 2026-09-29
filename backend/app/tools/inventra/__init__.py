"""Inventra agent tools (strictly read-only).

Thin typed wrappers over ``app.integrations.inventra.InventraClient`` for a
future LangGraph graph:

    LangGraph (future) → agent tools (this package) → InventraClient → HTTP

Rules enforced here (architecture audit §E):
- no HTTP code, no URLs, no auth details, no database access, no LLM calls,
  no recommendation logic inside tools;
- response contracts are reused from the client layer — tools add typed
  *input* models only;
- client failures are translated into the stable tool error hierarchy
  (``app.tools.errors``) and never silently become empty results;
- the surface is strictly read-only — no create/update/delete/stock-change
  tools exist by design.
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

__all__ = [
    "MAX_PER_PAGE",
    "InventoryMovementParams",
    "InventorySearchParams",
    "InventraClient",
    "ProductSearchParams",
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
]
