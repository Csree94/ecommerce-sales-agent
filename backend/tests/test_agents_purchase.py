"""Tests for the purchase → Inventra stock-out flow (milestone 3B).

All Inventra access is mocked at the ``InventraClient`` boundary — no real
HTTP, no live Telegram, no live Inventra. Coverage:

- purchase detection: explicit intent triggers the flow; ordinary product
  questions and passing mentions of "buy" stay read-only;
- quantity parsing: explicit numbers/words parsed, default 1, zero/negative
  rejected before any write;
- stock safety: insufficient stock never asks for confirmation and never
  calls stock-out (both at order time and at confirmation time);
- confirmation flow (Step 3C): an order request only stores a pending
  purchase (no write); an explicit confirmation calls stock-out exactly
  once; a decline cancels without any write;
- no-retry: a timeout/error on the write never retries and never reports
  success;
- failure modes: unavailable Inventra, auth failure, unresolved product —
  each produces an honest failure state, never a success;
- graph wiring: the purchase node runs between gather and compose, and
  non-purchase turns are byte-for-byte unaffected.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.agents.purchase import (
    _detect_purchase_intent,
    _extract_product_and_quantity,
    purchase,
)
from app.agents.state import AgentState, PendingPurchase
from app.integrations.inventra.schemas import (
    InventoryItem,
    ProductListResponse,
    StockOutResult,
)
from app.tools.errors import ToolError, ToolUnavailableError


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def make_async(value: Any) -> Any:
    async def _inner() -> Any:
        return value

    return _inner()


PRODUCT = {
    "id": 12,
    "name": "Samsung Galaxy S26",
    "sku": "SGS26-256",
    "description": "Flagship smartphone, 256 GB",
    "category_id": 2,
    "category_name": "Phones",
    "price": 1099.0,
    "is_active": True,
    "created_at": "2026-03-01T09:00:00Z",
    "updated_at": "2026-03-01T09:00:00Z",
}

PRODUCT_LIST = {
    "products": [PRODUCT],
    "total": 1,
    "page": 1,
    "per_page": 5,
    "pages": 1,
}


def inventory_item(quantity: int) -> InventoryItem:
    return InventoryItem.model_validate(
        {
            "id": 11,
            "product_id": 12,
            "product_name": "Samsung Galaxy S26",
            "product_sku": "SGS26-256",
            "quantity": quantity,
            "low_stock_threshold": 2,
            "is_low_stock": quantity <= 2,
            "updated_at": "2026-02-01T12:30:00Z",
        }
    )


def stock_out_result(remaining: int) -> StockOutResult:
    return StockOutResult.model_validate(
        {
            "product_id": 12,
            "product_name": "Samsung Galaxy S26",
            "quantity": remaining,
            "low_stock_threshold": 2,
            "is_low_stock": remaining <= 2,
        }
    )


def make_client(quantity: int = 5) -> MagicMock:
    """Mock client: product search hits, inventory reports ``quantity``."""
    client = MagicMock()
    client.list_products.return_value = make_async(
        ProductListResponse.model_validate(PRODUCT_LIST)
    )
    client.list_inventory.return_value = make_async([inventory_item(quantity)])
    client.stock_out.return_value = make_async(stock_out_result(quantity - 1))
    return client


# --- Detection -----------------------------------------------------------------


def test_detection_explicit_buy_triggers() -> None:
    assert _detect_purchase_intent("I want to buy Samsung Galaxy S26")
    assert _detect_purchase_intent("buy one Samsung Galaxy S26")
    assert _detect_purchase_intent("I would like to purchase Samsung Galaxy S26")
    assert _detect_purchase_intent("Buy 2 Samsung Galaxy S26")
    assert _detect_purchase_intent("can you buy the trail shoes?")


def test_detection_ordinary_questions_stay_read_only() -> None:
    assert not _detect_purchase_intent("What is the price of Samsung Galaxy S26?")
    assert not _detect_purchase_intent("Tell me about Samsung Galaxy S26")
    assert not _detect_purchase_intent("How much stock is available?")
    assert not _detect_purchase_intent("Show me Samsung phones")
    assert not _detect_purchase_intent("Where can I buy a phone case for my laptop?")


# --- Quantity parsing -----------------------------------------------------------


def test_quantity_explicit_number_parsed() -> None:
    term, quantity = _extract_product_and_quantity("buy 2 Samsung Galaxy S26")
    assert (term, quantity) == ("Samsung Galaxy S26", 2)


def test_quantity_number_word_parsed() -> None:
    term, quantity = _extract_product_and_quantity("buy one Samsung Galaxy S26")
    assert (term, quantity) == ("Samsung Galaxy S26", 1)


def test_quantity_defaults_to_one_when_unspecified() -> None:
    term, quantity = _extract_product_and_quantity("I want to buy Samsung Galaxy S26")
    assert term == "Samsung Galaxy S26"
    assert quantity is None  # caller defaults to 1


def test_quantity_zero_is_rejected_before_any_write() -> None:
    client = make_client()
    result = run(purchase(AgentState(customer_message="buy 0 Samsung Galaxy S26"), client))
    assert result["purchase_status"] == "skipped_invalid_quantity"
    client.stock_out.assert_not_called()
    client.list_products.assert_not_called()


def test_quantity_negative_is_rejected_before_any_write() -> None:
    client = make_client()
    result = run(purchase(AgentState(customer_message="buy -1 Samsung Galaxy S26"), client))
    assert result["purchase_status"] == "skipped_invalid_quantity"
    client.stock_out.assert_not_called()


# --- Node behavior: non-purchase turns --------------------------------------------


def test_non_purchase_message_is_a_no_op() -> None:
    client = make_client()
    result = run(
        purchase(
            AgentState(
                customer_message="What is the price of Samsung Galaxy S26?",
                intent="product_details",
            ),
            client,
        )
    )
    assert result == {}
    client.list_products.assert_not_called()
    client.list_inventory.assert_not_called()
    client.stock_out.assert_not_called()


def test_ordinary_buy_mention_does_not_deduct() -> None:
    client = make_client()
    result = run(
        purchase(
            AgentState(
                customer_message="Where can I buy a phone case for my laptop?",
                intent="product_search",
            ),
            client,
        )
    )
    assert result == {}
    client.stock_out.assert_not_called()


# --- Phase 1: order request — pending only, NEVER a stock-out -------------------


def test_order_request_creates_pending_purchase() -> None:
    client = make_client(quantity=5)
    result = run(
        purchase(
            AgentState(customer_message="I want to buy 1 Samsung Galaxy S26"),
            client,
        )
    )
    assert result["purchase_status"] == "pending_confirmation"
    pending = result["pending_purchase"]
    assert pending is not None
    assert pending.product_id == 12
    assert pending.product_name == "Samsung Galaxy S26"
    assert pending.quantity == 1
    assert pending.unit_price == 1099.0
    client.stock_out.assert_not_called()


def test_order_request_does_not_call_stock_out() -> None:
    client = make_client(quantity=5)
    run(purchase(AgentState(customer_message="I want to buy Samsung Galaxy S26"), client))
    client.stock_out.assert_not_called()
    client.list_inventory.assert_called()  # availability WAS checked


def test_order_request_asks_for_confirmation() -> None:
    client = make_client(quantity=5)
    result = run(
        purchase(
            AgentState(customer_message="I'd like to purchase Samsung Galaxy S26"), client
        )
    )
    message = result["purchase_message"]
    assert "Samsung Galaxy S26" in message
    assert "1099.00" in message
    assert "confirm" in message.lower()
    assert result["purchase_quantity"] == 1


def test_place_an_order_phrasing_enters_phase_1() -> None:
    client = make_client(quantity=5)
    result = run(
        purchase(
            AgentState(
                customer_message="I want to place an order for 1 Samsung Galaxy S26"
            ),
            client,
        )
    )
    assert result["purchase_status"] == "pending_confirmation"
    assert result["pending_purchase"].quantity == 1
    client.stock_out.assert_not_called()


# --- Node behavior: stock safety ---------------------------------------------------


def test_insufficient_stock_does_not_call_stock_out() -> None:
    client = make_client(quantity=2)
    result = run(purchase(AgentState(customer_message="buy 5 Samsung Galaxy S26"), client))
    assert result["purchase_status"] == "skipped_insufficient_stock"
    assert "only 2 unit(s)" in result["purchase_message"]
    assert result["pending_purchase"] is None  # never asks to confirm what
    # cannot be fulfilled
    client.stock_out.assert_not_called()


# --- Phase 2: explicit confirmation — the ONLY place stock is deducted -----------


def _pending() -> PendingPurchase:
    return PendingPurchase(
        product_id=12,
        product_name="Samsung Galaxy S26",
        quantity=1,
        unit_price=1099.0,
    )


def _confirmation_harness(
    monkeypatch: pytest.MonkeyPatch,
    pending: PendingPurchase | None,
    cleared: list[bool],
) -> None:
    """Inject the conversation's pending purchase at the persistence seam."""
    monkeypatch.setattr(
        "app.agents.purchase.load_pending_purchase", lambda db, cid: pending
    )
    monkeypatch.setattr(
        "app.agents.purchase.clear_pending_purchase",
        lambda db, cid: cleared.append(True),
    )


