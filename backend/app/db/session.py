"""SQLAlchemy engine/session factory tuned for Neon.

Tuned for **Neon serverless Postgres** (§L of the architecture audit):
short-lived connections via the Neon pooler, conservative pool sizing, and
``pool_pre_ping`` to survive cold starts. No application tables are created
here — schema/migrations arrive in a later phase (Alembic).
"""

from typing import Any

from psycopg import connect as psycopg_connect
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger
from app.db.dns_fallback import extract_db_host, get_shared_resolver

logger = get_logger(__name__)

_SUPPORTED_BACKENDS = {"postgresql", "postgres"}


def _dns_resilient_creator(url: str, connect_timeout: int) -> "Any":
    """Build a zero-arg SQLAlchemy ``creator`` with live DNS fallback.

    Called by the pool on every new connection (0-arg signature), so each
    attempt re-resolves: system first, cached last-known-good IP next, DoH as
    the final fallback. The resolved IP goes into psycopg's ``hostaddr`` while
    the original hostname stays in ``host`` for TLS/SNI certificate checks.
    """

    def _connect() -> Any:
        host = extract_db_host(url)
        if not host:
            return psycopg_connect(url, connect_timeout=connect_timeout)
        try:
            ip = get_shared_resolver().resolve(host)
        except OSError as exc:
            raise RuntimeError(f"cannot reach database host {host!r}: {exc}") from exc
        # hostaddr carries the resolved IP; `host` remains the hostname so
        # TLS/SNI certificate verification still checks the real domain.
        return psycopg_connect(url, hostaddr=ip, connect_timeout=connect_timeout)

    return _connect


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
    engine_kwargs = _engine_kwargs()
    if url.drivername in {"postgresql", "postgresql+psycopg"}:
        # DNS fallback: resolve per connection (system → cache → DoH) and hand
        # psycopg a direct IP while keeping the hostname for TLS/SNI
        # verification (no security loss). Survives startup-time outages and
        # cloud IP rotation because resolution is retried live on each connect.
        engine_kwargs["creator"] = _dns_resilient_creator(
            settings.sqlalchemy_database_uri, connect_timeout=settings.db_connect_timeout_seconds
        )
        engine_kwargs.pop("connect_args", None)
    engine = create_engine(settings.sqlalchemy_database_uri, **engine_kwargs)
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
