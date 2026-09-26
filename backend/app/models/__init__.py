"""Sales Agent application models.

Scope (architecture audit §I/§L): conversation-domain data owned by THIS
service. Inventra remains the source of truth for products/inventory — those
tables are deliberately NOT modeled here. LangGraph checkpoint tables are
created and owned by ``langgraph-checkpoint-postgres`` (later phase) — never
add them to this metadata.
"""

from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.base import Base, TimestampMixin, new_uuid, pg_enum, utc_now
from app.models.conversation import Conversation, ConversationChannel, ConversationStatus
from app.models.customer import Customer
from app.models.message import Message, MessageRole

__all__ = [
    "AgentRun",
    "AgentRunStatus",
    "Base",
    "Conversation",
    "ConversationChannel",
    "ConversationStatus",
    "Customer",
    "Message",
    "MessageRole",
    "TimestampMixin",
    "new_uuid",
    "pg_enum",
    "utc_now",
]
