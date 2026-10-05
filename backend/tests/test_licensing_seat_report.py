"""Seat report for offline true-up (LIC-30): `services/licensing/seat_report.py`
and `GET /license/seat-report`, on in-memory SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user, get_effective_license
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, Organization, User
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.seat_report import (
    active_intervals,
    months,
    period_usage,
    usage_by_month,
)
from app.services.licensing.state import EffectiveLicense, KeySource, resolve


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AuditEvent.__table__),
]


def at(day: str, hour: int = 12) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


# --- Pure pieces -----------------------------------------------------------------


def test_months_are_clipped_to_the_range() -> None:
    assert months(date(2026, 1, 15), date(2026, 3, 10)) == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 10)),
    ]
    assert months(date(2026, 12, 31), date(2027, 1, 1)) == [
        (date(2026, 12, 31), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 1, 1)),
    ]


def test_overlapping_sign_ins_merge_into_one_window() -> None:
    windows = active_intervals([at("2026-03-20"), at("2026-03-01"), at("2026-06-01")])
    assert windows == [
        (at("2026-03-01"), at("2026-03-20") + timedelta(days=30)),
        (at("2026-06-01"), at("2026-07-01")),
    ]


def test_peak_counts_people_at_one_moment_not_across_the_month() -> None:
    anna, ben, cleo = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    windows = {
        anna: active_intervals([at("2026-02-10")]),  # active until 12 Mar 12:00
        ben: active_intervals([at("2026-03-12", hour=12)]),  # starts exactly as Anna ends
        cleo: active_intervals([at("2026-03-20")]),
    }
    usage = period_usage(windows, date(2026, 3, 1), date(2026, 3, 31))
    assert usage.active_users == 3
    assert usage.peak_active_users == 2
    assert usage.peak_at == at("2026-03-20")


def test_empty_period() -> None:
    usage = period_usage({}, date(2026, 3, 1), date(2026, 3, 31))
    assert (usage.active_users, usage.peak_active_users, usage.peak_at) == (0, 0, None)


# --- Database ----------------------------------------------------------------------


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


async def _user_signing_in(
    session: AsyncSession,
    org: Organization,
    email: str,
    *times: datetime,
    is_service: bool = False,
    action: str = "auth.login",
) -> None:
    user = User(
        organization_id=org.id,
        email=email,
        display_name=email.split("@")[0],
        is_service=is_service,
    )
    session.add(user)
    await session.flush()
    for when in times:
        session.add(
            AuditEvent(
                organization_id=org.id,
                actor_id=user.id,
                action=action,
                target_type="user",
                target_id=user.id,
                created_at=when,
            )
        )


@pytest.fixture
async def history(sessionmaker: async_sessionmaker[AsyncSession], org: Organization) -> None:
    async with sessionmaker() as session:
        # Active across the Feb/Mar boundary: counts in both months.
        await _user_signing_in(session, org, "anna@acme.com", at("2026-02-20"))
        await _user_signing_in(session, org, "ben@acme.com", at("2026-03-05"), at("2026-03-25"))
        await _user_signing_in(session, org, "cleo@acme.com", at("2026-03-06"))
        # Neither a service account nor a non-login event is a seat.
        await _user_signing_in(session, org, "bot@acme.com", at("2026-03-06"), is_service=True)
        await _user_signing_in(
            session, org, "dan@acme.com", at("2026-03-06"), action="auth.login_failed"
        )
        # Signed in long before the range: not active in it.
        await _user_signing_in(session, org, "old@acme.com", at("2025-12-01"))
        await session.commit()


@pytest.mark.usefixtures("history")
async def test_usage_by_month_reads_the_sign_in_trail(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        usage = await usage_by_month(session, date(2026, 2, 1), date(2026, 4, 30))

    feb, mar, apr = usage
    assert (feb.active_users, feb.peak_active_users) == (1, 1)
    assert (mar.active_users, mar.peak_active_users) == (3, 3)
    assert mar.peak_at == at("2026-03-06")
    # Cleo is active until 5 April, Ben (second sign-in) until 24 April.
    assert (apr.active_users, apr.peak_active_users) == (2, 2)
    assert apr.peak_at == at("2026-04-01", hour=0)


# --- API -----------------------------------------------------------------------------


def _licence(seats: int, features: tuple[str, ...] = ("seat_report",)) -> EffectiveLicense:
    private, public = generate_keypair()
    key = issue(
        private,
        {
            "v": 1,
            "kid": "vreport",
            "lic": "lic-report",
            "tier": "business",
            "licensee": "Acme Oy",
            "seats": seats,
            "issued_at": "2026-01-01",
            "expires_at": "2099-01-01",
            "features": list(features),
        },
    )
    return resolve([(KeySource.ENV, key)], today=date(2026, 9, 25), public_keys={"vreport": public})


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession], org: Organization) -> Iterator[FastAPI]:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid.uuid4(),
        organization_id=org.id,
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )
    application.dependency_overrides[get_settings] = lambda: Settings(
        database_url="postgresql+asyncpg://t:t@localhost/t",
        secret_key="test-key",
        install_id="acme-prod",
    )
    application.dependency_overrides[get_effective_license] = lambda: _licence(2)
    yield application
    application.dependency_overrides.clear()


@pytest.mark.usefixtures("history")
def test_report_gives_monthly_peaks_and_overage(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/license/seat-report?start=2026-02-15&end=2026-03-31")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["install_id"] == "acme-prod"
    assert body["license_id"] == "lic-report"
    assert (body["seats"], body["seat_limit"]) == (2, 3)
    assert (body["peak_active_users"], body["peak_overage"]) == (3, 1)
    assert [(p["start"], p["end"], p["overage"]) for p in body["periods"]] == [
        ("2026-02-15", "2026-02-28", 0),
        ("2026-03-01", "2026-03-31", 1),
    ]


def test_report_without_a_key_has_no_overage(app: FastAPI) -> None:
    app.dependency_overrides[get_effective_license] = lambda: resolve(
        [(KeySource.ENV, None)], today=date(2026, 9, 25)
    )
    body = TestClient(app).get("/api/v1/license/seat-report").json()

    assert body["tier"] == "community"
    assert body["peak_overage"] is None
    assert len(body["periods"]) in {12, 13}
    assert body["periods"][-1]["end"] == body["end"]


def test_report_needs_the_seat_report_feature(app: FastAPI) -> None:
    app.dependency_overrides[get_effective_license] = lambda: _licence(2, features=())
    response = TestClient(app).get("/api/v1/license/seat-report")

    assert response.status_code == 403
    assert response.json()["type"].endswith("license-feature")


@pytest.mark.parametrize(
    "query", ["start=2026-03-02&end=2026-03-01", "start=2020-01-01&end=2026-01-01"]
)
def test_report_refuses_a_bad_range(app: FastAPI, query: str) -> None:
    response = TestClient(app).get(f"/api/v1/license/seat-report?{query}")
    assert response.status_code == 422


def test_report_requires_a_superuser(app: FastAPI, org: Organization) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid.uuid4(),
        organization_id=org.id,
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    assert TestClient(app).get("/api/v1/license/seat-report").status_code == 403
