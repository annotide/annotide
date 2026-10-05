"""OpenTelemetry wiring (OPS-3): off by default, spans and metrics when on."""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from arq.worker import Retry
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine, text

from app.core import observability
from app.core.config import get_settings
from app.main import create_app
from app.worker.main import WorkerSettings, traced


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    observability.shutdown()
    yield
    observability.shutdown()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def enabled(spans: InMemorySpanExporter, reader: InMemoryMetricReader) -> None:
    observability.configure(
        "annotation-test", span_exporter=spans, metric_reader=reader, set_global=False
    )


def _job_points(reader: InMemoryMetricReader) -> dict[tuple[str, str], int]:
    data = reader.get_metrics_data()
    assert data is not None
    points: dict[tuple[str, str], int] = {}
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != "annotation.worker.job.duration":
                    continue
                for point in metric.data.data_points:
                    attrs = point.attributes or {}
                    points[(str(attrs["job.name"]), str(attrs["outcome"]))] = point.count
    return points


def test_off_by_default_everything_is_a_no_op() -> None:
    assert get_settings().otel_enabled is False
    assert not observability.is_enabled()
    with observability.job_span("scan_source", job_id="j1"):
        pass
    assert observability.add_trace_ids(None, "info", {"event": "x"}) == {"event": "x"}
    observability.instrument_app(FastAPI())
    observability.instrument_engine(create_engine("sqlite://"))


def test_missing_extra_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("opentelemetry"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError, match="otel` extra"):
        observability.configure("annotation-test", set_global=False)
    assert not observability.is_enabled()


@pytest.mark.usefixtures("enabled")
def test_api_requests_are_traced_but_probes_are_not(spans: InMemorySpanExporter) -> None:
    app = create_app(get_settings().model_copy(update={"otel_enabled": True}))
    with TestClient(app) as client:
        client.get("/api/v1/health")
        client.get("/api/v1/auth/providers")

    routes = {span.attributes.get("http.route") for span in spans.get_finished_spans()}
    assert "/api/v1/auth/providers" in routes
    assert "/api/v1/health" not in routes


@pytest.mark.usefixtures("enabled")
def test_the_engine_is_instrumented_once(spans: InMemorySpanExporter) -> None:
    engine = create_engine("sqlite://")
    observability.instrument_engine(engine)
    observability.instrument_engine(engine)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    statements = [s for s in spans.get_finished_spans() if s.attributes.get("db.statement")]
    assert len(statements) == 1


@pytest.mark.usefixtures("enabled")
async def test_job_spans_record_outcomes(
    spans: InMemorySpanExporter, reader: InMemoryMetricReader
) -> None:
    logged: dict[str, Any] = {}

    async def scan_source(ctx: dict[str, Any], job_id: str) -> str:
        logged.update(observability.add_trace_ids(None, "info", {}))
        return job_id

    async def export(ctx: dict[str, Any]) -> None:
        raise Retry(defer=1)

    async def thumbnail(ctx: dict[str, Any]) -> None:
        raise ValueError("bad media")

    ctx = {"job_id": "abc", "job_try": 2}
    assert await traced(scan_source)(ctx, "row-1") == "row-1"
    with pytest.raises(Retry):
        await traced(export)(ctx)
    with pytest.raises(ValueError, match="bad media"):
        await traced(thumbnail, "thumb")(ctx)

    finished = {span.name: span for span in spans.get_finished_spans()}
    ok = finished["job scan_source"]
    assert ok.attributes == {"job.name": "scan_source", "job.id": "abc", "job.try": 2}
    assert logged["trace_id"] == format(ok.context.trace_id, "032x")
    assert logged["span_id"] == format(ok.context.span_id, "016x")
    assert finished["job export"].status.is_ok
    assert finished["job export"].attributes["error.type"] == "Retry"
    assert not finished["job thumb"].status.is_ok
    assert _job_points(reader) == {
        ("scan_source", "ok"): 1,
        ("export", "retry"): 1,
        ("thumb", "error"): 1,
    }


def test_wrapping_keeps_arq_job_names() -> None:
    async def prelabel(ctx: dict[str, Any]) -> None:
        return None

    assert traced(prelabel).__qualname__ == prelabel.__qualname__
    names = {getattr(fn, "name", None) or fn.__name__ for fn in WorkerSettings.functions}
    assert {"scan_source", "import", "thumbnail"} <= names


@pytest.mark.usefixtures("enabled")
async def test_a_queued_job_continues_the_enqueuers_trace(spans: InMemorySpanExporter) -> None:
    """The API request that queues a job and the job's run share one trace."""
    from opentelemetry.trace import SpanKind

    from app.models import Job, JobStatus, JobType
    from app.services.queue import ArqJobQueue
    from tests.test_service_queue import FakePool

    pool = FakePool()
    queue = ArqJobQueue(pool)  # type: ignore[arg-type]  # FakePool stands in for ArqRedis
    job = Job(id=uuid.uuid4(), type=JobType.EXPORT, status=JobStatus.QUEUED, payload={})
    assert observability._state is not None
    with observability._state.tracer.start_as_current_span("POST /jobs", kind=SpanKind.SERVER):
        await queue.enqueue(job)

    [(_, (function, args, kwargs))] = pool.calls
    assert function == "export"
    carrier = kwargs[observability.TRACE_CONTEXT_KWARG]
    assert set(carrier) >= {"traceparent"}

    seen: dict[str, Any] = {}

    async def export(ctx: dict[str, Any], job_id: str) -> None:
        seen["job_id"] = job_id

    # arq consumes its own `_`-prefixed options and passes the rest on.
    job_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
    await traced(export)({"job_id": str(job.id), "job_try": 1}, *args, **job_kwargs)

    assert seen == {"job_id": str(job.id)}
    finished = {span.name: span for span in spans.get_finished_spans()}
    request, run = finished["POST /jobs"], finished["job export"]
    assert run.context.trace_id == request.context.trace_id
    assert run.parent is not None
    assert run.parent.span_id == request.context.span_id


async def test_nothing_extra_is_queued_when_tracing_is_off() -> None:
    from app.models import Job, JobStatus, JobType
    from app.services.queue import ArqJobQueue
    from tests.test_service_queue import FakePool

    pool = FakePool()
    job = Job(id=uuid.uuid4(), type=JobType.EXPORT, status=JobStatus.QUEUED, payload={})
    await ArqJobQueue(pool).enqueue(job)  # type: ignore[arg-type]  # FakePool stands in for ArqRedis

    assert pool.calls == [("enqueue", ("export", (str(job.id),), {"_job_id": str(job.id)}))]