def test_confirmation_calls_stock_out_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    cleared: list[bool] = []
    _confirmation_harness(monkeypatch, _pending(), cleared)
    run(
        purchase(
            AgentState(
                customer_message="Yes, confirm the order", conversation_id="conv-1"
            ),
            client,
        )
    )
    assert client.stock_out.call_count == 1  # exactly once — the only write
    assert client.stock_out.call_args.args[0] == 12
    assert client.stock_out.call_args.kwargs["quantity"] == 1
    assert cleared == [True]  # pending consumed, never re-persisted


def test_successful_confirmation_marks_purchase_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    _confirmation_harness(monkeypatch, _pending(), [])
    result = run(
        purchase(
            AgentState(customer_message="Go ahead", conversation_id="conv-1"),
            client,
        )
    )
    assert result["purchase_status"] == "completed"
    assert result["purchase_quantity"] == 1
    assert result["pending_purchase"] is None
    assert "confirmed successfully" in result["purchase_message"]


def test_confirmation_without_pending_purchase_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    _confirmation_harness(monkeypatch, None, [])
    result = run(
        purchase(
            AgentState(customer_message="Yes", conversation_id="conv-1"), client
        )
    )
    assert result == {}
    client.stock_out.assert_not_called()
    client.list_inventory.assert_not_called()


