"""MLflow, Databricks and Azure ML integration (API-6).

All three speak MLflow's REST API, so one client serves them: the tracking
and registry `RestStore`s of `mlflow-skinny` (backend extra `mlflow`),
imported lazily. The stores take a callable for their host credentials,
which is what lets every platform row carry its own: nothing here touches
`MLFLOW_TRACKING_*` or `DATABRICKS_*` in the process environment, which
would leak one organisation's credentials into another's calls.

- `mlflow`: a tracking server, anonymous, bearer or basic auth.
- `databricks`: a workspace, a PAT or a service principal (OAuth M2M through
  `databricks-sdk`, which `mlflow-skinny` brings). Also runs Jobs.
- `azureml`: a workspace's MLflow endpoint with an Entra token for Azure
  Resource Manager, from `azure-identity` (a base dependency).

The SDKs are synchronous; every call runs in a thread under a timeout. The
platform trains nothing (ML-9): it registers snapshots as datasets, reads
finished runs back as model versions, and on Databricks starts the job the
customer configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import structlog

from app.models import MlPlatform, MlPlatformKind
from app.services.secrets import resolve_secret

log = structlog.get_logger(__name__)

#: Seconds before a platform call is abandoned; a check gets less.
CALL_TIMEOUT_SECONDS = 30
CHECK_TIMEOUT_SECONDS = 10

#: `source_type` of the dataset input a published snapshot carries.
SNAPSHOT_SOURCE_TYPE = "annotation-snapshot"
TAG_SNAPSHOT_ID = "annotation.snapshot_id"
TAG_SNAPSHOT_DIGEST = "annotation.snapshot_digest"
TAG_PROJECT_ID = "annotation.project_id"

_ARM_SCOPE = "https://management.azure.com/.default"


class MlPlatformError(Exception):
    """Base class. Messages name the platform's answer, never a credential."""


class MlPlatformUnavailable(MlPlatformError):  # noqa: N818 - a state, not an "…Error"
    """The platform cannot be used: extra missing, unreachable, refused, failed."""


class MlPlatformNotFound(MlPlatformError):  # noqa: N818 - a state, not an "…Error"
    """The platform has no such run, model version or job."""


# --------------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------------- #


def http_base(kind: MlPlatformKind, tracking_uri: str) -> str:
    """The HTTPS base the REST calls go to (`azureml://` becomes `https://`)."""
    uri = tracking_uri.rstrip("/")
    if kind is MlPlatformKind.AZUREML and uri.startswith("azureml://"):
        return "https://" + uri.removeprefix("azureml://")
    return uri


def _azureml_workspace_id(base: str) -> str | None:
    """`/subscriptions/…/workspaces/<ws>` from an Azure ML MLflow URI."""
    marker = "/subscriptions/"
    start = base.find(marker)
    return base[start:] if start >= 0 else None


def run_url(platform: MlPlatform, experiment_id: str, run_id: str) -> str | None:
    """Where a person opens the run in the platform's own UI."""
    base = http_base(platform.kind, platform.tracking_uri)
    if platform.kind is MlPlatformKind.MLFLOW:
        # The server's UI may sit elsewhere than the API the backend calls
        # (in compose: mlflow:5000 for the backend, localhost:5001 for people).
        ui = str(platform.config.get("ui_url") or base)
        return f"{ui}/#/experiments/{experiment_id}/runs/{run_id}"
    if platform.kind is MlPlatformKind.DATABRICKS:
        return f"{base}/ml/experiments/{experiment_id}/runs/{run_id}"
    workspace = _azureml_workspace_id(base)
    if workspace is None:
        return None
    return (
        f"https://ml.azure.com/experiments/id/{experiment_id}/runs/{run_id}?wsid={quote(workspace)}"
    )


