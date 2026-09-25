"""Health check endpoints (basic readiness/liveness for the foundation)."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from redis.asyncio import Redis

from app import __version__
from app.api.deps import get_database, get_redis, get_settings_dep
from app.config.settings import Settings
from app.db.session import Database
from app.integrations.redis_client import ping_redis

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Payload for the basic health endpoint."""

    status: Literal["ok"] = "ok"
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """Payload for the readiness endpoint with per-dependency status."""

    status: Literal["ok", "degraded"]
    version: str
    environment: str
    database: bool
    redis: bool


@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings_dep)) -> HealthResponse:
    """Basic liveness probe — the process is up and serving."""
    return HealthResponse(
        version=__version__,
        environment=settings.app_env.value,
    )


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(
    request: Request,
    database: Database = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> ReadinessResponse:
    """Readiness probe — checks Postgres/Neon and Redis connectivity.

    Never raises: dependency failures degrade the response instead.
    """
    settings: Settings = request.app.state.settings
    db_ok = database.check_connection()
    redis_ok = await ping_redis(redis)
    return ReadinessResponse(
        status="ok" if (db_ok and redis_ok) else "degraded",
        version=__version__,
        environment=settings.app_env.value,
        database=db_ok,
        redis=redis_ok,
    )
