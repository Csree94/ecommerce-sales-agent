"""External system integration clients.

Foundation only in this phase: Redis client factory. Shopify and Telegram
adapters are intentionally NOT implemented yet (later phases per §P of the
architecture audit).
"""

from app.integrations.redis_client import (
    close_redis_pool,
    get_redis_client,
    get_redis_pool,
    ping_redis,
)

__all__ = [
    "close_redis_pool",
    "get_redis_client",
    "get_redis_pool",
    "ping_redis",
]