def default_experiment(platform: MlPlatform, project_name: str) -> str:
    """`annotation/<project>`; Databricks wants a workspace path."""
    if platform.kind is MlPlatformKind.DATABRICKS:
        return f"/Shared/annotation/{project_name}"
    return f"annotation/{project_name}"


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HostCreds:
    """What one call authenticates with. Never logged, never returned."""

    host: str
    token: str | None = None
    username: str | None = None
    password: str | None = None

    def __repr__(self) -> str:
        return f"HostCreds(host={self.host!r})"


def _config_text(platform: MlPlatform, key: str) -> str | None:
    value = platform.config.get(key)
    return str(value) if value else None


def _databricks_token(host: str, client_id: str, client_secret: str) -> str:
    from databricks.sdk.core import Config

    headers = Config(
        host=host, client_id=client_id, client_secret=client_secret, auth_type="oauth-m2m"
    ).authenticate()
    return str(headers["Authorization"]).removeprefix("Bearer ")


def _azure_token(platform: MlPlatform, secret: str | None) -> str:
    from azure.identity import ClientSecretCredential, ManagedIdentityCredential

    client_id = _config_text(platform, "client_id")
    credential: ClientSecretCredential | ManagedIdentityCredential
    if platform.identity_type == "service_principal":
        credential = ClientSecretCredential(
            tenant_id=_config_text(platform, "tenant_id") or "",
            client_id=client_id or "",
            client_secret=secret or "",
        )
    else:
        credential = ManagedIdentityCredential(client_id=client_id)
    with credential:
        return credential.get_token(_ARM_SCOPE).token


async def credentials(platform: MlPlatform) -> HostCreds:
    """Resolve the secret and, for service principals / identities, a token.

    Raises `SecretResolutionError` for an unresolvable `secret_ref`, and
    `MlPlatformUnavailable` when no token can be obtained.
    """
    host = http_base(platform.kind, platform.tracking_uri)
    secret = await resolve_secret(platform.secret_ref)
    identity = platform.identity_type
    if identity == "none":
        return HostCreds(host=host)
    if identity == "bearer":
        return HostCreds(host=host, token=secret)
    if identity == "basic":
        username, sep, password = (secret or "").partition(":")
        if not sep:
            raise MlPlatformUnavailable("a basic-auth secret must be 'user:password'")
        return HostCreds(host=host, username=username, password=password)
    try:
        if platform.kind is MlPlatformKind.DATABRICKS:
            token = await asyncio.to_thread(
                _databricks_token, host, _config_text(platform, "client_id") or "", secret or ""
            )
        else:
            token = await asyncio.to_thread(_azure_token, platform, secret)
    except ImportError as exc:
        raise MlPlatformUnavailable(_missing_extra()) from exc
    except Exception as exc:
        # Identity libraries put request details in their messages; the type
        # tells an operator enough (wrong tenant, wrong client, no identity).
        raise MlPlatformUnavailable(
            f"cannot get a token for {platform.kind.value} ({type(exc).__name__})"
        ) from None
    return HostCreds(host=host, token=token)


# --------------------------------------------------------------------------- #
# The MLflow client
# --------------------------------------------------------------------------- #


def _missing_extra() -> str:
    return (
        "ML platform support needs the backend `mlflow` extra "
        "(image: --build-arg EXTRAS=mlflow, compose: BACKEND_EXTRAS=mlflow)"
    )


def _mlflow_defaults() -> None:
    # MLflow retries 7 times over minutes with a 120 s timeout by default;
    # a request waiting on a dead host should give up well inside our own.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "2")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "20")
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")


@dataclass
class RunRecord:
    """The parts of an MLflow run the platform stores."""

    run_id: str
    experiment_id: str
    name: str | None
    status: str
    start_time: int | None
    end_time: int | None
    params: dict[str, str]
    metrics: dict[str, float]
    tags: dict[str, str]
    #: `(source_type, source)` of every dataset input.
    datasets: list[tuple[str, str]] = field(default_factory=list)


