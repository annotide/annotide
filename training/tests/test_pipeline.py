from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from annotide import AnnotationError, Client

from annotide_training.dataset import DatasetError
from annotide_training.pipeline import run_pipeline
from annotide_training.trainers import BaselineTrainer, Record, TrainedModel
from annotide_training.webhook import RetrainRequest
from tests.conftest import DIGEST, MODEL, PROJECT, SNAPSHOT, FakePlatform, export_zip, record


def _request(model_id: str | None = MODEL, digest: str = DIGEST) -> RetrainRequest:
    return RetrainRequest(
        project_id=PROJECT,
        snapshot_id=SNAPSHOT,
        snapshot_digest=digest,
        model_id=model_id,
        note="nightly",
        delivery_id="d-1",
    )


def _split_archive() -> bytes:
    car = ("car", [10.0, 10.0, 50.0, 50.0])
    return export_zip(
        {
            "train": [record(f"t{n}", [car]) for n in range(4)],
            "val": [record("v0", [car])],
            "test": [record("x0", [car]), record("x1", [])],
        }
    )


def test_end_to_end_registers_a_version_with_lineage(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()

    result = run_pipeline(
        _request(), client, BaselineTrainer(), out_dir=tmp_path, params={"min_frequency": 0.5}
    )

    [body] = platform.registered
    assert body["snapshot_id"] == SNAPSHOT
    assert body["snapshot_digest"] == DIGEST
    run = body["training_run"]
    assert run["id"] == result.run_id
    assert run["trainer"] == "baseline-class-prior"
    assert run["params"] == {"min_frequency": 0.5}
    assert run["split_source"] == "snapshot"
    assert run["split_counts"] == {"train": 4, "val": 1, "test": 2}
    assert run["export_job_id"] == "job-1"
    assert run["delivery_id"] == "d-1"
    assert run["classes"] == ["car"]
    # One car in x0 found; x1 has none, so its prediction is a false positive.
    assert body["metrics"]["split"] == "test"
    assert (body["metrics"]["tp"], body["metrics"]["fp"], body["metrics"]["fn"]) == (1, 1, 0)
    assert result.version is not None and result.version["version"] == 2
    assert result.artifact_path.exists()
    assert (result.artifact_path.parent / "run.json").exists()


def test_the_export_is_requested_in_native_format_and_downloaded_without_the_key(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    run_pipeline(_request(), client, BaselineTrainer(), out_dir=tmp_path)

    export = next(r for r in platform.requests if r.url.path.endswith("/exports"))
    assert b'"format":"native"' in export.content.replace(b" ", b"")
    blob = next(r for r in platform.requests if r.url.path == "/blob/export.zip")
    assert str(blob.url) == "https://annotate.example.com/blob/export.zip?sig=s"
    assert "authorization" not in blob.headers
    api = next(r for r in platform.requests if r.url.path.startswith("/api/v1/"))
    assert api.headers["authorization"] == "Bearer key-123"


def test_a_digest_that_is_not_the_snapshots_fails_before_any_export(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    with pytest.raises(DatasetError, match="digest"):
        run_pipeline(_request(digest="b" * 64), client, BaselineTrainer(), out_dir=tmp_path)
    assert not any(r.url.path.endswith("/exports") for r in platform.requests)
    assert platform.registered == []


def test_a_failed_export_job_stops_the_run(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.job_status = "failed"
    with pytest.raises(AnnotationError, match="ended failed"):
        run_pipeline(_request(), client, BaselineTrainer(), out_dir=tmp_path)


def test_dry_run_trains_and_evaluates_without_registering(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    result = run_pipeline(
        _request(model_id=None), client, BaselineTrainer(), out_dir=tmp_path, register=False
    )
    assert result.version is None
    assert platform.registered == []
    assert result.metrics["images"] == 2


def test_registering_needs_a_model(client: Client, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no model"):
        run_pipeline(_request(model_id=None), client, BaselineTrainer(), out_dir=tmp_path)


def test_an_empty_train_split_is_an_error(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = export_zip({"val": [record("v0", [])]})
    with pytest.raises(DatasetError, match="train split is empty"):
        run_pipeline(_request(), client, BaselineTrainer(), out_dir=tmp_path)


class _WithNames(BaselineTrainer):
    def train(
        self, train: Sequence[Record], val: Sequence[Record], params: Mapping[str, Any]
    ) -> TrainedModel:
        model = super().train(train, val, params)
        model.extra_files = {"../model.names": b"car\n"}
        return model


def test_extra_files_land_next_to_the_artifact_and_never_outside_it(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    result = run_pipeline(_request(), client, _WithNames(), out_dir=tmp_path, register=False)
    assert (result.artifact_path.parent / "model.names").read_bytes() == b"car\n"
    assert not (tmp_path / "model.names").exists()


def test_the_run_is_recorded_in_mlflow_with_the_snapshot_as_its_input(
    platform: FakePlatform, client: Client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mlflow")
    # A file store keeps the test self-contained; real use is a server.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    import json

    from mlflow.tracking import MlflowClient

    from annotide_training.tracking import SNAPSHOT_SOURCE_TYPE, MlflowSettings

    platform.archive = _split_archive()
    store = (tmp_path / "mlruns").as_uri()
    result = run_pipeline(
        _request(),
        client,
        BaselineTrainer(),
        out_dir=tmp_path / "runs",
        mlflow=MlflowSettings(experiment="annotation/test", tracking_uri=store),
    )

    [body] = platform.registered
    recorded = body["training_run"]["mlflow"]
    run = MlflowClient(tracking_uri=store).get_run(recorded["run_id"])
    assert run.data.tags["annotation.snapshot_id"] == SNAPSHOT
    assert run.data.tags["annotation.snapshot_digest"] == DIGEST
    assert run.data.metrics["tp"] == 1.0
    [dataset_input] = run.inputs.dataset_inputs
    assert dataset_input.dataset.source_type == SNAPSHOT_SOURCE_TYPE
    assert json.loads(dataset_input.dataset.source)["digest"] == DIGEST
    artifacts = MlflowClient(tracking_uri=store).list_artifacts(recorded["run_id"], "model")
    assert result.artifact_path.name in {Path(a.path).name for a in artifacts}


def test_a_trained_version_says_so_and_carries_size_latency_and_dtype(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    run_pipeline(_request(), client, BaselineTrainer(), out_dir=tmp_path)

    [body] = platform.registered
    assert body["derivation"] == "trained"
    assert body["parent_version_id"] is None
    metrics = body["metrics"]
    assert metrics["dtype"] == "fp64"
    assert metrics["size_bytes"] == len(
        (tmp_path / body["training_run"]["id"] / "baseline.json").read_bytes()
    )
    assert isinstance(metrics["latency_ms_p50"], int)
    assert metrics["latency_device"]


def test_a_teacher_makes_the_version_distilled_from_it(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    run_pipeline(
        _request(), client, BaselineTrainer(), out_dir=tmp_path, teacher_version_id="teacher-1"
    )

    [body] = platform.registered
    assert body["derivation"] == "distilled"
    assert body["parent_version_id"] == "teacher-1"
    assert body["training_run"]["teacher_version_id"] == "teacher-1"


def test_quantize_registers_an_int8_child_of_the_trained_version(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    result = run_pipeline(
        _request(),
        client,
        BaselineTrainer(),
        out_dir=tmp_path,
        teacher_version_id="teacher-1",
        quantize="int8",
    )

    trained, small = platform.registered
    assert small["derivation"] == "quantized"
    assert small["parent_version_id"] == "v-1"  # the version this run registered first
    assert "teacher_version_id" not in small["training_run"]
    assert small["metrics"]["dtype"] == "int8"
    assert small["metrics"]["size_bytes"] < trained["metrics"]["size_bytes"]
    # 8-bit priors still find the one car in x0: same counts as full precision.
    for key in ("tp", "fp", "fn"):
        assert small["metrics"][key] == trained["metrics"][key]

    assert result.quantized is not None
    assert result.quantized.version is not None and result.quantized.version["version"] == 3
    assert result.quantized.artifact_path == result.artifact_path.parent / "int8" / "baseline.json"
    assert (result.quantized.artifact_path.parent / "run.json").exists()


def test_a_dry_run_quantizes_without_registering(
    platform: FakePlatform, client: Client, tmp_path: Path
) -> None:
    platform.archive = _split_archive()
    result = run_pipeline(
        _request(), client, BaselineTrainer(), out_dir=tmp_path, register=False, quantize="int8"
    )
    assert platform.registered == []
    assert result.quantized is not None and result.quantized.version is None
    assert result.quantized.metrics["dtype"] == "int8"


class _NoQuantize:
    name = "no-quantize"

    def train(
        self, train: Sequence[Record], val: Sequence[Record], params: Mapping[str, Any]
    ) -> TrainedModel:
        raise AssertionError("must fail before training")

    def predict(self, model: TrainedModel, record: Record) -> list[Any]:
        return []


@pytest.mark.parametrize(
    ("trainer", "dtype", "match"),
    [(_NoQuantize(), "int8", "cannot quantize"), (BaselineTrainer(), "int4", "int4")],
)
def test_quantize_is_refused_before_any_work(
    client: Client, tmp_path: Path, trainer: Any, dtype: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        run_pipeline(_request(), client, trainer, out_dir=tmp_path, quantize=dtype)
