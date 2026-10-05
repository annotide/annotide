"""The ML platform service (API-6): addresses, credentials, lineage, failure mapping.

The MLflow calls themselves go through `gateway_for`, replaced here by
fakes; the gateway was checked against a real MLflow server (`make mlflow`).
"""

from __future__ import annotations

import json
import sys
import types
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from app.models import MlPlatform, MlPlatformKind
from app.services import ml_platforms as mp
from app.services.secrets import SecretResolutionError, clear_cache

AZUREML_URI = (
    "azureml://swedencentral.api.azureml.ms/mlflow/v1.0/subscriptions/sub-1/resourceGroups/rg"
    "/providers/Microsoft.MachineLearningServices/workspaces/ws"
)


def platform(
    kind: MlPlatformKind = MlPlatformKind.MLFLOW,
    *,
    uri: str = "http://mlflow:5000",
    identity: str = "none",
    secret_ref: str | None = None,
    config: dict[str, Any] | None = None,
) -> MlPlatform:
    return MlPlatform(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="p",
        kind=kind,
        tracking_uri=uri,
        identity_type=identity,
        secret_ref=secret_ref,
        config=config or {},
    )


def record(**overrides: Any) -> mp.RunRecord:
    values: dict[str, Any] = {
        "run_id": "r1",
        "experiment_id": "7",
        "name": "train",
        "status": "FINISHED",
        "start_time": 1_700_000_000_000,
        "end_time": 1_700_000_060_000,
        "params": {"epochs": "3"},
        "metrics": {"map50": 0.61},
        "tags": {},
        "datasets": [],
    }
    values.update(overrides)
    return mp.RunRecord(**values)


class TestAddresses:
    def test_azureml_uri_becomes_https(self) -> None:
        assert mp.http_base(MlPlatformKind.AZUREML, AZUREML_URI).startswith(
            "https://swedencentral.api.azureml.ms/mlflow/v1.0/subscriptions/sub-1"
        )
        assert mp.http_base(MlPlatformKind.MLFLOW, "http://m:5000/") == "http://m:5000"

    def test_run_urls_point_at_each_platforms_ui(self) -> None:
        assert mp.run_url(platform(), "1", "abc") == "http://mlflow:5000/#/experiments/1/runs/abc"
        with_ui = platform(config={"ui_url": "http://localhost:5001"})
        assert mp.run_url(with_ui, "1", "abc") == "http://localhost:5001/#/experiments/1/runs/abc"
        databricks = platform(MlPlatformKind.DATABRICKS, uri="https://adb-1.azuredatabricks.net")
        assert mp.run_url(databricks, "1", "abc") == (
            "https://adb-1.azuredatabricks.net/ml/experiments/1/runs/abc"
        )
        azure = mp.run_url(platform(MlPlatformKind.AZUREML, uri=AZUREML_URI), "1", "abc")
        assert azure is not None
        assert azure.startswith("https://ml.azure.com/experiments/id/1/runs/abc?wsid=")
        assert "workspaces/ws" in azure
        assert (
            mp.run_url(platform(MlPlatformKind.AZUREML, uri="https://x.example"), "1", "a") is None
        )

    def test_default_experiment_is_a_workspace_path_on_databricks(self) -> None:
        assert mp.default_experiment(platform(), "Cars") == "annotation/Cars"
        databricks = platform(MlPlatformKind.DATABRICKS, uri="https://adb-1.azuredatabricks.net")
        assert mp.default_experiment(databricks, "Cars") == "/Shared/annotation/Cars"


class TestCredentials:
    async def test_anonymous_and_bearer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await mp.credentials(platform()) == mp.HostCreds(host="http://mlflow:5000")
        monkeypatch.setenv("ML_TOKEN", "tok")
        creds = await mp.credentials(platform(identity="bearer", secret_ref="env:ML_TOKEN"))
        assert creds.token == "tok"
        assert "tok" not in repr(creds)

    async def test_basic_splits_user_and_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ML_BASIC", "alice:pa:ss")
        creds = await mp.credentials(platform(identity="basic", secret_ref="env:ML_BASIC"))
        assert (creds.username, creds.password) == ("alice", "pa:ss")
        monkeypatch.setenv("ML_BASIC", "no-colon")
        clear_cache()
        with pytest.raises(mp.MlPlatformUnavailable, match="user:password"):
            await mp.credentials(platform(identity="basic", secret_ref="env:ML_BASIC"))

    async def test_an_unresolvable_secret_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ML_MISSING", raising=False)
        with pytest.raises(SecretResolutionError):
            await mp.credentials(platform(identity="bearer", secret_ref="env:ML_MISSING"))

    async def test_databricks_service_principal_gets_an_oauth_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DBX_SECRET", "s3cret")
        seen: list[tuple[str, str, str]] = []

        def token(host: str, client_id: str, secret: str) -> str:
            seen.append((host, client_id, secret))
            return "oauth-token"

        monkeypatch.setattr(mp, "_databricks_token", token)
        creds = await mp.credentials(
            platform(
                MlPlatformKind.DATABRICKS,
                uri="https://adb-1.azuredatabricks.net",
                identity="service_principal",
                secret_ref="env:DBX_SECRET",
                config={"client_id": "app-1"},
            )
        )
        assert creds.token == "oauth-token"
        assert seen == [("https://adb-1.azuredatabricks.net", "app-1", "s3cret")]

    async def test_azureml_gets_an_entra_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "_azure_token", lambda platform, secret: "arm-token")
        creds = await mp.credentials(
            platform(MlPlatformKind.AZUREML, uri=AZUREML_URI, identity="managed_identity")
        )
        assert creds.token == "arm-token"
        assert creds.host.startswith("https://swedencentral.api.azureml.ms/")

    async def test_a_token_failure_names_the_type_not_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(platform: MlPlatform, secret: str | None) -> str:
            raise RuntimeError("client_secret=leaked in the message")

        monkeypatch.setattr(mp, "_azure_token", fail)
        with pytest.raises(mp.MlPlatformUnavailable) as caught:
            await mp.credentials(
                platform(MlPlatformKind.AZUREML, uri=AZUREML_URI, identity="managed_identity")
            )
        assert "RuntimeError" in str(caught.value)
        assert "leaked" not in str(caught.value)

    async def test_a_missing_sdk_names_the_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(host: str, client_id: str, secret: str) -> str:
            raise ImportError("no databricks")

        monkeypatch.setenv("DBX_SECRET", "s")
        monkeypatch.setattr(mp, "_databricks_token", missing)
        with pytest.raises(mp.MlPlatformUnavailable, match="`mlflow` extra"):
            await mp.credentials(
                platform(
                    MlPlatformKind.DATABRICKS,
                    uri="https://adb-1.azuredatabricks.net",
                    identity="service_principal",
                    secret_ref="env:DBX_SECRET",
                    config={"client_id": "app-1"},
                )
            )


