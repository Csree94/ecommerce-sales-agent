"""Agent tools (typed wrappers over services/clients).

Implemented: Inventra tools (``app.tools.inventra``) — the read surface plus
the single purchase write (``stock_out_product``). LangGraph orchestration
consumes tools only through these typed interfaces.
"""

from app.tools.errors import ToolError, ToolInputError, ToolNotFoundError, ToolUnavailableError
from app.tools.inventra import (
    InventoryMovementParams,
    InventorySearchParams,
    ProductSearchParams,
    StockOutParams,
    check_inventory,
    get_category_details,
    get_inventory_movements,
    get_product_details,
    list_categories,
    search_products,
    stock_out_product,
)

__all__ = [
    "InventoryMovementParams",
    "InventorySearchParams",
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
