from __future__ import annotations

import json
import sys

import pytest

from annotide_training.evaluate import evaluate, iou
from annotide_training.trainers import BaselineTrainer, Prediction, TrainedModel, load_trainer
from tests.conftest import record


def test_baseline_learns_class_priors_and_honours_min_frequency() -> None:
    train = [
        record("a", [("car", [10, 20, 30, 40])], w=100, h=100),
        record("b", [("car", [30, 40, 50, 60])], w=100, h=100),
        record("c", [("sign", [0, 0, 10, 10])], w=100, h=100),
        record("d", [], w=0, h=0),  # no dimensions: not an image the prior can use
    ]
    model = BaselineTrainer().train(train, [], {"min_frequency": 0.5})
    body = json.loads(model.artifact)
    assert body["images"] == 3
    assert model.classes == ["car"]  # sign is in 1/3 of the images
    assert body["priors"]["car"] == {"frequency": 0.666667, "box": [0.2, 0.3, 0.4, 0.5]}

    [prediction] = BaselineTrainer().predict(model, record("e", [], w=200, h=100))
    assert prediction.class_ == "car"
    assert prediction.bbox == pytest.approx((40.0, 30.0, 80.0, 50.0))


def test_iou() -> None:
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)


class _Fixed:
    name = "fixed"

    def __init__(self, predictions: list[Prediction]) -> None:
        self._predictions = predictions

    def train(self, *_: object) -> TrainedModel:
        return TrainedModel(b"", "x", [])

    def predict(self, model: TrainedModel, rec: object) -> list[Prediction]:
        return self._predictions


def test_evaluate_matches_greedily_and_never_across_classes() -> None:
    truth = record("a", [("car", [0, 0, 10, 10]), ("sign", [50, 50, 60, 60])])
    trainer = _Fixed(
        [
            Prediction("car", (0, 0, 10, 10), 0.9),
            Prediction("car", (0, 0, 10, 10), 0.8),  # duplicate: false positive
            Prediction("sign", (0, 0, 10, 10), 0.7),  # right place for a car, wrong class
        ]
    )
    metrics = evaluate(trainer, TrainedModel(b"", "x", []), [truth])
    assert metrics["per_class"]["car"] == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "precision": 0.5,
        "recall": 1.0,
        "f1": 0.6667,
    }
    assert metrics["per_class"]["sign"]["fn"] == 1
    assert (metrics["tp"], metrics["fp"], metrics["fn"]) == (1, 2, 1)


def test_load_trainer() -> None:
    assert isinstance(load_trainer("baseline"), BaselineTrainer)
    assert load_trainer("annotide_training.trainers:BaselineTrainer").name == (
        "baseline-class-prior"
    )
    with pytest.raises(ValueError, match="module:ClassName"):
        load_trainer("nope")
    with pytest.raises(TypeError, match="does not implement"):
        load_trainer("annotide_training.dataset:SplitConfig")


def test_fasterrcnn_without_the_torch_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "annotide_training.detector", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)
    with pytest.raises(ValueError, match=r"annotide-training\[torch\]"):
        load_trainer("fasterrcnn")
