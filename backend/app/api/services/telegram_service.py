"""Identity resolution + turn pipeline for the Telegram webhook (audit §H).

Transport concerns stay here; the agent stays clean:

1. ``resolve_customer`` — get-or-create ``Customer`` by unique
   ``telegram_user_id`` (the external identity), refreshing public profile
   fields Telegram sends on every update.
2. ``resolve_conversation`` — reuse the customer's most recent OPEN
   conversation or create one (existing conversation model, audit §I).
3. ``is_duplicate_update`` — webhook idempotency (audit §H): Telegram
   retries webhooks on non-2xx; the customer Message row carries
   ``telegram_update_id`` (unique per conversation via the partial unique
   index from the initial migration), so a retried update is detected and
   skipped before running the agent twice.
4. ``process_agent_turn`` — run the EXISTING agent graph (classify → gather
   → compose → persist) and deliver ``draft_response`` back to the chat.
   Telegram identity travels through state metadata so ``persist_turn``
   stamps it on the customer message; no persistence logic is duplicated
   here. Failures are logged, never raised — the webhook already
   acknowledged Telegram, and a re-delivery would be skipped by (3).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.graph import run_turn
from app.core.logging import get_logger
from app.db.session import Database
from app.integrations.telegram.client import TelegramClient
from app.integrations.telegram.schemas import InboundMessage
from app.models import Conversation, ConversationStatus, Customer, Message

logger = get_logger(__name__)


def resolve_customer(session: Session, inbound: InboundMessage) -> Customer:
    """Get-or-create the Customer row for this Telegram identity.

    Persists username/names/locale once and refreshes them when Telegram
    sends updated profile data. The caller commits.
    """
    user = inbound.user
    customer = session.execute(
        select(Customer).where(Customer.telegram_user_id == user.id)
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(
            telegram_user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            locale=user.language_code,
        )
        session.add(customer)
    else:
        # Keep the public profile fresh (cheap, idempotent).
        customer.username = user.username
        customer.first_name = user.first_name
        customer.last_name = user.last_name
        customer.locale = user.language_code
    return customer


def resolve_conversation(session: Session, customer: Customer) -> Conversation:
    """Reuse the customer's most recent OPEN conversation, else create one.

    One customer may have several conversations over time; the webhook always
    continues the latest OPEN one so the dashboard shows a single thread per
    customer (audit §I).
    """
    conversation = session.execute(
        select(Conversation)
        .where(
            Conversation.customer_id == customer.id,
            Conversation.status == ConversationStatus.OPEN,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if conversation is None:
        conversation = Conversation(customer_id=customer.id)
        session.add(conversation)
    return conversation


def is_duplicate_update(session: Session, conversation: Conversation, update_id: int) -> bool:
    """True when this Telegram update was already processed (idempotency §H).

    Relies on the customer Message row written by ``persist_turn`` carrying
    ``telegram_update_id``; the DB-level partial unique index is the backstop
    against concurrent double-deliveries.
    """
    existing = session.execute(
        select(Message.id).where(
            Message.conversation_id == conversation.id,
            Message.telegram_update_id == update_id,
        )
    ).first()
    return existing is not None


async def process_inbound_message(
    inbound: InboundMessage,
    *,
    client: Any,
    telegram: TelegramClient,
    database: Database,
) -> None:
    """Full pipeline for one inbound message; never raises.

    1. Resolve identity + conversation (committed so the customer/conversation
       rows exist before the agent's persist node looks them up).
    2. Skip duplicates (idempotency §H).
    3. Run the EXISTING agent graph — no agent logic is duplicated here.
    4. Deliver ``draft_response`` back to the chat.

    Every failure path is logged and swallowed: the webhook has already
    acknowledged Telegram (fast-ack, audit §H), so raising here would only
    lose the log — Telegram will not re-deliver, and a manual replay would
    be skipped by the duplicate check anyway.
    """
    log = logger.bind(update_id=inbound.update_id, chat_id=inbound.chat_id)

    # --- Phase 1: identity + conversation (short-lived session) ----------
    try:
        session = database.session()
        try:
            customer = resolve_customer(session, inbound)
            session.flush()  # assign customer.id before it is used as a FK
            conversation = resolve_conversation(session, customer)
            session.flush()  # assign PKs before we hand the id to the graph
            if is_duplicate_update(session, conversation, inbound.update_id):
                session.rollback()
                log.info("telegram_update_duplicate_skipped")
                return
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — DB failure must not crash the task
        log.exception("telegram_identity_resolution_failed")
        return

    # --- Phase 2: agent turn (existing graph, persists everything) -------
    try:
        state = await run_turn(
            client,
            customer_message=inbound.text,
            conversation_id=str(conversation.id),
            database=database,
            metadata_extra={
                "telegram_update_id": inbound.update_id,
                "telegram_message_id": inbound.message_id,
                "external_created_at": inbound.sent_at,
            },
        )
    except Exception:  # noqa: BLE001 — turn failure must not crash the task
        log.exception("telegram_agent_turn_failed")
        return

    # --- Phase 3: delivery ------------------------------------------------
    await _deliver(telegram, inbound.chat_id, state.draft_response, log)


async def _deliver(telegram: TelegramClient, chat_id: int, text: str, log: Any) -> None:
    """Send the reply via the Bot API; log-and-continue on failure."""
    if not text:
        log.warning("telegram_empty_reply_skipped")
        return
    try:
        await telegram.send_message(chat_id, text)
    except Exception:  # noqa: BLE001 — delivery failure must not raise out
        log.exception("telegram_send_reply_failed")
