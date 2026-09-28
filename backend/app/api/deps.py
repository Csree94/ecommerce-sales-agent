"""Request-scoped dependencies (FastAPI ``Depends`` providers).

Single place where request handlers obtain infrastructure handles (DB session,
Redis client). Keeps route code independent of how resources are constructed.
"""

from collections.abc import Generator

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.session import Database
from app.integrations.inventra import InventraClient


def get_settings_dep(request: Request) -> Settings:
    """Return the application settings attached at startup."""
    return request.app.state.settings


def get_database(request: Request) -> Database:
    """Return the application database holder attached at startup."""
    return request.app.state.database


def get_db_session(
    database: Database = Depends(get_database),
) -> Generator[Session, None, None]:
    """Yield a short-lived DB session; commits on success, rolls back on error.

    Sessions are short-lived by design (Neon/serverless friendly — §L).
    """
    session = database.session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_redis(request: Request) -> Redis:
    """Return the shared Redis client attached at startup (not used yet)."""
    return request.app.state.redis


def get_inventra_client(request: Request) -> InventraClient:
    """Return the shared read-only Inventra client attached at startup.

    Raises 503 when the integration is not configured (no INVENTRA_BASE_URL),
    so misconfiguration surfaces as a clear HTTP error instead of an AttributeError
    deep inside a handler. Agent tools (later phase) receive this via typed
    parameters; this dependency exists for route-level plumbing.
    """
    client: InventraClient | None = getattr(request.app.state, "inventra_client", None)
    if client is None:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inventra integration is not configured",
        )
    return client
