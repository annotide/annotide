"""The Faster R-CNN trainer — skipped unless the `torch` extra is installed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from annotide_training.trainers import load_trainer
from tests.conftest import record

pytest.importorskip("torch")
pytest.importorskip("torchvision")
pytest.importorskip("onnxruntime")

import numpy as np
from PIL import Image, ImageDraw

from annotide_training.detector import (
    FasterRCNNTrainer,
    ImageRoot,
    Letterbox,
    letterbox,
)


def test_letterbox_round_trip_and_padding() -> None:
    transform = Letterbox.fit(320, 160)
    assert (transform.scale, transform.pad_x, transform.pad_y) == (2.0, 0.0, 160.0)
    box = (10.0, 20.0, 110.0, 80.0)
    assert transform.box_to_input(box) == (20.0, 200.0, 220.0, 320.0)
    assert transform.box_to_original(transform.box_to_input(box), 320, 160) == box
    # Clamped to the image, whatever the model predicts in the padding.
    assert transform.box_to_original((-5.0, 0.0, 700.0, 170.0), 320, 160) == (
        0.0,
        0.0,
        320.0,
        5.0,
    )

    array, _ = letterbox(Image.new("RGB", (320, 160), (255, 0, 0)))
    assert array.shape == (3, 640, 640)
    assert array.dtype == np.float32
    assert array[:, 0, 0] == pytest.approx([114 / 255] * 3)  # top padding
    assert array[:, 320, 320] == pytest.approx([1.0, 0.0, 0.0])  # the image


def test_image_root_refuses_paths_outside_it(tmp_path: Path) -> None:
    root = ImageRoot(tmp_path)
    assert root.path(record("a", [])) == tmp_path.resolve() / "images/a.jpg"
    with pytest.raises(ValueError, match="escapes image_root"):
        root.path({"item_id": "x", "path": "../etc/passwd"})
    with pytest.raises(ValueError, match="escapes image_root"):
        root.path({"item_id": "x", "path": ""})
    with pytest.raises(FileNotFoundError, match="2 image"):
        root.check([record("a", []), record("b", [])])
    with pytest.raises(ValueError, match="not a directory"):
        ImageRoot(tmp_path / "missing")


def _dataset(root: Path, count: int) -> list[dict[str, Any]]:
    (root / "images").mkdir()
    records = []
    for n in range(count):
        image = Image.new("RGB", (200, 100), (200, 200, 200))
        x = 20 + 30 * n
        ImageDraw.Draw(image).rectangle([x, 30, x + 40, 70], fill=(220, 30, 30))
        image.save(root / "images" / f"i{n}.jpg")
        records.append(record(f"i{n}", [("car", [x, 30, x + 40, 70])], w=200, h=100))
    return records


def test_train_exports_an_onnx_model_the_model_service_can_load(tmp_path: Path) -> None:
    records = _dataset(tmp_path, 3)
    trainer = load_trainer("fasterrcnn")
    assert isinstance(trainer, FasterRCNNTrainer)

    model = trainer.train(
        records[:2],
        records[2:],
        {
            "image_root": str(tmp_path),
            "weights": "none",
            "epochs": 1,
            "batch_size": 2,
            # One epoch from scratch detects nothing useful: keep everything
            # it emits so the mapping below has boxes to check.
            "score_threshold": 0.0,
        },
    )

    assert model.filename == "fasterrcnn.onnx"
    assert model.classes == ["car"]
    assert model.extra_files == {"fasterrcnn.names": b"car\n"}
    assert model.info["weights"] == "none"
    assert len(model.info["epoch_losses"]) == 1

    import onnxruntime

    session = onnxruntime.InferenceSession(model.artifact, providers=["CPUExecutionProvider"])
    [image_input] = session.get_inputs()
    assert (image_input.name, image_input.shape) == ("images", [1, 3, 640, 640])
    assert [o.name for o in session.get_outputs()] == ["boxes", "scores", "labels"]
    # A blank frame (nothing above the RPN floor) must run, not crash.
    blank = np.full((1, 3, 640, 640), 114 / 255, dtype=np.float32)
    boxes, _, _ = session.run(None, {"images": blank})
    assert boxes.shape[1] == 4

    # predict maps into the record's own pixel frame and class names.
    for prediction in trainer.predict(model, records[2]):
        assert prediction.class_ == "car"
        x1, y1, x2, y2 = prediction.bbox
        assert 0 <= x1 <= x2 <= 200
        assert 0 <= y1 <= y2 <= 100


def test_train_fails_fast_on_missing_images_and_empty_labels(tmp_path: Path) -> None:
    trainer = FasterRCNNTrainer(image_root=tmp_path)
    with pytest.raises(FileNotFoundError, match="1 image"):
        trainer.train([record("a", [("car", [0, 0, 5, 5])])], [], {"weights": "none"})
    with pytest.raises(ValueError, match="no bbox shapes"):
        trainer.train([record("a", [])], [], {"weights": "none"})
    with pytest.raises(ValueError, match="needs image_root"):
        FasterRCNNTrainer().train([record("a", [("car", [0, 0, 5, 5])])], [], {})
    with pytest.raises(ValueError, match="use one of"):
        _dataset(tmp_path, 1)
        trainer.train(
            [record("i0", [("car", [20, 30, 60, 70])], w=200, h=100)], [], {"weights": "x"}
        )


def test_int8_quantization_shrinks_the_model_and_still_predicts(tmp_path: Path) -> None:
    records = _dataset(tmp_path, 3)
    trainer = FasterRCNNTrainer()
    model = trainer.train(
        records[:2],
        records[2:],
        {
            "image_root": str(tmp_path),
            "weights": "none",
            "epochs": 1,
            "batch_size": 2,
            "score_threshold": 0.0,
        },
    )

    small = trainer.quantize(model, records[:2], "int8")

    assert small.info["dtype"] == "int8"
    assert small.info["calibration_images"] == 2
    assert small.filename == model.filename
    assert small.extra_files == model.extra_files
    # Conv and the box head's FC layers at 8 bits: well under half the size.
    assert len(small.artifact) < len(model.artifact) / 2

    import onnxruntime

    session = onnxruntime.InferenceSession(small.artifact, providers=["CPUExecutionProvider"])
    assert any(node.op_type == "QuantizeLinear" for node in _graph_nodes(small.artifact))
    blank = np.full((1, 3, 640, 640), 114 / 255, dtype=np.float32)
    boxes, _, _ = session.run(None, {"images": blank})
    assert boxes.shape[1] == 4
    for prediction in trainer.predict(small, records[2]):
        assert prediction.class_ == "car"

    with pytest.raises(ValueError, match="int8 only"):
        trainer.quantize(model, records[:2], "fp16")


def _graph_nodes(artifact: bytes) -> list[Any]:
    import onnx

    return list(onnx.load_from_string(artifact).graph.node)