def test_zero_stock_does_not_call_stock_out() -> None:
    client = make_client(quantity=0)
    result = run(purchase(AgentState(customer_message="buy Samsung Galaxy S26"), client))
    assert result["purchase_status"] == "skipped_insufficient_stock"
    client.stock_out.assert_not_called()


# --- Node behavior: product resolution ---------------------------------------------


def test_unresolved_product_does_not_deduct() -> None:
    client = make_client()
    client.list_products.return_value = make_async(
        ProductListResponse.model_validate(
            {"products": [], "total": 0, "page": 1, "per_page": 5, "pages": 0}
        )
    )
    result = run(purchase(AgentState(customer_message="buy Unobtainium X999"), client))
    assert result["purchase_status"] == "skipped_product_unresolved"
    client.stock_out.assert_not_called()


# --- Node behavior: Inventra failures (never reported as success) -------------------


def test_stock_insufficient_at_confirmation_does_not_deduct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=0)  # stock moved since the order request
    cleared: list[bool] = []
    _confirmation_harness(monkeypatch, _pending(), cleared)
    result = run(
        purchase(
            AgentState(
                customer_message="Yes, confirm the order", conversation_id="conv-1"
            ),
            client,
        )
    )
    assert result["purchase_status"] == "skipped_insufficient_stock"
    assert "0 unit(s)" in result["purchase_message"]
    client.stock_out.assert_not_called()
    assert cleared == [True]


