"""Tests for seat counting and the sign-in seat check (LIC-23, LIC-24).

`services/licensing/seats.py` against an in-memory SQLite database (`CITEXT`
compiled as `VARCHAR`, as in `test_api_oidc.py`), plus the refusal on
`POST /auth/login`. The OIDC refusal is in `test_api_oidc.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core.security import hash_password
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, LicenseState, Organization, User
from app.services.licensing import license as license_mod
from app.services.licensing import seats
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.state import EffectiveLicense, KeySource, resolve

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
#: Well before `NOW`, so an explicitly-dated owner always sorts first even
#: though SQLite's `CURRENT_TIMESTAMP` default only has second resolution.
EARLY = datetime(2020, 1, 1, tzinfo=UTC)
PASSWORD = "correct horse battery staple"


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AuditEvent.__table__),
    cast(Table, LicenseState.__table__),
]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def org(sessionmaker: async_sessionmaker[AsyncSession]) -> Organization:
    async with sessionmaker() as session:
        organization = Organization(name="Acme", slug="acme")
        session.add(organization)
        await session.commit()
        return organization


class Keys:
    """A vendor key pair and a helper that signs licences with it."""

    def __init__(self) -> None:
        self.private, public = generate_keypair()
        self.public: Mapping[str, bytes] = {"vseat": public}

    def licence(self, seats_: int, *, expires_at: str = "2099-01-01") -> str:
        return issue(
            self.private,
            {
                "v": 1,
                "kid": "vseat",
                "lic": "44444444-4444-4444-4444-444444444444",
                "tier": "commercial",
                "licensee": "Acme Oy",
                "seats": seats_,
                "issued_at": "2020-01-01",
                "expires_at": expires_at,
                "features": [],
            },
        )


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    organization: Organization,
    email: str,
    *,
    last_seen: datetime | None = None,
    is_active: bool = True,
    is_superuser: bool = False,
    is_service: bool = False,
    created_at: datetime | None = None,
) -> User:
    async with sessionmaker() as session:
        user = User(
            organization_id=organization.id,
            email=email,
            display_name=email.split("@")[0],
            password_hash=hash_password(PASSWORD),
            is_active=is_active,
            is_superuser=is_superuser,
            is_service=is_service,
            last_seen_at=last_seen,
        )
        # SQLite's `CURRENT_TIMESTAMP` default only has second resolution, so
        # users created in the same test can tie; set it explicitly wherever
        # creation order (the owner, LIC-36) matters.
        if created_at is not None:
            user.created_at = created_at
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _licence(key: str | None, public_keys: Mapping[str, bytes]) -> EffectiveLicense:
    return resolve([(KeySource.ENV, key)], today=NOW.date(), public_keys=public_keys)


async def _may_sign_in(
    sessionmaker: async_sessionmaker[AsyncSession],
    user: User,
    *,
    license_key: str | None,
    public_keys: Mapping[str, bytes],
) -> bool:
    async with sessionmaker() as session:
        fresh = await session.get(User, user.id)
        assert fresh is not None
        return await seats.may_sign_in(session, fresh, _licence(license_key, public_keys), now=NOW)


# --- seat_limit -----------------------------------------------------------------


class TestSeatLimit:
    def test_no_vendor_keys_means_no_limit(self, keys: Keys) -> None:
        assert seats.seat_limit(_licence(None, {})) is None
        assert seats.seat_limit(_licence(keys.licence(5), {})) is None

    def test_community_and_invalid_keys_allow_three(self, keys: Keys) -> None:
        assert seats.seat_limit(_licence(None, keys.public)) == 3
        assert seats.seat_limit(_licence("garbage", keys.public)) == 3

    @pytest.mark.parametrize(("licensed", "limit"), [(1, 2), (5, 6), (10, 11), (25, 27)])
    def test_seats_plus_ten_percent_at_least_one(
        self, keys: Keys, licensed: int, limit: int
    ) -> None:
        assert seats.seat_limit(_licence(keys.licence(licensed), keys.public)) == limit

    def test_expired_key_keeps_its_seats(self, keys: Keys) -> None:
        # Restricting an expired install is LIC-5's job, not the seat check's.
        key = keys.licence(10, expires_at="2020-06-01")
        assert seats.seat_limit(_licence(key, keys.public)) == 11

    async def test_refusal_message_depends_on_the_licence(
        self, sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
    ) -> None:
        async with sessionmaker() as session:
            message = await seats.refusal_message(session, _licence(None, keys.public))
            assert message == seats.COMMUNITY_REFUSAL
            key = keys.licence(3)
            message = await seats.refusal_message(session, _licence(key, keys.public))
            assert message == seats.SEATS_REFUSAL


# --- active_user_count ------------------------------------------------------------


async def test_active_users_are_recent_active_humans(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization
) -> None:
    recent = NOW - timedelta(days=1)
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=recent)
    await _add_user(sessionmaker, org, "root@acme.com", last_seen=recent, is_superuser=True)
    await _add_user(sessionmaker, org, "old@acme.com", last_seen=NOW - timedelta(days=31))
    await _add_user(sessionmaker, org, "never@acme.com")
    await _add_user(sessionmaker, org, "gone@acme.com", last_seen=recent, is_active=False)
    await _add_user(sessionmaker, org, "bot@acme.com", last_seen=recent, is_service=True)

    async with sessionmaker() as session:
        assert await seats.active_user_count(session, now=NOW) == 2


# --- may_sign_in: Personal (LIC-23) -------------------------------------------------


async def test_without_vendor_keys_nobody_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=NOW)
    ben = await _add_user(sessionmaker, org, "ben@acme.com")

    assert await _may_sign_in(sessionmaker, ben, license_key=None, public_keys={})


async def test_personal_allows_the_first_user(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    anna = await _add_user(sessionmaker, org, "anna@acme.com")

    assert await _may_sign_in(sessionmaker, anna, license_key=None, public_keys=keys.public)


async def test_community_refuses_a_fourth_person(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    for name in ("anna", "ben", "cara"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=2))
    dan = await _add_user(sessionmaker, org, "dan@acme.com")

    assert not await _may_sign_in(sessionmaker, dan, license_key=None, public_keys=keys.public)


async def test_community_gives_non_owner_superusers_no_exemption(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    # The owner is the first superuser created; a second superuser gets no
    # exemption once the three Community seats are taken (LIC-36).
    await _add_user(
        sessionmaker,
        org,
        "owner@acme.com",
        last_seen=NOW - timedelta(days=2),
        is_superuser=True,
        created_at=EARLY,
    )
    for name in ("ben", "cara"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=2))
    root = await _add_user(sessionmaker, org, "root@acme.com", is_superuser=True)

    assert not await _may_sign_in(sessionmaker, root, license_key=None, public_keys=keys.public)


async def test_personal_frees_the_seat_after_the_window(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=NOW - timedelta(days=31))
    ben = await _add_user(sessionmaker, org, "ben@acme.com")

    assert await _may_sign_in(sessionmaker, ben, license_key=None, public_keys=keys.public)


async def test_an_active_user_is_never_refused(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=NOW)
    ben = await _add_user(sessionmaker, org, "ben@acme.com", last_seen=NOW - timedelta(days=3))

    # Two active users in Community (from before enforcement): both keep working.
    assert await _may_sign_in(sessionmaker, ben, license_key=None, public_keys=keys.public)


# --- may_sign_in: owner-only above the Community limit (LIC-36) ---------------------


async def test_owner_only_kicks_in_above_three_active(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(
        sessionmaker,
        org,
        "owner@acme.com",
        last_seen=NOW - timedelta(days=1),
        is_superuser=True,
        created_at=EARLY,
    )
    for name in ("ben", "cara", "dan"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=1))

    async with sessionmaker() as session:
        assert await seats.owner_only(session, _licence(None, keys.public), now=NOW)


async def test_owner_may_sign_in_even_if_not_active(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    owner = await _add_user(
        sessionmaker, org, "owner@acme.com", is_superuser=True, created_at=EARLY
    )
    for name in ("ben", "cara", "dan"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=1))

    assert await _may_sign_in(sessionmaker, owner, license_key=None, public_keys=keys.public)


async def test_another_active_superuser_is_refused_when_owner_only(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(
        sessionmaker,
        org,
        "owner@acme.com",
        last_seen=NOW - timedelta(days=1),
        is_superuser=True,
        created_at=EARLY,
    )
    other_root = await _add_user(
        sessionmaker, org, "root2@acme.com", last_seen=NOW - timedelta(days=1), is_superuser=True
    )
    for name in ("ben", "cara"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=1))

    assert not await _may_sign_in(
        sessionmaker, other_root, license_key=None, public_keys=keys.public
    )


async def test_active_non_owner_is_refused_when_owner_only(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(
        sessionmaker,
        org,
        "owner@acme.com",
        last_seen=NOW - timedelta(days=1),
        is_superuser=True,
        created_at=EARLY,
    )
    ben = await _add_user(sessionmaker, org, "ben@acme.com", last_seen=NOW - timedelta(days=1))
    for name in ("cara", "dan"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=1))

    assert not await _may_sign_in(sessionmaker, ben, license_key=None, public_keys=keys.public)


async def test_refusal_message_is_owner_only_above_the_limit(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(
        sessionmaker,
        org,
        "owner@acme.com",
        last_seen=NOW - timedelta(days=1),
        is_superuser=True,
        created_at=EARLY,
    )
    for name in ("ben", "cara", "dan"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW - timedelta(days=1))

    async with sessionmaker() as session:
        message = await seats.refusal_message(session, _licence(None, keys.public), now=NOW)
    assert message == seats.OWNER_ONLY_REFUSAL


# --- may_sign_in: seats (LIC-24) ------------------------------------------------------


async def test_keyed_install_allows_up_to_the_overage(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    key = keys.licence(2)  # limit 3: two seats plus one of overage
    for name in ("a", "b"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW)
    third = await _add_user(sessionmaker, org, "c@acme.com")
    fourth = await _add_user(sessionmaker, org, "d@acme.com")

    assert await _may_sign_in(sessionmaker, third, license_key=key, public_keys=keys.public)
    async with sessionmaker() as session:
        fresh = await session.get(User, third.id)
        assert fresh is not None
        fresh.last_seen_at = NOW
        await session.commit()
    assert not await _may_sign_in(sessionmaker, fourth, license_key=key, public_keys=keys.public)


async def test_keyed_install_always_lets_a_superuser_in(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    key = keys.licence(1)  # limit 2
    for name in ("a", "b"):
        await _add_user(sessionmaker, org, f"{name}@acme.com", last_seen=NOW)
    root = await _add_user(sessionmaker, org, "root@acme.com", is_superuser=True)

    assert await _may_sign_in(sessionmaker, root, license_key=key, public_keys=keys.public)


async def test_service_accounts_are_never_refused(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, keys: Keys
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=NOW)
    bot = await _add_user(sessionmaker, org, "bot@acme.com", is_service=True)

    assert await _may_sign_in(sessionmaker, bot, license_key=None, public_keys=keys.public)


# --- POST /auth/login ---------------------------------------------------------------


@pytest.fixture
def enforcing(keys: Keys, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give the build a vendor key, so Personal mode's one-user limit applies."""
    monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vseat", keys.public["vseat"])
    yield


