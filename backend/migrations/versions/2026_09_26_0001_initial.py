"""Initial schema: customers, conversations, messages, agent_runs

Sales Agent application data only (architecture audit §I/§L):
- No product/inventory tables (Inventra remains the source of truth).
- No LangGraph checkpoint tables (owned by langgraph-checkpoint-postgres).

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Native PostgreSQL enum types (mirrors app/models enums) -------------
channel_enum = postgresql.ENUM("telegram", name="conversation_channel", create_type=False)
status_enum = postgresql.ENUM(
    "open", "closed", "needs_human", name="conversation_status", create_type=False
)
role_enum = postgresql.ENUM(
    "customer", "agent", "system", "admin", name="message_role", create_type=False
)
run_status_enum = postgresql.ENUM(
    "running", "succeeded", "failed", "timeout", name="agent_run_status", create_type=False
)

_ALL_ENUMS = (channel_enum, status_enum, role_enum, run_status_enum)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in _ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "telegram_user_id",
            sa.BigInteger(),
            nullable=False,
            comment="Telegram user id (external identity)",
        ),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column("locale", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("telegram_user_id", name="uq_customers_telegram_user_id"),
    )

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            channel_enum,
            nullable=False,
            server_default=sa.text("'telegram'::conversation_channel"),
        ),
        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default=sa.text("'open'::conversation_status"),
        ),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            name="fk_conversations_customer_id_customers",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_conversations_customer_id", "conversations", ["customer_id"], unique=False)
    op.create_index(
        "ix_conversations_last_message_at", "conversations", ["last_message_at"], unique=False
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_metadata", postgresql.JSONB(), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("external_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_messages_conversation_created_at",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    # Telegram webhook idempotency (audit §H) — partial unique per conversation.
    op.create_index(
        "uq_messages_conversation_telegram_update_id",
        "messages",
        ["conversation_id", "telegram_update_id"],
        unique=True,
        postgresql_where=sa.text("telegram_update_id IS NOT NULL"),
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            run_status_enum,
            nullable=False,
            server_default=sa.text("'running'::agent_run_status"),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("token_usage", postgresql.JSONB(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_agent_runs_conversation_id_conversations",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_agent_runs_conversation_started_at",
        "agent_runs",
        ["conversation_id", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_conversation_started_at", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index(
        "uq_messages_conversation_telegram_update_id",
        table_name="messages",
        postgresql_where=sa.text("telegram_update_id IS NOT NULL"),
    )
    op.drop_index("ix_messages_conversation_created_at", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_last_message_at", table_name="conversations")
    op.drop_index("ix_conversations_customer_id", table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("customers")

    bind = op.get_bind()
    for enum_type in reversed(_ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)
