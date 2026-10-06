"""arq worker entrypoint (ARC-4).

    arq app.worker.main.WorkerSettings

The worker is the same Python package as the API, started with a different
command. It uses the API's engine, settings, logging and secret-store setup,
so a job body can call the same service functions a request handler does.
"""

from __future__ import annotations

import functools
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings
from arq.typing import WorkerCoroutine
from arq.worker import func

from app.core import observability
from app.core.config import BackingServiceAuth, get_settings
from app.core.entra import aclose_token_sources
from app.core.logging import configure_logging, get_logger
from app.core.redis import arq_redis
from app.db.session import get_engine, get_sessionmaker
from app.services.secrets import aclose_backends
from app.services.secrets import configure as configure_secrets
from app.services.storage import aclose_storage
from app.worker.jobs import (
    export,
    extract_text,
    import_annotations,
    prelabel,
    rebuild_cache,
    scan_source,
    snapshot,
    thumbnail,
    tile_image,
)
from app.worker.licensing import licence_calls
from app.worker.locks import reap_expired_task_locks
from app.worker.metrics import record_backlog
from app.worker.notifications import send_notification_emails
from app.worker.outbox import publish_outbox_events
from app.worker.stranded import requeue_stranded_jobs
from app.worker.webhooks import deliver_webhooks

log = get_logger(__name__)


def traced(fn: WorkerCoroutine, name: str | None = None) -> WorkerCoroutine:
    """Run a job or cron body inside `observability.job_span` (OPS-3).

    The span continues the trace that queued the job, when the enqueuer sent
    one (`observability.TRACE_CONTEXT_KWARG`).

    `functools.wraps` keeps `__qualname__`, which arq uses as the job name,
    so wrapping changes nothing about how jobs are enqueued.
    """
    job = name if name is not None else str(getattr(fn, "__name__", "job"))

    @functools.wraps(fn)
    async def wrapper(ctx: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        # The enqueuer's trace context is for the span, not the job body.
        parent = kwargs.pop(observability.TRACE_CONTEXT_KWARG, None)
        with observability.job_span(
            job, job_id=ctx.get("job_id"), job_try=ctx.get("job_try"), parent=parent
        ):
            return await fn(ctx, *args, **kwargs)

    return wrapper


#: arq's per-job timeout; the stranded-job cron waits this long (plus grace)
#: before failing a `running` row the queue no longer holds.
JOB_TIMEOUT_SECONDS = 1800
#: arq tries reserved for waiting on a project's running-job limit (`run_job`).
CAPACITY_WAIT_TRIES = 720

#: On SIGTERM (deploy, scale-down, drain) running jobs get this long to finish
#: before arq cancels them and queues them again (`jobs._after_cancel`). The
#: pod's terminationGracePeriodSeconds and compose's stop_grace_period must
#: be longer, or the process is killed before the job is put back.
SHUTDOWN_JOB_WAIT_SECONDS = 45


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    if settings.otel_enabled:
        observability.configure("annotation-worker")
    configure_logging()
    configure_secrets(
        file_root=settings.secret_file_root,
        azure_default_vault=settings.azure_key_vault_name,
        azure_client_id=settings.managed_identity_client_id,
        aws_default_region=settings.aws_region,
        cache_ttl=settings.secret_cache_ttl,
    )
    ctx["settings"] = settings
    ctx["job_timeout_seconds"] = JOB_TIMEOUT_SECONDS
    ctx["sessionmaker"] = get_sessionmaker()
    if observability.is_enabled():
        observability.instrument_engine(get_engine().sync_engine)
    log.info("worker.startup", env=settings.env, otel=observability.is_enabled())


async def shutdown(ctx: dict[str, Any]) -> None:
    await aclose_storage()
    await aclose_backends()
    await get_engine().dispose()
    await aclose_token_sources()
    observability.shutdown()
    log.info("worker.shutdown")


class WorkerSettings:
    """arq configuration.

    - `functions`: one handler per `job_type` in the `job` table. The function
      name *is* the job type — `app.services.queue` enqueues by that string.
      `import` is a Python keyword, so that handler is registered under its
      job-type name explicitly.
    - `cron_jobs`: the outbox publisher (DATA-2), once a minute. It claims rows
      with `FOR UPDATE SKIP LOCKED`, so several worker replicas can run it.
      The task lock reaper (WF-3) runs once a minute too, offset by 30 s,
      returning expired `in_progress` tasks to the open queue.
    - Every body is wrapped in `traced` for OTel job spans (a no-op when off).
    - `allow_abort_jobs`: lets `POST /jobs/{id}/cancel` stop a running job.
    - `keep_result = 0`: the `job` row already holds the result; keeping a copy
      in Redis would only block a retry from reusing the job id.
    """

    functions: ClassVar[list[Any]] = [
        traced(scan_source),
        traced(tile_image),
        traced(prelabel),
        traced(export),
        traced(snapshot),
        func(traced(import_annotations, "import"), name="import"),
        traced(thumbnail),
        traced(rebuild_cache),
        traced(extract_text),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(traced(publish_outbox_events), second=0, unique=True),
        cron(traced(reap_expired_task_locks), second=30, unique=True),
        # Webhook deliveries (API-4): four times a minute, so a subscriber
        # hears about a submit within ~15 s without a per-event queue job.
        cron(traced(deliver_webhooks), second={5, 20, 35, 50}, unique=True),
        # Notification e-mail (API-7): every minute; a no-op without SMTP.
        cron(traced(send_notification_emails), second=25, unique=True),
        # Licence refresh (LIC-27) and heartbeat (LIC-6): hourly; each is
        # due daily / weekly and decides that itself.
        cron(traced(licence_calls), minute=17, second=10, unique=True),
        # Backlog gauges for the alert rules (OPS-4): every minute, every replica.
        cron(traced(record_backlog), second=45),
        # Rows the queue lost (Redis flushed or restored): every five minutes.
        cron(traced(requeue_stranded_jobs), minute=set(range(2, 60, 5)), second=40, unique=True),
    ]

    on_startup = startup
    on_shutdown = shutdown

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    #: arq builds its pool from `redis_settings`, which knows only a static
    #: password; with Entra sign-in the worker gets one that keeps its token
    #: fresh instead (`app.core.redis`).
    redis_pool = arq_redis() if get_settings().redis_auth is BackingServiceAuth.ENTRA else None

    allow_abort_jobs = True
    # arq counts every hand-back, including a job waiting for a free slot in
    # its project (`run_job`); the real failure limit is `job.attempts`
    # against APP_JOB_MAX_TRIES. The headroom covers ~3 h of 15 s waits; past
    # it the stranded-job sweeper queues the row again.
    max_tries = get_settings().job_max_tries + CAPACITY_WAIT_TRIES
    job_timeout = JOB_TIMEOUT_SECONDS
    job_completion_wait = SHUTDOWN_JOB_WAIT_SECONDS
    keep_result = 0
