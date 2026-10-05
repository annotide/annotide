"""Model backends: implementations of `ModelBackend`, selected by `MODEL_BACKEND`."""

from __future__ import annotations

import os

from app.backends.base import ModelBackend


def get_backend(name: str | None = None) -> ModelBackend:
    """Instantiate the backend named by `name`, or the `MODEL_BACKEND` env var.

    Defaults to `heuristic` — the offline, no-weights demo backend. Set
    `MODEL_BACKEND=onnx` (and `MODEL_PATH`) to load a real ONNX detector.
    """
    backend_name = name or os.environ.get("MODEL_BACKEND", "heuristic")

    if backend_name == "heuristic":
        from app.backends.heuristic import HeuristicBackend

        return HeuristicBackend()
    if backend_name == "onnx":
        from app.backends.onnx import OnnxBackend

        return OnnxBackend()

    raise ValueError(f"unknown MODEL_BACKEND '{backend_name}': expected 'heuristic' or 'onnx'")
