"""Tests for the liveness and readiness endpoints and the problem-details shape."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
    register_exception_handlers,
)
from app.api.v1.health import DependencyStatus, _check_database, _check_redis
from app.main import create_app
from app.services.pagination import InvalidCursorError
from app.services.workflow import ItemStatus, Trigger, WorkflowError


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _override(app: FastAPI, dependency: Any, ok: bool) -> None:
    """Point a readiness check at a canned result instead of a real service."""
    status = DependencyStatus(ok=ok, detail=None if ok else "ConnectionError")
    app.dependency_overrides[dependency] = lambda: status


class TestHealth:
    def test_liveness_is_always_ok(self, client: TestClient) -> None:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_liveness_touches_no_dependency(self, client: TestClient) -> None:
        # No overrides are registered, so a dependency call would fail the request.
        assert client.get("/api/v1/health").status_code == 200


class TestReady:
    def test_all_dependencies_up(self, app: FastAPI, client: TestClient) -> None:
        _override(app, _check_database, True)
        _override(app, _check_redis, True)

        response = client.get("/api/v1/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["ok"] is True
        assert body["checks"]["queue"]["ok"] is True

    def test_database_down_returns_503(self, app: FastAPI, client: TestClient) -> None:
        _override(app, _check_database, False)
        _override(app, _check_redis, True)

        response = client.get("/api/v1/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert body["checks"]["database"]["ok"] is False
        assert body["checks"]["queue"]["ok"] is True

    def test_queue_down_returns_503(self, app: FastAPI, client: TestClient) -> None:
        _override(app, _check_database, True)
        _override(app, _check_redis, False)

        assert client.get("/api/v1/ready").status_code == 503


class TestProblemDetails:
    """Every error the API raises must come back in the RFC 9457 envelope."""

    @pytest.fixture
    def error_app(self) -> FastAPI:
        app = FastAPI()
        register_exception_handlers(app)

        @app.get("/not-found")
        async def _not_found() -> None:
            raise NotFoundError("Project 123 does not exist.")

        @app.get("/conflict")
        async def _conflict() -> None:
            raise ConflictError("Already exists.")

        @app.get("/invalid-annotation")
        async def _invalid() -> None:
            raise ValidationFailedError(
                "Two shapes are invalid.", extra={"violations": ["bbox is inverted"]}
            )

        @app.get("/bad-transition")
        async def _transition() -> None:
            raise WorkflowError("cannot approve", source=ItemStatus.NEW, trigger=Trigger.APPROVE)

        @app.get("/bad-cursor")
        async def _cursor() -> None:
            raise InvalidCursorError("cursor is not a valid token")

        return app

    @pytest.fixture
    def error_client(self, error_app: FastAPI) -> TestClient:
        return TestClient(error_app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        ("path", "expected_status", "expected_type"),
        [
            ("/not-found", 404, "urn:annotation:error:not-found"),
            ("/conflict", 409, "urn:annotation:error:conflict"),
            ("/invalid-annotation", 422, "urn:annotation:error:validation-failed"),
            ("/bad-transition", 409, "urn:annotation:error:illegal-transition"),
            ("/bad-cursor", 400, "urn:annotation:error:invalid-cursor"),
        ],
    )
    def test_envelope(
        self, error_client: TestClient, path: str, expected_status: int, expected_type: str
    ) -> None:
        response = error_client.get(path)
        assert response.status_code == expected_status
        assert response.headers["content-type"].startswith("application/problem+json")

        body = response.json()
        assert body["type"] == expected_type
        assert body["status"] == expected_status
        assert isinstance(body["title"], str) and body["title"]
        assert isinstance(body["detail"], str) and body["detail"]

    def test_extra_fields_ride_alongside(self, error_client: TestClient) -> None:
        body = error_client.get("/invalid-annotation").json()
        assert body["violations"] == ["bbox is inverted"]

    def test_workflow_error_reports_the_state(self, error_client: TestClient) -> None:
        body = error_client.get("/bad-transition").json()
        assert body["source_status"] == "new"
        assert body["trigger"] == "approve"


class TestOpenApi:
    def test_schema_is_generated_under_the_v1_prefix(self, client: TestClient) -> None:
        response = client.get("/api/v1/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/v1/health" in paths
        assert "/api/v1/ready" in paths