class MlflowGateway:
    """Synchronous MLflow REST calls with one platform's credentials.

    Built by `gateway_for`; tests replace that function with a fake.
    """

    def __init__(self, creds: HostCreds) -> None:
        _mlflow_defaults()
        try:
            from mlflow.store.model_registry.rest_store import RestStore as RegistryStore
            from mlflow.store.tracking.rest_store import RestStore as TrackingStore
            from mlflow.utils.rest_utils import MlflowHostCreds
        except ImportError as exc:
            raise MlPlatformUnavailable(_missing_extra()) from exc
        host_creds = MlflowHostCreds(
            host=creds.host,
            token=creds.token,
            username=creds.username,
            password=creds.password,
        )
        self.tracking = TrackingStore(lambda: host_creds)
        self.registry = RegistryStore(lambda: host_creds)

    def experiment_names(self, limit: int) -> list[str]:
        return [e.name for e in self.tracking.search_experiments(max_results=limit)]

    def experiment(self, name: str) -> tuple[str, bool]:
        """The experiment's id, created when missing; and whether it was."""
        existing = self.tracking.get_experiment_by_name(name)
        if existing is not None:
            return str(existing.experiment_id), False
        return str(self.tracking.create_experiment(name)), True

    def find_snapshot_run(self, experiment_id: str, snapshot_id: str) -> str | None:
        from mlflow.entities import ViewType

        runs = self.tracking.search_runs(
            experiment_ids=[experiment_id],
            filter_string=f"tags.`{TAG_SNAPSHOT_ID}` = '{snapshot_id}'",
            run_view_type=ViewType.ACTIVE_ONLY,
            max_results=1,
        )
        return str(runs[0].info.run_id) if runs else None

    def create_snapshot_run(
        self,
        experiment_id: str,
        *,
        run_name: str,
        dataset_name: str,
        digest: str,
        source: dict[str, Any],
        profile: dict[str, Any],
        params: dict[str, str],
        tags: dict[str, str],
    ) -> str:
        from mlflow.entities import (
            Dataset,
            DatasetInput,
            InputTag,
            Param,
            RunStatus,
            RunTag,
        )

        now = int(time.time() * 1000)
        run = self.tracking.create_run(
            experiment_id=experiment_id,
            user_id="annotide",
            start_time=now,
            tags=[RunTag(key, value) for key, value in tags.items()],
            run_name=run_name,
        )
        run_id = str(run.info.run_id)
        self.tracking.log_batch(
            run_id,
            metrics=[],
            params=[Param(key, value) for key, value in params.items()],
            tags=[],
        )
        dataset = Dataset(
            name=dataset_name,
            # MLflow keeps at most 36 characters; the full sha256 is in the
            # source and the run tag.
            digest=digest[:16],
            source_type=SNAPSHOT_SOURCE_TYPE,
            source=json.dumps(source, sort_keys=True),
            profile=json.dumps(profile, sort_keys=True),
        )
        self.tracking.log_inputs(
            run_id,
            datasets=[DatasetInput(dataset, tags=[InputTag("mlflow.data.context", "annotation")])],
        )
        self.tracking.update_run_info(
            run_id, run_status=RunStatus.FINISHED, end_time=now, run_name=run_name
        )
        return run_id

    def run(self, run_id: str) -> RunRecord:
        run = self.tracking.get_run(run_id)
        inputs = getattr(run, "inputs", None)
        dataset_inputs = getattr(inputs, "dataset_inputs", None) or []
        return RunRecord(
            run_id=str(run.info.run_id),
            experiment_id=str(run.info.experiment_id),
            name=run.info.run_name,
            status=str(run.info.status),
            start_time=run.info.start_time,
            end_time=run.info.end_time,
            params=dict(run.data.params),
            metrics={key: float(value) for key, value in run.data.metrics.items()},
            tags=dict(run.data.tags),
            datasets=[(d.dataset.source_type, d.dataset.source) for d in dataset_inputs],
        )

    def registered_run_id(self, name: str, version: str) -> str:
        model_version = self.registry.get_model_version(name, version)
        if not model_version.run_id:
            raise MlPlatformNotFound(f"model version {name}/{version} has no run")
        return str(model_version.run_id)