@pytest.fixture
def client(sessionmaker: async_sessionmaker[AsyncSession]) -> TestClient:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return TestClient(application, raise_server_exceptions=False)


def _login(client: TestClient, email: str) -> Any:
    return client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})


async def test_login_refuses_a_fourth_person_on_community(
    client: TestClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    org: Organization,
    enforcing: None,
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com")
    await _add_user(sessionmaker, org, "ben@acme.com")
    await _add_user(sessionmaker, org, "cara@acme.com")
    await _add_user(sessionmaker, org, "dan@acme.com")

    assert _login(client, "anna@acme.com").status_code == 200
    assert _login(client, "ben@acme.com").status_code == 200
    assert _login(client, "cara@acme.com").status_code == 200
    response = _login(client, "dan@acme.com")

    assert response.status_code == 403
    body = response.json()
    assert body["type"].endswith(":seat-limit")
    assert body["detail"] == seats.COMMUNITY_REFUSAL
    async with sessionmaker() as session:
        failed = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "auth.login_failed")
        )
        dan = await session.scalar(select(User).where(User.email == "dan@acme.com"))
    assert failed is not None
    assert failed.after == {"reason": "seat_limit"}
    assert dan is not None
    assert dan.last_seen_at is None


async def test_login_lets_the_same_person_back_in(
    client: TestClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    org: Organization,
    enforcing: None,
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com")

    assert _login(client, "anna@acme.com").status_code == 200
    assert _login(client, "anna@acme.com").status_code == 200


async def test_login_wrong_password_never_mentions_seats(
    client: TestClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    org: Organization,
    enforcing: None,
) -> None:
    await _add_user(sessionmaker, org, "anna@acme.com", last_seen=datetime.now(UTC))
    await _add_user(sessionmaker, org, "ben@acme.com")

    response = client.post(
        "/api/v1/auth/login", json={"email": "ben@acme.com", "password": "wrong"}
    )

    assert response.status_code == 401
