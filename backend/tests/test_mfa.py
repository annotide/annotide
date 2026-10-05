"""TOTP MFA (AUTH-2): `services/mfa.py`, the sealing helpers, the login step
and `/auth/mfa`, on in-memory SQLite.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core.security import UnsealError, create_access_token, hash_password, seal, unseal
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, LicenseState, Organization, User
from app.services import mfa

PASSWORD = "correct horse battery staple"
#: RFC 6238 appendix B seed ("12345678901234567890").
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, t)
    for t in (Organization.__table__, User.__table__, AuditEvent.__table__, LicenseState.__table__)
]


# --- TOTP and recovery codes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_rfc_6238_vectors(unix_time: int, code: str) -> None:
    assert mfa.totp(RFC_SECRET, unix_time // 30) == code


def test_codes_allow_one_step_of_drift_and_never_repeat() -> None:
    now = datetime.fromtimestamp(1111111109, tz=UTC)
    step = mfa.current_step(now)
    for offset in (-1, 0, 1):
        code = mfa.totp(RFC_SECRET, step + offset)
        assert mfa.match_step(RFC_SECRET, code, now=now, after=None) == step + offset
    assert mfa.match_step(RFC_SECRET, mfa.totp(RFC_SECRET, step + 2), now=now, after=None) is None
    # Already used: a step at or before `after` is refused.
    assert mfa.match_step(RFC_SECRET, mfa.totp(RFC_SECRET, step), now=now, after=step) is None
    assert mfa.match_step(RFC_SECRET, "12345", now=now, after=None) is None
    assert mfa.match_step(RFC_SECRET, "abcdef", now=now, after=None) is None
    # `str.isdigit()` accepts non-ASCII digits; they must be a miss, not a TypeError.
    assert mfa.match_step(RFC_SECRET, "\u0663" * 6, now=now, after=None) is None


def test_otpauth_uri() -> None:
    uri = mfa.otpauth_uri("ABC", "anna@acme.com")
    assert uri.startswith("otpauth://totp/Annotation%3Aanna%40acme.com?")
    assert "secret=ABC" in uri and "issuer=Annotation" in uri


def test_recovery_codes_are_single_use_and_forgiving() -> None:
    user = User(email="a@acme.com", display_name="A", password_hash="x")
    user.totp_secret = seal(mfa.generate_secret(), purpose="totp")
    codes, hashes = mfa.new_recovery_codes()
    assert len(set(codes)) == mfa.RECOVERY_CODE_COUNT
    user.mfa_recovery_codes = hashes

    assert mfa.verify(user, f"  {codes[0].upper().replace('-', ' ')} ")
    assert not mfa.verify(user, codes[0])
    assert len(user.mfa_recovery_codes) == mfa.RECOVERY_CODE_COUNT - 1


def test_a_recovery_code_works_when_the_seed_cannot_be_read() -> None:
    user = User(email="a@acme.com", display_name="A", password_hash="x")
    user.totp_secret = "not-a-sealed-value"
    codes, user.mfa_recovery_codes = mfa.new_recovery_codes()
    assert not mfa.verify(user, "123456")
    assert mfa.verify(user, codes[3])


def test_sealing_is_bound_to_its_purpose_and_the_secret_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = seal("JBSWY3DPEHPK3PXP", purpose="totp")
    assert sealed != seal("JBSWY3DPEHPK3PXP", purpose="totp")  # fresh nonce
    assert unseal(sealed, purpose="totp") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(UnsealError):
        unseal(sealed, purpose="other")
    with pytest.raises(UnsealError):
        unseal("garbage", purpose="totp")

    from app.core import security

    monkeypatch.setattr(security, "_require_secret_key", lambda: "a-rotated-key")
    with pytest.raises(UnsealError):
        unseal(sealed, purpose="totp")


# --- API ----------------------------------------------------------------------------------


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def client(sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return TestClient(application, raise_server_exceptions=False)


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    org_id: UUID | None = None,
    *,
    superuser: bool = False,
    password: bool = True,
) -> User:
    async with sessionmaker() as session:
        if org_id is None:
            org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
            session.add(org)
            await session.flush()
            org_id = org.id
        user = User(
            organization_id=org_id,
            email=f"{uuid4().hex[:8]}@acme.com",
            display_name="Person",
            password_hash=hash_password(PASSWORD) if password else None,
            idp_subject=None if password else f"sub-{uuid4().hex}",
            is_superuser=superuser,
        )
        session.add(user)
        await session.commit()
        return user


def _auth(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        extra_claims={
            "org": str(user.organization_id),
            "email": user.email,
            "superuser": user.is_superuser,
            "service": False,
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _code(secret: str, offset: int = 0) -> str:
    return mfa.totp(secret, mfa.current_step(datetime.now(UTC)) + offset)


def _login(client: TestClient, user: User, otp: str | None = None) -> Any:
    body: dict[str, str] = {"email": user.email, "password": PASSWORD}
    if otp is not None:
        body["otp"] = otp
    return client.post("/api/v1/auth/login", json=body)


async def _enrol(client: TestClient, user: User) -> tuple[str, list[str]]:
    setup = client.post("/api/v1/auth/mfa/setup", headers=_auth(user))
    assert setup.status_code == 200, setup.text
    secret: str = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    enabled = client.post(
        "/api/v1/auth/mfa/enable", json={"code": _code(secret)}, headers=_auth(user)
    )
    assert enabled.status_code == 200, enabled.text
    return secret, enabled.json()["recovery_codes"]


async def test_enrolment_then_sign_in_asks_for_a_code(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user = await _add_user(sessionmaker)
    assert client.get("/api/v1/auth/mfa", headers=_auth(user)).json() == {
        "enabled": False,
        "pending": False,
        "recovery_codes_left": 0,
        "available": True,
    }
    secret, recovery = await _enrol(client, user)
    assert len(recovery) == 10
    assert client.get("/api/v1/auth/me", headers=_auth(user)).json()["mfa_enabled"] is True

    missing = _login(client, user)
    assert missing.status_code == 401
    assert missing.json()["type"].endswith(":mfa-required")

    # The enrolment code's step is spent; the next one works, once.
    code = _code(secret, 1)
    assert _login(client, user, code).status_code == 200
    replayed = _login(client, user, code)
    assert replayed.status_code == 401
    assert replayed.json()["type"].endswith(":mfa-invalid")

    assert _login(client, user, recovery[0]).status_code == 200
    assert _login(client, user, recovery[0]).status_code == 401
    assert client.get("/api/v1/auth/mfa", headers=_auth(user)).json()["recovery_codes_left"] == 9

    async with sessionmaker() as session:
        actions = list((await session.scalars(select(AuditEvent.action))).all())
        stored = await session.get(User, user.id)
    assert actions.count("auth.mfa_enable") == 1
    assert actions.count("auth.login_failed") == 2
    assert stored is not None and stored.totp_secret is not None
    assert secret not in stored.totp_secret  # sealed, never in the clear


async def test_a_wrong_enrolment_code_keeps_mfa_off(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user = await _add_user(sessionmaker)
    secret = client.post("/api/v1/auth/mfa/setup", headers=_auth(user)).json()["secret"]
    wrong = str((int(_code(secret)) + 1) % 1_000_000).zfill(6)
    response = client.post("/api/v1/auth/mfa/enable", json={"code": wrong}, headers=_auth(user))

    assert response.status_code == 422
    assert client.get("/api/v1/auth/mfa", headers=_auth(user)).json()["pending"] is True
    assert _login(client, user).status_code == 200


async def test_setup_conflicts(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    sso = await _add_user(sessionmaker, password=False)
    assert client.post("/api/v1/auth/mfa/setup", headers=_auth(sso)).status_code == 409
    assert client.get("/api/v1/auth/mfa", headers=_auth(sso)).json()["available"] is False

    user = await _add_user(sessionmaker)
    await _enrol(client, user)
    assert client.post("/api/v1/auth/mfa/setup", headers=_auth(user)).status_code == 409
    nothing_pending = client.post(
        "/api/v1/auth/mfa/enable", json={"code": "123456"}, headers=_auth(user)
    )
    assert nothing_pending.status_code == 409


async def test_recovery_codes_can_be_replaced_with_an_app_code(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user = await _add_user(sessionmaker)
    secret, old = await _enrol(client, user)
    refused = client.post(
        "/api/v1/auth/mfa/recovery-codes", json={"code": old[0]}, headers=_auth(user)
    )
    assert refused.status_code == 422

    new = client.post(
        "/api/v1/auth/mfa/recovery-codes", json={"code": _code(secret, 1)}, headers=_auth(user)
    )
    assert new.status_code == 200
    assert set(new.json()["recovery_codes"]).isdisjoint(old)
    assert _login(client, user, old[1]).status_code == 401


async def test_turning_mfa_off_needs_a_code(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user = await _add_user(sessionmaker)
    _secret, recovery = await _enrol(client, user)

    no_code = client.request("DELETE", "/api/v1/auth/mfa", headers=_auth(user))
    assert no_code.status_code == 422
    off = client.request(
        "DELETE", "/api/v1/auth/mfa", json={"code": recovery[0]}, headers=_auth(user)
    )
    assert off.status_code == 204
    assert _login(client, user).status_code == 200


async def test_an_admin_resets_a_locked_out_user(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    user = await _add_user(sessionmaker)
    admin = await _add_user(sessionmaker, user.organization_id, superuser=True)
    peer = await _add_user(sessionmaker, user.organization_id)
    outsider = await _add_user(sessionmaker, superuser=True)
    await _enrol(client, user)

    path = f"/api/v1/auth/mfa?user_id={user.id}"
    assert client.request("DELETE", path, headers=_auth(peer)).status_code == 403
    assert client.request("DELETE", path, headers=_auth(outsider)).status_code == 404
    assert client.request("DELETE", path, headers=_auth(admin)).status_code == 204
    assert _login(client, user).status_code == 200

    async with sessionmaker() as session:
        event = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.mfa_disable")
        )
    assert event is not None
    assert (event.actor_id, event.target_id) == (admin.id, user.id)


async def test_the_caller_can_turn_notification_email_off_and_on(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`PATCH /auth/me` (API-7): a person's own e-mail switch."""
    user = await _add_user(sessionmaker)
    assert client.get("/api/v1/auth/me", headers=_auth(user)).json()["email_notifications"]

    off = client.patch("/api/v1/auth/me", headers=_auth(user), json={"email_notifications": False})
    assert off.status_code == 200
    assert off.json()["email_notifications"] is False
    assert client.get("/api/v1/auth/me", headers=_auth(user)).json()["email_notifications"] is False

    # An empty body changes nothing.
    assert (
        client.patch("/api/v1/auth/me", headers=_auth(user), json={}).json()["email_notifications"]
        is False
    )
    on = client.patch("/api/v1/auth/me", headers=_auth(user), json={"email_notifications": True})
    assert on.json()["email_notifications"] is True


