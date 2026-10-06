"""Category agent tools (read-only wrappers over ``InventraClient``).

Same discipline as ``products.py``: thin, typed, no HTTP/URL/auth/DB/LLM
knowledge, structured failures via the tool error hierarchy, response
contracts reused from the client layer.
"""

from __future__ import annotations

from app.integrations.cache import (
    TTL_CATEGORIES,
    InventraCache,
    categories_all_key,
    category_detail_key,
)
from app.integrations.inventra import InventraClient
from app.integrations.inventra.errors import InventraError
from app.integrations.inventra.schemas import Category
from app.tools.inventra.base import translate_inventra_error


async def list_categories(
    client: InventraClient, cache: InventraCache | None = None
) -> list[Category]:
    """List all categories via ``GET /api/categories`` (bare array upstream).

    Cached with the long categories TTL (milestone 4); ``cache=None`` keeps
    the historical direct-read behavior.
    """
    if cache is not None:
        try:
            categories = await cache.read_through(
                categories_all_key(),
                list[Category],
                lambda: client.list_categories(),
                ttl_setting=TTL_CATEGORIES,
            )
        except InventraError as exc:
            translate_inventra_error(exc)  # always raises (same contract as direct path)
        return categories if categories is not None else []  # empty-list safety
    try:
        return await client.list_categories()
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises


async def get_category_details(
    client: InventraClient, category_id: int, cache: InventraCache | None = None
) -> Category:
    """Fetch one category via ``GET /api/categories/{category_id}`` (cached)."""
    if cache is not None:
        try:
            return await cache.read_through(
                category_detail_key(category_id),
                Category,
                lambda: client.get_category(category_id),
                ttl_setting=TTL_CATEGORIES,
            )
        except InventraError as exc:
            translate_inventra_error(exc)  # always raises (same contract as direct path)
    try:
        return await client.get_category(category_id)
    except InventraError as exc:
        translate_inventra_error(exc)  # always raises
