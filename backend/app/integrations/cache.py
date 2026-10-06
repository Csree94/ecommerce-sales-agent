"""Inventra read-through cache (milestone 4, Redis cache-aside).

A thin, deliberately small layer between the agent tools and the Inventra
read tools. Reads flow through ``read_through``: Redis GET → on HIT return
the cached JSON; on MISS call the provided coroutine (the existing read tool
call — the only place Inventra is touched) → Redis SET with the family TTL →
return. Confirmed-empty reads are cached briefly (negative caching) so a
multi-step zero-result ladder does not hammer Inventra.

Error contract (hard rule): Redis is a CACHE, never a hard dependency —
any Redis failure (unreachable, timeout, closed pool, deserialize mismatch)
behaves exactly like a MISS for reads and as a no-op for invalidation. Redis
problems are logged at WARNING level and never propagate; actual Inventra
errors are re-raised untouched (the fetch coroutine is only wrapped on the
cache-write side, so an Inventra failure never produces a cache write).

Serialization is JSON via pydantic ``TypeAdapter`` (handles single models and
``list[Model]`` uniformly) — no new dependencies, and a HIT returns an equal
(frozen) model instance with the same shape as a MISS.

Key format (all under one versioned namespace so a format change is a mass
invalidation): ``inv:v1:{family}:{params}`` where ``{params}`` is a sha256 of
the canonical JSON of every request parameter that can change the response
(lowercased/trimmed search, None/False no-op fields dropped). Detail families
keep the id in plain form. Negative entries reuse the exact same key as the
real result (only the TTL differs), so a later real result simply overwrites.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

from pydantic import TypeAdapter

from app.config.settings import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Versioned key namespace: bump v1 → v2 to invalidate everything at once.
_PREFIX = "inv:v1"
# Envelope marker for negative (confirmed-empty) cache entries. Distinctive
# enough that a real payload can never collide with it.
_NEGATIVE_KEY = "__negative__"

#: Settings field for the products/inventory TTL (teacher band 30–120 s).
TTL_PRODUCTS = "cache_ttl_products_seconds"
#: Settings field for the negative (confirmed-empty) TTL (5–10 s band).
TTL_NEGATIVE = "cache_ttl_negative_seconds"
#: Settings field for the categories TTL ("longer TTL").
TTL_CATEGORIES = "cache_ttl_categories_seconds"


def _ttl_for(ttl_setting: str) -> int:
    """Resolve a TTL (seconds) from the centralized settings."""
    return int(getattr(get_settings(), ttl_setting))


def _canonical_params(params: dict[str, Any]) -> str:
    """Deterministic JSON for the parameters that define a cache key.

    ``search`` values are trimmed/lowercased (Inventra matching is
    case-insensitive, so ``Samsung``/``samsung`` share an entry); ``None``/
    ``False`` no-op fields are dropped; keys are serialized in sorted order so
    identical requests always hash identically.
    """
    normalized: dict[str, Any] = {}
    for key in sorted(params):
        value = params[key]
        if value is None or value is False:
            continue
        if key == "search" and isinstance(value, str):
            value = value.strip().lower()
            if not value:
                continue
        normalized[key] = value
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True)


def _hash_params(params: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_params(params).encode("utf-8")).hexdigest()


# --- Cache key builders (exact request parameters → exact key) --------------


def products_list_key(
    *,
    page: int,
    per_page: int,
    search: str | None = None,
    category_id: int | None = None,
    is_active: bool | None = None,
    stock_status: str | None = None,
) -> str:
    """Key for ``GET /api/products`` — includes every list filter."""
    params: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
        "search": search,
        "category_id": category_id,
        "is_active": is_active,
        "stock_status": stock_status,
    }
    return f"{_PREFIX}:products:list:{_hash_params(params)}"


def product_detail_key(product_id: int) -> str:
    """Key for ``GET /api/products/{id}`` — the id is the only parameter."""
    return f"{_PREFIX}:products:detail:{int(product_id)}"


def inventory_key(
    *,
    search: str | None = None,
    stock_status: str | None = None,
    low_stock_only: bool = False,
) -> str:
    """Key for ``GET /api/inventory`` — includes every inventory filter."""
    params: dict[str, Any] = {
        "search": search,
        "stock_status": stock_status,
        "low_stock_only": low_stock_only,
    }
    return f"{_PREFIX}:inventory:{_hash_params(params)}"


def categories_all_key() -> str:
    """Key for ``GET /api/categories`` (no parameters upstream)."""
    return f"{_PREFIX}:categories:all"


def category_detail_key(category_id: int) -> str:
    """Key for ``GET /api/categories/{id}``."""
    return f"{_PREFIX}:categories:detail:{int(category_id)}"


@lru_cache(maxsize=32)
def _adapter(model_cls: Any) -> TypeAdapter:
    """Cached TypeAdapter (schema built once per response shape)."""
    return TypeAdapter(model_cls)


def _dump(result: Any) -> str:
    """Serialize a tool result (single model or list of models) to JSON."""
    if isinstance(result, list):
        return json.dumps([item.model_dump(mode="json") for item in result])
    return json.dumps(result.model_dump(mode="json"))


def _decode(cached: str, model_cls: Any) -> Any:
    """Deserialize one cache entry; ``None`` means a negative (empty) hit.

    Raises on shape mismatch — the caller treats that as a MISS and refetches.
    """
    payload = json.loads(cached)
    if isinstance(payload, dict) and payload.get(_NEGATIVE_KEY) is True:
        return None
    return _adapter(model_cls).validate_python(payload)


def _is_empty(result: Any) -> bool:
    """True when a result is a confirmed empty read (negative-cache eligible)."""
    products = getattr(result, "products", None)
    if products is not None:
        return len(products) == 0
    if isinstance(result, list):
        return len(result) == 0
    return False


class InventraCache:
    """Best-effort read-through cache over the shared Redis client.

    Holds no resources of its own (the application owns the Redis client and
    its pool); constructing one per process is enough.
    """

    def __init__(self, redis_client: Any) -> None:
        self._redis = redis_client

    async def read_through(
        self,
        key: str,
        model_cls: Any,
        fetch: Callable[[], Awaitable[Any]],
        *,
        ttl_setting: str = TTL_PRODUCTS,
        negative: bool = False,
    ) -> Any:
        """Cache-aside read: GET → HIT returns cached; MISS fetches → SET.

        ``negative=True`` stores confirmed-empty results under the SAME key
        with the short negative TTL (a real result overwrites it later).
        Returns the decoded model(s), or ``None`` for a negative HIT (empty
        result). Redis failures degrade to a straight-through read — never an
        error; the fetch coroutine's own (Inventra) errors propagate untouched.
        """
        try:
            cached = await self._redis.get(key)
        except Exception:  # noqa: BLE001 — Redis down must never fail a read
            logger.warning("cache_read_failed", cache_key=key)
            cached = None
        if cached is not None:
            try:
                decoded = _decode(cached, model_cls)
                if decoded is None:
                    return None  # negative HIT: cached confirmed-empty result
                return decoded
            except Exception:  # noqa: BLE001 — bad entry behaves as a MISS
                logger.warning("cache_deserialize_failed", cache_key=key)

        result = await fetch()
        if negative and _is_empty(result):
            payload = json.dumps({_NEGATIVE_KEY: True})
            ttl = _ttl_for(TTL_NEGATIVE)
        else:
            payload = _dump(result)
            ttl = _ttl_for(ttl_setting)
        try:
            await self._redis.set(key, payload, ex=ttl)
        except Exception:  # noqa: BLE001 — Redis down must never fail a read
            logger.warning("cache_write_failed", cache_key=key)
        return result

    async def invalidate_product_stock(self, product_id: int) -> int:
        """Delete every cache entry that could carry product ``product_id``.

        Called after a successful stock-out. Deletes the product-detail key,
        then clears both hash-keyed families (``products:list:*`` and
        ``inventory:*``): any search/filter combination may embed the changed
        stock, and reverse-hashing every possible query is impossible — the
        failure mode of missing one (stale stock offered to the next customer)
        is exactly what must be prevented. Returns the number of keys deleted;
        never raises (a failed invalidation must not fail the purchase).
        """
        deleted = 0
        try:
            inventory_keys = await self._redis.keys(f"{_PREFIX}:inventory:*")
            list_keys = await self._redis.keys(f"{_PREFIX}:products:list:*")
            victims = [product_detail_key(product_id), *inventory_keys, *list_keys]
            if victims:
                deleted = int(await self._redis.delete(*victims))
        except Exception:  # noqa: BLE001 — invalidation is best-effort
            logger.warning("cache_invalidation_failed", cache_product_id=int(product_id))
            return 0
        return deleted
