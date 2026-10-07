"""Tests for the DNS fallback resolver (app.db.dns_fallback).

Covers the resolution ladder (system → cache → DoH), the persistent on-disk
cache, ``hostaddr`` injection for psycopg URLs, and the guaranteed non-touch
of non-Postgres URLs. No real network: system resolution and DoH are both
patched at the seam.
"""

from __future__ import annotations

import json
import socket
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.db.dns_fallback import DnsResolver, build_conninfo_kwargs, set_shared_resolver


@pytest.fixture(autouse=True)
def _isolated_resolver() -> Any:
    """Keep every test's resolver/cache isolated from the shared one."""
    resolver = DnsResolver(cache_ttl_seconds=60.0)
    resolver._cache_path = None  # tests set tmp paths explicitly
    set_shared_resolver(resolver)
    yield
    set_shared_resolver(None)


def _tmp_resolver(tmp_path: Any) -> DnsResolver:
    resolver = DnsResolver(cache_ttl_seconds=60.0)
    resolver._cache_path = tmp_path / ".dns_cache.json"
    return resolver


# --- Resolution ladder --------------------------------------------------------


def test_system_resolution_success_populates_cache(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.0.0.1", 0))]):
        assert resolver.resolve("db.example.com") == "93.0.0.1"
    # Persisted for restarts.
    assert json.loads((tmp_path / ".dns_cache.json").read_text())["entries"][
        "db.example.com"
    ] == "93.0.0.1"


def test_system_failure_falls_back_to_doh(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    with (
        patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")),
        patch("app.db.dns_fallback.httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = {
            "Answer": [{"type": 1, "data": "93.0.0.2"}]
        }
        assert resolver.resolve("db.example.com") == "93.0.0.2"


def test_doh_tries_second_resolver_when_first_fails(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    with (
        patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")),
        patch("app.db.dns_fallback.httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        ok = MagicMock()
        ok.json.return_value = {"Answer": [{"type": 1, "data": "93.0.0.3"}]}
        bad = MagicMock()
        bad.raise_for_status.side_effect = RuntimeError("boom")  # any failure is skipped
        mock_client.get.side_effect = [bad, ok]
        # First endpoint fails → the resolver ladder falls through to the second.
        assert resolver.resolve("db.example.com") == "93.0.0.3"
        # And with a real transport error too:
        import httpx

        mock_client.get.side_effect = [httpx.ConnectError("x"), ok]
        assert resolver.resolve("db.example.com") == "93.0.0.3"


def test_all_paths_failing_raises_honest_oserror(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    with (
        patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")),
        patch("app.db.dns_fallback.httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("network down")
        with pytest.raises(OSError, match="failed to resolve host"):
            resolver.resolve("db.example.com")


def test_stale_cache_entry_is_not_used(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    resolver._cache_ttl = -1.0  # everything expires immediately
    with (
        patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")),
        patch("app.db.dns_fallback.httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.json.return_value = {
            "Answer": [{"type": 1, "data": "93.0.0.4"}]
        }
        assert resolver.resolve("db.example.com") == "93.0.0.4"


def test_cached_ip_survives_subsequent_system_failure(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.0.0.5", 0))]):
        assert resolver.resolve("db.example.com") == "93.0.0.5"
    # System resolver now broken — cache should carry the connection.
    with patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")):
        assert resolver.resolve("db.example.com") == "93.0.0.5"


def test_cache_survives_process_restart_via_disk(tmp_path: Any) -> None:
    first = _tmp_resolver(tmp_path)
    with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.0.0.6", 0))]):
        first.resolve("db.example.com")

    second = _tmp_resolver(tmp_path)  # fresh process, cold memory
    with patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")):
        assert second.resolve("db.example.com") == "93.0.0.6"


# --- hostaddr injection ---------------------------------------------------------


def test_build_conninfo_kwargs_injects_resolved_hostaddr(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    url = "postgresql+psycopg://user:pass@db.example.com:5432/db"
    with patch.object(socket, "getaddrinfo", return_value=[(0, 0, 0, "", ("93.0.0.7", 0))]):
        kwargs = build_conninfo_kwargs(url, resolver=resolver)
    assert kwargs == {"hostaddr": "93.0.0.7"}


def test_build_conninfo_kwargs_unresolvable_raises(tmp_path: Any) -> None:
    resolver = _tmp_resolver(tmp_path)
    url = "postgresql+psycopg://user:pass@db.example.com:5432/db"
    with (
        patch.object(socket, "getaddrinfo", side_effect=OSError("getaddrinfo failed")),
        patch("app.db.dns_fallback.httpx.Client") as mock_client_cls,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("network down")
        with pytest.raises(OSError):
            build_conninfo_kwargs(url, resolver=resolver)


def test_build_conninfo_kwargs_passthrough_for_non_postgres() -> None:
    # SQLite URLs have no resolvable host — function must return None, not raise.
    assert build_conninfo_kwargs("sqlite:///./local.db") is None
