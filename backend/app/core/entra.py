"""Microsoft Entra tokens for the backend's own PostgreSQL and Redis (SEC-1).

With `APP_DATABASE_AUTH=entra` / `APP_REDIS_AUTH=entra` the backend signs in
to Azure Database for PostgreSQL and Azure Cache for Redis with its managed
identity instead of a password: user-assigned when
`APP_MANAGED_IDENTITY_CLIENT_ID` is set, otherwise the system-assigned one, or
on AKS the workload identity the webhook names in `AZURE_CLIENT_ID`.

A token lasts between an hour and a day. `azure-identity` caches it and fetches
a new one five minutes before it expires, so asking for a token on every new
connection is cheap and never hands out an expired one. Connections that stay
open longer than their token are handled where they live: the PostgreSQL pool
recycles them (`app.db.session`), Redis connections re-authenticate in place
(`app.core.redis`).

Tokens are never logged.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import jwt

from app.core.config import get_settings

if TYPE_CHECKING:
    from azure.core.credentials import AccessToken
    from azure.core.credentials_async import AsyncTokenCredential

#: Audience of Azure Database for PostgreSQL tokens.
POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
#: Audience of Azure Cache for Redis / Azure Managed Redis tokens.
REDIS_SCOPE = "https://redis.azure.com/.default"


class TokenSource:
    """Tokens for one scope from one credential."""

    def __init__(self, scope: str, credential: AsyncTokenCredential) -> None:
        self.scope = scope
        self._credential = credential

    async def get(self) -> AccessToken:
        """A token with at least a few minutes left (`azure-identity` caches it)."""
        return await self._credential.get_token(self.scope)

    async def password(self) -> str:
        """The token alone: what PostgreSQL and Redis take as the password."""
        return (await self.get()).token


def token_object_id(token: str) -> str:
    """The identity's object id (`oid` claim), Redis's user name for it.

    Read without verifying the signature: the token came from Entra over TLS
    a moment ago, and Redis verifies it anyway.
    """
    claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    oid = claims.get("oid")
    if not isinstance(oid, str) or not oid:
        raise ValueError("the Entra token has no oid claim")
    return oid


_credential: AsyncTokenCredential | None = None
_sources: dict[str, TokenSource] = {}


def token_source(scope: str) -> TokenSource:
    """The process-wide token source for `scope`, on the managed identity.

    The credential is built on first use; it opens no connection until the
    first token is asked for.
    """
    global _credential
    if scope not in _sources:
        if _credential is None:
            from azure.identity.aio import ManagedIdentityCredential

            _credential = ManagedIdentityCredential(
                client_id=get_settings().managed_identity_client_id
            )
        _sources[scope] = TokenSource(scope, _credential)
    return _sources[scope]


def set_credential(credential: AsyncTokenCredential | None) -> None:
    """Install a credential (tests), or `None` to build the real one on next use."""
    global _credential
    _credential = credential
    _sources.clear()


async def aclose_token_sources() -> None:
    """Close the credential's HTTP session at shutdown."""
    global _credential
    credential, _credential = _credential, None
    _sources.clear()
    if credential is not None:
        # Shutdown must not fail because the session was already closed.
        with contextlib.suppress(Exception):
            await credential.close()