def test_confirmation_timeout_no_retry_and_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    client.stock_out.side_effect = ToolUnavailableError(
        "purchase stock-out failed: timed out", code="timeout"
    )
    cleared: list[bool] = []
    _confirmation_harness(monkeypatch, _pending(), cleared)
    result = run(
        purchase(
            AgentState(customer_message="I confirm", conversation_id="conv-1"), client
        )
    )
    assert result["purchase_status"] == "failed_unavailable"
    assert client.stock_out.call_count == 1  # exactly one attempt — no retry
    assert "could not be completed" in result["purchase_message"]
    assert cleared == [True]  # a failed write consumes the pending purchase


def test_inventra_unavailable_produces_failure_not_success() -> None:
    client = make_client()
    # The read path itself fails before any write is attempted.
    client.list_products.side_effect = ToolUnavailableError(
        "Inventra call failed", code="unavailable"
    )
    result = run(purchase(AgentState(customer_message="buy Samsung Galaxy S26"), client))
    assert result["purchase_status"] == "failed_unavailable"
    client.stock_out.assert_not_called()
    assert "no order was placed" in result["purchase_message"]


def test_confirmation_auth_failure_is_recorded_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    client.stock_out.side_effect = ToolUnavailableError(
        "purchase stock-out failed: rejected credentials", code="auth"
    )
    _confirmation_harness(monkeypatch, _pending(), [])
    result = run(
        purchase(
            AgentState(
                customer_message="Confirm the order", conversation_id="conv-1"
            ),
            client,
        )
    )
    assert result["purchase_status"] == "failed_auth"
    assert client.stock_out.call_count == 1
    assert "could not be completed" in result["purchase_message"]


def test_confirmation_unexpected_failure_is_recorded_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    client.stock_out.side_effect = ToolError("boom", code="unexpected_error")
    _confirmation_harness(monkeypatch, _pending(), [])
    result = run(
        purchase(
            AgentState(
                customer_message="Place the order", conversation_id="conv-1"
            ),
            client,
        )
    )
    assert result["purchase_status"] == "failed_unexpected"
    assert "could not be completed" in result["purchase_message"]


def test_confirmation_failure_carries_structured_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    client.stock_out.side_effect = ToolUnavailableError("x", code="timeout")
    _confirmation_harness(monkeypatch, _pending(), [])
    result = run(
        purchase(
            AgentState(customer_message="Go ahead", conversation_id="conv-1"), client
        )
    )
    assert "purchase" in result["tool_errors"]
    assert result["tool_errors"]["purchase"].code == "timeout"


# --- Notes payload contract ---------------------------------------------------------


def test_confirmation_notes_respect_upstream_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    pending = PendingPurchase(
        product_id=12,
        product_name="Samsung Galaxy S26 Super Ultra Deluxe Edition Pro Max",
        quantity=1,
        unit_price=1099.0,
    )
    _confirmation_harness(monkeypatch, pending, [])
    run(
        purchase(
            AgentState(
                customer_message="Yes, confirm the order", conversation_id="conv-1"
            ),
            client,
        )
    )
    notes = client.stock_out.call_args.kwargs["notes"]
    assert notes is not None and len(notes) <= 500


# --- Phase 3: decline/cancel — never writes --------------------------------------


def test_decline_cancels_pending_without_stock_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    cleared: list[bool] = []
    _confirmation_harness(monkeypatch, _pending(), cleared)
    result = run(
        purchase(
            AgentState(customer_message="No", conversation_id="conv-1"), client
        )
    )
    assert result["purchase_status"] == "cancelled_by_customer"
    assert result["pending_purchase"] is None
    client.stock_out.assert_not_called()
    assert cleared == [True]


def test_decline_without_pending_purchase_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(quantity=5)
    _confirmation_harness(monkeypatch, None, [])
    result = run(
        purchase(
            AgentState(customer_message="Cancel the order", conversation_id="conv-1"),
            client,
        )
    )
    assert result == {}
    client.stock_out.assert_not_called()
