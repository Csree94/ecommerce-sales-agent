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
from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import Database
from app.integrations.redis_client import close_redis_pool, get_redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown lifecycle for infrastructure resources."""
    settings: Settings = app.state.settings
    logger = get_logger(__name__)

    logger.info("application_startup_begin", environment=settings.app_env.value)
    app.state.database = Database.from_settings()
    app.state.redis = get_redis_client()
    logger.info("application_startup_complete")

    yield

    logger.info("application_shutdown_begin")
    app.state.database.dispose()
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
