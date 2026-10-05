"""Optional MLflow tracking for a training run (API-6, EXP-8).

With `--mlflow-experiment`, the pipeline also records its run in the
customer's MLflow — a local server, Databricks or Azure ML, wherever
`MLFLOW_TRACKING_URI` (or `--mlflow-uri`) points and however that
environment authenticates — with the snapshot as the run's dataset input in
exactly the shape the platform publishes (`source_type`
`annotation-snapshot`) and the `annotation.snapshot_*` tags. The platform's
`POST /models/{id}/versions/import` reads the lineage back from either, so a
version imported from this run, or from the registered model it creates, is
linked to its snapshot without a separate step.

Needs the `mlflow` extra (`mlflow-skinny`); imported only when used.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SNAPSHOT_SOURCE_TYPE = "annotation-snapshot"


@dataclass(frozen=True, slots=True)
class MlflowSettings:
    experiment: str
    tracking_uri: str | None = None
    #: Register the run's artifacts as a new version of this registered model.
    registered_model: str | None = None


def _numeric(metrics: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """MLflow takes numbers only; nested dicts are flattened with dots."""
    flat: dict[str, float] = {}
    for key, value in metrics.items():
        name = f"{prefix}{key}"
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            flat[name] = float(value)
        elif isinstance(value, dict):
            flat.update(_numeric(value, f"{name}."))
    return flat


def log_run(
    settings: MlflowSettings,
    *,
    project_id: str,
    snapshot_id: str,
    snapshot_digest: str,
    run_name: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """Record the finished run in MLflow; what to keep in `training_run["mlflow"]`."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    try:
        import mlflow
        from mlflow.entities import Dataset, DatasetInput, InputTag
        from mlflow.tracking import MlflowClient
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError("--mlflow-* needs the `mlflow` extra (mlflow-skinny)") from exc

    if settings.tracking_uri:
        mlflow.set_tracking_uri(settings.tracking_uri)
    experiment = mlflow.set_experiment(settings.experiment)
    with mlflow.start_run(run_name=run_name) as run:
        run_id = run.info.run_id
        mlflow.set_tags(
            {
                "annotation.project_id": project_id,
                "annotation.snapshot_id": snapshot_id,
                "annotation.snapshot_digest": snapshot_digest,
            }
        )
        if params:
            mlflow.log_params({key: str(value) for key, value in params.items()})
        numbers = _numeric(metrics)
        if numbers:
            mlflow.log_metrics(numbers)
        dataset = Dataset(
            name=f"snapshot-{snapshot_id[:8]}",
            digest=snapshot_digest[:16],
            source_type=SNAPSHOT_SOURCE_TYPE,
            source=json.dumps(
                {"snapshot_id": snapshot_id, "project_id": project_id, "digest": snapshot_digest},
                sort_keys=True,
            ),
        )
        MlflowClient().log_inputs(
            run_id,
            datasets=[DatasetInput(dataset, tags=[InputTag("mlflow.data.context", "training")])],
        )
        mlflow.log_artifacts(str(run_dir), artifact_path="model")

    record: dict[str, Any] = {
        "run_id": run_id,
        "experiment_id": experiment.experiment_id,
        "tracking_uri": mlflow.get_tracking_uri(),
    }
    if settings.registered_model:
        registered = mlflow.register_model(f"runs:/{run_id}/model", settings.registered_model)
        record["registered_model"] = settings.registered_model
        record["model_version"] = str(registered.version)
    return record
