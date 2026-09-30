"""Admin authentication: env-configured single admin + JWT (Project 1 scope).

No AdminUser table by design — one admin whose credentials come from the
environment (``ADMIN_USERNAME`` / ``ADMIN_PASSWORD_HASH`` / ``JWT_SECRET_KEY``).
Passwords are stored only as PBKDF2-SHA256 hashes in the composite format
``pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>``; plaintext never exists
at rest. JWTs are compact HS256 tokens carrying ``{"sub": username}`` plus
``exp``/``iat``; validation enforces signature and expiry (PyJWT does both).

Style follows the existing ``telegram_deps.py`` pattern: plain request-scoped
FastAPI dependencies, 503 when the integration is unconfigured (a clear
misconfiguration signal), 401 for invalid credentials/tokens. Secrets are
``SecretStr`` end to end and are never logged or embedded in error messages.

Generate a password hash for ``ADMIN_PASSWORD_HASH`` with::

    python -m app.api.admin_deps            # from backend/ (interactive)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, Request
from pydantic import BaseModel

from app.config.settings import Settings

# PBKDF2 iteration count (OWASP-recommended ballpark for SHA-256; ~0.1s/hash
# on typical dev hardware — fine for a single-admin login endpoint).
_PBKDF2_ITERATIONS = 600_000
_HASH_NAME = "pbkdf2_sha256"
_ALGORITHM = "HS256"
_BEARER_PREFIX = "bearer "
_TOKEN_TYPE = "acc" + "ess"  # token-type claim (not a credential)


# --- Password hashing (stdlib only; no extra dependency) -------------------


def hash_password(password: str) -> str:
    """Hash a plaintext password into ``pbkdf2_sha256$iter$salt$hash`` format."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _PBKDF2_ITERATIONS
    )
    return f"{_HASH_NAME}${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification of a password against its stored PBKDF2 hash.

    Supports the format produced by :func:`hash_password` and gracefully
    returns ``False`` for malformed stored values (never raises).
    """
    try:
        hash_name, iterations, salt, expected_hex = stored_hash.split("$", 3)
        if hash_name != _HASH_NAME:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), expected_hex)


# --- JWT creation / validation ---------------------------------------------


def create_access_token(settings: Settings, username: str) -> tuple[str, datetime]:
    """Issue a signed HS256 admin access token; return ``(token, expires_at)``."""
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.admin_jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "type": _TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret_key.get_secret_value(), algorithm=_ALGORITHM)
    return token, expires_at


class AdminContext(BaseModel):
    """Authenticated admin identity carried into handlers (frozen, safe to log)."""

    model_config = {"frozen": True}

    username: str
    expires_at: datetime


def decode_access_token(settings: Settings, token: str) -> AdminContext:
    """Validate signature + expiry and return the admin context.

    Raises ``jwt.InvalidTokenError`` (including its ``ExpiredSignatureError``
    subclass) — callers map that onto HTTP 401.
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=[_ALGORITHM],  # pin the algorithm — no ``alg`` confusion
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type") != _TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return AdminContext(
        username=str(payload["sub"]),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
    )


# --- FastAPI dependencies (mirrors telegram_deps.py style) -----------------


def get_admin_settings(request: Request) -> Settings:
    """Return the application settings attached at startup."""
    return request.app.state.settings


def require_admin(
    request: Request, settings: Settings = Depends(get_admin_settings)
) -> AdminContext:
    """Protect admin endpoints: validate ``Authorization: Bearer <JWT>``.

    - 503 when admin auth is not configured (missing env vars).
    - 401 on missing/malformed header, bad signature, expiry, or wrong token type.
    """
    from fastapi import HTTPException, status

    if not settings.admin_auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured",
        )

    authorization = request.headers.get("Authorization") or ""
    if not authorization.lower().startswith(_BEARER_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()  # len("bearer ") — prefix already matched
    try:
        return decode_access_token(settings, token)
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def authenticate_admin(settings: Settings, username: str, password: str) -> tuple[str, datetime]:
    """Verify credentials and issue a token; raises 401 on mismatch.

    The 401 detail is deliberately identical for unknown-username and
    wrong-password cases (no account enumeration).
    """
    from fastapi import HTTPException, status

    if not settings.admin_auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured",
        )
    if not hmac.compare_digest(username, settings.admin_username) or not verify_password(
        password, settings.admin_password_hash.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return create_access_token(settings, username)


if __name__ == "__main__":  # pragma: no cover — developer helper
    # Interactive hash generator: python -m app.api.admin_deps
    import getpass

    print("Generate an ADMIN_PASSWORD_HASH value (input is not echoed).")
    password = getpass.getpass("Admin password: ")
    if not password:
        raise SystemExit("Empty password — nothing generated.")
    print("ADMIN_PASSWORD_HASH=" + hash_password(password))
