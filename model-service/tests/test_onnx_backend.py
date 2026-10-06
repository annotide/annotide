"""The onnx backend's start-up, against a fake onnxruntime (the real one is an extra)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.backends.onnx import OnnxBackend


class _Session:
    def __init__(self, path: str, providers: list[str]) -> None:
        self.providers = providers

    def get_inputs(self) -> list[Any]:
        return [SimpleNamespace(name="images")]


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, available: list[str]) -> None:
    runtime = SimpleNamespace(get_available_providers=lambda: available, InferenceSession=_Session)
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)


def _model(tmp_path: Path) -> Path:
    path = tmp_path / "fasterrcnn.onnx"
    path.write_bytes(b"onnx")
    path.with_suffix(".names").write_text("car\nsign\n\n")
    return path


def test_uses_cpu_not_coreml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # CoreML cannot run a detector's zero-detection branch; CPU can.
    _fake_runtime(monkeypatch, ["CoreMLExecutionProvider", "CPUExecutionProvider"])
    backend = OnnxBackend(str(_model(tmp_path)))
    session: Any = backend._session
    assert session.providers == ["CPUExecutionProvider"]
    assert backend.gpu is False
    assert backend.classes() == ["car", "sign"]
    assert backend.version == "fasterrcnn"


def test_prefers_cuda(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _fake_runtime(monkeypatch, ["CPUExecutionProvider", "CUDAExecutionProvider"])
    backend = OnnxBackend(str(_model(tmp_path)))
    session: Any = backend._session
    assert session.providers == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert backend.gpu is True
