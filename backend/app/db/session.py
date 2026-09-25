"""SQLAlchemy engine/session factory tuned for Neon.

Tuned for **Neon serverless Postgres** (§L of the architecture audit):
short-lived connections via the Neon pooler, conservative pool sizing, and
``pool_pre_ping`` to survive cold starts. No application tables are created
here — schema/migrations arrive in a later phase (Alembic).
"""

from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_SUPPORTED_BACKENDS = {"postgresql", "postgres"}


def _engine_kwargs() -> dict[str, Any]:
    """Build engine kwargs from settings (Neon/serverless-friendly defaults)."""
    settings = get_settings()
    kwargs: dict[str, Any] = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "connect_args": {"connect_timeout": settings.db_connect_timeout_seconds},
    }
    if settings.db_use_null_pool:
        # NullPool: no pooled connections held open — safe across event loops,
        # workers, and Neon cold starts (recommended for serverless Postgres).
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
    return kwargs


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create the application database engine from settings (injectable for tests)."""
    settings = settings or get_settings()
    url = make_url(settings.sqlalchemy_database_uri)
    if url.get_backend_name() not in _SUPPORTED_BACKENDS:
        msg = (
            f"Unsupported DATABASE_URL backend: {url.get_backend_name()!r} "
            f"(expected one of {sorted(_SUPPORTED_BACKENDS)})"
        )
        raise ValueError(msg)
    engine = create_engine(settings.sqlalchemy_database_uri, **_engine_kwargs())
    logger.info(
        "database_engine_created",
        backend=url.get_backend_name(),
        host=url.host,
        pool_class="NullPool" if settings.db_use_null_pool else "QueuePool",
    )
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Database:
    """Owns the engine + session factory; created once at application startup."""

    def __init__(self, engine: Engine, session_factory: sessionmaker[Session]) -> None:
        self._engine = engine
        self._session_factory = session_factory

    @classmethod
    def from_settings(cls) -> "Database":
        """Build a Database instance from application settings."""
        engine = create_db_engine()
        return cls(engine=engine, session_factory=create_session_factory(engine))

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        return self._session_factory

    def session(self) -> Session:
        """Create a new session (caller owns closing/committing)."""
        return self._session_factory()

    def check_connection(self) -> bool:
        """Return True when the database answers a trivial query (health probe)."""
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 — the health probe must never raise
            logger.exception("database_health_check_failed")
            return False
        return True

    def dispose(self) -> None:
        """Dispose the engine's connection pool at shutdown."""
        self._engine.dispose()
        logger.info("database_engine_disposed")
