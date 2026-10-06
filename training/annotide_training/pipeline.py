"""prepare → split → train → evaluate → [quantize → evaluate] → register (ML-9, EXP-8).

Each registered version names how it came to be (`derivation`): `trained`,
or `distilled` from a teacher version when the run says the snapshot was
pre-labelled by one, and `quantized` for the int8 copy, whose parent is the
version this run just registered. The UI draws that as the model's family.
"""

from __future__ import annotations

import json
import logging
import platform
import random
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from annotide import Client

from annotide_training import __version__
from annotide_training.dataset import DatasetError, SplitConfig, SplitName, load_export
from annotide_training.evaluate import evaluate
from annotide_training.tracking import MlflowSettings, log_run
from annotide_training.trainers import (
    QUANTIZE_DTYPES,
    Quantizer,
    Record,
    TrainedModel,
    Trainer,
)
from annotide_training.webhook import RetrainRequest

log = logging.getLogger("annotide_training")


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    artifact_path: Path
    metrics: dict[str, Any]
    #: The registered version, or `None` for a `register=False` (dry) run.
    version: dict[str, Any] | None
    #: The lower-precision copy when the run was asked to `quantize`.
    quantized: QuantizedResult | None = None


@dataclass(frozen=True, slots=True)
class QuantizedResult:
    artifact_path: Path
    metrics: dict[str, Any]
    version: dict[str, Any] | None


def run_pipeline(
    request: RetrainRequest,
    client: Client,
    trainer: Trainer,
    *,
    out_dir: Path,
    model_id: str | None = None,
    params: Mapping[str, Any] | None = None,
    split: SplitConfig | None = None,
    register: bool = True,
    job_timeout: float = 600.0,
    mlflow: MlflowSettings | None = None,
    quantize: str | None = None,
    teacher_version_id: str | None = None,
) -> RunResult:
    """Train on the snapshot the request names and register the result.

    `model_id` is the model to add a version to when the event did not name
    one. With `register=False` everything runs except the registration, so a
    trainer can be tried against real data without touching the registry.
    With `mlflow`, the run is also recorded there (API-6) before the version
    is registered, and `training_run["mlflow"]` names it.

    `quantize="int8"` also stores the trained model at that precision (the
    trainer must implement `Quantizer`), scores it on the same held-out
    split and registers it as a `quantized` child of the version trained
    here. `teacher_version_id` registers the trained version as `distilled`
    from that version: the model whose pre-labels, corrected by people, are
    the snapshot this run trained on.
    """
    target_model = request.model_id or model_id
    if register and not target_model:
        raise ValueError("no model to register a version for: the event names none")
    quantizer: Quantizer | None = None
    if quantize is not None:
        if quantize not in QUANTIZE_DTYPES:
            raise ValueError(f"cannot quantize to {quantize!r}: use one of {QUANTIZE_DTYPES}")
        if not isinstance(trainer, Quantizer):
            raise ValueError(f"trainer {trainer.name} cannot quantize (no quantize method)")
        quantizer = trainer
    run_params = dict(params or {})
    run_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)

    # prepare: the snapshot row first, so a stale or forged digest fails before
    # an export is queued.
    snapshot = client.get_snapshot(request.project_id, request.snapshot_id)
    if snapshot.get("digest") != request.snapshot_digest:
        raise DatasetError(
            f"snapshot {request.snapshot_id} has digest {snapshot.get('digest')}, "
            f"the request says {request.snapshot_digest}"
        )
    export_job = client.create_export(
        request.project_id, "native", snapshot_id=request.snapshot_id
    )["id"]
    client.wait_for_job(export_job, timeout=job_timeout)
    with tempfile.TemporaryDirectory() as tmp:
        archive = client.download_export(export_job, Path(tmp) / "export.zip").read_bytes()

    # split: the platform's partition when the snapshot has one (EXP-3).
    dataset = load_export(
        archive,
        snapshot_id=request.snapshot_id,
        snapshot_digest=request.snapshot_digest,
        split=split,
    )
    log.info("prepared run=%s counts=%s source=%s", run_id, dataset.counts, dataset.split_source)
    if not dataset.records["train"]:
        raise DatasetError("the train split is empty; nothing to train on")

    # train
    model = trainer.train(dataset.records["train"], dataset.records["val"], run_params)

    # evaluate: on test, or val when the split left test empty.
    held_out: SplitName = "test" if dataset.records["test"] else "val"
    metrics = _metrics(trainer, model, dataset.records[held_out], held_out)
    finished_at = datetime.now(UTC)

    run_dir = out_dir / run_id
    artifact_path = _write(model, run_dir)
    training_run = {
        "id": run_id,
        "pipeline": f"annotide-training {__version__}",
        "trainer": trainer.name,
        "params": run_params,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "export_job_id": export_job,
        "split_source": dataset.split_source,
        "split_counts": dataset.counts,
        "classes": model.classes,
        "artifact": str(artifact_path),
        "info": model.info,
        "delivery_id": request.delivery_id,
        "note": request.note,
    }
    if teacher_version_id is not None:
        training_run["teacher_version_id"] = teacher_version_id
    if mlflow is not None:
        training_run["mlflow"] = log_run(
            mlflow,
            project_id=request.project_id,
            snapshot_id=request.snapshot_id,
            snapshot_digest=request.snapshot_digest,
            run_name=f"train {trainer.name} {run_id[:8]}",
            params=run_params,
            metrics=metrics,
            run_dir=run_dir,
        )
    (run_dir / "run.json").write_text(
        json.dumps({"training_run": training_run, "metrics": metrics}, indent=2, sort_keys=True)
    )

    version: dict[str, Any] | None = None
    if register and target_model:
        # register: the platform re-checks the digest against the row (409).
        registered = client.create_model_version(
            target_model,
            {
                "class_mapping": {},
                "snapshot_id": request.snapshot_id,
                "snapshot_digest": request.snapshot_digest,
                "training_run": training_run,
                "metrics": metrics,
                "parent_version_id": teacher_version_id,
                "derivation": "distilled" if teacher_version_id else "trained",
            },
        )
        version = dict(registered)
        log.info("registered run=%s model=%s version=%s", run_id, target_model, version["version"])

    quantized: QuantizedResult | None = None
    if quantizer is not None and quantize is not None:
        # Calibrate on a fixed-seed sample of train, not its first rows.
        calibration = list(dataset.records["train"])
        random.Random(0).shuffle(calibration)
        small = quantizer.quantize(model, calibration, quantize)
        small_metrics = _metrics(trainer, small, dataset.records[held_out], held_out)
        small_dir = run_dir / quantize
        small_path = _write(small, small_dir)
        small_run = {
            **training_run,
            "quantized_from": str(artifact_path),
            "artifact": str(small_path),
            "info": small.info,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        small_run.pop("mlflow", None)
        small_run.pop("teacher_version_id", None)
        if mlflow is not None:
            small_run["mlflow"] = log_run(
                mlflow,
                project_id=request.project_id,
                snapshot_id=request.snapshot_id,
                snapshot_digest=request.snapshot_digest,
                run_name=f"quantize {quantize} {trainer.name} {run_id[:8]}",
                params={**run_params, "quantize": quantize},
                metrics=small_metrics,
                run_dir=small_dir,
            )
        (small_dir / "run.json").write_text(
            json.dumps(
                {"training_run": small_run, "metrics": small_metrics}, indent=2, sort_keys=True
            )
        )
        small_version: dict[str, Any] | None = None
        if version is not None and target_model:
            small_version = dict(
                client.create_model_version(
                    target_model,
                    {
                        "class_mapping": {},
                        "snapshot_id": request.snapshot_id,
                        "snapshot_digest": request.snapshot_digest,
                        "training_run": small_run,
                        "metrics": small_metrics,
                        "parent_version_id": version["id"],
                        "derivation": "quantized",
                    },
                )
            )
            log.info(
                "registered %s copy run=%s version=%s", quantize, run_id, small_version["version"]
            )
        quantized = QuantizedResult(
            artifact_path=small_path, metrics=small_metrics, version=small_version
        )
    return RunResult(
        run_id=run_id,
        artifact_path=artifact_path,
        metrics=metrics,
        version=version,
        quantized=quantized,
    )


def _metrics(
    trainer: Trainer, model: TrainedModel, records: Sequence[Record], split: SplitName
) -> dict[str, Any]:
    """The held-out scores plus what the UI compares versions on (size, dtype)."""
    metrics: dict[str, Any] = {"split": split, **evaluate(trainer, model, records)}
    metrics["size_bytes"] = len(model.artifact)
    metrics["latency_device"] = f"{platform.system()} {platform.machine()}".strip()
    if "dtype" in model.info:
        metrics["dtype"] = model.info["dtype"]
    return metrics


def _write(model: TrainedModel, run_dir: Path) -> Path:
    """The artifact and its sibling files, never outside `run_dir`."""
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = run_dir / model.filename
    artifact_path.write_bytes(model.artifact)
    for name, data in model.extra_files.items():
        (run_dir / Path(name).name).write_bytes(data)
    return artifact_path
