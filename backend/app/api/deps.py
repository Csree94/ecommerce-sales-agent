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
