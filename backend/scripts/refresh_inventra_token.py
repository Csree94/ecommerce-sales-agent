"""Refresh INVENTRA_BEARER_TOKEN in backend/.env via the running Inventra login.

Why this exists: Inventra JWTs expire every JWT_EXPIRATION_MINUTES (currently
24h), and Project 1 stores a static bearer token in ``backend/.env``. During
E2E testing the token expires daily; this helper refreshes it in place.

Security contract:
- Credentials are read interactively (getpass) — never echoed, never stored.
- The fresh token is written ONLY into ``backend/.env`` (in place, single
  line replaced) and is NEVER printed to stdout/stderr or logged.
- Output is limited to success/failure plus the token's own ``exp`` claim
  (a timestamp) so the operator can see when the next refresh is due.

Usage (from ``backend/``):
    .venv/Scripts/python.exe scripts/refresh_inventra_token.py   # Windows
    .venv/bin/python scripts/refresh_inventra_token.py           # POSIX

Stdlib only — works with any Python 3.10+, no venv activation required.
Exit codes: 0 = token refreshed; 1 = failure (credentials, network, .env).
"""

from __future__ import annotations

import base64
import getpass
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BACKEND_DIR / ".env"
ENV_KEY = "INVENTRA_BEARER_TOKEN"

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
LOGIN_TIMEOUT_SECONDS = 10


def _read_env_value(key: str, default: str = "") -> str:
    """Read one value from backend/.env without printing it."""
    if not ENV_PATH.exists():
        return default
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def _login(base_url: str, email: str, password: str) -> str:
    """POST /api/auth/login and return the fresh access token (never printed)."""
    payload = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/auth/login",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=LOGIN_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    token = body.get("access_token")
    if not token or not isinstance(token, str):
        raise RuntimeError("login response did not contain an access_token")
    return token


def _expiry_utc_epoch(token: str) -> int | None:
    """Decode ONLY the token's exp claim (timestamp is safe to display)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return None
    exp = claims.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def _update_env_file(token: str) -> None:
    """Replace only the INVENTRA_BEARER_TOKEN line, preserving everything else.

    Line-based (not regex over the whole text) so each line's exact ending
    (CRLF/LF/none) is preserved and token characters can never be
    misinterpreted as replacement-pattern syntax.
    """
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"{ENV_PATH} not found")
    # open() with newline="" (not Path.read_text/write_text) — works on every
    # Python 3.x and preserves CRLF line endings byte-for-byte.
    with open(ENV_PATH, "r", encoding="utf-8", newline="") as handle:
        raw = handle.read()  # keep CRLF intact
    lines = raw.splitlines(keepends=True)
    key_prefix = f"{ENV_KEY}="
    replaced = False
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if content.split("=", 1)[0].strip() == ENV_KEY:
            ending = line[len(content):]  # preserve original EOL exactly
            lines[index] = key_prefix + token + ending
            replaced = True
            break
    if not replaced:
        eol = "\r\n" if any(l.endswith("\r\n") for l in lines) else "\n"
        lines.append(key_prefix + token + eol)
    tmp_path = ENV_PATH.parent / (ENV_PATH.name + ".tmp-refresh")
    with open(tmp_path, "w", encoding="utf-8", newline="") as handle:
        handle.write("".join(lines))
    tmp_path.replace(ENV_PATH)


def main() -> int:
    base_url = _read_env_value("INVENTRA_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    print(f"Inventra login ({base_url}) — credentials are not echoed or stored.")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    if not email or not password:
        print("FAILED: email and password are required.")
        return 1

    try:
        token = _login(base_url, email, password)
    except urllib.error.HTTPError as exc:
        detail = "invalid credentials" if exc.code in (400, 401) else f"HTTP {exc.code}"
        print(f"FAILED: login rejected ({detail}). Token NOT changed.")
        return 1
    except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
        print(f"FAILED: could not reach Inventra at {base_url} ({exc}). Token NOT changed.")
        return 1

    try:
        _update_env_file(token)
    except (OSError, FileNotFoundError) as exc:
        print(f"FAILED: could not update {ENV_PATH} ({exc}).")
        return 1

    exp = _expiry_utc_epoch(token)
    if exp is None:
        print("OK: INVENTRA_BEARER_TOKEN refreshed in .env (expiry unknown).")
    else:
        remaining = exp - int(time.time())
        if remaining > 0:
            print(
                f"OK: INVENTRA_BEARER_TOKEN refreshed in .env — "
                f"valid ~{remaining // 3600}h {(remaining % 3600) // 60}m (exp epoch {exp})."
            )
        else:
            print("WARNING: server returned an already-expired token; refresh again later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
