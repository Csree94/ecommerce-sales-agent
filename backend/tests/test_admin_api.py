"""Admin dashboard API tests (auth + read endpoints).

No live database (project convention): infrastructure is faked at the exact
seams the routes use — ``get_db_session`` is overridden with a scripted fake
session, and admin credentials/JWT material come from monkeypatched env vars.

Coverage mapping (task requirements):
1. valid admin login                          → test_login_*_success
2. invalid admin credentials                  → test_login_wrong_password / unknown_user
3. missing JWT                                → test_conversations_without_token
4. invalid JWT                                → test_conversations_with_garbage_token
5. expired JWT                                → test_conversations_with_expired_token
6. protected conversations endpoint           → test_conversations_* (401/503/200)
7. protected messages endpoint                → test_messages_requires_auth
8. valid conversations retrieval              → test_conversations_returns_dashboard_rows
9. valid messages retrieval                   → test_messages_returns_chronological_thread
10. pagination                                → test_conversations_pagination / 422 bounds

Plus: password hash round-trip, token round-trip/expiry units, runs endpoint,
404 handling, CORS opt-in, and a no-secrets-in-payload guard.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.api.admin_deps import hash_password, verify_password

TEST_PASSWORD = "correct-horse-battery"
TEST_HASH = hash_password(TEST_PASSWORD)
TEST_SECRET = "unit-test-jwt-secret"


def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a fully-configured admin auth environment."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("JWT_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("ADMIN_JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "")


@contextmanager
def make_client(monkeypatch: pytest.MonkeyPatch):
    """Fresh app + entered TestClient with admin env applied (cache reset).

    The client is entered so lifespan runs and ``app.state.database`` exists —
    dependency resolution reaches ``get_db_session`` before the 401 path, and
    it needs the startup state. No connection is opened unless a query runs.
    """
    from app.config.settings import get_settings
    from app.main import create_app

    admin_env(monkeypatch)
    get_settings.cache_clear()  # env changes must reach the freshly built app
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    with make_client(monkeypatch) as test_client:
        yield test_client


def login(client: TestClient, username: str = "admin", password: str = TEST_PASSWORD):
    return client.post("/api/v1/admin/login", json={"username": username, "password": password})


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Fake session seam --------------------------------------------------------


class FakeResult:
    """Mimics the small Result surface the admin routes consume."""

    def __init__(
        self,
        scalar_one: Any = None,
        scalar_one_or_none: Any = None,
        rows: list[Any] | None = None,
        scalars: list[Any] | None = None,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar_one_or_none = scalar_one_or_none
        self._rows = rows or []
        self._scalars = scalars or []

    def scalar_one(self) -> Any:
        return self._scalar_one

    def scalar_one_or_none(self) -> Any:
        return self._scalar_one_or_none

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> "FakeResult":
        """Mirror SQLAlchemy: the scalars() chain yields the mapped rows."""
        return FakeResult(rows=self._scalars)


class FakeSession:
    """Scripted session: execute() pops queued results; get() answers by model."""

    def __init__(self, results: list[FakeResult], gets: dict[type, Any] | None = None) -> None:
        self._results = list(results)
        self._gets = gets or {}
        self.executed: list[Any] = []

    def execute(self, stmt: Any) -> FakeResult:
        self.executed.append(stmt)
        return self._results.pop(0)

    def get(self, model: type, pk: Any) -> Any:
        return self._gets.get(model)

    def close(self) -> None:
        return None


def override_session(app: Any, session: FakeSession) -> None:
    """Route get_db_session to the fake session for this app instance."""
    from app.api.deps import get_db_session

    app.dependency_overrides[get_db_session] = lambda: session


# --- Password hashing units -----------------------------------------------------


def test_password_hash_roundtrip() -> None:
    stored = hash_password("s3cret!")
    assert stored.startswith("pbkdf2_sha256$")
    assert "s3cret!" not in stored
    assert verify_password("s3cret!", stored)
    assert not verify_password("wrong", stored)


def test_verify_password_rejects_malformed_hash() -> None:
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")


# --- 1/2. Login -----------------------------------------------------------------


def test_login_with_valid_credentials_returns_bearer_token(client: TestClient) -> None:
    resp = login(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    # Token decodes with our secret and carries the admin identity.
    payload = pyjwt.decode(body["access_token"], TEST_SECRET, algorithms=["HS256"])
    assert payload["sub"] == "admin"
    assert payload["exp"] > datetime.now(UTC).timestamp()


def test_login_with_wrong_password_is_401(client: TestClient) -> None:
    resp = login(client, password="definitely-wrong")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


def test_login_with_unknown_username_is_indistinguishable_401(client: TestClient) -> None:
    resp = login(client, username="who-is-this")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"  # no enumeration


def test_login_rejects_missing_fields(client: TestClient) -> None:
    assert client.post("/api/v1/admin/login", json={"username": "admin"}).status_code == 422
    assert client.post("/api/v1/admin/login", json={}).status_code == 422


def test_login_unconfigured_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import Settings
    from app.main import create_app

    # Isolate from developer configuration: build Settings explicitly WITHOUT
    # the .env file (same convention as test_health.py) and clear any real
    # ADMIN_* process env vars, so the unconfigured path is actually exercised.
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    isolated_settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/testdb",
    )
    assert isolated_settings.admin_auth_configured is False
    with TestClient(create_app(isolated_settings)) as bare:
        resp = bare.post("/api/v1/admin/login", json={"username": "a", "password": "b"})
        assert resp.status_code == 503
        assert "not configured" in resp.json()["detail"]


# --- 3/4/5. Token validation on protected endpoints ------------------------------


def test_conversations_without_token_is_401(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/conversations")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_conversations_with_malformed_header_is_401(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/conversations", headers={"Authorization": "Token abc"})
    assert resp.status_code == 401


def test_conversations_with_garbage_token_is_401(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/conversations", headers=auth_header("not-a-jwt"))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid or expired token"


def test_conversations_with_expired_token_is_401(client: TestClient) -> None:
    expired = pyjwt.encode(
        {"sub": "admin", "type": "access", "exp": int(datetime.now(UTC).timestamp()) - 3600},
        TEST_SECRET,
        algorithm="HS256",
    )
    resp = client.get("/api/v1/admin/conversations", headers=auth_header(expired))
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Token has expired"


def test_conversations_with_wrongly_signed_token_is_401(client: TestClient) -> None:
    forged = pyjwt.encode(
        {"sub": "admin", "type": "access", "exp": int(datetime.now(UTC).timestamp()) + 3600},
        "attacker-secret",
        algorithm="HS256",
    )
    resp = client.get("/api/v1/admin/conversations", headers=auth_header(forged))
    assert resp.status_code == 401


# --- Fixtures for authenticated read tests ---------------------------------------


def make_customer(i: int) -> Any:
    """Real model instances (no DB): explicit ids/enums, defaults irrelevant."""
    from app.models import Customer

    return Customer(
        id=uuid.uuid4(),
        telegram_user_id=700_000_000 + i,
        username=f"customer{i}",
        first_name=f"First{i}",
        last_name=None,
        locale="en",
    )


def make_conversation(customer: Any) -> Any:
    from datetime import datetime as dt

    from app.models import Conversation, ConversationChannel, ConversationStatus

    return Conversation(
        id=uuid.uuid4(),
        customer_id=customer.id,
        status=ConversationStatus.OPEN,
        channel=ConversationChannel.TELEGRAM,
        last_message_at=dt.now(UTC),
        created_at=dt.now(UTC) - timedelta(hours=1),
    )


def login_token(client: TestClient) -> dict[str, str]:
    return auth_header(login(client).json()["access_token"])


# --- 6/8. Conversations endpoint ---------------------------------------------------


def test_conversations_returns_dashboard_rows(client: TestClient) -> None:
    customer_a, customer_b = make_customer(1), make_customer(2)
    conv_a, conv_b = make_conversation(customer_a), make_conversation(customer_b)

    session = FakeSession(
        results=[
            FakeResult(scalar_one=2),  # total count
            FakeResult(rows=[(conv_a, customer_a), (conv_b, customer_b)]),  # page rows
            FakeResult(scalar_one_or_none="Do you have laptops?"),  # preview conv_a
            FakeResult(scalar_one_or_none="Show me Samsung phones"),  # preview conv_b
        ]
    )
    override_session(client.app, session)

    resp = client.get("/api/v1/admin/conversations", headers=login_token(client))

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert body["page"] == 1 and body["page_size"] == 25
    first = body["items"][0]
    assert first["status"] == "open"
    assert first["channel"] == "telegram"
    assert first["last_message_preview"] == "Do you have laptops?"
    assert first["customer"]["username"] == "customer1"
    assert first["customer"]["first_name"] == "First1"
    assert first["last_message_at"]
    # Customer data is limited to public profile fields — nothing else leaks.
    assert set(first["customer"]) == {"id", "username", "first_name", "last_name", "locale"}
    assert TEST_SECRET not in resp.text and TEST_PASSWORD not in resp.text


def test_conversations_pagination_params_and_bounds(client: TestClient) -> None:
    customer = make_customer(1)
    conv = make_conversation(customer)
    session = FakeSession(
        results=[
            FakeResult(scalar_one=5),
            FakeResult(rows=[(conv, customer)]),
            FakeResult(scalar_one_or_none="hi"),
        ]
    )
    override_session(client.app, session)

    resp = client.get(
        "/api/v1/admin/conversations?page=2&page_size=1", headers=login_token(client)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 2 and body["page_size"] == 1
    assert len(body["items"]) == 1

    # Out-of-bounds pagination params are rejected by validation.
    assert (
        client.get("/api/v1/admin/conversations?page=0", headers=login_token(client)).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/admin/conversations?page_size=1000", headers=login_token(client)
        ).status_code
        == 422
    )


# --- 7/9. Messages endpoint ---------------------------------------------------------


def test_messages_requires_auth(client: TestClient) -> None:
    resp = client.get(f"/api/v1/admin/conversations/{uuid.uuid4()}/messages")
    assert resp.status_code == 401


def test_messages_returns_chronological_thread(client: TestClient) -> None:
    from datetime import datetime as dt

    from app.models import Conversation, Customer, Message, MessageRole

    customer = make_customer(1)
    conversation = make_conversation(customer)
    base = dt.now(UTC)

    def message(role: Any, content: str, minutes: int) -> Any:
        return Message(
            id=uuid.uuid4(),
            conversation_id=conversation.id,
            role=role,
            content_text=content,
            created_at=base + timedelta(minutes=minutes),
            correlation_id=uuid.uuid4(),
        )

    customer_msg = message(MessageRole.CUSTOMER, "Do you have laptops?", 0)
    agent_msg = message(MessageRole.AGENT, "Yes — we have three laptop models.", 1)
    later_customer_msg = message(MessageRole.CUSTOMER, "Show me Samsung phones", 2)

    session = FakeSession(
        results=[
            FakeResult(scalar_one=3),  # total
            FakeResult(scalars=[customer_msg, agent_msg, later_customer_msg]),  # thread
        ],
        gets={Conversation: conversation, Customer: customer},
    )
    override_session(client.app, session)

    resp = client.get(
        f"/api/v1/admin/conversations/{conversation.id}/messages", headers=login_token(client)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert body["conversation"]["id"] == str(conversation.id)
    assert body["conversation"]["customer"]["username"] == "customer1"
    assert [m["role"] for m in body["messages"]] == ["customer", "agent", "customer"]
    assert [m["content"] for m in body["messages"]] == [
        "Do you have laptops?",
        "Yes — we have three laptop models.",
        "Show me Samsung phones",
    ]
    timestamps = [m["created_at"] for m in body["messages"]]
    assert timestamps == sorted(timestamps)  # chronological


def test_messages_unknown_or_malformed_conversation_is_404(client: TestClient) -> None:
    empty = FakeSession(results=[], gets={})  # session.get → None for unknown id
    override_session(client.app, empty)
    headers = login_token(client)

    unknown = client.get(f"/api/v1/admin/conversations/{uuid.uuid4()}/messages", headers=headers)
    assert unknown.status_code == 404

    malformed = client.get("/api/v1/admin/conversations/not-a-uuid/messages", headers=headers)
    assert malformed.status_code == 404


# --- Optional runs endpoint -----------------------------------------------------------


def test_runs_returns_diagnostics(client: TestClient) -> None:
    from datetime import datetime as dt

    from app.models import AgentRun, AgentRunStatus, Conversation

    customer = make_customer(1)
    conversation = make_conversation(customer)
    run = AgentRun(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        status=AgentRunStatus.SUCCEEDED,
        started_at=dt.now(UTC),
        finished_at=dt.now(UTC),
        model_used="models/gemini-2.5-flash",
        fallback_used=False,
        latency_ms=812,
        tool_calls={"intent": "product_search", "tool_errors": []},
        token_usage={"total_tokens": 59},
        error=None,
    )

    session = FakeSession(
        results=[FakeResult(scalar_one=1), FakeResult(scalars=[run])],
        gets={Conversation: conversation},
    )
    override_session(client.app, session)

    resp = client.get(
        f"/api/v1/admin/conversations/{conversation.id}/runs", headers=login_token(client)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "succeeded"
    assert body["items"][0]["tool_calls"]["intent"] == "product_search"
    assert body["items"][0]["latency_ms"] == 812


# --- CORS (opt-in via env) -------------------------------------------------------------


def test_cors_headers_only_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config.settings import get_settings
    from app.main import create_app

    admin_env(monkeypatch)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173")
    get_settings.cache_clear()
    with TestClient(create_app()) as cors_client:
        resp = cors_client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"

        # A non-allowed origin gets no CORS grant.
        denied = cors_client.get("/api/v1/health", headers={"Origin": "http://evil.example"})
        assert denied.headers.get("access-control-allow-origin") is None
