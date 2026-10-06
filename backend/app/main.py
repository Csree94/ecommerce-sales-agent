"""Application factory + ASGI entrypoint.

Creates the FastAPI application with lifespan-managed infrastructure
(database engine, Redis pool). Designed so later phases (LangGraph agent,
Shopify/Telegram adapters, admin API) plug in as additional routers and
state — the factory itself stays stable.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import api_router
from app.config.inventra import get_inventra_settings
from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Database
from app.integrations.cache import InventraCache
from app.integrations.inventra import InventraClient
from app.integrations.redis_client import close_redis_pool, get_redis_client
from app.integrations.telegram.client import TelegramClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle for infrastructure resources."""
    settings: Settings = app.state.settings
    inventra_settings = get_inventra_settings()
    logger = get_logger(__name__)

    logger.info("application_startup_begin", environment=settings.app_env.value)
    app.state.database = Database.from_settings()
    redis_client = get_redis_client()
    app.state.redis = redis_client
    # Milestone 4: read-through cache over the shared Redis client. Redis is
    # lazily connected and the cache degrades to direct reads when absent —
    # constructing it here never requires a running Redis.
    app.state.inventra_cache = InventraCache(redis_client)
    # Optional integration: only constructed when INVENTRA_BASE_URL is set.
    # Built once here so tools/routes share one httpx pool (later phases).
    # app/config/inventra.py is the single source of truth for Inventra config.
    app.state.inventra_client = (
        InventraClient(inventra_settings) if inventra_settings.base_url else None
    )
    # Optional integration: only constructed when TELEGRAM_BOT_TOKEN is set.
    # The webhook route answers 503 when absent (clear misconfiguration signal).
    telegram_token = settings.telegram_bot_token.get_secret_value()
    app.state.telegram_client = TelegramClient(telegram_token) if telegram_token else None
    # Strong references to in-flight turn tasks (asyncio GCs unreferenced tasks).
    app.state.telegram_background_tasks = set()
    logger.info("application_startup_complete")

    yield

    logger.info("application_shutdown_begin")
    app.state.database.dispose()
    inventra_client: InventraClient | None = getattr(app.state, "inventra_client", None)
    if inventra_client is not None:
        await inventra_client.aclose()
    telegram_client: TelegramClient | None = getattr(app.state, "telegram_client", None)
    if telegram_client is not None:
        await telegram_client.aclose()
    await close_redis_pool()
    logger.info("application_shutdown_complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (app-factory pattern)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.is_production)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # Docs stay available in non-production for now; tighten later if needed.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.state.settings = settings

    # CORS for the admin dashboard dev server (opt-in via CORS_ALLOWED_ORIGINS;
    # empty = no CORS headers at all). Production should serve the dashboard
    # from the API origin or set an explicit allowlist.
    allowed_origins = [
        origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()
    ]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
