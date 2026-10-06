"""Password hashing (argon2, AUTH-2), JWT helpers (PyJWT, HS256),
signatures for the local media proxy (AUTH-6) and sealing small secrets at
rest (the MFA seed, AUTH-2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from jwt.types import Options

from app.core.config import get_settings

ALGORITHM = "HS256"
#: Our own tokens are checked on `exp` alone, as before PyJWT: `iat` is
#: informational, and rejecting an `iat` a second ahead would break a token
#: minted by a replica whose clock runs slightly fast.
OWN_TOKEN_OPTIONS: Options = {"verify_iat": False}

_password_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password with argon2id."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an argon2 hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _require_secret_key() -> str:
    secret_key = get_settings().secret_key
    if not secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to create or decode access tokens")
    return secret_key


def verification_keys() -> list[str]:
    """Keys a signature or sealed value may have been made with: the current
    `APP_SECRET_KEY`, then `APP_SECRET_KEY_PREVIOUS` during a rotation.
    Signing and sealing always use the current one."""
    previous = get_settings().secret_key_previous
    current = _require_secret_key()
    return [current, previous] if previous and previous != current else [current]


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    """Create a signed JWT access token for ``subject``, honouring ``APP_ACCESS_TOKEN_TTL``."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.access_token_ttl)
    claims: dict[str, Any] = {"sub": subject, "iat": now, "exp": expires_at}
    if extra_claims:
        claims.update(extra_claims)
    encoded: str = jwt.encode(claims, _require_secret_key(), algorithm=ALGORITHM)
    return encoded


def create_api_key_token(
    *, key_id: str, subject: str, organization_id: str, expires_at: datetime | None
) -> str:
    """Create the bearer token for an API key (AUTH-4).

    Unlike :func:`create_access_token` it ignores ``APP_ACCESS_TOKEN_TTL``: the
    key lives until its own ``expires_at`` (or forever) and is revoked through
    its ``kid`` row, which the request path looks up every time.

    Legacy: keys issued before migration 0032 are these JWTs and still work,
    but new keys are opaque ``ant_…`` tokens (``services.api_keys``) that do
    not depend on ``APP_SECRET_KEY`` at all.
    """
    claims: dict[str, Any] = {
        "sub": subject,
        "org": organization_id,
        "kid": key_id,
        "service": True,
        "iat": datetime.now(UTC),
    }
    if expires_at is not None:
        claims["exp"] = expires_at
    encoded: str = jwt.encode(claims, _require_secret_key(), algorithm=ALGORITHM)
    return encoded


def decode_token(token: str) -> dict[str, Any]:
    """Decode and verify a JWT access token, returning its claims.

    Tries each of :func:`verification_keys`; only a bad signature moves on to
    the next key — an expired or malformed token fails at once.
    """
    for key in verification_keys():
        try:
            claims: dict[str, Any] = jwt.decode(
                token, key, algorithms=[ALGORITHM], options=OWN_TOKEN_OPTIONS
            )
        except jwt.InvalidSignatureError:
            continue
        except jwt.PyJWTError as exc:
            raise ValueError("invalid or expired token") from exc
        return claims
    raise ValueError("invalid or expired token")


# --- Local media proxy signatures (AUTH-6, SEC-4) ----------------------------
#
# Local disk has no native signed-URL mechanism, so the API proxies it. The
# proxy is fetched by an `<img>` tag, which carries no Authorization header and
# no cookies, so the signature in the query string *is* the authorisation:
# it binds one connector, one object path and one expiry to the server's key.

_STORAGE_SIGNATURE_VERSION = "v1"


def sign_storage_path(connector_id: str, path: str, expires: int, *, write: bool = False) -> str:
    """Sign one object path for the local media proxy.

    `expires` is an absolute Unix timestamp; the caller enforces it. The
    version prefix keeps old signatures from surviving a format change, and
    the `w` scope marker keeps a read signature from ever authorising a
    `PUT` (and vice versa): the two routes verify with different scopes.
    """
    return _storage_signature(_require_secret_key(), connector_id, path, expires, write=write)


def _storage_signature(key: str, connector_id: str, path: str, expires: int, *, write: bool) -> str:
    scope = "w:" if write else ""
    message = f"{_STORAGE_SIGNATURE_VERSION}:{scope}{connector_id}:{path}:{expires}".encode()
    digest = hmac.new(key.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def verify_storage_path(
    connector_id: str, path: str, expires: int, signature: str, *, write: bool = False
) -> bool:
    """Constant-time check of a media-proxy signature. Never raises.

    A missing `APP_SECRET_KEY` cannot verify anything, so it fails closed
    rather than propagating a 500 out of a public media route.
    """
    try:
        keys = verification_keys()
    except RuntimeError:
        return False
    return any(
        hmac.compare_digest(
            _storage_signature(key, connector_id, path, expires, write=write).encode(),
            signature.encode(),
        )
        for key in keys
    )


class UnsealError(ValueError):
    """A sealed value cannot be opened: tampered, or `APP_SECRET_KEY` changed."""


def _sealing_key(purpose: str, secret: str | None = None) -> bytes:
    # One key per purpose, so a value sealed for one use never opens for another.
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=f"seal:{purpose}".encode()
    ).derive((secret or _require_secret_key()).encode())


def seal(plaintext: str, *, purpose: str) -> str:
    """AES-GCM encrypt ``plaintext`` under a key derived from `APP_SECRET_KEY`."""
    nonce = os.urandom(12)
    sealed = AESGCM(_sealing_key(purpose)).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def unseal(token: str, *, purpose: str) -> str:
    """Open a value from :func:`seal`, under any of :func:`verification_keys`.

    Raises :class:`UnsealError` when no key opens it.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except ValueError as exc:
        raise UnsealError("The sealed value cannot be opened.") from exc
    for secret in verification_keys():
        try:
            return AESGCM(_sealing_key(purpose, secret)).decrypt(raw[:12], raw[12:], None).decode()
        except (InvalidTag, ValueError):
            continue
    raise UnsealError("The sealed value cannot be opened.")
