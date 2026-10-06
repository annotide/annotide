"""OpenTelemetry tracing and metrics (OPS-3), optional.

Off unless ``APP_OTEL_ENABLED`` is true. The packages come from the backend's
``otel`` extra (``pip install ".[otel]"``, or the image built with
``--build-arg EXTRAS=otel``); the default image does not carry them, and
turning the flag on without them fails at startup rather than silently.

Everything else is the standard OpenTelemetry environment: OTLP over HTTP to
``OTEL_EXPORTER_OTLP_ENDPOINT`` (default ``http://localhost:4318``), plus
``OTEL_EXPORTER_OTLP_HEADERS``, ``OTEL_SERVICE_NAME``,
``OTEL_RESOURCE_ATTRIBUTES`` and ``OTEL_TRACES_SAMPLER``. Data goes to the
operator's collector only; this is not the vendor telemetry of LIC-16.

What is instrumented: FastAPI requests, SQLAlchemy statements, httpx calls
(connectors, model endpoints, webhooks) and Redis commands, plus one span and
a duration histogram per worker job (``job_span``). Log lines carry
``trace_id`` / ``span_id`` while a span is active (``add_trace_ids``).

Queue health is exported as gauges (``BACKLOG_GAUGES``): a worker cron counts
the backlog in the database once a minute and hands the numbers to
``record_backlog``; a process reports them only while they are fresh, so the
series of a worker that stopped counting disappears instead of going stale.
HTTP metrics follow the stable semantic conventions
(``http.server.request.duration`` in seconds) unless
``OTEL_SEMCONV_STABILITY_OPT_IN`` says otherwise. The Helm chart's alert rules
(OPS-4) are written against these names.

A job's span continues the trace of whatever queued it: the enqueuer passes
``trace_context()`` (W3C ``traceparent`` / ``tracestate``) as the arq keyword
``TRACE_CONTEXT_KWARG`` and the worker hands it to ``job_span``. The `job` row
is still the source of truth; the carrier only links spans.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentelemetry.metrics import Histogram, MeterProvider
    from opentelemetry.sdk.metrics.export import MetricReader
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Tracer, TracerProvider
    from sqlalchemy.engine import Engine

INSTRUMENTATION_NAME = "annotide"

#: The arq keyword argument that carries the enqueuer's trace context.
TRACE_CONTEXT_KWARG = "otel_context"

MISSING_EXTRA = (
    "APP_OTEL_ENABLED is true but the OpenTelemetry packages are not installed. "
    'Install the backend with the `otel` extra (pip install ".[otel]") or build '
    "the image with --build-arg EXTRAS=otel."
)


#: Backlog gauges: name → (unit, description). Values come from
#: ``record_backlog``, keyed by name.
BACKLOG_GAUGES: dict[str, tuple[str, str]] = {
    "annotation.jobs.queued": ("{job}", "Jobs waiting for a worker"),
    "annotation.jobs.running": ("{job}", "Jobs a worker is running"),
    "annotation.jobs.queued.oldest_age": ("s", "How long the oldest queued job has waited"),
    "annotation.outbox.pending": ("{event}", "Outbox events not yet published"),
    "annotation.outbox.pending.oldest_age": ("s", "Age of the oldest unpublished outbox event"),
    "annotation.outbox.dead": ("{event}", "Outbox events given up after the maximum attempts"),
}

#: A backlog reading older than this is not reported (the cron runs every minute).
BACKLOG_FRESH_SECONDS = 180.0


@dataclass
class _State:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    tracer: Tracer
    job_duration: Histogram
    engine_instrumented: bool = False
    backlog: dict[str, float] = field(default_factory=dict)
    backlog_at: float = float("-inf")


_state: _State | None = None


def is_enabled() -> bool:
    return _state is not None


def configure(
    service_name: str,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    set_global: bool = True,
) -> None:
    """Build the providers once per process; later calls are no-ops.

    Tests pass in-memory exporters and ``set_global=False``, because the
    global providers can be set only once per process.
    """
    global _state
    if _state is not None:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError as exc:
        raise RuntimeError(MISSING_EXTRA) from exc

    # Stable HTTP metric names (seconds, `http.response.status_code`) for the
    # alert rules; an operator's own OTEL_SEMCONV_STABILITY_OPT_IN wins.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    # OTEL_SERVICE_NAME, when set, wins over our default name.
    attributes = {} if os.environ.get("OTEL_SERVICE_NAME") else {SERVICE_NAME: service_name}
    resource = Resource.create(attributes)

    tracer_provider = SdkTracerProvider(resource=resource)
    if span_exporter is None:
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    else:
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    reader = metric_reader or PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = SdkMeterProvider(resource=resource, metric_readers=[reader])

    if set_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        HTTPXClientInstrumentor().instrument(
            tracer_provider=tracer_provider, meter_provider=meter_provider
        )
        RedisInstrumentor().instrument(tracer_provider=tracer_provider)

    meter = meter_provider.get_meter(INSTRUMENTATION_NAME)
    for name, (unit, description) in BACKLOG_GAUGES.items():
        meter.create_observable_gauge(
            name, callbacks=[_backlog_callback(name)], unit=unit, description=description
        )
    _state = _State(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        tracer=tracer_provider.get_tracer(INSTRUMENTATION_NAME),
        job_duration=meter.create_histogram(
            "annotation.worker.job.duration",
            unit="s",
            description="Wall time of one worker job run, by job and outcome",
        ),
    )


def instrument_app(app: Any) -> None:
    """Add request spans and HTTP server metrics to a FastAPI app."""
    if _state is None:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=_state.tracer_provider,
        meter_provider=_state.meter_provider,
        # Liveness and readiness probes would drown the traces.
        excluded_urls="/api/v1/health,/api/v1/ready",
    )


def instrument_engine(engine: Engine) -> None:
    """Add statement spans to the process's (sync) SQLAlchemy engine.

    The instrumentor is a process-wide singleton that ignores a second
    ``instrument()``, so only the first engine counts, which is the one
    ``app.db.session`` creates.
    """
    if _state is None or _state.engine_instrumented:
        return
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(
        engine=engine,
        tracer_provider=_state.tracer_provider,
        meter_provider=_state.meter_provider,
        enable_commenter=False,
    )
    _state.engine_instrumented = True


def record_backlog(values: Mapping[str, float]) -> None:
    """Hand the gauges a fresh backlog reading (see ``BACKLOG_GAUGES``); no-op when off."""
    if _state is None:
        return
    _state.backlog = {name: float(values[name]) for name in BACKLOG_GAUGES if name in values}
    _state.backlog_at = time.monotonic()


def _backlog_callback(name: str) -> Any:
    from opentelemetry.metrics import CallbackOptions, Observation

    def observe(options: CallbackOptions) -> list[Observation]:
        state = _state
        if state is None or time.monotonic() - state.backlog_at > BACKLOG_FRESH_SECONDS:
            return []
        value = state.backlog.get(name)
        return [] if value is None else [Observation(value)]

    return observe


def trace_context() -> dict[str, str]:
    """The active span's W3C trace context, to carry into a queued job; empty when off."""
    if _state is None:
        return {}
    from opentelemetry.propagate import inject

    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier


