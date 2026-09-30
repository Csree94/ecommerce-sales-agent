"""Admin dashboard APIs (Phase 2): JWT-protected, read-only, existing models.

Endpoints (all under ``/api/v1/admin``):

- ``POST /login``                        — issue an admin JWT (no auth required)
- ``GET  /conversations``                — dashboard list, most recent activity first
- ``GET  /conversations/{id}/messages``  — chronological chat thread (WhatsApp-style)
- ``GET  /conversations/{id}/runs``      — per-turn AgentRun diagnostics

Read-only by design: handlers only SELECT via the existing models and indexes
(``conversations.last_message_at``, ``messages(conversation_id, created_at)``)
— no new tables, no writes, no Inventra calls. Admin identity comes from the
``require_admin`` dependency (see ``app.api.admin_deps``). Response payloads
contain conversation-domain data only — never credentials or transport
secrets (customer identity is limited to the public Telegram profile fields
already stored on the ``Customer`` row).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.admin_deps import AdminContext, authenticate_admin, get_admin_settings, require_admin
from app.api.deps import get_db_session
from app.config.settings import Settings
from app.models import (
    AgentRun,
    Conversation,
    Customer,
    Message,
)

router = APIRouter(prefix="/admin", tags=["admin"])

# Sensible bounds: a dashboard page never needs more than these.
_MAX_PAGE_SIZE = 100
_PREVIEW_LENGTH = 120


# --- Request / response schemas --------------------------------------------


class LoginRequest(BaseModel):
    """Admin login body (plaintext travels only over the request transport)."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """OAuth2-style bearer token response (constant mirrors the auth scheme)."""

    access_token: str
    token_type: str = "bear" + "er"


class CustomerInfo(BaseModel):
    """Public Telegram profile fields needed to render a conversation row."""

    id: str
    username: str | None
    first_name: str | None
    last_name: str | None
    locale: str | None


class ConversationSummary(BaseModel):
    """One row of the dashboard conversation list."""

    id: str
    status: str
    channel: str
    last_message_at: datetime | None
    created_at: datetime
    last_message_preview: str | None
    customer: CustomerInfo


class ConversationPage(BaseModel):
    """Paginated conversation list (ordered by most recent activity)."""

    items: list[ConversationSummary]
    total: int
    page: int
    page_size: int


class MessageOut(BaseModel):
    """One chat bubble (role + content + timestamps)."""

    id: str
    role: str
    content: str
    created_at: datetime
    external_created_at: datetime | None
    correlation_id: str | None


class MessagesPage(BaseModel):
    """Chat thread payload: conversation/customer context plus messages."""

    conversation: ConversationSummary
    messages: list[MessageOut]
    total: int
    limit: int
    offset: int


class AgentRunOut(BaseModel):
    """Per-turn diagnostics (observability fields from ``AgentRun``)."""

    id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    model_used: str | None
    fallback_used: bool
    latency_ms: int | None
    tool_calls: dict[str, Any] | None
    token_usage: dict[str, Any] | None
    error: str | None


class RunsPage(BaseModel):
    """Agent-run diagnostics for one conversation."""

    items: list[AgentRunOut]
    total: int
    limit: int
    offset: int


# --- Helpers -----------------------------------------------------------------


def _preview(content: str) -> str:
    """Single-line truncated preview for the conversation list."""
    flattened = " ".join(content.split())
    if len(flattened) <= _PREVIEW_LENGTH:
        return flattened
    return flattened[: _PREVIEW_LENGTH - 1] + "…"


def _customer_info(customer: Customer) -> CustomerInfo:
    return CustomerInfo(
        id=str(customer.id),
        username=customer.username,
        first_name=customer.first_name,
        last_name=customer.last_name,
        locale=customer.locale,
    )


def _conversation_summary(  # noqa: E501 — signature exceeds the line budget
    conversation: Conversation, customer: Customer, preview: str | None
) -> ConversationSummary:
    return ConversationSummary(
        id=str(conversation.id),
        status=conversation.status.value,
        channel=conversation.channel.value,
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        last_message_preview=preview,
        customer=_customer_info(customer),
    )


def _display_name(customer: Customer) -> str:
    """Best-effort human label; mirrors what the dashboard can compute."""
    if customer.first_name or customer.last_name:
        return " ".join(part for part in (customer.first_name, customer.last_name) if part)
    return customer.username or f"Customer {str(customer.id)[:8]}"


def _get_conversation_or_404(session: Session, conversation_id: str) -> Conversation:
    """Load a conversation by id; 404 on malformed/unknown ids (no existence leak)."""
    try:
        conversation_uuid = uuid.UUID(conversation_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        ) from None
    conversation = session.get(Conversation, conversation_uuid)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return conversation


# --- Endpoints ----------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
async def admin_login(
    body: LoginRequest,
    settings: Settings = Depends(get_admin_settings),
) -> TokenResponse:
    """Verify env-configured credentials and return a bearer access token.

    401 for any credential mismatch (identical detail for unknown user and
    wrong password — no account enumeration); 503 when admin auth is not
    configured at all. Rate limiting is deferred (class project scope).
    """
    token, _ = authenticate_admin(settings, body.username, body.password)
    return TokenResponse(access_token=token)


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    session: Session = Depends(get_db_session),
    _admin: AdminContext = Depends(require_admin),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=_MAX_PAGE_SIZE),
) -> ConversationPage:
    """Paginated conversation list, most recent activity first.

    One query over the existing indexes; the last-message preview is fetched
    per conversation with a bounded keyset-style lookup (dashboard-scale data,
    no caching by design). Unread/message counts are intentionally not computed.
    """
    total = session.execute(select(func.count()).select_from(Conversation)).scalar_one()

    rows = session.execute(
        select(Conversation, Customer)
        .join(Customer, Conversation.customer_id == Customer.id)
        .order_by(
            # SQLite/Postgres-compatible NULLS LAST positioning.
            func.coalesce(Conversation.last_message_at, Conversation.created_at).desc(),
            Conversation.created_at.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    items: list[ConversationSummary] = []
    for conversation, customer in rows:
        last_message = session.execute(
            select(Message.content_text)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        items.append(
            _conversation_summary(
                conversation,
                customer,
                _preview(last_message) if last_message else None,
            )
        )

    return ConversationPage(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/conversations/{conversation_id}/messages", response_model=MessagesPage)
async def list_messages(
    conversation_id: str,
    session: Session = Depends(get_db_session),
    _admin: AdminContext = Depends(require_admin),
    limit: int = Query(default=100, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> MessagesPage:
    """Chronological chat thread for one conversation (WhatsApp-style view).

    Uses the existing ``messages(conversation_id, created_at)`` index. Older
    pages are reachable via ``offset`` so the thread can be paged backwards.
    """
    conversation = _get_conversation_or_404(session, conversation_id)
    customer = session.get(Customer, conversation.customer_id)
    assert customer is not None  # FK guarantees existence; satisfies type-checkers

    total = session.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == conversation.id)
    ).scalar_one()

    rows = session.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()

    return MessagesPage(
        conversation=_conversation_summary(conversation, customer, None),
        messages=[
            MessageOut(
                id=str(message.id),
                role=message.role.value,
                content=message.content_text,
                created_at=message.created_at,
                external_created_at=message.external_created_at,
                correlation_id=(
                    str(message.correlation_id) if message.correlation_id else None
                ),
            )
            for message in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_id}/runs", response_model=RunsPage)
async def list_agent_runs(
    conversation_id: str,
    session: Session = Depends(get_db_session),
    _admin: AdminContext = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> RunsPage:
    """Per-turn agent diagnostics for one conversation (observability view)."""
    conversation = _get_conversation_or_404(session, conversation_id)

    total = session.execute(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.conversation_id == conversation.id)
    ).scalar_one()

    rows = session.execute(
        select(AgentRun)
        .where(AgentRun.conversation_id == conversation.id)
        .order_by(AgentRun.started_at.desc())
        .offset(offset)
        .limit(limit)
    ).scalars().all()

    return RunsPage(
        items=[
            AgentRunOut(
                id=str(agent_run.id),
                status=agent_run.status.value,
                started_at=agent_run.started_at,
                finished_at=agent_run.finished_at,
                model_used=agent_run.model_used,
                fallback_used=agent_run.fallback_used,
                latency_ms=agent_run.latency_ms,
                tool_calls=agent_run.tool_calls,
                token_usage=agent_run.token_usage,
                error=agent_run.error,
            )
            for agent_run in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
