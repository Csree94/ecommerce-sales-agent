"""DNS fallback for database connections.

Some networks (corporate VPNs, captive portals, some ISPs) refuse DNS queries
for peer domains like ``neon.tech`` — the app then fails with
``failed to resolve host ... getaddrinfo failed`` while the database itself,
its credentials, and the TLS path are all perfectly reachable (verified by
direct-by-IP connect during the 2026-10 incident on this app).

Strategy, tried in order per connection attempt:

1. **System resolution** — normal ``socket.getaddrinfo`` path.
2. **Last-known-good cache** — a resolved IP remembered in-process (and, when
   writable, in ``.dns_cache.json`` next to the environment config), so a
   temporary outage never escalates while a perfectly good cached address
   exists. IPs are cached with a TTL because cloud targets (Neon included)
   rotate addresses.
3. **DNS-over-HTTPS** — bootstrap resolution via Cloudflare and Google's DoH
   endpoints, using only the ``httpx`` client the app already requires. This
   bypasses the local resolver entirely.
4. **``hostaddr`` injection** — psycopg's supported mechanism for connecting
   ``by IP while still verifying the server certificate against the original
   hostname`` (TLS/SNI unaffected — no security downgrade).

Everything is best-effort: if any fallback path fails, resolution re-raises
the original error so callers see an honest failure instead of a masked one.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

# Public DoH resolvers (RFC 8484 JSON endpoints). Tried in order; the first
# answer wins. These are infrastructure resolvers, not app logic.
_DOH_ENDPOINTS: Final[tuple[str, ...]] = (
    "https://1.1.1.1/dns-query?name={name}&type=A",
    "https://8.8.8.8/resolve?name={name}&type=A",
)

_DOH_TIMEOUT_SECONDS: Final[float] = 4.0
# Cached addresses live for 10 minutes: short enough to respect cloud IP
# rotation, long enough that one DoH failure never clears the cache.
_CACHE_TTL_SECONDS: Final[float] = 600.0
_CACHE_FILENAME: Final[str] = ".dns_cache.json"


class _CacheEntry:
    """One successful resolution (IPv4 preferred, with a decay timestamp)."""

    __slots__ = ("ip", "expires_at")

    def __init__(self, ip: str, expires_at: float) -> None:
        self.ip = ip
        self.expires_at = expires_at


class DnsResolver:
    """Resolve hostnames with system-first, cache, and DoH fallbacks."""

    def __init__(self, cache_ttl_seconds: float = _CACHE_TTL_SECONDS) -> None:
        self._cache_ttl = cache_ttl_seconds
        self._entries: dict[str, _CacheEntry] = {}
        self._loaded = False
        self._cache_path: Path | None = None

    # --- public API -------------------------------------------------------

    def resolve(self, host: str) -> str:
        """Return an IP address for ``host`` or raise the original OS error.

        Deliberately *not* cached-negative: repeated resolution attempts on
        the happy path are a no-op relative to a TCP+TLS handshake.
        """
        # 1) System resolver (the normal, no-cost path).
        try:
            system_ip = self._system_resolve(host)
            self._remember(host, system_ip)
            return system_ip
        except OSError:
            pass

        # 2) Last-known-good (in-memory or on-disk) — never expires silently.
        cached = self._cached_ip(host)
        if cached is not None:
            logger.warning("dns_fallback_cache_hit host=%s ip=%s", host, cached)
            return cached

        # 3) DNS-over-HTTPS (bypasses the local resolver entirely).
        doh_ip = self._doh_resolve(host)
        if doh_ip is not None:
            logger.warning("dns_fallback_doh_hit host=%s ip=%s", host, doh_ip)
            self._remember(host, doh_ip)
            return doh_ip

        # Nothing worked — surface the honest failure.
        msg = f"failed to resolve host {host!r} (system, cache, and DoH all failed)"
        raise OSError(msg)

    # --- system resolver ----------------------------------------------------

    def _system_resolve(self, host: str) -> str:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return str(infos[0][4][0])

    # --- cache ----------------------------------------------------------------

    def _cached_ip(self, host: str) -> str | None:
        entry = self._entries.get(host)
        now = time.monotonic()
        if entry is not None:
            if entry.expires_at < now:
                # Expired: drop it so Flappy-DNS networks re-verify periodically.
                del self._entries[host]
                return None
            return entry.ip
        if not self._loaded:
            self._load_cache()
            entry = self._entries.get(host)
        if entry is None:
            return None
        if entry.expires_at < now:
            del self._entries[host]
            return None
        return entry.ip

    def _remember(self, host: str, ip: str) -> None:
        self._entries[host] = _CacheEntry(ip, time.monotonic() + self._cache_ttl)
        self._persist()

    # --- on-disk cache (survives restarts; failures are non-fatal) ------------

    def _cache_file(self) -> Path | None:
        if self._cache_path is not None:
            return self._cache_path
        # Locate the service root by its env file: the first ancestor directory
        # that contains a ``.env`` (typically ``backend/``). The cache lives
        # beside it so it is restart-scoped like local config, never packaged.
        here = Path(__file__).resolve()
        for parent in here.parents:
            if (parent / "__init__.py").is_file():
                continue  # still inside the package — keep walking up
            if (parent / ".env").is_file():
                self._cache_path = parent / _CACHE_FILENAME
                return self._cache_path
        # No ``backend/.env`` in sight (e.g. packaged installs): next to the
        # module is still better than cwd — always writable-by-owner territory.
        self._cache_path = here.parent / _CACHE_FILENAME
        return self._cache_path

    def _load_cache(self) -> None:
        self._loaded = True
        path = self._cache_file()
        assert path is not None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            loaded = float(data.get("expires_at", 0.0))
            entries = data.get("entries")
            if isinstance(entries, dict):
                remaining = max(0.0, loaded - time.time())
                for host, ip in entries.items():
                    if isinstance(ip, str):
                        self._entries[host] = _CacheEntry(ip, time.monotonic() + remaining)
        except (OSError, ValueError, TypeError):
            return  # Missing or corrupt cache — fall through to system/DoH.

    def _persist(self) -> dict[str, str] | None:
        """Best-effort on-disk persist; returns the snapshot for tests."""
        path = self._cache_file()
        assert path is not None
        snapshot = {host: entry.ip for host, entry in self._entries.items()}
        latest = max((e.expires_at for e in self._entries.values()), default=0.0)
        ttl_left = max(0.0, latest - time.monotonic())
        payload = {"expires_at": time.time() + ttl_left, "entries": snapshot}
        try:
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return snapshot  # Read-only FS etc.—cache still works in-memory.
        return snapshot

    # --- DNS-over-HTTPS -------------------------------------------------------

    def _doh_resolve(self, host: str) -> str | None:
        """Query Cloudflare/Google DoH for an A record. Best-effort by design.

        Any failure of a DoH endpoint (transport, HTTP status, malformed JSON)
        only moves on to the next resolver — never masks the honest resolution
        error that :meth:`resolve` raises when everything failed.
        """
        with httpx.Client(timeout=_DOH_TIMEOUT_SECONDS) as client:
            for endpoint in _DOH_ENDPOINTS:
                try:
                    response = client.get(endpoint.format(name=host))
                    response.raise_for_status()
                    answers = response.json().get("Answer") or []
                    for answer in answers:
                        if answer.get("type") == 1 and answer.get("data"):
                            return str(answer["data"])  # one solid A record
                except Exception:  # noqa: BLE001, S112 — DoH is best-effort
                    logger.debug("doh_endpoint_failed endpoint=%s", endpoint, exc_info=True)
                    continue  # try the next resolver
        return None


# --- psycopg integration ---------------------------------------------------


def extract_db_host(url: str) -> str | None:
    """Return the host from a DB URL (any dialect) without importing psycopg.

    Deliberately dialect-agnostic (stdlib parsing): :mod:`psycopg` cannot even
    be import-time coupled here, because Alembic and tests may hand us sqlite
    or other DSNs that ``psycopg.conninfo`` would reject outright.
    """
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host


def build_conninfo_kwargs(
    url: str, resolver: DnsResolver | None = None
) -> dict[str, Any] | None:
    """Return kwargs to pass to a psycopg connection for ``url``.

    Injects a resolved ``hostaddr`` so connections do not depend on the local
    resolver. TLS verification is untouched (the real ``host`` is retained for
    SNI and certificate checks). Returns ``None`` when the URL has no
    resolvable host (e.g. sqlite) — the caller should treat it as "nothing to
    inject" and connect the ordinary way.
    """
    host = extract_db_host(url)
    if not host:
        return None
    resolver = resolver if resolver is not None else get_shared_resolver()
    ip = resolver.resolve(host)  # OSError propagates to the caller
    if ip == host:  # already an IP literal / unix socket — nothing to add
        return None
    return {"hostaddr": ip}


_shared_resolver: DnsResolver | None = None


def get_shared_resolver() -> DnsResolver:
    """Process-wide resolver (one cache; cheap to inject in tests)."""
    global _shared_resolver
    if _shared_resolver is None:
        _shared_resolver = DnsResolver()
    return _shared_resolver


# Backwards-compatible private alias.
_get_shared_resolver = get_shared_resolver


def set_shared_resolver(resolver: DnsResolver | None) -> None:
    """Replace the shared resolver (used by tests and by callers that want to
    force a specific cache path)."""
    global _shared_resolver
    _shared_resolver = resolver
