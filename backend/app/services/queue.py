"""The job queue: how the API hands a `job` row to the worker (ARC-4).

Redis + arq carry only the *signal* — "run job `<id>`". Everything about the
job (its type, payload, progress, result) lives in the `job` table, which is
the source of truth the API reads and the worker writes. The arq job id is
the database id, so one row maps to exactly one queue entry and a cancel can
name it.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.constants import (
    abort_jobs_ss,
    default_queue_name,
    job_key_prefix,
    result_key_prefix,
)
from arq.utils import timestamp_ms
from redis.exceptions import RedisError

from app.api.errors import ServiceUnavailableError
from app.core import observability
from app.core.config import BackingServiceAuth, get_settings
from app.core.redis import arq_redis
from app.models import Job


class QueueUnavailableError(RuntimeError):
    """Redis could not be reached; the job row exists but nothing will run it."""


class JobAlreadyQueuedError(RuntimeError):
    """The queue already holds an entry for this job id."""


class JobQueue(Protocol):
    """What the API needs from a queue. `ArqJobQueue` is the real one."""

    async def enqueue(self, job: Job) -> None: ...

    async def requeue(self, job: Job) -> None: ...

    async def abort(self, job_id: UUID) -> None: ...
    async def exists(self, job_id: UUID) -> bool: ...

    async def aclose(self) -> None: ...


def _trace_kwargs() -> dict[str, Any]:
    """The enqueuer's trace context for the worker's job span (OPS-3); nothing when off."""
    carrier = observability.trace_context()
    return {observability.TRACE_CONTEXT_KWARG: carrier} if carrier else {}


class ArqJobQueue:
    """Queue backed by an arq Redis pool."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, job: Job) -> None:
        """Enqueue `job` under its own id. The worker function is the job type."""
        try:
            queued = await self._pool.enqueue_job(
                job.type.value, str(job.id), _job_id=str(job.id), **_trace_kwargs()
            )
        except OSError as exc:  # redis.ConnectionError subclasses OSError
            raise QueueUnavailableError(str(exc)) from exc
        if queued is None:
            raise JobAlreadyQueuedError(f"job {job.id} is already queued")

    async def requeue(self, job: Job) -> None:
        """Enqueue a job the queue may still know about, from a previous attempt.

        A cancelled job can leave an entry behind: its queue member, job key
        and abort flag stay until a worker gets round to it, and until then a
        plain `enqueue` reports a duplicate. The `job` row is the source of
        truth here — the API has already checked it says failed or cancelled —
        so whatever Redis holds under this id is stale and is cleared first.
        The in-progress key is left alone: if a worker is still finishing the
        old attempt, arq will not start the new one until it has.
        """
        job_id = str(job.id)
        try:
            async with self._pool.pipeline(transaction=True) as pipe:
                pipe.delete(job_key_prefix + job_id, result_key_prefix + job_id)
                pipe.zrem(default_queue_name, job_id)
                pipe.zrem(abort_jobs_ss, job_id)
                await pipe.execute()
        except OSError as exc:
            raise QueueUnavailableError(str(exc)) from exc
        await self.enqueue(job)

    async def abort(self, job_id: UUID) -> None:
        """Ask the worker to stop this job if it is running or about to.

        Best-effort and non-blocking: the `job` row is what records the
        cancellation, this only shortens how long a running job keeps going.
        """
        try:
            await self._pool.zadd(abort_jobs_ss, {str(job_id): timestamp_ms()})
        except OSError as exc:
            raise QueueUnavailableError(str(exc)) from exc

    async def exists(self, job_id: UUID) -> bool:
        """Whether arq still holds this job: waiting, deferred for a retry or running.

        arq keeps the job key from enqueue until the job finishes, so a
        `queued` / `running` row without one was lost by Redis.
        """
        try:
            return bool(await self._pool.exists(job_key_prefix + str(job_id)))
        except OSError as exc:
            raise QueueUnavailableError(str(exc)) from exc

    async def aclose(self) -> None:
        await self._pool.aclose()


async def create_queue(redis_url: str | None = None) -> ArqJobQueue:
    """Connect a pool to Redis and wrap it.

    Raises :class:`QueueUnavailableError` when Redis cannot be reached. arq
    would retry five times a second apart by default; a request handler
    cannot wait that long, so this gives up after two tries.
    """
    url = redis_url or get_settings().redis_url
    if get_settings().redis_auth is BackingServiceAuth.ENTRA:
        # arq's create_pool knows only a static password (app.core.redis).
        pool = arq_redis(url)
        try:
            await pool.ping()
        except (OSError, RedisError) as exc:
            await pool.aclose()
            raise QueueUnavailableError(str(exc)) from exc
        return ArqJobQueue(pool)
    settings = RedisSettings.from_dsn(url)
    settings.conn_retries = 2
    try:
        pool = await create_pool(settings)
    except OSError as exc:
        raise QueueUnavailableError(str(exc)) from exc
    return ArqJobQueue(pool)


_queue: JobQueue | None = None


async def get_job_queue() -> JobQueue:
    """FastAPI dependency: one lazily connected queue per process.

    Lazy because tests override this dependency and never touch Redis, and
    because the API should start (and serve `/health`) even when Redis is
    down — only the endpoints that enqueue will then fail, with a 503.
    """
    global _queue
    if _queue is None:
        try:
            _queue = await create_queue()
        except QueueUnavailableError as exc:
            raise ServiceUnavailableError(
                "The job queue is not reachable; try again in a moment."
            ) from exc
    return _queue


async def aclose_job_queue() -> None:
    """Disconnect the process-wide queue, if one was ever opened."""
    global _queue
    if _queue is not None:
        await _queue.aclose()
        _queue = None


def set_job_queue(queue: JobQueue | None) -> None:
    """Replace the process-wide queue. For tests and the worker's own use."""
    global _queue
    _queue = queue
