"""Our own HS256 tokens (core/security.py) after the move to PyJWT."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    UnsealError,
    create_access_token,
    create_api_key_token,
    decode_token,
    seal,
    sign_storage_path,
    unseal,
    verify_storage_path,
)


def _secret() -> str:
    secret = get_settings().secret_key
    assert secret
    return secret


def test_access_token_round_trip() -> None:
    claims = decode_token(create_access_token("user-1", {"sso": True}))
    assert claims["sub"] == "user-1"
    assert claims["sso"] is True
    assert claims["exp"] > claims["iat"]


def test_api_key_token_without_expiry_decodes() -> None:
    token = create_api_key_token(
        key_id="k1", subject="svc-1", organization_id="org-1", expires_at=None
    )
    claims = decode_token(token)
    assert (claims["kid"], claims["service"]) == ("k1", True)
    assert "exp" not in claims


def test_a_token_from_a_replica_with_a_fast_clock_is_accepted() -> None:
    # `iat` is informational; only `exp` gates our own tokens.
    now = int(time.time())
    token = jwt.encode({"sub": "u", "iat": now + 120, "exp": now + 600}, _secret(), "HS256")
    assert decode_token(token)["sub"] == "u"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            lambda: jwt.encode(
                {"sub": "u", "exp": datetime.now(UTC) - timedelta(seconds=1)}, _secret(), "HS256"
            ),
            id="expired",
        ),
        pytest.param(lambda: jwt.encode({"sub": "u"}, "another-secret" * 3, "HS256"), id="foreign"),
        pytest.param(lambda: jwt.encode({"sub": "u"}, _secret(), "HS512"), id="other-alg"),
        pytest.param(lambda: jwt.encode({"sub": "u"}, None, "none"), id="alg-none"),
        pytest.param(lambda: create_access_token("u")[:-2] + "xx", id="tampered"),
    ],
)
def test_bad_tokens_are_refused(token: object) -> None:
    assert callable(token)
    with pytest.raises(ValueError, match="invalid or expired token"):
        decode_token(token())


@pytest.fixture
def rotate(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    """Switch to a new `APP_SECRET_KEY`, with or without the old one as previous."""
    old = _secret()

    def apply(keep_previous: bool) -> None:
        monkeypatch.setenv("APP_SECRET_KEY", "the-rotated-secret-key")
        if keep_previous:
            monkeypatch.setenv("APP_SECRET_KEY_PREVIOUS", old)
        else:
            monkeypatch.delenv("APP_SECRET_KEY_PREVIOUS", raising=False)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@pytest.mark.parametrize("keep_previous", [True, False])
def test_a_rotation_keeps_what_the_previous_key_made_only_while_it_is_set(
    rotate: Callable[[bool], None], keep_previous: bool
) -> None:
    token = create_access_token("u1")
    sealed = seal("JBSWY3DPEHPK3PXP", purpose="totp")
    signature = sign_storage_path("c1", "a.jpg", 2_000_000_000)

    rotate(keep_previous)

    assert verify_storage_path("c1", "a.jpg", 2_000_000_000, signature) is keep_previous
    if keep_previous:
        assert decode_token(token)["sub"] == "u1"
        assert unseal(sealed, purpose="totp") == "JBSWY3DPEHPK3PXP"
    else:
        with pytest.raises(ValueError):
            decode_token(token)
        with pytest.raises(UnsealError):
            unseal(sealed, purpose="totp")
    # Whatever is made now uses the new key alone.
    assert decode_token(create_access_token("u2"))["sub"] == "u2"


def test_an_expired_token_is_not_retried_with_the_previous_key(
    rotate: Callable[[bool], None],
) -> None:
    expired = jwt.encode(
        {"sub": "u1", "exp": datetime.now(UTC) - timedelta(hours=1)}, _secret(), algorithm="HS256"
    )
    rotate(True)
    with pytest.raises(ValueError):
        decode_token(expired)
