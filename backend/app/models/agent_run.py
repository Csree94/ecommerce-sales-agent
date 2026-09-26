"""AgentRun model — per-turn observability record (audit §I/§L, optional but recommended)."""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid, pg_enum, utc_now

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class AgentRunStatus(enum.StrEnum):
    """Execution status of one agent turn."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"


class AgentRun(TimestampMixin, Base):
    """One execution of the agent graph for a conversation turn.

    Pure observability: model used, fallback flag, tool calls, token usage,
    latency, error. Execution *state* lives in LangGraph checkpoints (managed
    by LangGraph itself later) — never here.
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_conversation_started_at", "conversation_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[AgentRunStatus] = mapped_column(
        pg_enum(AgentRunStatus, name="agent_run_status"),
        nullable=False,
        default=AgentRunStatus.RUNNING,
        server_default=AgentRunStatus.RUNNING.value,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_used: Mapped[bool] = mapped_column(nullable=False, default=False)
    tool_calls: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="agent_runs")

    def __repr__(self) -> str:
        return f"<AgentRun id={self.id} status={self.status.value}>"
