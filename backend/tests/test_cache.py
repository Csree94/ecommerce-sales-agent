"""Tests for the milestone-4 Inventra read-through cache.

Style matches the existing suite: sync tests driving coroutines via
``asyncio.run`` and a ``MagicMock`` at the ``InventraClient`` seam — plus a
tiny in-memory fake of the Redis interface the cache uses (get/set/keys/
delete) so no real Redis is ever contacted. Coverage: miss→fetch→store,
hit→no Inventra call, per-parameter key separation, case-insensitive search
keys, TTL values (incl. env override), negative caching with the short TTL,
Redis-outage degradation, Inventra-error pass-through (no cache write),
corrupt-entry recovery, and post-stock-out invalidation.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.config.settings import get_settings
from app.integrations.cache import (
    InventraCache,
    categories_all_key,
    category_detail_key,
    inventory_key,
    product_detail_key,
    products_list_key,
)
from app.integrations.inventra.errors import (
    InventraConnectionError,
    InventraServerError,
)
from app.integrations.inventra.schemas import (
    Category,
    InventoryItem,
    Product,
    ProductListResponse,
    StockOutResult,
)
from app.tools.errors import ToolError, ToolUnavailableError
from app.tools.inventra import (
    InventorySearchParams,
    ProductSearchParams,
    StockOutParams,
    check_inventory,
    get_category_details,
    get_product_details,
    list_categories,
    search_products,
    stock_out_product,
)


def make_async(value: Any) -> Any:
    """Wrap a value in a resolved coroutine (mock client methods are async)."""

    async def _inner() -> Any:
        return value

    return _inner()


def returns_async(value: Any) -> Any:
    """side_effect factory: a FRESH coroutine per call (repeat-call safe)."""

    def _factory(*args: Any, **kwargs: Any) -> Any:
        return make_async(value)

    return _factory


class FakeRedis:
    """Minimal in-memory stand-in for the Redis interface the cache uses."""

    def __init__(self, *, fail: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = fail

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def keys(self, pattern: str) -> list[str]:
        if self.fail:
            raise ConnectionError("redis down")
        prefix = pattern.rstrip("*")
        return [k for k in self.store if k.startswith(prefix)]

    async def delete(self, *keys: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                self.ttls.pop(key, None)
                removed += 1
        return removed


# Verified-shape fixtures (mirrors of Inventra response contracts).
PRODUCT = {
    "id": 7,
    "name": "Trail Shoes",
    "sku": "TS-001",
    "description": "Waterproof hiking shoes",
    "category_id": 3,
    "category_name": "Footwear",
    "price": 129.99,
    "is_active": True,
    "created_at": "2026-01-15T10:00:00Z",
    "updated_at": "2026-02-01T12:30:00Z",
}
PRODUCT_LIST = {
    "products": [PRODUCT],
    "total": 1,
    "page": 1,
    "per_page": 5,
    "pages": 1,
}
CATEGORY = {
    "id": 3,
    "name": "Footwear",
    "description": "Shoes and boots",
    "created_at": "2026-01-10T09:00:00Z",
    "updated_at": "2026-01-10T09:00:00Z",
}
INVENTORY_ITEM = {
    "id": 11,
    "product_id": 7,
    "product_name": "Trail Shoes",
    "product_sku": "TS-01...",
    "quantity": 4,
    "low_stock_threshold": 10,
    "is_low_stock": True,
    "updated_at": "2026-02-01T12:30:00Z",
}


def _client_with_list(payload: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.list_products.side_effect = returns_async(ProductListResponse.model_validate(payload))
    return client


# --- 1+2. MISS → fetch → store with TTL; HIT → no Inventra call -------------


def test_product_list_miss_then_hit() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = _client_with_list(PRODUCT_LIST)
    params = ProductSearchParams(search="trail shoes", per_page=5)

    first = asyncio.run(search_products(client, params, cache=cache))
    assert first.total == 1
    assert client.list_products.call_count == 1  # MISS → Inventra called

    expected_key = products_list_key(page=1, per_page=5, search="trail shoes")
    assert expected_key in redis.store  # stored under the exact params key
    assert redis.ttls[expected_key] == 60  # products TTL band default

    second = asyncio.run(search_products(client, params, cache=cache))
    assert second == first  # same logical result ...
    assert client.list_products.call_count == 1  # ... served from cache (HIT)


def test_product_detail_miss_then_hit() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = MagicMock()
    client.get_product.side_effect = returns_async(Product.model_validate(PRODUCT))

    product = asyncio.run(get_product_details(client, 7, cache=cache))
    assert product.id == 7
    assert client.get_product.call_count == 1
    key = product_detail_key(7)
    assert key == "inv:v1:products:detail:7"
    assert redis.ttls[key] == 60

    again = asyncio.run(get_product_details(client, 7, cache=cache))
    assert again == product
    assert client.get_product.call_count == 1  # HIT


# --- 3. Different parameters never share entries ----------------------------


def test_different_searches_use_different_keys() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = _client_with_list(PRODUCT_LIST)

    asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
    asyncio.run(search_products(client, ProductSearchParams(search="laptop"), cache=cache))
    assert client.list_products.call_count == 2  # distinct params → distinct keys
    keys = set(redis.store)
    assert keys == {
        products_list_key(page=1, per_page=20, search="shoes"),
        products_list_key(page=1, per_page=20, search="laptop"),
    }


def test_every_list_filter_changes_the_key() -> None:
    base = dict(page=1, per_page=20)
    keys = {
        products_list_key(**base),
        products_list_key(page=2, per_page=20),
        products_list_key(page=1, per_page=100),
        products_list_key(page=1, per_page=20, search="x"),
        products_list_key(page=1, per_page=20, category_id=3),
        products_list_key(page=1, per_page=20, is_active=True),
        products_list_key(page=1, per_page=20, stock_status="in_stock"),
    }
    assert len(keys) == 7  # no collisions between filter combinations


def test_search_key_is_case_and_whitespace_insensitive() -> None:
    # Inventra matching is case-insensitive → Samsung/samsung share one entry.
    assert products_list_key(page=1, per_page=5, search="Samsung Galaxy") == products_list_key(
        page=1, per_page=5, search="samsung galaxy "
    )
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = _client_with_list(PRODUCT_LIST)
    asyncio.run(search_products(client, ProductSearchParams(search="Trail Shoes"), cache=cache))
    asyncio.run(search_products(client, ProductSearchParams(search="trail shoes"), cache=cache))
    assert client.list_products.call_count == 1  # second spelling HIT the cache


# --- 5. Inventory keys include the filters ----------------------------------


def test_inventory_keys_include_filters_and_round_trip() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = MagicMock()
    client.list_inventory.side_effect = returns_async(
        [InventoryItem.model_validate(INVENTORY_ITEM)]
    )

    params = InventorySearchParams(search="TS-001", stock_status="in_stock")
    items = asyncio.run(check_inventory(client, params, cache=cache))
    assert items[0].product_id == 7
    expected_key = inventory_key(search="TS-001", stock_status="in_stock", low_stock_only=False)
    assert expected_key in redis.store
    assert redis.ttls[expected_key] == 60

    # low_stock_only flips the key (stock-status filtered views are distinct).
    other = inventory_key(search="TS-001", stock_status="in_stock", low_stock_only=True)
    assert other != expected_key

    asyncio.run(check_inventory(client, params, cache=cache))
    assert client.list_inventory.call_count == 1  # HIT


# --- 6. Categories: long TTL, exact keys ------------------------------------


def test_categories_cached_with_long_ttl() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = MagicMock()
    client.list_categories.side_effect = returns_async([Category.model_validate(CATEGORY)])

    categories = asyncio.run(list_categories(client, cache=cache))
    assert categories[0].id == 3
    key = categories_all_key()
    assert key == "inv:v1:categories:all"
    assert redis.ttls[key] == 900  # categories = longer TTL

    asyncio.run(list_categories(client, cache=cache))
    assert client.list_categories.call_count == 1  # HIT

    client.get_category.side_effect = returns_async(Category.model_validate(CATEGORY))
    asyncio.run(get_category_details(client, 3, cache=cache))
    assert category_detail_key(3) in redis.store


# --- 7. Negative caching (short TTL, faithful empty result) -----------------


def test_negative_cache_products_with_short_ttl() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    empty = ProductListResponse(products=[], total=0, page=1, per_page=5, pages=0)
    client = MagicMock()
    client.list_products.side_effect = returns_async(empty)

    params = ProductSearchParams(search="ghost", per_page=5)
    first = asyncio.run(search_products(client, params, cache=cache))
    assert first.total == 0
    key = products_list_key(page=1, per_page=5, search="ghost")
    assert redis.ttls[key] == 8  # negative TTL band 5-10s
    assert "__negative__" in redis.store[key]

    second = asyncio.run(search_products(client, params, cache=cache))
    assert second.total == 0 and second.page == 1 and second.per_page == 5
    assert client.list_products.call_count == 1  # negative HIT


def test_negative_cache_inventory_with_short_ttl() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = MagicMock()
    client.list_inventory.side_effect = returns_async([])

    params = InventorySearchParams(search="nothing")
    assert asyncio.run(check_inventory(client, params, cache=cache)) == []
    key = inventory_key(search="nothing")
    assert redis.ttls[key] == 8
    assert asyncio.run(check_inventory(client, params, cache=cache)) == []
    assert client.list_inventory.call_count == 1  # negative HIT


# --- 8. Redis unavailable → Inventra still works -----------------------------


def test_redis_down_degrades_to_direct_read() -> None:
    cache = InventraCache(FakeRedis(fail=True))
    client = _client_with_list(PRODUCT_LIST)

    result = asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
    assert result.total == 1  # Inventra answer returned, no exception
    assert client.list_products.call_count == 1


def test_redis_set_failure_still_returns_result() -> None:
    class FailOnSet(FakeRedis):
        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            raise ConnectionError("redis write failed")

    cache = InventraCache(FailOnSet())
    client = _client_with_list(PRODUCT_LIST)
    result = asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
    assert result.total == 1  # read path unaffected by write failure


def test_invalidation_with_redis_down_is_silent_noop() -> None:
    cache = InventraCache(FakeRedis(fail=True))
    assert asyncio.run(cache.invalidate_product_stock(7)) == 0  # never raises


# --- Inventra errors propagate; failed calls never populate the cache -------


def test_inventra_error_propagates_and_writes_nothing() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    client = MagicMock()
    client.list_products.side_effect = InventraServerError("boom")

    with pytest.raises(ToolError):
        asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
    assert redis.store == {}  # no cache write for a failed read


def test_corrupt_entry_behaves_as_miss_and_is_replaced() -> None:
    redis = FakeRedis()
    key = products_list_key(page=1, per_page=20, search="shoes")
    redis.store[key] = "not-json"
    cache = InventraCache(redis)
    client = _client_with_list(PRODUCT_LIST)

    result = asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
    assert result.total == 1  # refetched from Inventra
    assert client.list_products.call_count == 1
    stored = ProductListResponse.model_validate_json(redis.store[key])  # overwritten with real data
    assert stored.total == 1


# --- TTLs are centralized/configurable ---------------------------------------


def test_ttl_override_via_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_TTL_PRODUCTS", "45")
    get_settings.cache_clear()
    try:
        redis = FakeRedis()
        cache = InventraCache(redis)
        client = _client_with_list(PRODUCT_LIST)
        asyncio.run(search_products(client, ProductSearchParams(search="shoes"), cache=cache))
        key = products_list_key(page=1, per_page=20, search="shoes")
        assert redis.ttls[key] == 45  # from env, not the 60 default
    finally:
        get_settings.cache_clear()


# --- 11. Stock-out invalidation ----------------------------------------------


def _seed_product_caches(redis: FakeRedis, product_id: int = 7) -> dict[str, str]:
    seeded = {
        product_detail_key(product_id): '{"stale": true}',
        products_list_key(page=1, per_page=5, search="shoes"): '{"stale": true}',
        inventory_key(search=str(product_id)): '{"stale": true}',
        inventory_key(search="other-product"): '{"stale": true}',
        categories_all_key(): '{"keep": true}',  # categories are stock-free
        "unrelated:key": '{"keep": true}',
    }
    redis.store.update(seeded)
    return seeded


def test_successful_stock_out_invalidates_all_stock_carrying_entries() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    seeded = _seed_product_caches(redis)
    client = MagicMock()
    client.stock_out.side_effect = returns_async(
        StockOutResult(product_id=7, product_name="Trail Shoes", quantity=48)
    )

    result = asyncio.run(
        stock_out_product(client, StockOutParams(product_id=7, quantity=1), cache=cache)
    )
    assert result.quantity == 48
    assert client.stock_out.call_count == 1  # exactly-once write untouched

    for victim in (
        product_detail_key(7),
        products_list_key(page=1, per_page=5, search="shoes"),
        inventory_key(search="7"),
        inventory_key(search="other-product"),
    ):
        assert victim not in redis.store  # every stock-carrying entry gone
    for kept in (categories_all_key(), "unrelated:key"):
        assert redis.store[kept] == seeded[kept]  # nothing else touched


def test_failed_stock_out_invalidates_nothing() -> None:
    redis = FakeRedis()
    cache = InventraCache(redis)
    seeded = _seed_product_caches(redis)
    client = MagicMock()
    client.stock_out.side_effect = InventraConnectionError("inventra down")

    with pytest.raises(ToolUnavailableError):
        asyncio.run(
            stock_out_product(client, StockOutParams(product_id=7, quantity=1), cache=cache)
        )
    assert redis.store == seeded  # failed write → no invalidation


def test_stock_out_without_cache_is_unchanged() -> None:
    """cache=None keeps the historical stock-out behavior byte-for-byte."""
    client = MagicMock()
    client.stock_out.side_effect = returns_async(
        StockOutResult(product_id=7, product_name="Trail Shoes", quantity=48)
    )
    result = asyncio.run(stock_out_product(client, StockOutParams(product_id=7, quantity=1)))
    assert result.quantity == 48
    assert client.stock_out.call_count == 1
