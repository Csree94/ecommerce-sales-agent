"""Purchase node (milestone 3B; revised by Step 3C — confirmation flow).

Sits between ``gather_context`` and ``compose_reply`` and is conditionally
gated: for every non-purchase message the node returns ``{}`` immediately —
no tool calls, no state changes, zero behavior change for read-only turns.

Business rule (Step 3C): an order request NEVER deducts stock.

    PHASE 1 — order request ("I want to buy 1 Samsung Galaxy S26"):
        resolve product → check stock → store a PENDING purchase (no write)
        → the composer asks the customer to confirm.
    PHASE 2 — explicit confirmation ("yes, confirm the order"):
        reload the pending purchase from the conversation's persisted turn
        evidence → re-check stock → stock-out EXACTLY ONCE (no retry) →
        mark completed and reply with the confirmation.
    PHASE 3 — decline ("no" / "cancel"):
        clear the pending purchase, never touch Inventra writes, reply that
        the order was cancelled.

Safety properties (all preserved from the original 3B design):
- Deterministic, narrow detection: only plainly-framed order requests enter
  Phase 1; passing mentions of "buy" never do. Confirmations/declines are
  only recognized when a pending purchase actually exists in the
  conversation — a bare "yes" in an unrelated chat is a no-op.
- Ambiguity never deducts: unresolved product or unparseable quantity bail
  out with ``skipped_*`` status (Phase 1 creates no pending purchase).
- Stock safety: availability is re-checked immediately before every write
  (Phase 1 informs, Phase 2 protects — stock may have moved between the
  request and the confirmation). Insufficient stock NEVER asks for
  confirmation as though it were available.
- Exactly-once write: the client's ``stock_out`` is a single explicit
  attempt (no retry), so a timeout can never double-deduct; failures are
  recorded as structured state, never as a successful purchase.
- Pending state persistence: stored on the AGENT message's
  ``content_metadata`` JSONB by the existing persist node — no schema
  change. The node never re-persists a consumed pending purchase.

Like ``gather_context``, the node contains no HTTP/DB session code: it uses
the typed tools in ``app.tools.inventra`` and the injected ``Database``
holder (same seam the persist node uses) for pending-state access only.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.agents.state import AgentState, PendingPurchase
from app.integrations.cache import InventraCache
from app.models import Message, MessageRole
from app.tools.errors import ToolError
from app.tools.inventra import (
    InventorySearchParams,
    ProductSearchParams,
    StockOutParams,
    check_inventory,
    search_products,
    stock_out_product,
)

# Purchase-intent phrases: explicit first-person/imperative order verbs,
# anchored to the start of the message. A passing mention of "buy" inside an
# unrelated question ("where can I buy a case for my laptop?") does NOT start
# the flow — only plainly-framed order requests do.
_PURCHASE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "I want to buy X" / "I'd like to purchase X" / "I'll buy X" ...
    re.compile(
        r"^\s*(?:please\s+)?i(?:'d| would|'ll| will)?\s*"
        r"(?:want to|wanna|'d like to|would like to|like to|'ll|will|'m going to|am going to)?\s*"
        r"(?:please\s+)?(?:buy|purchase|take|order)\b",
        re.IGNORECASE,
    ),
    # "I want to buy" split across helper: plain "i buy" not matched (ungrammatical),
    # but "I am buying" / "I'm buying" are purchase statements.
    re.compile(r"^\s*i(?:'m| am)\s+buying\b", re.IGNORECASE),
    # Bare imperative: "Buy 2 Samsung Galaxy S26" / "purchase trail shoes".
    re.compile(r"^\s*(?:please\s+)?(?:buy|purchase|order)\s+\S", re.IGNORECASE),
    # Polite request to the agent: "can you buy X" / "could you please purchase X".
    re.compile(
        r"^\s*(?:can|could|will|would)\s+you\s+(?:please\s+)?(?:buy|purchase|order)\b",
        re.IGNORECASE,
    ),
    # "Let me buy/get/order X".
    re.compile(r"^\s*(?:please\s+)?let\s+me\s+(?:buy|purchase|get|order)\b", re.IGNORECASE),
    # "Place an order for 2 Samsung Galaxy S26" / "place my order" — Step 3C
    # order-request phrasing (explicit order intent, still NOT a confirmation).
    re.compile(
        r"^\s*(?:please\s+)?i(?:'d| would|'ll| will)?\s*"
        r"(?:want to|wanna|'d like to|would like to|like to|'ll|will)?\s*"
        r"place\s+(?:an|my|the)\s+order\b",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*(?:please\s+)?place\s+(?:an|my|the)?\s*order\b", re.IGNORECASE),
)

# Confirmation of a PENDING purchase (Step 3C Phase 2). Anchored and narrow:
# a bare "yes" alone is NOT enough (yes-typing is common and ambiguous);
# the customer must affirmatively confirm the order itself. Only consulted
# when pending state actually exists for the conversation.
_CONFIRM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:yes,?\s*|yep,?\s*|yeah,?\s*|sure,?\s*|ok(?:ay)?,?\s*|confirmed,?\s*|"
        r"please\s+)?"
        r"(?:confirm(?:\s+(?:the|my|it))?\s+(?:order|purchase)?"
        r"|\s*confirm\s+it"
        r"|place\s+(?:the|my|it)?\s*order"
        r"|go\s+ahead"
        r"|proceed"
        r"|i\s+confirm"
        r"|i\s+(?:do\s+)?want\s+it"
        r"|buy\s+it)",
        re.IGNORECASE,
    ),
)

# Decline / cancel of a PENDING purchase (Step 3C Phase 3). Narrow on purpose:
# a "no" that is part of an unrelated answer must not cancel anything when no
# pending purchase exists (the caller only consults these when it does).
_DECLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:no,?\s*|nope,?\s*|nah,?\s*|cancel,?\s*|please\s+)?"
        r"(?:cancel(?:\s+(?:the|my|it))?\s*(?:order|purchase|it)?"
        r"|don'?t\s+(?:place|confirm|buy|purchase)\s+(?:the\s+|my\s+|it\b)?\s*(?:order|purchase|it)?"
        r"|not\s+now"
        r"|do\s+not\s+(?:place|confirm|buy|purchase)"
        r"|i\s+(?:do\s+)?(?:not\s+)?(?:want|don'?t\s+want)\s+it\s*(?:anymore|any\s+more)?"
        r"|stop\s+the\s+order)",
        re.IGNORECASE,
    ),
    # Bare "no" / "nope" / "nah" — valid ONLY when pending state exists.
    re.compile(r"^\s*(?:no|nope|nah)[.!?\s]*$", re.IGNORECASE),
)

# Bare affirmative WITHOUT a purchase noun ("yes", "yes please", "sure",
# "go ahead" is in the table above) — valid only with pending state.
_BARE_CONFIRM_PATTERN = re.compile(
    r"^\s*(?:yes|yep|yeah|sure|ok(?:ay)?|please\s+do)[.!?\s]*$", re.IGNORECASE
)


def _detect_purchase_intent(message: str) -> bool:
    """True only for an explicit, plainly-framed order request (Phase 1)."""
    stripped = message.strip()
    return any(pattern.match(stripped) for pattern in _PURCHASE_PATTERNS)


def _detect_confirmation(message: str) -> bool:
    """True when the message explicitly confirms an order (Phase 2 gate)."""
    stripped = message.strip()
    if _BARE_CONFIRM_PATTERN.match(stripped):
        return True
    return any(pattern.match(stripped) for pattern in _CONFIRM_PATTERNS)


def _detect_decline(message: str) -> bool:
    """True when the message explicitly declines/cancels (Phase 3 gate)."""
    stripped = message.strip()
    return any(pattern.match(stripped) for pattern in _DECLINE_PATTERNS)


def _extract_product_and_quantity(message: str) -> tuple[str | None, int | None]:
    """Split an order message into (product term, quantity).

    ``quantity`` is ``None`` when unspecified (defaults to 1 later) or when
    it cannot be parsed safely (caller must NOT deduct). A zero/negative
    digit quantity is returned as-is so the caller can reject it explicitly.
    """
    stripped = message.strip().rstrip(".!?")
    match = _QUANTITY_BEFORE_PRODUCT.match(stripped)
    if match is None:
        # No leading quantity: strip the order framing and keep the rest.
        term = re.sub(
            r"^\s*(?:please\s+)?(?:i(?:'d| would|'ll| will| want to|'d like to| would like to)?"
            r"\s+(?:like to\s+)?|can you\s+|could you\s+)?(?:please\s+)?"
            r"(?:place\s+(?:an|my|the)\s+order\s+for\s+|place\s+an\s+order\s+for\s+|buy|purchase)\s+",
            " ",
            stripped,
            flags=re.IGNORECASE,
        )
        term = re.sub(r"^(?:a|an|the|some)\s+", " ", term, flags=re.IGNORECASE)
        term = term.strip()
        # "buy -1 X" / "buy +3 X": a signed integer leading the remainder is
        # always a quantity (never a product name) — surface it so the caller
        # can reject non-positive values instead of defaulting to 1.
        leading_number = re.match(r"^([+-]?\d+)\s+(.+)$", term)
        if leading_number is not None:
            return leading_number.group(2).strip(), int(leading_number.group(1))
        return (term or None), None

    digits, word, product = match.group(1), match.group(2), match.group(3)
    if digits is not None:
        return product.strip(), int(digits)
    value = _NUMBER_WORDS.get(word.lower())
    if value is not None:
        return product.strip(), value
    # Unknown word between the verb and the rest (e.g. "buy please shoes"):
    # treat the whole remainder as the product — quantity unspecified.
    return f"{word} {product}".strip(), None


# "buy 2 Samsung Galaxy S26" / "buy one Samsung Galaxy S26" / "order 2 X" —
# quantity comes first, then the product. Only digits or known number words
# count as a quantity; any other token is left in the product name.
_QUANTITY_BEFORE_PRODUCT = re.compile(
    r"^\s*(?:please\s+)?(?:buy|purchase|order)\s+(?:(\d+)|([a-zA-Z]+))\s+(.+)$",
    re.IGNORECASE,
)

# Number words accepted for an explicit quantity (deterministic, tiny set).
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


async def _resolve_product(
    client: Any, term: str, cache: InventraCache | None = None
) -> tuple[int | None, str | None, float | None]:
    """Resolve the product term to (product_id, product_name, unit_price).

    Reuses the existing read-only product search tool (same call the
    ``product_search`` intent makes) — one bounded search, exact
    case-insensitive name preferred, first hit otherwise. Never invents a
    product; ``None`` ids mean the caller must not deduct. The price is
    carried so the confirmation request can quote it (Step 3C). The optional
    cache (milestone 4) rides the same tool call.
    """
    result = await search_products(
        client, ProductSearchParams(search=term, per_page=5), cache=cache
    )
    if result.total == 0:
        return None, None, None
    wanted = term.strip().lower()
    for candidate in result.products:
        if candidate.name.strip().lower() == wanted:
            return candidate.id, candidate.name, float(candidate.price)
    first = result.products[0]
    return first.id, first.name, float(first.price)


async def _available_stock(
    client: Any, product_id: int, cache: InventraCache | None = None
) -> int | None:
    """Available quantity for a product, or ``None`` when it cannot be read.

    There is no per-product inventory endpoint upstream, so the stock check
    uses the same verified list endpoint the ``inventory_check`` intent uses.
    A missing row (product without inventory) reads as 0 available — never
    as "unknown, deduct anyway".
    """
    items = await check_inventory(
        client, InventorySearchParams(search=str(product_id)), cache=cache
    )
    for item in items:
        if item.product_id == product_id:
            return item.quantity
    return 0


# --- Pending-purchase state (Step 3C) -------------------------------------------


def load_pending_purchase(database: Any, conversation_id: str | None) -> PendingPurchase | None:
    """Reload the conversation's pending purchase from persisted turn evidence.

    Reads the most recent AGENT message's ``content_metadata`` for the
    conversation (JSONB, written by the existing persist node). ``None``
    when there is no database, no conversation id, or no pending purchase —
    never raises: a read-only confirmation flow must not crash the turn.
    """
    if database is None or not conversation_id:
        return None
    try:
        conversation_uuid = _to_uuid(conversation_id)
        if conversation_uuid is None:
            return None
        session = database.session()
        try:
            row = session.execute(
                select(Message.content_metadata)
                .where(
                    Message.conversation_id == conversation_uuid,
                    Message.role == MessageRole.AGENT,
                    Message.content_metadata["pending_purchase"].isnot(None),
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — pending state must never crash the turn
        return None
    if not row:
        return None
    try:
        return PendingPurchase.model_validate(row["pending_purchase"])
    except Exception:  # noqa: BLE001 — malformed evidence behaves as "no pending"
        return None


def _to_uuid(raw: str | None):
    """Convert a conversation id string to UUID (None when invalid)."""
    import uuid as _uuid

    if not raw:
        return None
    try:
        return _uuid.UUID(str(raw))
    except ValueError:
        return None


def clear_pending_purchase(database: Any, conversation_id: str | None) -> None:
    """Remove the pending purchase from the conversation's latest evidence.

    Used on Phase 2 (confirmation consumed) and Phase 3 (decline): the
    pending purchase must never outlive its resolution, otherwise a later
    unrelated "yes" could re-trigger a deduction. Fire-and-forget: a failed
    clear is logged by the persist layer and does not crash the turn — but
    the in-turn state update already removes it from future persistence.
    """
    if database is None or not conversation_id:
        return
    conversation_uuid = _to_uuid(conversation_id)
    if conversation_uuid is None:
        return
    try:
        session = database.session()
        try:
            row = session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_uuid,
                    Message.role == MessageRole.AGENT,
                    Message.content_metadata["pending_purchase"].isnot(None),
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None:
                metadata = dict(row.content_metadata or {})
                metadata.pop("pending_purchase", None)
                row.content_metadata = metadata  # JSONB mutation tracking
                session.commit()
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — best-effort cleanup, never crash the turn
        return


def _pending_failure(
    database: Any, conversation_id: str | None, status: str, message: str
) -> dict[str, Any]:
    """Build a failed-confirmation update that also clears the pending state.

    A failed write (timeout/auth/unexpected) consumes the pending purchase:
    the customer must re-request the order explicitly rather than the next
    bare "yes" silently retrying a write that may or may not have landed.
    """
    clear_pending_purchase(database, conversation_id)
    return {
        "intent": "purchase",
        "purchase_requested": True,
        "purchase_status": status,
        "pending_purchase": None,
        "purchase_message": message,
        "tool_errors": {},  # populated by the caller for write failures
    }


async def purchase(
    state: AgentState,
    client: Any,
    database: Any = None,
    cache: InventraCache | None = None,
) -> dict[str, Any]:
    """Node: run the confirmation-gated purchase flow when — and only when —
    an order is explicitly requested and later explicitly confirmed.

    ``client`` is the injected ``InventraClient``; ``database`` the optional
    ``Database`` holder (pending-state persistence; same seam as the persist
    node); ``cache`` the optional milestone-4 read-through cache (rides the
    same tool calls; the write itself is untouched apart from its cache
    invalidation hook). Returns a state update; every non-purchase turn
    returns ``{}``.
    """
    message = state.customer_message

    # --- Phase 2: explicit confirmation of an existing pending purchase -----
    if _detect_confirmation(message):
        pending = load_pending_purchase(database, state.conversation_id)
        if pending is None:
            return {}  # a bare "yes" without a pending purchase is a no-op
        # Re-check stock immediately before the write (it may have moved).
        try:
            available = await _available_stock(client, pending.product_id, cache=cache)
        except ToolError as exc:
            return _pending_failure(
                database,
                state.conversation_id,
                "failed_unavailable"
                if exc.code in ("timeout", "unavailable", "upstream_error")
                else "failed_unexpected",
                "I couldn't verify current stock, so nothing was purchased. "
                "Please confirm again in a moment.",
            )
        if available is None or available < pending.quantity:
            clear_pending_purchase(database, state.conversation_id)
            return {
                "intent": "purchase",
                "purchase_requested": True,
                "purchase_quantity": pending.quantity,
                "purchase_status": "skipped_insufficient_stock",
                "pending_purchase": None,
                "purchase_message": (
                    f"Sorry, only {available} unit(s) of {pending.product_name} are "
                    "currently available, so the order was not placed."
                ),
            }
        try:
            result = await stock_out_product(
                client,
                StockOutParams(
                    product_id=pending.product_id,
                    quantity=pending.quantity,
                    notes=f"Sales agent purchase ({pending.quantity} x {pending.product_name})",
                ),
                cache=cache,
            )
        except ToolError as exc:
            code = exc.code
            status = (
                "failed_unavailable"
                if code in ("timeout", "unavailable", "upstream_error")
                else "failed_auth" if code == "auth" else "failed_unexpected"
            )
            update = _pending_failure(
                database,
                state.conversation_id,
                status,
                "Your purchase could not be completed at this time. "
                "Nothing was confirmed — please place the order again.",
            )
            update["purchase_quantity"] = pending.quantity
            update["tool_errors"] = {
                "purchase": type(exc)(f"purchase stock-out failed: {exc}", code=code)
            }
            return update

        # Success: mark completed and consume the pending purchase.
        clear_pending_purchase(database, state.conversation_id)
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_quantity": pending.quantity,
            "purchase_status": "completed",
            "pending_purchase": None,
            "purchase_message": (
                f"Your order for {pending.quantity} x {pending.product_name} at "
                f"{pending.unit_price:.2f} has been confirmed successfully. "
                f"Remaining stock: {result.quantity}."
            ),
        }

    # --- Phase 3: explicit decline of an existing pending purchase ----------
    if _detect_decline(message):
        pending = load_pending_purchase(database, state.conversation_id)
        if pending is None:
            return {}  # a bare "no" without a pending purchase is a no-op
        clear_pending_purchase(database, state.conversation_id)
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_status": "cancelled_by_customer",
            "pending_purchase": None,
            "purchase_message": (
                "Understood — the order was cancelled and nothing was purchased."
            ),
        }

    # --- Phase 1: order request — NEVER writes ------------------------------
    if not _detect_purchase_intent(message):
        return {}

    term, quantity = _extract_product_and_quantity(message)
    if term is None:
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_status": "skipped_invalid_quantity",
            "purchase_message": (
                "I couldn't tell which product you'd like to order. "
                "Could you name the product?"
            ),
        }

    if quantity is None:
        quantity = 1  # no quantity specified → default exactly one unit
    if quantity <= 0:
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_quantity": quantity,
            "purchase_status": "skipped_invalid_quantity",
            "pending_purchase": None,
            "purchase_message": "The quantity must be at least 1.",
        }

    try:
        product_id, product_name, unit_price = await _resolve_product(client, term, cache=cache)
        if product_id is None or product_name is None:
            return {
                "intent": "purchase",
                "purchase_requested": True,
                "purchase_quantity": quantity,
                "purchase_status": "skipped_product_unresolved",
                "pending_purchase": None,
                "purchase_message": (
                    "I couldn't find that product in the catalog, so nothing "
                    "was ordered. Could you check the name?"
                ),
            }

        available = await _available_stock(client, product_id, cache=cache)
        if available is None or available < quantity:
            # Insufficient stock must never ask for confirmation as though
            # the order were possible — no pending purchase is created.
            return {
                "intent": "purchase",
                "purchase_requested": True,
                "purchase_quantity": quantity,
                "purchase_status": "skipped_insufficient_stock",
                "pending_purchase": None,
                "purchase_message": (
                    f"Sorry, only {available} unit(s) of {product_name} are "
                    "currently available, so I can't place that order."
                ),
            }

        # Phase 1 outcome: pending confirmation — NO stock-out, NOT completed.
        # Supersede any older pending purchase first so the newest request is
        # always the one a later confirmation resolves (load_pending reads the
        # most recent marker; a stale marker must never survive beneath it).
        clear_pending_purchase(database, state.conversation_id)
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_quantity": quantity,
            "purchase_status": "pending_confirmation",
            "pending_purchase": PendingPurchase(
                product_id=product_id,
                product_name=product_name,
                quantity=quantity,
                unit_price=unit_price if unit_price is not None else 0.0,
            ),
            "purchase_message": (
                f"{product_name} is available for {unit_price:.2f}. "
                f"Would you like me to confirm the order for {quantity} unit(s)?"
            ),
        }
    except ToolError as exc:
        code = exc.code
        status = (
            "failed_unavailable"
            if code in ("timeout", "unavailable", "upstream_error")
            else "failed_auth" if code == "auth" else "failed_unexpected"
        )
        return {
            "intent": "purchase",
            "purchase_requested": True,
            "purchase_quantity": quantity,
            "purchase_status": status,
            "pending_purchase": None,
            "purchase_message": (
                "I couldn't check the catalog just now, so no order was placed. "
                "Please try again shortly."
            ),
            "tool_errors": {
                "purchase": type(exc)(f"purchase lookup failed: {exc}", code=code)
            },
        }
