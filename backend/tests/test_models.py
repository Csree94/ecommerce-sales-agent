"""Database model tests.

These run without a live database: they validate schema structure (tables,
constraints, indexes, enum values, cascade rules) directly against the
SQLAlchemy metadata, and cross-check the initial Alembic migration by
executing its ``upgrade()``/``downgrade()`` with mocked DDL operations.
"""

from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import ForeignKeyConstraint

from app.models import (
    AgentRun,
    AgentRunStatus,
    Base,
    Conversation,
    ConversationChannel,
    ConversationStatus,
    Customer,
    Message,
    MessageRole,
    utc_now,
)

EXPECTED_TABLES = {"customers", "conversations", "messages", "agent_runs"}


def _index_names(model: type) -> set[str]:
    return {idx.name for idx in model.__table__.indexes}


# --- Scope guardrails (architecture audit §I/§L) -------------------------


def test_metadata_contains_exactly_the_application_tables() -> None:
    """No product/inventory/cart/order tables; no LangGraph checkpoint tables."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_inventra_or_langgraph_tables_defined() -> None:
    forbidden = {"products", "inventory", "carts", "orders", "checkpoints", "checkpoint_blobs"}
    assert not (set(Base.metadata.tables) & forbidden)


# --- Customer ------------------------------------------------------------


def test_customer_unique_telegram_identity() -> None:
    table = Customer.__table__
    assert table.c.telegram_user_id.nullable is False
    assert any(c.name == "uq_customers_telegram_user_id" for c in table.constraints)


def test_customer_defaults_are_uuid_and_tz_aware() -> None:
    assert Customer.__table__.c.id.default is not None
    assert utc_now().tzinfo is UTC


# --- Conversation --------------------------------------------------------


def test_conversation_fk_cascades_and_indexes() -> None:
    table = Conversation.__table__
    fk = next(iter(table.c.customer_id.foreign_keys))
    assert fk.column.table.name == "customers"
    assert fk.ondelete == "CASCADE"
    assert "ix_conversations_last_message_at" in _index_names(Conversation)


def test_conversation_enum_values_match_migration() -> None:
    assert {s.value for s in ConversationStatus} == {"open", "closed", "needs_human"}
    assert {c.value for c in ConversationChannel} == {"telegram"}


# --- Message -------------------------------------------------------------


def test_message_indexes_for_history_and_telegram_idempotency() -> None:
    names = _index_names(Message)
    assert "ix_messages_conversation_created_at" in names
    assert "uq_messages_conversation_telegram_update_id" in names
    partial = next(
        idx
        for idx in Message.__table__.indexes
        if idx.name == "uq_messages_conversation_telegram_update_id"
    )
    assert partial.dialect_options["postgresql"]["where"] is not None


def test_message_roles_match_migration() -> None:
    assert {r.value for r in MessageRole} == {"customer", "agent", "system", "admin"}


def test_message_correlation_and_metadata_columns_are_optional() -> None:
    table = Message.__table__
    assert table.c.correlation_id.nullable is True
    assert table.c.content_metadata.nullable is True
    assert table.c.telegram_update_id.nullable is True


# --- AgentRun ------------------------------------------------------------


def test_agent_run_columns_and_index() -> None:
    table = AgentRun.__table__
    assert "ix_agent_runs_conversation_started_at" in _index_names(AgentRun)
    assert table.c.started_at.default is not None
    assert table.c.fallback_used.nullable is False
    assert {s.value for s in AgentRunStatus} == {"running", "succeeded", "failed", "timeout"}


# --- Relationships -------------------------------------------------------


def test_relationships_are_bidirectional() -> None:
    assert Customer.__mapper__.relationships["conversations"].back_populates == "customer"
    assert Conversation.__mapper__.relationships["customer"].back_populates == "conversations"
    assert Conversation.__mapper__.relationships["messages"].back_populates == "conversation"
    assert Message.__mapper__.relationships["conversation"].back_populates == "messages"
    assert Conversation.__mapper__.relationships["agent_runs"].back_populates == "conversation"
    assert AgentRun.__mapper__.relationships["conversation"].back_populates == "agent_runs"


def test_cascades_delete_children_with_the_conversation() -> None:
    """Cascades live on the parent (one-to-many) side of each relationship."""
    assert Conversation.__mapper__.relationships["messages"].cascade.delete_orphan is True
    assert Conversation.__mapper__.relationships["agent_runs"].cascade.delete_orphan is True


# --- Migration cross-check -----------------------------------------------
#
# The initial migration is hand-written (no live DB in this environment), so
# we execute its upgrade()/downgrade() against mocked Alembic ops and compare
# the DDL it *would* run with the SQLAlchemy metadata.


@pytest.fixture()
def migration_module():  # type: ignore[no-untyped-def]
    pytest.importorskip("alembic")
    import importlib

    return importlib.import_module("migrations.versions.2026_09_26_0001_initial".replace("-", "_"))


def test_initial_migration_upgrade_creates_expected_schema(migration_module) -> None:  # type: ignore[no-untyped-def]
    created_tables: set[str] = set()
    created_indexes: set[str] = set()

    def fake_create_table(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        created_tables.add(name)
        return MagicMock()

    def fake_create_index(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        created_indexes.add(name)
        return MagicMock()

    with (
        patch("alembic.op.create_table", side_effect=fake_create_table),
        patch("alembic.op.create_index", side_effect=fake_create_index),
        patch("alembic.op.get_bind", return_value=MagicMock()),
    ):
        migration_module.upgrade()

    assert created_tables == EXPECTED_TABLES
    assert {
        "ix_conversations_customer_id",
        "ix_conversations_last_message_at",
        "ix_messages_conversation_created_at",
        "uq_messages_conversation_telegram_update_id",
        "ix_agent_runs_conversation_started_at",
    } <= created_indexes


def test_initial_migration_upgrade_fks_and_uniques_match_models(migration_module) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, list] = {}

    def fake_create_table(name, *columns, **kwargs):  # type: ignore[no-untyped-def]
        captured[name] = list(columns) + list(kwargs.get("constraints", []) or [])
        return MagicMock()

    with (
        patch("alembic.op.create_table", side_effect=fake_create_table),
        patch("alembic.op.create_index", return_value=MagicMock()),
        patch("alembic.op.get_bind", return_value=MagicMock()),
    ):
        migration_module.upgrade()

    model_by_table = {
        "customers": Customer,
        "conversations": Conversation,
        "messages": Message,
        "agent_runs": AgentRun,
    }
    for table_name, model in model_by_table.items():
        model_fk_names = {
            c.name
            for c in model.__table__.constraints
            if isinstance(c, ForeignKeyConstraint) and c.name
        }
        migrated_fk_names = {
            c.name for c in captured[table_name] if isinstance(c, ForeignKeyConstraint) and c.name
        }
        assert model_fk_names == migrated_fk_names, table_name

    # Unique constraints defined at model level must appear in migration DDL.
    assert any(
        getattr(c, "name", None) == "uq_customers_telegram_user_id" for c in captured["customers"]
    )


def test_initial_migration_downgrade_reverses_upgrade(migration_module) -> None:  # type: ignore[no-untyped-def]
    dropped_tables: list[str] = []
    with (
        patch("alembic.op.drop_table", side_effect=lambda name, **kw: dropped_tables.append(name)),
        patch("alembic.op.drop_index", return_value=MagicMock()),
        patch("alembic.op.get_bind", return_value=MagicMock()),
    ):
        migration_module.downgrade()

    assert dropped_tables == ["agent_runs", "messages", "conversations", "customers"]
