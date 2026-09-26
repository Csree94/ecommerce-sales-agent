"""Declarative base and shared model mixins (SQLAlchemy 2.x).

Covers Sales Agent application data only (audit §I/§L). Product/inventory
truth stays in Inventra and is deliberately NOT modeled here; LangGraph
checkpoint tables are managed by ``langgraph-checkpoint-postgres`` itself and
must never be added to this metadata.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Enum as SAEnum
from sqlalchemy import MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Alembic-friendly, deterministic constraint names.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp (Python-side default)."""
    return datetime.now(UTC)


def pg_enum(enum_cls: type[Any], *, name: str) -> SAEnum:
    """Native PostgreSQL enum bound to a ``str``-valued Python enum."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [member.value for member in e],
    )


class Base(DeclarativeBase):
    """Base for all Sales Agent application models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at``/``updated_at`` — timezone-aware, DB- and Python-defaulted."""

    created_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=text("now()"),
    )


def new_uuid() -> uuid.UUID:
    """Default UUID primary key generator."""
    return uuid.uuid4()
