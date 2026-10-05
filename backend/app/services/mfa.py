"""TOTP multi-factor authentication for local accounts (AUTH-2).

RFC 6238 with the parameters every authenticator app defaults to: SHA-1,
six digits, 30-second steps. One step of clock drift is accepted either way,
and a step is never accepted twice, so an observed code cannot be replayed.
Single sign-on accounts get MFA from their identity provider instead.

Nothing here commits; the caller does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote, urlencode

from app.core.security import UnsealError, seal, unseal
from app.models import User

ISSUER: Final = "Annotation"
DIGITS: Final = 6
STEP_SECONDS: Final = 30
#: Steps of clock drift accepted either side of now.
DRIFT_STEPS: Final = 1
RECOVERY_CODE_COUNT: Final = 10
SEAL_PURPOSE: Final = "totp"


class MfaError(Exception):
    """The requested MFA change is not possible in the account's current state."""


def generate_secret() -> str:
    """A new 160-bit seed, base32 without padding, as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp(secret: str, step: int) -> str:
    """The code for ``step`` (RFC 4226 HOTP over the time step)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: datetime) -> int:
    return int(now.timestamp()) // STEP_SECONDS


def match_step(secret: str, code: str, *, now: datetime, after: int | None) -> int | None:
    """The time step ``code`` belongs to, if it is current and newer than ``after``."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now_step = current_step(now)
    for step in range(now_step - DRIFT_STEPS, now_step + DRIFT_STEPS + 1):
        if after is not None and step <= after:
            continue
        if hmac.compare_digest(totp(secret, step).encode(), code.encode()):
            return step
    return None


def otpauth_uri(secret: str, email: str) -> str:
    """The URI an authenticator app reads (usually from a QR code)."""
    label = quote(f"{ISSUER}:{email}")
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "digits": DIGITS, "period": STEP_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"


def _hash_code(code: str) -> str:
    # Recovery codes carry 50 random bits; a fast hash is enough, and the
    # table never holds them in the clear.
    normalized = code.strip().lower().replace(" ", "").replace("-", "")
    return hashlib.sha256(normalized.encode()).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    """``(codes to show once, hashes to store)``."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = base64.b32encode(secrets.token_bytes(7)).decode("ascii").lower()[:10]
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash_code(code) for code in codes]


def is_available(user: User) -> bool:
    """MFA here is for people who sign in with a password."""
    return user.password_hash is not None and not user.is_service


def is_enabled(user: User) -> bool:
    return user.totp_enabled_at is not None


def _secret(user: User) -> str:
    if user.totp_secret is None:
        raise MfaError("No authenticator is set up for this account.")
    try:
        return unseal(user.totp_secret, purpose=SEAL_PURPOSE)
    except UnsealError as exc:
        # APP_SECRET_KEY was rotated: only an administrator reset helps.
        raise MfaError(
            "The authenticator for this account can no longer be read; ask an "
            "administrator to reset it."
        ) from exc


def begin_setup(user: User) -> str:
    """Store a fresh, pending seed and return it. Replaces an unconfirmed one."""
    if not is_available(user):
        raise MfaError("This account signs in through single sign-on; use the provider's MFA.")
    if is_enabled(user):
        raise MfaError("MFA is already on for this account. Turn it off first to replace it.")
    secret = generate_secret()
    user.totp_secret = seal(secret, purpose=SEAL_PURPOSE)
    user.totp_last_step = None
    return secret


def enable(user: User, code: str, *, now: datetime | None = None) -> list[str] | None:
    """Confirm the pending seed with a code. Returns the recovery codes, or
    ``None`` for a wrong code."""
    if is_enabled(user) or user.totp_secret is None:
        raise MfaError("There is no authenticator waiting to be confirmed.")
    step = match_step(_secret(user), code, now=now or datetime.now(UTC), after=None)
    if step is None:
        return None
    codes, hashes = new_recovery_codes()
    user.totp_enabled_at = now or datetime.now(UTC)
    user.totp_last_step = step
    user.mfa_recovery_codes = hashes
    return codes


def verify_totp(user: User, code: str, *, now: datetime | None = None) -> bool:
    """Accept a current code once."""
    step = match_step(_secret(user), code, now=now or datetime.now(UTC), after=user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    return True


def verify(user: User, code: str, *, now: datetime | None = None) -> bool:
    """A TOTP code, or an unused recovery code, which is then spent.

    Recovery codes still work when the seed can no longer be read.
    """
    try:
        if verify_totp(user, code, now=now):
            return True
    except MfaError:
        pass
    hashed = _hash_code(code)
    remaining = list(user.mfa_recovery_codes or [])
    for stored in remaining:
        if hmac.compare_digest(stored.encode(), hashed.encode()):
            remaining.remove(stored)
            user.mfa_recovery_codes = remaining
            return True
    return False


def replace_recovery_codes(user: User) -> list[str]:
    codes, hashes = new_recovery_codes()
    user.mfa_recovery_codes = hashes
    return codes


def disable(user: User) -> None:
    user.totp_secret = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.mfa_recovery_codes = None
