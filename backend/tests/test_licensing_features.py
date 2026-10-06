"""Business features, the lapse rules and the trial (LIC-32, LIC-33, LIC-34).

See docs/LICENSING.md ("Editions", "Business features and a lapsed licence",
"Trial") and docs/CONTRACTS.md ("Licence key").
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user, get_effective_license
from app.api.errors import LicenceFeatureError
from app.api.v1 import connectors as connectors_router
from app.api.v1 import license as license_router_module
from app.api.v1 import members as members_router
from app.api.v1 import projects as projects_router
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, ConnectorType, LicenseState
from app.services.licensing import keys as keys_mod
from app.services.licensing import trial
from app.services.licensing.features import (
    BUSINESS_FEATURES,
    Feature,
    has_feature,
    licensed_features,
)
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.license import LicenseStatus
from app.services.licensing.state import EffectiveLicense, KeySource, get_state, resolve

TODAY = date(2026, 10, 2)
KID = "vfeat"
PRIVATE, PUBLIC = generate_keypair()
KEYS = {KID: PUBLIC}


def key(
    tier: str = "business",
    *,
    features: list[str] | None = None,
    expires: date = date(2099, 1, 1),
    hosts: list[str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "v": 1,
        "kid": KID,
        "lic": str(uuid.uuid4()),
        "tier": tier,
        "licensee": "Acme Oy",
        "seats": 10,
        "issued_at": "2026-01-01",
        "expires_at": expires.isoformat(),
        "features": list(BUSINESS_FEATURES) if features is None else features,
    }
    if hosts:
        payload["hosts"] = hosts
    return issue(PRIVATE, payload)


def licence(*keys: str | None, today: date = TODAY) -> EffectiveLicense:
    return resolve(
        [(KeySource.ENV, k) for k in keys] or [(KeySource.ENV, None)],
        today=today,
        public_keys=KEYS,
    )


COMMUNITY = licence(None)
BUSINESS = licence(key())


# --- licensed_features ------------------------------------------------------------


class TestLicensedFeatures:
    def test_a_build_without_vendor_keys_enforces_nothing(self) -> None:
        unenforced = resolve([(KeySource.ENV, None)], today=TODAY, public_keys={})
        assert licensed_features(unenforced) == frozenset(BUSINESS_FEATURES)

    def test_community_has_none(self) -> None:
        assert COMMUNITY.status is LicenseStatus.COMMUNITY
        assert licensed_features(COMMUNITY) == frozenset()

    def test_a_key_unlocks_what_it_lists_and_ignores_unknown_ids(self) -> None:
        partial = licence(key(features=["sso", "quality", "time_travel"]))
        assert licensed_features(partial) == {"sso", "quality"}
        assert has_feature(partial, Feature.SSO)
        assert not has_feature(partial, Feature.SCIM)

    def test_a_team_key_has_no_business_features(self) -> None:
        assert licensed_features(licence(key("team", features=[]))) == frozenset()

    def test_an_expired_key_keeps_them_through_grace_then_loses_them(self) -> None:
        ended = key(expires=TODAY - timedelta(days=5))
        in_grace = licence(ended)
        assert in_grace.status is LicenseStatus.EXPIRED
        assert licensed_features(in_grace) == frozenset(BUSINESS_FEATURES)
        restricted = licence(ended, today=TODAY + timedelta(days=40))
        assert licensed_features(restricted) == frozenset()


# --- resolve: tiers and the trial -----------------------------------------------------


class TestTrialResolution:
    def test_a_running_trial_is_the_trial_edition(self) -> None:
        running = licence(key("trial", expires=TODAY + timedelta(days=10)))
        assert running.status is LicenseStatus.VALID
        assert running.edition == "trial"

    def test_an_ended_trial_alone_is_community_not_expired(self) -> None:
        ended = licence(key("trial", expires=TODAY - timedelta(days=1)))
        assert ended.status is LicenseStatus.COMMUNITY
        assert ended.license is None
        assert ended.edition == "community"

    def test_an_ended_trial_never_shadows_a_paid_key(self) -> None:
        paid = key("team", features=[], expires=TODAY - timedelta(days=3))
        ended_trial = key("trial", expires=TODAY - timedelta(days=1))
        chosen = licence(ended_trial, paid)
        assert chosen.edition == "team"
        assert chosen.status is LicenseStatus.EXPIRED

    def test_a_trial_on_another_host_is_ignored(self) -> None:
        bound = key("trial", expires=TODAY + timedelta(days=10), hosts=["a.example.com"])
        elsewhere = resolve(
            [(KeySource.TRIAL, bound)], today=TODAY, public_keys=KEYS, host="b.example.com"
        )
        assert elsewhere.status is LicenseStatus.COMMUNITY

    def test_the_old_commercial_tier_reads_as_team(self) -> None:
        assert licence(key("commercial", features=[])).edition == "team"


# --- the trial service ---------------------------------------------------------------


@pytest.fixture
def vendor_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(keys_mod.VENDOR_PUBLIC_KEYS, KID, PUBLIC)


@pytest.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[cast(Table, LicenseState.__table__), cast(Table, AuditEvent.__table__)],
        )
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def trial_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "sqlite+aiosqlite://",
        "secret_key": "test-key",
        "license_server_url": "https://licence.example",
        "install_id": "inst-1",
    }
    base.update(overrides)
    return Settings(**base)


def server(answer: httpx.Response, seen: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/trial"
        seen.append(json.loads(request.content))
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def running_trial() -> str:
    return key("trial", expires=datetime.now(UTC).date() + timedelta(days=30))


class TestStartTrial:
    async def test_the_trial_key_is_stored_and_in_force(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        seen: list[dict[str, Any]] = []
        client = server(httpx.Response(200, json={"key": running_trial()}), seen)
        async with maker() as session:
            await trial.start_trial(session, trial_settings(), client, host="annotate.acme.com")
            await session.commit()
            state = await get_state(session)
        assert seen == [{"install_id": "inst-1", "host": "annotate.acme.com"}]
        assert state is not None and state.key_source == KeySource.TRIAL
        assert trial.trial_used(state)

    async def test_loopback_sends_no_host(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        seen: list[dict[str, Any]] = []
        client = server(httpx.Response(200, json={"key": running_trial()}), seen)
        async with maker() as session:
            await trial.start_trial(session, trial_settings(), client, host="localhost")
        assert seen[0]["host"] is None

    @pytest.mark.parametrize(
        "settings",
        [
            trial_settings(license_refresh_enabled=False),
            trial_settings(license_server_url=None),
            trial_settings(install_id=None),
        ],
    )
    async def test_unavailable_when_licence_calls_cannot_run(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        client = server(httpx.Response(200, json={"key": running_trial()}), [])
        async with maker() as session:
            with pytest.raises(trial.TrialUnavailableError):
                await trial.start_trial(session, settings, client, host=None)

    async def test_unavailable_with_a_paid_key_or_after_a_trial(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        client = server(httpx.Response(200, json={"key": running_trial()}), [])
        paid = trial_settings(license_key=key("team", features=[]))
        async with maker() as session:
            with pytest.raises(trial.TrialUnavailableError, match="paid licence"):
                await trial.start_trial(session, paid, client, host=None)
            await trial.start_trial(session, trial_settings(), client, host=None)
            with pytest.raises(trial.TrialUnavailableError, match="already running"):
                await trial.start_trial(session, trial_settings(), client, host=None)

    async def test_the_servers_409_is_unavailable(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        client = server(httpx.Response(409, json={"detail": "already"}), [])
        async with maker() as session:
            with pytest.raises(trial.TrialUnavailableError, match="already had a trial"):
                await trial.start_trial(session, trial_settings(), client, host=None)

    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(500),
            httpx.Response(200, json={"key": "ANN1.not.a-key"}),
            httpx.Response(200, json={"nope": True}),
            httpx.Response(200, content=b"not json"),
        ],
    )
    async def test_a_bad_answer_is_a_service_error_and_stores_nothing(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession], answer: httpx.Response
    ) -> None:
        async with maker() as session:
            with pytest.raises(trial.TrialServiceError):
                await trial.start_trial(session, trial_settings(), server(answer, []), host=None)
            state = await get_state(session)
            assert state is None or not state.key

    async def test_a_paid_key_is_not_accepted_as_a_trial(
        self, vendor_keys: None, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        client = server(httpx.Response(200, json={"key": key("business")}), [])
        async with maker() as session:
            with pytest.raises(trial.TrialServiceError, match="not a usable trial"):
                await trial.start_trial(session, trial_settings(), client, host=None)


# --- routes ---------------------------------------------------------------------------


def admin() -> CurrentUser:
    return CurrentUser(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )


@pytest.fixture
def routes(maker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
    settings = trial_settings(
        oidc_issuer="https://idp.example",
        oidc_client_id="annotide",
        oidc_redirect_uri="http://localhost:8000/api/v1/auth/oidc/callback",
    )
    application = create_app(settings)

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[get_current_user] = admin
    application.dependency_overrides[get_effective_license] = lambda: COMMUNITY
    yield application
    application.dependency_overrides.clear()


def unlock(app: FastAPI) -> None:
    app.dependency_overrides[get_effective_license] = lambda: BUSINESS


def is_feature_refusal(response: httpx.Response) -> bool:
    return response.status_code == 403 and response.json()["type"].endswith("license-feature")


class TestRoutes:
    def test_quality_routes_need_quality(self, routes: FastAPI) -> None:
        client = TestClient(routes, raise_server_exceptions=False)
        path = f"/api/v1/projects/{uuid.uuid4()}/agreement"
        assert is_feature_refusal(client.get(path))
        unlock(routes)
        assert not is_feature_refusal(client.get(path))

    def test_registering_an_ml_platform_needs_ml_platforms(self, routes: FastAPI) -> None:
        client = TestClient(routes, raise_server_exceptions=False)
        assert is_feature_refusal(client.post("/api/v1/ml-platforms", json={}))
        unlock(routes)
        assert not is_feature_refusal(client.post("/api/v1/ml-platforms", json={}))

    def test_minting_a_scim_token_needs_scim(self, routes: FastAPI) -> None:
        client = TestClient(routes, raise_server_exceptions=False)
        assert is_feature_refusal(client.post("/api/v1/scim/token"))

    def test_sso_is_hidden_and_refused_without_sso(self, routes: FastAPI) -> None:
        client = TestClient(routes, raise_server_exceptions=False, follow_redirects=False)
        assert client.get("/api/v1/auth/providers").json()["oidc"] is None
        login = client.get("/api/v1/auth/oidc/login")
        assert login.status_code == 303
        assert login.headers["location"].endswith("/login?error=license_feature")
        callback = client.get("/api/v1/auth/oidc/callback?code=x&state=y")
        assert callback.headers["location"].endswith("/login?error=license_feature")
        unlock(routes)
        assert client.get("/api/v1/auth/providers").json()["oidc"] is not None

    async def test_audit_reads_30_days_without_audit_history(
        self, routes: FastAPI, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        user = admin()
        routes.dependency_overrides[get_current_user] = lambda: user
        now = datetime.now(UTC)
        async with maker() as session:
            for action, age in (("old.event", 45), ("new.event", 2)):
                session.add(
                    AuditEvent(
                        organization_id=user.organization_id,
                        action=action,
                        target_type="test",
                        created_at=now - timedelta(days=age),
                    )
                )
            await session.commit()
        client = TestClient(routes, raise_server_exceptions=False)

        def actions(query: str = "") -> list[str]:
            response = client.get(f"/api/v1/audit{query}")
            assert response.status_code == 200, response.text
            return [item["action"] for item in response.json()["items"]]

        assert actions() == ["new.event"]
        assert actions("?since=2000-01-01T00:00:00") == ["new.event"]
        unlock(routes)
        assert actions() == ["new.event", "old.event"]


class TestRouteHelpers:
    """The checks inside handlers that need a project or a membership first."""

    def test_folder_permissions(self) -> None:
        with pytest.raises(LicenceFeatureError):
            members_router._require_path_permissions(COMMUNITY)
        members_router._require_path_permissions(BUSINESS)

    def test_new_sharepoint_connectors(self) -> None:
        with pytest.raises(LicenceFeatureError):
            connectors_router._require_type_licensed(ConnectorType.SHAREPOINT, COMMUNITY)
        connectors_router._require_type_licensed(ConnectorType.SHAREPOINT, BUSINESS)
        connectors_router._require_type_licensed(ConnectorType.S3, COMMUNITY)

    def test_turning_consensus_or_gold_on_or_changing_them(self) -> None:
        off: dict[str, Any] = {"consensus_annotators": 1, "gold_every": None}
        on: dict[str, Any] = {"consensus_annotators": 2, "gold_every": None}
        gold: dict[str, Any] = {"consensus_annotators": 1, "gold_every": 10}
        for new in (on, gold):
            with pytest.raises(LicenceFeatureError):
                projects_router._licensed_workflow(new, off, COMMUNITY)
            assert projects_router._licensed_workflow(new, off, BUSINESS) == new
        # Leaving it as it is, or turning it off, never needs the licence.
        assert projects_router._licensed_workflow(on, on, COMMUNITY) == on
        assert projects_router._licensed_workflow(off, on, COMMUNITY) == off
        assert projects_router._licensed_workflow(off, {}, COMMUNITY) == off


class TestTrialEndpoint:
    def _client(self, routes: FastAPI, answer: httpx.Response) -> TestClient:
        async def _vendor() -> AsyncIterator[httpx.AsyncClient]:
            yield server(answer, [])

        routes.dependency_overrides[license_router_module._vendor_client] = _vendor
        routes.dependency_overrides[license_router_module._active_users] = lambda: 1
        return TestClient(routes)

    async def test_starts_the_trial_and_audits_it(
        self, vendor_keys: None, routes: FastAPI, maker: async_sessionmaker[AsyncSession]
    ) -> None:
        del routes.dependency_overrides[get_effective_license]
        client = self._client(routes, httpx.Response(200, json={"key": running_trial()}))

        response = client.post("/api/v1/license/trial")

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["tier"], body["source"], body["trial_used"]) == ("trial", "trial", True)
        assert body["features"] == list(BUSINESS_FEATURES)
        async with maker() as session:
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "license.trial")
            )
        assert audit is not None

    @pytest.mark.parametrize(
        ("answer", "status", "kind"),
        [
            (httpx.Response(409), 409, "trial-unavailable"),
            (httpx.Response(500), 503, "trial-service"),
        ],
    )
    def test_refusals_and_failures(
        self, vendor_keys: None, routes: FastAPI, answer: httpx.Response, status: int, kind: str
    ) -> None:
        del routes.dependency_overrides[get_effective_license]
        response = self._client(routes, answer).post("/api/v1/license/trial")
        assert response.status_code == status
        assert response.json()["type"].endswith(kind)


class TestCloudIdentity:
    """Identity credentials are part of the security baseline in every edition (LIC-32)."""

    def test_community_registers_a_model_with_managed_identity(self, routes: FastAPI) -> None:
        client = TestClient(routes, raise_server_exceptions=False)
        body = {
            "name": "detector",
            "task": "detect",
            "endpoint_url": "https://model.example",
            "identity_type": "managed_identity",
            "identity_config": {"scope": "api://model/.default"},
        }
        assert not is_feature_refusal(client.post("/api/v1/models", json=body))

    def test_cloud_identity_is_not_a_business_feature(self) -> None:
        assert "cloud_identity" not in BUSINESS_FEATURES
