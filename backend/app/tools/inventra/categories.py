"""Category agent tools (read-only wrappers over ``InventraClient``).

Same discipline as ``products.py``: thin, typed, no HTTP/URL/auth/DB/LLM
knowledge, structured failures via the tool error hierarchy, response
contracts reused from the client layer.
"""

from __future__ import annotations

from app.integrations.inventra import InventraClient
from app.integrations.inventra.errors import InventraError
from app.integrations.inventra.schemas import Category
from app.tools.inventra.base import translate_inventra_error


async def list_categories(client: InventraClient) -> list[Category]:
    """List all categories via ``GET /api/categories`` (bare array upstream)."""
    try:
        return await client.list_categories()
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises


async def get_category_details(client: InventraClient, category_id: int) -> Category:
    """Fetch one category via ``GET /api/categories/{category_id}``."""
    try:
        return await client.get_category(category_id)
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises
