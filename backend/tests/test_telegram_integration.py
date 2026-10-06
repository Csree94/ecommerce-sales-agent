"""Tests for the Telegram integration phase (webhook → agent → reply).

No real infrastructure: Bot API calls are mocked at the ``TelegramClient``
boundary (``httpx.MockTransport``/fakes), the agent's ``run_turn`` is patched
at the service seam, and database sessions follow the project's
no-live-DB convention (``MagicMock`` at the ``Database`` boundary).

Background tasks: the webhook fast-acks and schedules processing via
``asyncio.create_task``. Tests capture the scheduled coroutine (coroutines
are loop-agnostic until awaited) and await it synchronously with
``asyncio.run`` — fully deterministic, no cross-loop task juggling.

Coverage:
1. Valid text update → 200 ack → agent turn → reply sent to the chat.
2. Conversation/chat id propagation into the existing agent/persistence.
3. Idempotency: already-processed update_id runs no second agent turn.
4. Invalid/non-text/edited/malformed updates: acked, safely skipped.
5. Telegram API failures (send + client-level typed errors) never crash.
6. Missing/invalid configuration (no token → 503; wrong secret → 401).
7. Secret/token protection (ack body, client errors, state metadata).
8. Schema parsing/normalization of the verified Telegram update subset.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.state import AgentState
from app.main import create_app

# ---------------------------------------------------------------------------
# Fakes at the existing seams
# ---------------------------------------------------------------------------


class FakeTelegram:
    """Records sendMessage calls; can simulate Bot API failures."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_message(self, chat_id: int, text: str) -> None:
        if self._fail:
            raise RuntimeError("telegram send failed")
        self.sent.append((chat_id, text))

    async def aclose(self) -> None:
        return None


class FakeInventra:
    """Minimal InventraClient stand-in used by gather_context."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_categories(self) -> list[Any]:
        self.calls.append("list_categories")

        class _Cat:
            def model_dump(self, mode: str = "python") -> dict[str, Any]:
                return {
                    "id": 3,
                    "name": "Footwear",
                    "description": "Shoes and boots",
                    "created_at": "2026-01-10T09:00:00Z",
                    "updated_at": "2026-01-10T09:00:00Z",
                }

        return [_Cat()]

    async def aclose(self) -> None:
        return None


def make_update(
    *,
    update_id: int = 1001,
    text: str = "what categories do you have",
    chat_id: int = 555_001,
    user_id: int = 42,
) -> dict[str, Any]:
    """A valid private-chat text message update (verified Telegram shape)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "from": {
                "id": user_id,
                "first_name": "Ada",
                "username": "ada_shop",
                "language_code": "en",
            },
            "chat": {"id": chat_id, "type": "private"},
            "date": 1_790_000_000,
            "text": text,
        },
    }


def make_session_result(scalar: Any = None, first: Any = None) -> MagicMock:
    """A MagicMock shaped like a SQLAlchemy Result (service consumes both)."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.first.return_value = first
    return result


def make_database(
    *,
    customer: Any = None,
    conversation: Any = None,
    duplicate_row: Any = None,
) -> tuple[MagicMock, MagicMock]:
    """Database mock whose session.execute answers the service's three queries.

    Order of ``session.execute`` calls in ``process_inbound_message``:
    customer lookup → conversation lookup → duplicate-update check.
    """
    database = MagicMock()
    session = MagicMock()
    session.execute.side_effect = [
        make_session_result(scalar=customer),
        make_session_result(scalar=conversation),
        make_session_result(first=duplicate_row),
    ]
    database.session.return_value = session
    return database, session


def make_conversation() -> MagicMock:
    conversation = MagicMock()
    conversation.id = "conv-uuid-123"
    return conversation


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def telegram_env(monkeypatch: pytest.MonkeyPatch):
    """Env: Telegram configured, LLM keys empty; settings cache kept fresh."""
    from app.config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN-DO-NOT-LEAK")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("FALLBACK_LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def unconfigured_env(monkeypatch: pytest.MonkeyPatch):
    """Env: no Telegram token configured at all."""
    from app.config.settings import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_ENV", "local")
    # Hermetic: explicit EMPTY values (env vars beat env_file values) — delenv
    # alone lets a real local .env leak TELEGRAM_BOT_TOKEN back in, constructing
    # a client and turning the expected 503 into a 200 with a background turn.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("FALLBACK_LLM_API_KEY", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def harness(telegram_env, monkeypatch: pytest.MonkeyPatch):
    """TestClient + captured background coroutine + service-level fakes.

    The webhook schedules processing via ``asyncio.create_task``; the patch
    captures the coroutine so each test can await it deterministically.
    """
    captured: dict[str, Any] = {}

    def fake_create_task(coro: Any) -> MagicMock:
        captured["coro"] = coro
        return MagicMock()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    run_turn_calls: list[dict[str, Any]] = []

    async def fake_run_turn(client_arg: Any, **kwargs: Any) -> AgentState:
        run_turn_calls.append({"client": client_arg, **kwargs})
        return AgentState(draft_response="We offer Footwear!")

    monkeypatch.setattr("app.api.services.telegram_service.run_turn", fake_run_turn)

    app = create_app()
    fake_telegram = FakeTelegram()
    database, session = make_database(conversation=make_conversation())

    with TestClient(app) as test_client:
        test_client.app.state.telegram_client = fake_telegram  # type: ignore[assignment]
        test_client.app.state.inventra_client = FakeInventra()  # type: ignore[assignment]
        test_client.app.state.database = database  # type: ignore[assignment]
        harness_data = {
            "client": test_client,
            "telegram": fake_telegram,
            "database": database,
            "session": session,
            "captured": captured,
            "run_turn_calls": run_turn_calls,
        }
        yield harness_data

    # Auto-drain any captured-but-undrained coroutine (skip-path tests leave
    # none; ack-only tests may leave one) so nothing is 'never awaited'.
    leftover = captured.pop("coro", None)
    if leftover is not None:
        asyncio.run(leftover)


def drain(harness: dict[str, Any]) -> None:
    """Await the captured background coroutine (if any) deterministically."""
    coro = harness["captured"].pop("coro", None)
    if coro is not None:
        asyncio.run(coro)


def post_update(client: TestClient, payload: dict[str, Any], *, secret: str | None = None):
    headers: dict[str, str] = {}
    if secret is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret
    return client.post("/api/v1/telegram/webhook", json=payload, headers=headers)


# ---------------------------------------------------------------------------
# 1-2. Valid message → agent → reply; conversation/chat id propagation
# ---------------------------------------------------------------------------


def test_valid_message_ack_and_reply_sent(harness):
    """Valid text update: 200 ack, agent turn runs, reply goes to the chat."""
    response = post_update(harness["client"], make_update(), secret="test-webhook-secret")

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    drain(harness)

    telegram: FakeTelegram = harness["telegram"]
    assert telegram.sent == [(555_001, "We offer Footwear!")]


def test_message_reaches_existing_agent_run_turn(harness):
    """The webhook path calls the EXISTING agent entrypoint (run_turn)."""
    post_update(harness["client"], make_update(text="show me shoes"), secret="test-webhook-secret")
    drain(harness)

    calls = harness["run_turn_calls"]
    assert len(calls) == 1
    assert calls[0]["customer_message"] == "show me shoes"


def test_conversation_id_propagates_to_agent(harness):
    """The resolved conversation id is passed into the existing agent flow."""
    post_update(harness["client"], make_update(), secret="test-webhook-secret")
    drain(harness)

    calls = harness["run_turn_calls"]
    assert len(calls) == 1
    assert calls[0]["conversation_id"] == "conv-uuid-123"
    assert calls[0]["database"] is harness["database"]


def test_telegram_ids_travel_via_metadata_only(harness):
    """Transport ids go through metadata (persist stamps them); never raw chat."""
    post_update(harness["client"], make_update(update_id=1001), secret="test-webhook-secret")
    drain(harness)

    extra = harness["run_turn_calls"][0]["metadata_extra"]
    assert extra["telegram_update_id"] == 1001
    assert extra["telegram_message_id"] == 1011
    assert "TEST-TOKEN-DO-NOT-LEAK" not in str(extra)


def test_identity_resolution_commits_session(harness):
    """Customer/conversation resolution commits its short-lived session."""
    post_update(harness["client"], make_update(), secret="test-webhook-secret")
    drain(harness)

    session: MagicMock = harness["session"]
    session.commit.assert_called_once()
    session.close.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Idempotency
# ---------------------------------------------------------------------------


def test_duplicate_update_skips_agent_turn(harness, monkeypatch: pytest.MonkeyPatch):
    """A retried update_id (duplicate row found) runs no second agent turn."""
    # Reconfigure: duplicate check finds an existing row.
    database, session = make_database(conversation=make_conversation(), duplicate_row=("existing",))
    harness["client"].app.state.database = database  # type: ignore[assignment]

    run_turn_calls: list[Any] = []

    async def spy_run_turn(*args: Any, **kwargs: Any) -> AgentState:
        run_turn_calls.append(kwargs)
        return AgentState(draft_response="should not happen")

    monkeypatch.setattr("app.api.services.telegram_service.run_turn", spy_run_turn)

    response = post_update(
        harness["client"], make_update(update_id=1001), secret="test-webhook-secret"
    )
    assert response.status_code == 200
    drain(harness)

    session.rollback.assert_called()
    assert run_turn_calls == []
    assert harness["telegram"].sent == []


# ---------------------------------------------------------------------------
# 4. Invalid / non-text updates
# ---------------------------------------------------------------------------


def test_non_text_update_acked_and_skipped(harness):
    """Photo update: acked 200, no background processing, no reply."""
    update = {
        "update_id": 2002,
        "message": {
            "message_id": 21,
            "from": {"id": 42, "first_name": "Ada"},
            "chat": {"id": 555_001, "type": "private"},
            "date": 1_790_000_000,
            "photo": [{"file_id": "abc"}],
        },
    }
    response = post_update(harness["client"], update, secret="test-webhook-secret")

    assert response.status_code == 200
    assert "coro" not in harness["captured"]
    assert harness["telegram"].sent == []


def test_edited_message_is_skipped(harness):
    """edited_message deliveries never trigger agent turns."""
    update = make_update(update_id=3003)
    update["edited_message"] = update.pop("message")
    response = post_update(harness["client"], update, secret="test-webhook-secret")

    assert response.status_code == 200
    assert "coro" not in harness["captured"]


def test_group_message_is_skipped(harness):
    """Non-private chats are out of scope in this phase (audit §H)."""
    update = make_update(update_id=3004)
    update["message"]["chat"]["type"] = "group"
    response = post_update(harness["client"], update, secret="test-webhook-secret")

    assert response.status_code == 200
    assert "coro" not in harness["captured"]


def test_malformed_body_acked(harness):
    """Garbage JSON: acked (no Telegram retry loop), nothing processed."""
    response = harness["client"].post(
        "/api/v1/telegram/webhook",
        content=b"not-json{{",
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret",
        },
    )
    assert response.status_code == 200
    assert "coro" not in harness["captured"]


def test_unparsable_update_acked(harness):
    """A JSON body that is not an update shape: acked, nothing processed."""
    response = post_update(harness["client"], {"foo": "bar"}, secret="test-webhook-secret")
    assert response.status_code == 200
    assert "coro" not in harness["captured"]


# ---------------------------------------------------------------------------
# 5. Telegram API failures
# ---------------------------------------------------------------------------


def test_send_failure_does_not_crash_pipeline(harness):
    """Bot API send failure is swallowed (logged); turn pipeline completes."""
    harness["client"].app.state.telegram_client = FakeTelegram(fail=True)  # type: ignore[assignment]

    response = post_update(harness["client"], make_update(), secret="test-webhook-secret")
    assert response.status_code == 200

    drain(harness)  # must not raise
    assert harness["telegram"].sent == []


def test_bot_api_401_maps_to_typed_auth_error():
    """Client-level: 401 → TelegramAuthError without echoing the token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    from app.integrations.telegram.client import TelegramClient
    from app.integrations.telegram.errors import TelegramAuthError

    client = TelegramClient("123456:TEST-TOKEN-DO-NOT-LEAK", transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramAuthError) as excinfo:
        asyncio.run(client.send_message(1, "hi"))
    assert "TEST-TOKEN-DO-NOT-LEAK" not in str(excinfo.value)


def test_bot_api_500_maps_to_typed_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False, "description": "boom"})

    from app.integrations.telegram.client import TelegramClient
    from app.integrations.telegram.errors import TelegramServerError

    client = TelegramClient("123456:TEST-TOKEN", transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramServerError):
        asyncio.run(client.send_message(1, "hi"))


def test_bot_api_timeout_maps_to_typed_timeout_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    from app.integrations.telegram.client import TelegramClient
    from app.integrations.telegram.errors import TelegramTimeoutError

    client = TelegramClient("123456:TEST-TOKEN", transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramTimeoutError):
        asyncio.run(client.send_message(1, "hi"))


def test_bot_api_not_ok_envelope_maps_to_response_error():
    """HTTP 200 with ``ok: false`` body → TelegramResponseError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    from app.integrations.telegram.client import TelegramClient
    from app.integrations.telegram.errors import TelegramResponseError

    client = TelegramClient("123456:TEST-TOKEN", transport=httpx.MockTransport(handler))
    with pytest.raises(TelegramResponseError):
        asyncio.run(client.send_message(1, "hi"))


def test_send_message_payload_shape():
    """sendMessage posts chat_id/text with preview disabled to the right path."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    from app.integrations.telegram.client import TelegramClient

    client = TelegramClient("123456:TEST-TOKEN", transport=httpx.MockTransport(handler))
    asyncio.run(client.send_message(777, "hello"))

    assert captured["url"].endswith("/bot123456:TEST-TOKEN/sendMessage")
    assert b'"chat_id":777' in captured["body"]
    assert b"hello" in captured["body"]


# ---------------------------------------------------------------------------
# 6. Missing/invalid configuration
# ---------------------------------------------------------------------------


def test_missing_telegram_client_returns_503(unconfigured_env):
    """No TELEGRAM_BOT_TOKEN → telegram_client is None → webhook answers 503."""
    app = create_app()
    with TestClient(app) as test_client:
        response = post_update(test_client, make_update())
    assert response.status_code == 503


def test_missing_webhook_secret_header_rejected(telegram_env):
    """Secret configured but header absent → 401 at the edge."""
    app = create_app()
    with TestClient(app) as test_client:
        response = post_update(test_client, make_update(), secret=None)
    assert response.status_code == 401


def test_wrong_webhook_secret_rejected(telegram_env):
    app = create_app()
    with TestClient(app) as test_client:
        response = post_update(test_client, make_update(), secret="wrong-secret")
    assert response.status_code == 401


def test_correct_webhook_secret_accepted(harness):
    response = post_update(harness["client"], make_update(), secret="test-webhook-secret")
    assert response.status_code == 200


def test_telegram_client_requires_token():
    from app.integrations.telegram.client import TelegramClient
    from app.integrations.telegram.errors import TelegramConfigError

    with pytest.raises(TelegramConfigError):
        TelegramClient("")


# ---------------------------------------------------------------------------
# 7. Secret/token protection
# ---------------------------------------------------------------------------


def test_bot_token_never_appears_in_ack_body(harness):
    response = post_update(harness["client"], make_update(), secret="test-webhook-secret")
    assert "TEST-TOKEN-DO-NOT-LEAK" not in response.text


def test_bot_token_never_appears_in_state_metadata(harness):
    post_update(harness["client"], make_update(), secret="test-webhook-secret")
    drain(harness)

    for call in harness["run_turn_calls"]:
        assert "TEST-TOKEN-DO-NOT-LEAK" not in str(call)


def test_webhook_secret_never_appears_in_agent_inputs(harness):
    post_update(harness["client"], make_update(), secret="test-webhook-secret")
    drain(harness)

    for call in harness["run_turn_calls"]:
        assert "test-webhook-secret" not in str(call)


# ---------------------------------------------------------------------------
# 8. Schema parsing / normalization
# ---------------------------------------------------------------------------


def test_parse_update_rejects_non_dict_payloads():
    from app.integrations.telegram.schemas import parse_update

    assert parse_update(None) is None
    assert parse_update([1, 2, 3]) is None
    assert parse_update("update") is None


def test_extract_inbound_normalizes_valid_update():
    from app.integrations.telegram.schemas import extract_inbound, parse_update

    update = parse_update(make_update(update_id=5005, text="  hello bot  "))
    assert update is not None
    inbound = extract_inbound(update)
    assert inbound is not None
    assert inbound.chat_id == 555_001
    assert inbound.user.id == 42
    assert inbound.text == "hello bot"  # stripped
    assert inbound.update_id == 5005
    assert inbound.message_id == 5015
    assert inbound.sent_at is not None


def test_extract_inbound_skips_messages_without_user():
    from app.integrations.telegram.schemas import extract_inbound, parse_update

    update = parse_update(
        {
            "update_id": 6006,
            "message": {
                "message_id": 1,
                "chat": {"id": -100, "type": "channel"},
                "date": 1_790_000_000,
                "text": "channel post",
            },
        }
    )
    assert update is not None
    assert extract_inbound(update) is None


def test_extract_inbound_skips_empty_text():
    from app.integrations.telegram.schemas import extract_inbound, parse_update

    update = parse_update(
        {
            "update_id": 7007,
            "message": {
                "message_id": 1,
                "from": {"id": 42, "first_name": "Ada"},
                "chat": {"id": 555_001, "type": "private"},
                "date": 1_790_000_000,
                "text": "   ",
            },
        }
    )
    assert update is not None
    assert extract_inbound(update) is None


# ---------------------------------------------------------------------------
# 9. Existing agent behavior intact (regression through the Telegram seam)
# ---------------------------------------------------------------------------


def test_service_uses_injected_client_and_database(harness):
    """The service passes the app's Inventra client into the existing graph."""
    post_update(harness["client"], make_update(), secret="test-webhook-secret")
    drain(harness)

    call = harness["run_turn_calls"][0]
    assert call["client"] is harness["client"].app.state.inventra_client
