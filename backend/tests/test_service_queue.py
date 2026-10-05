"""Tests for the API side of the queue (`app.services.queue`) and the worker wiring."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from arq.constants import abort_jobs_ss

from app.api.errors import ServiceUnavailableError
from app.models import Job, JobStatus, JobType
from app.services import queue as queue_module
from app.services.queue import (
    ArqJobQueue,
    JobAlreadyQueuedError,
    QueueUnavailableError,
    aclose_job_queue,
    get_job_queue,
    set_job_queue,
)
from tests.support import FakeJobQueue


class FakePool:
    """Just enough of `ArqRedis` for `ArqJobQueue`."""

    def __init__(self, *, duplicate: bool = False, down: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.duplicate = duplicate
        self.down = down
        self.closed = False

    async def enqueue_job(self, function: str, *args: Any, **kwargs: Any) -> object | None:
        if self.down:
            raise ConnectionError("redis down")
        self.calls.append(("enqueue", (function, args, kwargs)))
        return None if self.duplicate else object()

    def pipeline(self, transaction: bool = True) -> FakePool:
        return self

    async def __aenter__(self) -> FakePool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def delete(self, *keys: str) -> None:
        self.calls.append(("delete", keys))

    def zrem(self, key: str, *members: str) -> None:
        self.calls.append(("zrem", (key, members)))

    async def execute(self) -> list[Any]:
        if self.down:
            raise ConnectionError("redis down")
        return []

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        if self.down:
            raise ConnectionError("redis down")
        self.calls.append(("zadd", (key, mapping)))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def _job() -> Job:
    return Job(id=uuid.uuid4(), type=JobType.EXPORT, status=JobStatus.QUEUED, payload={})


class TestArqJobQueue:
    def test_enqueues_by_job_type_with_the_row_id_as_arq_id(self) -> None:
        pool = FakePool()
        job = _job()
        asyncio.run(ArqJobQueue(pool).enqueue(job))  # type: ignore[arg-type]

        [(name, (function, args, kwargs))] = pool.calls
        assert (name, function) == ("enqueue", "export")
        assert args == (str(job.id),)
        assert kwargs == {"_job_id": str(job.id)}

    def test_duplicate_is_reported(self) -> None:
        with pytest.raises(JobAlreadyQueuedError):
            asyncio.run(ArqJobQueue(FakePool(duplicate=True)).enqueue(_job()))  # type: ignore[arg-type]

    def test_connection_errors_become_queue_unavailable(self) -> None:
        pool = FakePool(down=True)
        with pytest.raises(QueueUnavailableError):
            asyncio.run(ArqJobQueue(pool).enqueue(_job()))  # type: ignore[arg-type]
        with pytest.raises(QueueUnavailableError):
            asyncio.run(ArqJobQueue(pool).abort(uuid.uuid4()))  # type: ignore[arg-type]

    def test_abort_adds_the_id_to_arqs_abort_set(self) -> None:
        pool = FakePool()
        job_id = uuid.uuid4()
        asyncio.run(ArqJobQueue(pool).abort(job_id))  # type: ignore[arg-type]

        [(name, (key, mapping))] = pool.calls
        assert (name, key) == ("zadd", abort_jobs_ss)
        assert list(mapping) == [str(job_id)]

    def test_requeue_clears_stale_keys_before_enqueueing(self) -> None:
        from arq.constants import default_queue_name, job_key_prefix, result_key_prefix

        pool = FakePool()
        job = _job()
        asyncio.run(ArqJobQueue(pool).requeue(job))  # type: ignore[arg-type]

        names = [name for name, _ in pool.calls]
        assert names == ["delete", "zrem", "zrem", "enqueue"]
        assert pool.calls[0][1] == (job_key_prefix + str(job.id), result_key_prefix + str(job.id))
        assert pool.calls[1][1] == (default_queue_name, (str(job.id),))
        assert pool.calls[2][1] == (abort_jobs_ss, (str(job.id),))

    def test_requeue_reports_a_down_redis(self) -> None:
        with pytest.raises(QueueUnavailableError):
            asyncio.run(ArqJobQueue(FakePool(down=True)).requeue(_job()))  # type: ignore[arg-type]

    def test_aclose_closes_the_pool(self) -> None:
        pool = FakePool()
        asyncio.run(ArqJobQueue(pool).aclose())  # type: ignore[arg-type]
        assert pool.closed


class TestProcessQueue:
    def test_get_returns_the_installed_queue_and_aclose_forgets_it(self) -> None:
        fake = FakeJobQueue()
        set_job_queue(fake)
        try:
            assert asyncio.run(get_job_queue()) is fake
            asyncio.run(aclose_job_queue())
            assert queue_module._queue is None
        finally:
            set_job_queue(None)

    def test_unreachable_redis_is_a_503_not_a_500(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _cannot_connect(redis_url: str | None = None) -> ArqJobQueue:
            raise QueueUnavailableError("nope")

        monkeypatch.setattr(queue_module, "create_queue", _cannot_connect)
        set_job_queue(None)
        with pytest.raises(ServiceUnavailableError):
            asyncio.run(get_job_queue())


class TestWorkerWiring:
    def test_every_job_type_has_a_worker_function_of_the_same_name(self) -> None:
        """`ArqJobQueue.enqueue` uses the job type as the arq function name."""
        from app.worker.main import WorkerSettings

        # `import` is a keyword, so that handler is wrapped in `arq.worker.func(name=...)`.
        names = {getattr(fn, "name", None) or fn.__name__ for fn in WorkerSettings.functions}
        assert names == {job_type.value for job_type in JobType}

    def test_worker_can_abort_and_keeps_no_redis_results(self) -> None:
        from app.worker.main import WorkerSettings

        assert WorkerSettings.allow_abort_jobs is True
        assert WorkerSettings.keep_result == 0
        # Outbox publisher (DATA-2) and task lock reaper (WF-3), once a minute
        # each; webhook deliveries (API-4) four times a minute; licence
        # refresh and heartbeat (LIC-27, LIC-6) hourly; stranded-job recovery
        # every five minutes; backlog gauges (OPS-4) and notification e-mail
        # (API-7) every minute.
        assert len(WorkerSettings.cron_jobs) == 7
        assert {job.name for job in WorkerSettings.cron_jobs} == {
            "cron:publish_outbox_events",
            "cron:reap_expired_task_locks",
            "cron:deliver_webhooks",
            "cron:licence_calls",
            "cron:requeue_stranded_jobs",
            "cron:record_backlog",
            "cron:send_notification_emails",
        }

    def test_startup_and_shutdown_wire_the_sessionmaker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.worker import main as worker_main

        disposed: list[bool] = []

        class _Engine:
            async def dispose(self) -> None:
                disposed.append(True)

        monkeypatch.setattr(worker_main, "get_engine", lambda: _Engine())
        monkeypatch.setattr(worker_main, "configure_logging", lambda: None)
        ctx: dict[str, Any] = {}
        asyncio.run(worker_main.startup(ctx))
        assert "sessionmaker" in ctx and "settings" in ctx
        asyncio.run(worker_main.shutdown(ctx))
        assert disposed == [True]