async def test_admins_without_mfa_may_only_set_it_up_when_required(
    client: TestClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import get_settings

    monkeypatch.setenv("APP_MFA_REQUIRED_FOR_ADMINS", "true")
    get_settings.cache_clear()
    try:
        admin = await _add_user(sessionmaker, superuser=True)
        person = await _add_user(sessionmaker, admin.organization_id)

        # Ordinary users are not affected.
        assert _login(client, person).json()["mfa_setup_required"] is False

        login = _login(client, admin)
        assert login.status_code == 200
        assert login.json()["mfa_setup_required"] is True
        limited = {"Authorization": f"Bearer {login.json()['access_token']}"}
        refused = client.get("/api/v1/projects", headers=limited)
        assert refused.status_code == 403
        assert refused.json()["type"].endswith("mfa-setup-required")
        assert client.get("/api/v1/auth/me", headers=limited).status_code == 200

        setup = client.post("/api/v1/auth/mfa/setup", headers=limited)
        assert setup.status_code == 200, setup.text
        secret = setup.json()["secret"]
        enabled = client.post(
            "/api/v1/auth/mfa/enable", json={"code": _code(secret)}, headers=limited
        )
        assert enabled.status_code == 200, enabled.text

        # Signed in again with a code: a full token.
        again = _login(client, admin, otp=_code(secret, 1))
        assert again.status_code == 200, again.text
        assert again.json()["mfa_setup_required"] is False
        full = {"Authorization": f"Bearer {again.json()['access_token']}"}
        # Outside the set-up allowlist (this file's database has only users).
        assert client.patch("/api/v1/auth/me", json={}, headers=limited).status_code == 403
        assert client.patch("/api/v1/auth/me", json={}, headers=full).status_code == 200

        # And the administrator cannot turn it off for themself.
        off = client.request(
            "DELETE", "/api/v1/auth/mfa", json={"code": _code(secret, -1)}, headers=full
        )
        assert off.status_code == 403
    finally:
        get_settings.cache_clear()
