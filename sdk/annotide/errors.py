"""Exceptions raised by the SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from annotide.models import JobRead


class AnnotationError(Exception):
    """Base class of every error the SDK raises."""


class ApiError(AnnotationError):
    """The API answered with an error: an RFC 9457 problem (CONTRACTS.md, API-2)."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        type: str = "about:blank",  # noqa: A002 - the problem-details field name
        method: str = "",
        path: str = "",
    ) -> None:
        self.status = status
        self.title = title
        self.detail = detail
        self.type = type
        self.method = method
        self.path = path
        where = f"{method} {path}: " if method else ""
        super().__init__(f"{where}{status} {detail or title}")


class JobFailedError(AnnotationError):
    """A job ended `failed` or `cancelled`. `job` is its final state."""

    def __init__(self, job: JobRead) -> None:
        self.job = job
        reason = f": {job['error']}" if job.get("error") else ""
        super().__init__(f"job {_describe(job)} ended {job.get('status')}{reason}")


class JobTimeoutError(AnnotationError):
    """A job did not finish in time. `job` is the last state seen."""

    def __init__(self, job: JobRead, timeout: float) -> None:
        self.job = job
        super().__init__(f"job {_describe(job)} still {job.get('status')} after {timeout:.0f}s")


def _describe(job: JobRead) -> str:
    # Tolerant of a partial body: an error message must never raise itself.
    kind = job.get("type")
    return f"{job.get('id')} ({kind})" if kind else str(job.get("id"))
