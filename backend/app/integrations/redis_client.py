"""Redis client factory (cache layer groundwork).

Redis is created lazily so importing the app never requires a running Redis
(§G of the architecture audit: cache layer, not wired to any feature yet).
"""

from functools import lru_cache

from redis.asyncio import ConnectionPool, Redis

from app.config.settings import get_settings


@lru_cache
def get_redis_pool() -> ConnectionPool:
    """Build (once) an async Redis connection pool from settings."""
    settings = get_settings()
    return ConnectionPool.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )


def get_redis_client() -> Redis:
    """Create an async Redis client bound to the shared pool.

    Callers own the client lifecycle; use ``close_redis_client`` on shutdown
    or per-call ``aclose()`` for short-lived clients.
    """
    return Redis(connection_pool=get_redis_pool())


async def ping_redis(client: Redis) -> bool:
    """Return True when Redis answers PING (health probe; never raises)."""
    try:
        await client.ping()
    except Exception:  # noqa: BLE001 — the health probe must never raise
        return False
    return True


async def close_redis_pool() -> None:
    """Disconnect the shared connection pool (application shutdown)."""
    pool = get_redis_pool()
    await pool.disconnect(inuse_connections=True)
    get_redis_pool.cache_clear()