@contextmanager
def job_span(
    job: str,
    *,
    job_id: str | None = None,
    job_try: int | None = None,
    parent: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """One span and one duration sample per worker job run; no-op when off.

    ``parent`` is the carrier from ``trace_context()`` at enqueue time; the
    job's span becomes a child of that span, in the same trace.
    """
    if _state is None:
        yield
        return
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind, Status, StatusCode

    attributes: dict[str, str | int] = {"job.name": job}
    if job_id is not None:
        attributes["job.id"] = job_id
    if job_try is not None:
        attributes["job.try"] = job_try
    outcome = "ok"
    started = time.perf_counter()
    with _state.tracer.start_as_current_span(
        f"job {job}",
        context=extract(parent) if parent else None,
        kind=SpanKind.CONSUMER,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield
        except BaseException as exc:
            # arq's Retry is how a transient failure asks for another try.
            outcome = "retry" if type(exc).__name__ == "Retry" else "error"
            span.set_attribute("error.type", type(exc).__name__)
            if outcome == "error":
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            raise
        finally:
            _state.job_duration.record(
                time.perf_counter() - started, {"job.name": job, "outcome": outcome}
            )


def add_trace_ids(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: correlate log lines with the active span."""
    if _state is None:
        return event_dict
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def shutdown() -> None:
    """Flush and stop the exporters (process shutdown)."""
    global _state
    if _state is None:
        return
    shutdown_tracer = getattr(_state.tracer_provider, "shutdown", None)
    shutdown_meter = getattr(_state.meter_provider, "shutdown", None)
    if callable(shutdown_tracer):
        shutdown_tracer()
    if callable(shutdown_meter):
        shutdown_meter()
    if _state.engine_instrumented:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().uninstrument()
    _state = None
