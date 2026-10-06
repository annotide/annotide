"""Restricted mode on the annotation and task routes (LIC-5).

The licence in force is pinned by overriding `get_effective_license`, so no
database is needed: the check refuses before any route body runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user, get_effective_license
from app.main import create_app
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.state import EffectiveLicense, KeySource, resolve

EXPIRES = "2026-06-30"
PROJECT = uuid4()
ITEM = uuid4()
ANNOTATION = uuid4()

#: Every route that does annotation work or makes new tasks.
RESTRICTED_ROUTES = [
    f"/api/v1/items/{ITEM}/annotations",
    f"/api/v1/annotations/{ANNOTATION}/submit",
    f"/api/v1/annotations/{ANNOTATION}/review",
    f"/api/v1/projects/{PROJECT}/tasks",
    "/api/v1/tasks/next",
    f"/api/v1/items/{ITEM}/interactive",
    f"/api/v1/items/{ITEM}/ocr",
    f"/api/v1/projects/{PROJECT}/items/bulk",
    f"/api/v1/projects/{PROJECT}/imports",
    f"/api/v1/projects/{PROJECT}/imports/upload",
    f"/api/v1/projects/{PROJECT}/scan",
    f"/api/v1/projects/{PROJECT}/prelabel",
]


def _licence(today: date) -> EffectiveLicense:
    private_key, public_key = generate_keypair()
    key = issue(
        private_key,
        {
            "v": 1,
            "kid": "vrestrict",
            "lic": "55555555-5555-5555-5555-555555555555",
            "tier": "commercial",
            "licensee": "Acme Oy",
            "seats": 5,
            "issued_at": "2025-07-01",
            "expires_at": EXPIRES,
            "features": [],
        },
    )
    return resolve([(KeySource.ENV, key)], today=today, public_keys={"vrestrict": public_key})


@pytest.fixture
def app() -> Iterator[FastAPI]:
    application = create_app()
    application.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", RESTRICTED_ROUTES)
def test_restricted_mode_refuses_annotation_work(
    app: FastAPI, client: TestClient, path: str
) -> None:
    restricted = _licence(date(2026, 7, 31))  # the day after the 30-day grace
    app.dependency_overrides[get_effective_license] = lambda: restricted

    response = client.post(path, json={})

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["type"].endswith(":license-restricted")
    assert "expired on 2026-06-30" in body["detail"]
    assert "reading and export still work" in body["detail"]


@pytest.mark.parametrize("path", RESTRICTED_ROUTES)
def test_grace_period_lets_work_continue(app: FastAPI, client: TestClient, path: str) -> None:
    in_grace = _licence(date(2026, 7, 30))  # the last day of grace
    app.dependency_overrides[get_effective_license] = lambda: in_grace

    response = client.post(path, json={})

    # Whatever the route answers to an empty body, it is not the licence.
    assert "license-restricted" not in response.text


def test_anonymous_callers_learn_nothing_about_the_licence(app: FastAPI) -> None:
    del app.dependency_overrides[get_current_user]
    restricted = _licence(date(2026, 8, 15))
    app.dependency_overrides[get_effective_license] = lambda: restricted

    response = TestClient(app, raise_server_exceptions=False).post("/api/v1/tasks/next", json={})

    assert response.status_code == 401
