"""Telegram webhook endpoint — receives Telegram updates (audit §H).

Contract with Telegram:
- Telegram POSTs one JSON update per call and retries on non-2xx. The route
  therefore validates what it can, schedules what it must, and answers 200
  for anything that parses as an update (even non-text ones, which are
  skipped) — a 4xx/5xx would make Telegram retry the same update forever.
- Verification: ``X-Telegram-Bot-Api-Secret-Token`` is compared in constant
  time against ``TELEGRAM_WEBHOOK_SECRET`` (reject non-matching at the edge).
- Decoupling: the route only parses/acknowledges; conversation resolution,
  the agent turn and delivery live in the service + integration layers.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.telegram_deps import get_telegram_client, verify_webhook_secret
from app.core.logging import get_logger
from app.db.session import Database
from app.integrations.telegram.client import TelegramClient
from app.integrations.telegram.schemas import extract_inbound, parse_update

logger = get_logger(__name__)

router = APIRouter(tags=["telegram"])

# Bounded worker pool (audit §H): Telegram gets an immediate ack; turns run
# with limited concurrency so a slow LLM turn never causes webhook timeouts
# and unbounded re-deliveries.
_TURN_SEMAPHORE = asyncio.Semaphore(5)


class WebhookAck(BaseModel):
    """Minimal 200 response body for Telegram webhook deliveries."""

    ok: bool = True


@router.post("/telegram/webhook", response_model=WebhookAck)
async def telegram_webhook(
    request: Request,
    telegram: TelegramClient = Depends(get_telegram_client),
    _verified: None = Depends(verify_webhook_secret),
) -> WebhookAck:
    """Receive one Telegram update; fast-ack and process in the background."""
    database: Database = request.app.state.database
    client = request.app.state.inventra_client
    # Milestone 4: read-through cache (getattr: absent in minimal test apps →
    # direct reads, the historical behavior).
    cache = getattr(request.app.state, "inventra_cache", None)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("telegram_webhook_malformed_body")
        return WebhookAck(ok=True)  # acknowledge garbage; do not cause retries

    update = parse_update(payload)
    if update is None:
        logger.warning("telegram_webhook_unparsable_update")
        return WebhookAck(ok=True)

    inbound = extract_inbound(update)
    if inbound is None:
        # Non-text/edited/non-private update: safely ignored (still acked).
        return WebhookAck(ok=True)

    async def _process() -> None:
        from app.api.services.telegram_service import process_inbound_message

        async with _TURN_SEMAPHORE:
            await process_inbound_message(
                inbound,
                client=client,
                telegram=telegram,
                database=database,
                cache=cache,
            )

    task = asyncio.create_task(_process())
    # Hold a strong reference: asyncio may garbage-collect unreferenced tasks.
    background = getattr(request.app.state, "telegram_background_tasks", None)
    if background is not None:
        background.add(task)
        task.add_done_callback(background.discard)
    return WebhookAck(ok=True)
