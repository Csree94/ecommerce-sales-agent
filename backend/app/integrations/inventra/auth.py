"""Inventra authentication abstraction.

The client delegates credential injection to an ``InventraAuthProvider`` so the
mechanism can be replaced later without touching the client or agent tools.

Verified Inventra reality (September 2026): Inventra exposes only
``OAuth2PasswordBearer`` JWT tokens issued to *users*. There is no
service-to-service auth, API key, or client-credentials flow — so the current
provider is a *static bearer token* configured via the environment
(``INVENTRA_BEARER_TOKEN``). When Inventra gains proper service
authentication, implement a new ``InventraAuthProvider`` (e.g. client
credentials with token caching in Redis) and pass it to ``InventraClient`` —
nothing else changes.

Security notes:
- The token is a ``SecretStr``; it is never logged, repr'd, or embedded in
  exceptions raised from this module.
- ``headers()`` failure raises ``InventraAuthError`` without any secret detail.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from app.integrations.inventra.errors import InventraAuthError


class InventraAuthProvider(Protocol):
    """Supplies authorization headers for every Inventra request.

    Keep this protocol tiny: the client asks for headers, the provider decides
    where the credential comes from and how/when it is refreshed.
    """

    def headers(self) -> dict[str, str]:
        """Return the authorization headers to attach to a request."""
        ...


class StaticBearerTokenProvider:
    """Current provider: a static, pre-issued Inventra JWT.

    This works with Inventra's verified JWT-Bearer auth but is a stopgap — it
    holds a user token, which expires and is not scoped to a service. Replace
    with a service-credential provider as soon as Inventra offers one.
    """

    def __init__(self, token: SecretStr) -> None:
        self._token = token

    def headers(self) -> dict[str, str]:
        token = self._token.get_secret_value()
        if not token:
            # No credential configured: fail loudly but leak nothing.
            raise InventraAuthError("No Inventra credential configured (set INVENTRA_BEARER_TOKEN)")
        return {"Authorization": f"Bearer {token}"}


def build_auth_provider(token: SecretStr) -> InventraAuthProvider:
    """Build the auth provider for the current Inventra mechanism.

    Single seam where the mechanism is chosen; swapping providers later means
    changing only this function.
    """
    return StaticBearerTokenProvider(token)
