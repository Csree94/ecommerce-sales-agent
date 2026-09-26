"""Conversation model — one chat thread between a customer and the agent."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid, pg_enum

if TYPE_CHECKING:
    from app.models.agent_run import AgentRun
    from app.models.customer import Customer
    from app.models.message import Message


class ConversationStatus(enum.StrEnum):
    """Lifecycle of a conversation (audit §I)."""

    OPEN = "open"
    CLOSED = "closed"
    NEEDS_HUMAN = "needs_human"


class ConversationChannel(enum.StrEnum):
    """Messaging channel the conversation runs on."""

    TELEGRAM = "telegram"


class Conversation(TimestampMixin, Base):
    """A conversation/session belonging to exactly one customer.

    ``last_message_at`` drives the admin dashboard's conversation list
    ordering (audit §I). All timestamps are timezone-aware.
    """

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_last_message_at", "last_message_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[ConversationChannel] = mapped_column(
        pg_enum(ConversationChannel, name="conversation_channel"),
        nullable=False,
        default=ConversationChannel.TELEGRAM,
        server_default=ConversationChannel.TELEGRAM.value,
    )
    status: Mapped[ConversationStatus] = mapped_column(
        pg_enum(ConversationStatus, name="conversation_status"),
        nullable=False,
        default=ConversationStatus.OPEN,
        server_default=ConversationStatus.OPEN.value,
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.created_at",
    )
    agent_runs: Mapped[list["AgentRun"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} status={self.status.value}>"
