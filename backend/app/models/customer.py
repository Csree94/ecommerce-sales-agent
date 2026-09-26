"""Customer model — Telegram-identified customer of the sales agent."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Customer(TimestampMixin, Base):
    """A customer, uniquely identified by their Telegram user id.

    Telegram supplies user identity on every update; we persist it once and
    reference it internally (audit §H: the agent sees an internal customer id,
    never raw transport details).
    """

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, comment="Telegram user id (external identity)"
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(16), nullable=True)

    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="customer",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Customer id={self.id} telegram_user_id={self.telegram_user_id}>"