class TestLineage:
    def test_tags_come_first(self) -> None:
        run = record(
            tags={mp.TAG_SNAPSHOT_ID: "s1", mp.TAG_SNAPSHOT_DIGEST: "d1"},
            datasets=[(mp.SNAPSHOT_SOURCE_TYPE, json.dumps({"snapshot_id": "s2"}))],
        )
        assert mp.lineage_of(run) == ("s1", "d1")

    def test_a_snapshot_dataset_input_is_the_fallback(self) -> None:
        run = record(
            datasets=[
                ("delta_table", "{}"),
                (mp.SNAPSHOT_SOURCE_TYPE, "not json"),
                (mp.SNAPSHOT_SOURCE_TYPE, json.dumps({"snapshot_id": "s2", "digest": "d2"})),
            ]
        )
        assert mp.lineage_of(run) == ("s2", "d2")

    def test_no_claim_is_none(self) -> None:
        assert mp.lineage_of(record()) == (None, None)


class FailingGateway:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def experiment_names(self, limit: int) -> list[str]:
        raise self.exc


class MlflowNotFoundError(Exception):
    error_code = "RESOURCE_DOES_NOT_EXIST"


class TestFailures:
    async def test_platform_errors_become_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mp, "gateway_for", lambda creds: FailingGateway(OSError("refused")))
        with pytest.raises(mp.MlPlatformUnavailable, match="mlflow call failed: refused"):
            await mp.check(platform())

    async def test_missing_resources_become_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mp, "gateway_for", lambda creds: FailingGateway(MlflowNotFoundError("no run r9"))
        )
        with pytest.raises(mp.MlPlatformNotFound, match="no run r9"):
            await mp.check(platform())

    async def test_a_slow_platform_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "CHECK_TIMEOUT_SECONDS", 0.05)

        class Slow:
            def experiment_names(self, limit: int) -> list[str]:
                import time

                time.sleep(0.3)
                return []

        monkeypatch.setattr(mp, "gateway_for", lambda creds: Slow())
        with pytest.raises(mp.MlPlatformUnavailable, match="did not answer"):
            await mp.check(platform())

    async def test_long_messages_are_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mp, "gateway_for", lambda creds: FailingGateway(OSError("x" * 1000)))
        with pytest.raises(mp.MlPlatformUnavailable) as caught:
            await mp.check(platform())
        assert len(str(caught.value)) < 400

    def test_without_the_extra_the_gateway_names_it(self) -> None:
        with (
            patch.dict(sys.modules, {"mlflow.store.tracking.rest_store": None}),
            pytest.raises(mp.MlPlatformUnavailable, match="`mlflow` extra"),
        ):
            mp.MlflowGateway(mp.HostCreds(host="http://mlflow:5000"))


class TestDatabricksJob:
    def test_run_now_with_the_request_as_parameters(self) -> None:
        calls: list[dict[str, Any]] = []

        class Jobs:
            def run_now(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return types.SimpleNamespace(response=types.SimpleNamespace(run_id=991))

        class WorkspaceClient:
            def __init__(self, **kwargs: Any) -> None:
                calls.append(kwargs)
                self.jobs = Jobs()

        fake = types.ModuleType("databricks.sdk")
        fake.WorkspaceClient = WorkspaceClient  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"databricks.sdk": fake}):
            run_id = mp.start_databricks_job(
                mp.HostCreds(host="https://adb-1.azuredatabricks.net", token="t"),
                42,
                {"snapshot_id": "s1"},
            )
        assert run_id == "991"
        assert calls[0]["host"] == "https://adb-1.azuredatabricks.net"
        assert calls[1] == {"job_id": 42, "job_parameters": {"snapshot_id": "s1"}}

    async def test_only_databricks_with_a_job_runs_one(self) -> None:
        with pytest.raises(ValueError, match=r"config\.job_id"):
            await mp.run_retrain_job(platform(), {})

    async def test_returns_the_run_and_its_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBX_PAT", "pat")
        monkeypatch.setattr(mp, "start_databricks_job", lambda creds, job_id, params: "55")
        run_id, url = await mp.run_retrain_job(
            platform(
                MlPlatformKind.DATABRICKS,
                uri="https://adb-1.azuredatabricks.net",
                identity="bearer",
                secret_ref="env:DBX_PAT",
                config={"job_id": 42},
            ),
            {},
        )
        assert (run_id, url) == ("55", "https://adb-1.azuredatabricks.net/jobs/42/runs/55")
