"""Application factory + ASGI entrypoint.

Creates the FastAPI application with lifespan-managed infrastructure
(database engine, Redis pool). Designed so later phases (LangGraph agent,
Shopify/Telegram adapters, admin API) plug in as additional routers and
state — the factory itself stays stable.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.routes import api_router
from app.config.inventra import get_inventra_settings
from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Database
from app.integrations.inventra import InventraClient
from app.integrations.redis_client import close_redis_pool, get_redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle for infrastructure resources."""
    settings: Settings = app.state.settings
    inventra_settings = get_inventra_settings()
    logger = get_logger(__name__)

    logger.info("application_startup_begin", environment=settings.app_env.value)
    app.state.database = Database.from_settings()
    app.state.redis = get_redis_client()
    # Optional integration: only constructed when INVENTRA_BASE_URL is set.
    # Built once here so tools/routes share one httpx pool (later phases).
    # app/config/inventra.py is the single source of truth for Inventra config.
    app.state.inventra_client = (
        InventraClient(inventra_settings) if inventra_settings.base_url else None
    )
    logger.info("application_startup_complete")

    yield

    logger.info("application_shutdown_begin")
    app.state.database.dispose()
    inventra_client: InventraClient | None = getattr(app.state, "inventra_client", None)
    if inventra_client is not None:
        await inventra_client.aclose()
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
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
