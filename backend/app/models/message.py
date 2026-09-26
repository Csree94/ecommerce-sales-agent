"""Message model — a single message within a conversation (audit §I)."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid, pg_enum

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class MessageRole(enum.StrEnum):
    """Who produced the message (audit §I: customer vs agent must be distinguishable)."""

    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    ADMIN = "admin"


class Message(TimestampMixin, Base):
    """A message inside a conversation.

    ``correlation_id`` groups all rows produced by one agent turn (customer
    message, tool activity metadata, agent reply — audit §I). Telegram's
    ``update_id`` is stored unique-per-conversation for webhook idempotency
    (audit §H); it is nullable because agent/system/admin messages have no
    Telegram update of their own.
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Dashboard history query: chronological messages per conversation.
        Index("ix_messages_conversation_created_at", "conversation_id", "created_at"),
        # Telegram webhook idempotency — scoped per conversation.
        Index(
            "uq_messages_conversation_telegram_update_id",
            "conversation_id",
            "telegram_update_id",
            unique=True,
            postgresql_where=text("telegram_update_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[MessageRole] = mapped_column(
        pg_enum(MessageRole, name="message_role"),
        nullable=False,
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Tool calls made, product cards shown, model used, fallback flag, etc.
    content_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Telegram-side identifiers (§H idempotency); null for non-customer roles.
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # When the message happened per Telegram (vs. when we persisted it).
    external_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message id={self.id} role={self.role.value}>"