def gateway_for(creds: HostCreds) -> MlflowGateway:
    """The seam tests replace."""
    return MlflowGateway(creds)


def start_databricks_job(creds: HostCreds, job_id: int, parameters: dict[str, str]) -> str:
    """Jobs `run-now`; the run id. Synchronous, like `MlflowGateway` (tests replace it)."""
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as exc:
        raise MlPlatformUnavailable(_missing_extra()) from exc
    client = WorkspaceClient(host=creds.host, token=creds.token, auth_type="pat")
    waiter = client.jobs.run_now(job_id=job_id, job_parameters=parameters)
    return str(waiter.response.run_id)


def _is_not_found(exc: Exception) -> bool:
    code = str(getattr(exc, "error_code", "") or "")
    name = type(exc).__name__
    return code in {"RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"} or name in {
        "NotFound",
        "ResourceDoesNotExist",
    }


async def _call[T](platform: MlPlatform, fn: Callable[[], T], timeout: float) -> T:
    """Run a synchronous SDK call in a thread, map its failures to ours."""
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.to_thread(fn)
    except MlPlatformError:
        raise
    except TimeoutError:
        raise MlPlatformUnavailable(
            f"{platform.kind.value} did not answer within {timeout:.0f} s"
        ) from None
    except Exception as exc:
        if _is_not_found(exc):
            raise MlPlatformNotFound(_short(exc)) from None
        log.warning("ml_platform.call_failed", kind=platform.kind.value, error=type(exc).__name__)
        raise MlPlatformUnavailable(f"{platform.kind.value} call failed: {_short(exc)}") from None


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return (text[:300] + "…") if len(text) > 300 else (text or type(exc).__name__)


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #


async def check(platform: MlPlatform) -> dict[str, Any]:
    """One experiment search with the platform's credentials."""
    creds = await credentials(platform)
    names = await _call(
        platform, lambda: gateway_for(creds).experiment_names(5), CHECK_TIMEOUT_SECONDS
    )
    return {"tracking_uri": creds.host, "experiments": names}


@dataclass
class SnapshotFacts:
    """What a published snapshot says about itself."""

    snapshot_id: str
    project_id: str
    name: str
    digest: str
    blob_path: str
    item_count: int
    label_schema_version_id: str
    result_connector_id: str | None
    split: dict[str, Any] | None


@dataclass
class PublishedRun:
    experiment_id: str
    experiment_name: str
    run_id: str
    run_url: str | None
    created: bool


async def publish_snapshot(
    platform: MlPlatform, snapshot: SnapshotFacts, experiment: str
) -> PublishedRun:
    """A finished run standing for the snapshot; the existing one if there is."""
    creds = await credentials(platform)

    def publish() -> PublishedRun:
        gateway = gateway_for(creds)
        experiment_id, _ = gateway.experiment(experiment)
        run_id = gateway.find_snapshot_run(experiment_id, snapshot.snapshot_id)
        created = run_id is None
        if run_id is None:
            run_id = gateway.create_snapshot_run(
                experiment_id,
                run_name=f"snapshot {snapshot.name}",
                dataset_name=snapshot.name,
                digest=snapshot.digest,
                source={
                    "snapshot_id": snapshot.snapshot_id,
                    "project_id": snapshot.project_id,
                    "digest": snapshot.digest,
                    "blob_path": snapshot.blob_path,
                    "result_connector_id": snapshot.result_connector_id,
                },
                profile={"item_count": snapshot.item_count, "split": snapshot.split},
                params={
                    TAG_PROJECT_ID: snapshot.project_id,
                    TAG_SNAPSHOT_ID: snapshot.snapshot_id,
                    "annotation.label_schema_version_id": snapshot.label_schema_version_id,
                    "item_count": str(snapshot.item_count),
                },
                tags={
                    TAG_SNAPSHOT_ID: snapshot.snapshot_id,
                    TAG_SNAPSHOT_DIGEST: snapshot.digest,
                    TAG_PROJECT_ID: snapshot.project_id,
                },
            )
        return PublishedRun(
            experiment_id=experiment_id,
            experiment_name=experiment,
            run_id=run_id,
            run_url=run_url(platform, experiment_id, run_id),
            created=created,
        )

    return await _call(platform, publish, CALL_TIMEOUT_SECONDS)


