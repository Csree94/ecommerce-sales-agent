"""Smoke tests for the backend foundation.

Run from ``backend/``: ``pytest`` (see ``pytest.ini``). These tests import the
full application with placeholder configuration — they verify structure,
config parsing and route wiring, not live external services.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Provide the minimum env required to construct Settings."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_ENV", "local")
    yield


@pytest.fixture()
def client(env: None) -> Iterator[TestClient]:
    """TestClient over the real app factory (lifespan runs)."""
    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["environment"] == "local"


def test_health_ready_degraded_without_infra(client: TestClient) -> None:
    """DB/Redis are unreachable in CI; readiness must degrade, not crash."""
    resp = client.get("/api/v1/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] is False
    assert body["redis"] is False


def test_settings_parse_from_env() -> None:
    from app.config.settings import Settings

    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/testdb",
        GEMINI_MODEL="models/gemini-2.5-pro",
        SHOPIFY_SHOP_URL="my-store.myshopify.com",
    )
    assert settings.gemini_model == "models/gemini-2.5-pro"
    assert settings.shopify_shop_url == "my-store.myshopify.com"
    assert settings.database_url.get_secret_value().startswith("postgresql")


def test_settings_secret_not_in_repr() -> None:
    from app.config.settings import Settings

    settings = Settings(
        _env_file=None,
        DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/testdb",
        SHOPIFY_ACCESS_TOKEN="shpat_secret",
    )
    assert "shpat_secret" not in repr(settings)
    assert "shpat_secret" not in str(settings)


def test_create_db_engine_rejects_non_postgres_url() -> None:
    from app.config.settings import Settings
    from app.db.session import create_db_engine

    settings = Settings(
        _env_file=None,
        DATABASE_URL="mysql://user:pass@localhost:3306/db",
    )
    with pytest.raises(ValueError, match="Unsupported DATABASE_URL backend"):
        create_db_engine(settings)