@dataclass
class ImportedRun:
    """A run read back, shaped for a `model_version`."""

    metrics: dict[str, Any]
    training_run: dict[str, Any]
    snapshot_id: str | None
    snapshot_digest: str | None


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat() if ms else None


def lineage_of(record: RunRecord) -> tuple[str | None, str | None]:
    """`(snapshot_id, snapshot_digest)` the run claims (EXP-8).

    The run's own tags first — a training run tags what it proved — then an
    `annotation-snapshot` dataset input, which a run that logged the
    published snapshot as its input carries.
    """
    snapshot_id = record.tags.get(TAG_SNAPSHOT_ID)
    digest = record.tags.get(TAG_SNAPSHOT_DIGEST)
    if snapshot_id or digest:
        return snapshot_id or None, digest or None
    for source_type, source in record.datasets:
        if source_type != SNAPSHOT_SOURCE_TYPE:
            continue
        try:
            data = json.loads(source)
        except ValueError:
            continue
        if isinstance(data, dict):
            return (data.get("snapshot_id") or None), (data.get("digest") or None)
    return None, None


async def read_run(
    platform: MlPlatform,
    *,
    run_id: str | None = None,
    registered_model: str | None = None,
    model_version: str | None = None,
) -> ImportedRun:
    """A finished (or running) MLflow run, by id or through a registered version."""
    creds = await credentials(platform)

    def read() -> RunRecord:
        gateway = gateway_for(creds)
        if run_id is not None:
            return gateway.run(run_id)
        if registered_model is None or model_version is None:
            raise ValueError("name a run, or a registered model and its version")
        return gateway.run(gateway.registered_run_id(registered_model, model_version))

    record = await _call(platform, read, CALL_TIMEOUT_SECONDS)
    snapshot_id, digest = lineage_of(record)
    source: dict[str, Any] = {"kind": platform.kind.value, "ml_platform_id": str(platform.id)}
    if registered_model is not None:
        source["registered_model"] = registered_model
        source["model_version"] = model_version
        source["model_uri"] = f"models:/{registered_model}/{model_version}"
    else:
        source["model_uri"] = f"runs:/{record.run_id}"
    training_run = {
        "id": record.run_id,
        "url": run_url(platform, record.experiment_id, record.run_id),
        "name": record.name,
        "experiment_id": record.experiment_id,
        "status": record.status,
        "started_at": _iso(record.start_time),
        "finished_at": _iso(record.end_time),
        "params": record.params,
        "source": source,
    }
    return ImportedRun(
        metrics=dict(record.metrics),
        training_run=training_run,
        snapshot_id=snapshot_id,
        snapshot_digest=digest,
    )


async def run_retrain_job(platform: MlPlatform, parameters: dict[str, str]) -> tuple[str, str]:
    """Start the platform's Databricks job; `(run_id, run_url)`."""
    job_id = platform.config.get("job_id")
    if platform.kind is not MlPlatformKind.DATABRICKS or not isinstance(job_id, int):
        raise ValueError("only a databricks platform with config.job_id runs a retrain job")
    creds = await credentials(platform)
    run_id = await _call(
        platform,
        lambda: start_databricks_job(creds, job_id, parameters),
        CALL_TIMEOUT_SECONDS,
    )
    return run_id, f"{creds.host}/jobs/{job_id}/runs/{run_id}"
