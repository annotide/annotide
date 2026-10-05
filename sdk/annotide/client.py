"""Synchronous client for the Annotide REST API (API-3).

Thin by design: one method per endpoint a script needs, plus a few helpers
(`export`, `import_file`, `take_snapshot`, `wait_for_job`) that chain them.
Responses are the API's JSON as plain dicts, typed with the generated
`annotide.models` TypedDicts.
"""

from __future__ import annotations

import json as jsonlib
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, Any, Literal, cast
from urllib.parse import urljoin, urlsplit

import httpx

from annotide.errors import AnnotationError, ApiError, JobFailedError, JobTimeoutError
from annotide.models import (
    AnnotationRead,
    DatasetFilter,
    ImportStatus,
    ItemRead,
    JobRead,
    JobStatusOutput,
    JobTypeOutput,
    LabelSchemaVersionRead,
    ModelVersionCreate,
    ModelVersionRead,
    ProjectRead,
    ProjectStats,
    SnapshotRead,
    TaskRead,
    UserRead,
)

__all__ = ["Client"]

URL_ENV = "ANNOTIDE_URL"
API_KEY_ENV = "ANNOTIDE_API_KEY"

TERMINAL_JOB_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
#: Gateway errors: the request may not have reached the app. Retried only
#: when a repeat is harmless (a read, or a create carrying an Idempotency-Key).
RETRY_STATUSES = frozenset({502, 503, 504})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
MAX_RETRY_AFTER = 60.0
PAGE_SIZE = 100

Split = Literal["train", "val", "test"]


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", "1"))
    except ValueError:
        seconds = 1.0
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _backoff(attempt: int) -> float:
    return float(min(0.5 * 2**attempt, 8.0))


def _problem_reasons(problem: Mapping[str, Any], limit: int = 5) -> str:
    """`loc: msg` for the first few field errors or violations, or ""."""
    reasons: list[str] = []
    for key in ("errors", "violations"):
        entries = problem.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                where = ".".join(str(part) for part in entry.get("loc", []) if part != "body")
                message = str(entry.get("msg") or entry.get("message") or entry)
                reasons.append(f"{where}: {message}" if where else message)
            else:
                reasons.append(str(entry))
    if len(reasons) > limit:
        reasons = [*reasons[:limit], f"and {len(reasons) - limit} more"]
    return "; ".join(reasons)


def _api_error(response: httpx.Response, method: str, path: str) -> ApiError:
    try:
        problem = response.json()
    except ValueError:
        problem = None
    if not isinstance(problem, dict):
        return ApiError(
            response.status_code,
            response.reason_phrase or "Error",
            response.text or None,
            method=method,
            path=path,
        )
    detail = problem.get("detail")
    if not isinstance(detail, str | None):
        # FastAPI's 422 carries a list of validation errors in `detail`.
        detail = str(detail)
    # A 422 names each bad field in `errors` (and QA-6 rule breaks in
    # `violations`): the part a caller, or an agent, needs to fix the request.
    reasons = _problem_reasons(problem)
    if reasons:
        detail = f"{detail}: {reasons}" if detail else reasons
    return ApiError(
        int(problem.get("status") or response.status_code),
        str(problem.get("title") or response.reason_phrase or "Error"),
        detail,
        type=str(problem.get("type") or "about:blank"),
        method=method,
        path=path,
    )


class Client:
    """A connection to one platform install, authenticated with an API key.

    `base_url` is the site root (`https://annotate.example.com`), not the
    `/api/v1` prefix; both it and `api_key` fall back to the
    `ANNOTIDE_URL` / `ANNOTIDE_API_KEY` environment variables. Mint a key
    under Settings → API keys; a CI job should use a service account's key.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        base_url = base_url or os.environ.get(URL_ENV)
        api_key = api_key or os.environ.get(API_KEY_ENV)
        if not base_url:
            raise AnnotationError(f"no base URL: pass base_url or set {URL_ENV}")
        if not api_key:
            raise AnnotationError(f"no API key: pass api_key or set {API_KEY_ENV}")
        self._root = base_url.rstrip("/") + "/"
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=urljoin(self._root, "api/v1/"),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        # Signed download URLs carry their own authorisation: never send the
        # API key to the storage host.
        self._storage = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)

    def close(self) -> None:
        self._http.close()
        self._storage.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[str, IO[bytes], str]] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        repeatable = method in IDEMPOTENT_METHODS or idempotency_key is not None
        attempt = 0
        while True:
            for _, stream, _ in (files or {}).values():
                stream.seek(0)
            try:
                response = self._http.request(
                    method,
                    path.lstrip("/"),
                    params=_drop_none(params or {}),
                    json=json,
                    data=data,
                    files=files,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                # A failed connect never reached the server, so it is always
                # safe to repeat; anything later only when repeatable.
                safe = repeatable or isinstance(exc, httpx.ConnectError)
                if not safe or attempt >= self._max_retries:
                    raise AnnotationError(f"{method} {path}: {exc}") from exc
                self._sleep(_backoff(attempt))
                attempt += 1
                continue
            if attempt < self._max_retries:
                # The rate limiter answers before any handler runs, so a 429
                # is safe to repeat whatever the method.
                if response.status_code == 429:
                    self._sleep(_retry_after(response))
                    attempt += 1
                    continue
                if response.status_code in RETRY_STATUSES and repeatable:
                    self._sleep(_backoff(attempt))
                    attempt += 1
                    continue
            if response.is_error:
                raise _api_error(response, method, path)
            return response

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Call any endpoint under `/api/v1` and return its JSON (None for 204).

        The escape hatch for endpoints without a method here; errors, retries
        and authentication behave as for every other call.
        """
        response = self._send(
            method.upper(), path, params=params, json=json, idempotency_key=idempotency_key
        )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _create(self, path: str, body: Any, idempotency_key: str | None) -> Any:
        """POST a creation. The key makes a retried create answer the first row."""
        return self.request("POST", path, json=body, idempotency_key=idempotency_key or _new_key())

    def _paginate(self, path: str, params: Mapping[str, Any] | None = None) -> Iterator[Any]:
        cursor: str | None = None
        while True:
            page = self.request(
                "GET", path, params={**(params or {}), "limit": PAGE_SIZE, "cursor": cursor}
            )
            yield from page["items"]
            cursor = page.get("next_cursor")
            if not cursor:
                return

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def me(self) -> UserRead:
        """The user (or service account) the API key acts as."""
        return cast(UserRead, self.request("GET", "auth/me"))

    # ------------------------------------------------------------------
    # Projects and items
    # ------------------------------------------------------------------

    def list_projects(self) -> Iterator[ProjectRead]:
        """Every project the caller is a member of, following the cursor."""
        return cast(Iterator[ProjectRead], self._paginate("projects"))

    def get_project(self, project_id: str) -> ProjectRead:
        return cast(ProjectRead, self.request("GET", f"projects/{project_id}"))

    def create_project(
        self, name: str, *, idempotency_key: str | None = None, **fields: Any
    ) -> ProjectRead:
        """Create a project; `fields` are the other `ProjectCreate` keys."""
        body = {"name": name, **fields}
        return cast(ProjectRead, self._create("projects", body, idempotency_key))

    def get_stats(self, project_id: str) -> ProjectStats:
        """Dashboard numbers: items, tasks, annotations, review, class balance."""
        return cast(ProjectStats, self.request("GET", f"projects/{project_id}/stats"))

    def list_items(
        self,
        project_id: str,
        *,
        status: str | None = None,
        media_type: str | None = None,
        q: str | None = None,
    ) -> Iterator[ItemRead]:
        """The project's items, each with a short-lived signed `media_url`."""
        params = {"status": status, "media_type": media_type, "q": q}
        return cast(Iterator[ItemRead], self._paginate(f"projects/{project_id}/items", params))

    def get_item(self, item_id: str) -> ItemRead:
        return cast(ItemRead, self.request("GET", f"items/{item_id}"))

    def list_annotations(self, item_id: str) -> list[AnnotationRead]:
        """Every annotation version of the item, newest first."""
        return cast(list[AnnotationRead], self.request("GET", f"items/{item_id}/annotations"))

    def download_media(self, item: ItemRead, *, max_bytes: int | None = None) -> bytes:
        """The item's media, fetched from storage on its signed `media_url` (ARC-3).

        `max_bytes` refuses anything larger before reading it all.
        """
        url = item.get("media_url")
        if not url:
            raise AnnotationError(f"item {item['id']} has no media URL")
        with self._storage.stream("GET", urljoin(self._root, str(url))) as response:
            if response.is_error:
                response.read()
                raise _api_error(response, "GET", "media download")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise AnnotationError(f"item {item['id']} is larger than {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)

    def list_schema_versions(self, project_id: str) -> list[LabelSchemaVersionRead]:
        """The project's label schema versions, newest first."""
        return cast(
            list[LabelSchemaVersionRead], self.request("GET", f"projects/{project_id}/schemas")
        )

    def create_prelabel(
        self,
        item_id: str,
        *,
        model_version_id: str,
        result: Mapping[str, Any],
        label_schema_version_id: str | None = None,
    ) -> AnnotationRead:
        """Post a pre-label as an external producer (API-8): a model-authored draft."""
        body = _drop_none(
            {
                "model_version_id": model_version_id,
                "result": dict(result),
                "label_schema_version_id": label_schema_version_id,
            }
        )
        return cast(AnnotationRead, self.request("POST", f"items/{item_id}/prelabels", json=body))

    # ------------------------------------------------------------------
    # Tasks (WF-2, WF-3)
    # ------------------------------------------------------------------

    def claim_task(self, project_id: str, *, task_type: str = "annotate") -> TaskRead | None:
        """Claim the next open task (taking its lock), or None when the queue is empty."""
        task = self.request(
            "POST", "tasks/next", params={"project_id": project_id, "type": task_type}
        )
        return cast(TaskRead | None, task)

    def release_task(self, task_id: str) -> TaskRead:
        """Give a claimed task back to the queue."""
        return cast(TaskRead, self.request("POST", f"tasks/{task_id}/release"))

    def scan(self, project_id: str, *, idempotency_key: str | None = None) -> JobRead:
        """Queue a scan of the project's source storage for new items."""
        return cast(JobRead, self._create(f"projects/{project_id}/scan", {}, idempotency_key))

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def list_snapshots(self, project_id: str) -> Iterator[SnapshotRead]:
        return cast(Iterator[SnapshotRead], self._paginate(f"projects/{project_id}/snapshots"))

    def get_snapshot(self, project_id: str, snapshot_id: str) -> SnapshotRead:
        return cast(
            SnapshotRead, self.request("GET", f"projects/{project_id}/snapshots/{snapshot_id}")
        )

    def create_snapshot(
        self,
        project_id: str,
        name: str,
        *,
        filter: DatasetFilter | None = None,  # noqa: A002 - the API's field name
        split: Mapping[str, Any] | None = None,
        label_schema_version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobRead:
        """Queue a snapshot job (EXP-1). `split` is `{train, val, test, seed, group_by}`."""
        body = _drop_none(
            {
                "name": name,
                "filter": filter,
                "split": split,
                "label_schema_version_id": label_schema_version_id,
            }
        )
        return cast(
            JobRead, self._create(f"projects/{project_id}/snapshots", body, idempotency_key)
        )

    def take_snapshot(
        self,
        project_id: str,
        name: str,
        *,
        filter: DatasetFilter | None = None,  # noqa: A002 - the API's field name
        split: Mapping[str, Any] | None = None,
        label_schema_version_id: str | None = None,
        timeout: float = 600.0,
    ) -> SnapshotRead:
        """Create a snapshot, wait for it, and return the frozen snapshot."""
        job = self.create_snapshot(
            project_id,
            name,
            filter=filter,
            split=split,
            label_schema_version_id=label_schema_version_id,
        )
        done = self.wait_for_job(job["id"], timeout=timeout)
        snapshot_id = (done["result"] or {}).get("snapshot_id")
        if not snapshot_id:
            raise AnnotationError(f"snapshot job {done['id']} returned no snapshot_id")
        return self.get_snapshot(project_id, str(snapshot_id))

    # ------------------------------------------------------------------
    # Exports and imports
    # ------------------------------------------------------------------

    def create_export(
        self,
        project_id: str,
        format: str,  # noqa: A002 - the API's field name
        *,
        snapshot_id: str | None = None,
        split: Split | None = None,
        filter: DatasetFilter | None = None,  # noqa: A002 - the API's field name
        label_schema_version_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobRead:
        """Queue an export (EXP-5): `coco`, `yolo` or `native`.

        Export a snapshot for a reproducible dataset; `split` (needs
        `snapshot_id`) exports one partition of a split snapshot.
        """
        body = _drop_none(
            {
                "format": format,
                "snapshot_id": snapshot_id,
                "split": split,
                "filter": filter,
                "label_schema_version_id": label_schema_version_id,
            }
        )
        return cast(JobRead, self._create(f"projects/{project_id}/exports", body, idempotency_key))

    def download_export(self, job_id: str, dest: str | Path) -> Path:
        """Stream a succeeded export's archive to `dest` (a file, or a directory).

        The archive is written to `<dest>.part` first and renamed when
        complete, so an interrupted download never leaves a truncated file.
        """
        link = self.request("GET", f"jobs/{job_id}/download")
        url = urljoin(self._root, str(link["url"]))
        target = Path(dest)
        if target.is_dir():
            name = Path(urlsplit(url).path).name or f"{job_id}.zip"
            target = target / name
        partial = target.with_name(target.name + ".part")
        with self._storage.stream("GET", url) as response:
            if response.is_error:
                response.read()
                raise _api_error(response, "GET", "export download")
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        partial.replace(target)
        return target

    def export(
        self,
        project_id: str,
        format: str,  # noqa: A002 - the API's field name
        dest: str | Path,
        *,
        snapshot_id: str | None = None,
        split: Split | None = None,
        filter: DatasetFilter | None = None,  # noqa: A002 - the API's field name
        timeout: float = 1800.0,
    ) -> Path:
        """Queue an export, wait for it and download the archive to `dest`."""
        job = self.create_export(
            project_id, format, snapshot_id=snapshot_id, split=split, filter=filter
        )
        self.wait_for_job(job["id"], timeout=timeout)
        return self.download_export(job["id"], dest)

    def create_import(
        self,
        project_id: str,
        format: str,  # noqa: A002 - the API's field name
        path: str,
        *,
        connector_id: str | None = None,
        class_mapping: Mapping[str, str | None] | None = None,
        status: ImportStatus | None = None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> JobRead:
        """Queue an import (EXP-6) of a file already in storage, at `path`.

        Formats: `coco`, `yolo`, `voc`, `cvat`, `label_studio`. A `dry_run`
        reports what would be imported without writing anything.
        """
        body = _drop_none(
            {
                "format": format,
                "path": path,
                "connector_id": connector_id,
                "class_mapping": dict(class_mapping) if class_mapping is not None else None,
                "status": status,
                "dry_run": dry_run,
            }
        )
        return cast(JobRead, self._create(f"projects/{project_id}/imports", body, idempotency_key))

    def upload_import(
        self,
        project_id: str,
        file: str | Path,
        format: str,  # noqa: A002 - the API's field name
        *,
        class_mapping: Mapping[str, str | None] | None = None,
        status: ImportStatus | None = None,
        dry_run: bool = False,
    ) -> JobRead:
        """Upload a local annotation file and queue its import (EXP-6).

        This endpoint takes no `Idempotency-Key`, so a gateway error is not
        retried (a repeat could import twice); a 429 or a failed connect is.
        """
        source = Path(file)
        data = _drop_none(
            {
                "format": format,
                "status": status,
                "dry_run": "true" if dry_run else "false",
                "class_mapping": jsonlib.dumps(dict(class_mapping)) if class_mapping else None,
            }
        )
        with source.open("rb") as stream:
            response = self._send(
                "POST",
                f"projects/{project_id}/imports/upload",
                data=data,
                files={"file": (source.name, stream, "application/octet-stream")},
            )
        return cast(JobRead, response.json())

    def import_file(
        self,
        project_id: str,
        file: str | Path,
        format: str,  # noqa: A002 - the API's field name
        *,
        class_mapping: Mapping[str, str | None] | None = None,
        status: ImportStatus | None = None,
        dry_run: bool = False,
        timeout: float = 1800.0,
    ) -> JobRead:
        """Upload and import a local file, wait, and return the finished job.

        The job's `result` carries the import tally (items matched, shapes
        written, unmapped classes).
        """
        job = self.upload_import(
            project_id,
            file,
            format,
            class_mapping=class_mapping,
            status=status,
            dry_run=dry_run,
        )
        return self.wait_for_job(job["id"], timeout=timeout)

    # ------------------------------------------------------------------
    # Models (BYOM-2, EXP-8)
    # ------------------------------------------------------------------

    def create_model_version(self, model_id: str, body: ModelVersionCreate) -> ModelVersionRead:
        """Register a trained version of a model.

        With `snapshot_id` and `snapshot_digest` the version records what it
        was trained on (EXP-8); the platform answers 409 when the digest is
        not that snapshot's. The route takes no `Idempotency-Key`, so a
        gateway error is not retried.
        """
        return cast(
            ModelVersionRead, self.request("POST", f"models/{model_id}/versions", json=body)
        )

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def list_jobs(
        self,
        project_id: str,
        *,
        status: JobStatusOutput | None = None,
        type: JobTypeOutput | None = None,  # noqa: A002 - the API's field name
    ) -> Iterator[JobRead]:
        params = {"status": status, "type": type}
        return cast(Iterator[JobRead], self._paginate(f"projects/{project_id}/jobs", params))

    def get_job(self, job_id: str) -> JobRead:
        return cast(JobRead, self.request("GET", f"jobs/{job_id}"))

    def cancel_job(self, job_id: str) -> JobRead:
        return cast(JobRead, self.request("POST", f"jobs/{job_id}/cancel"))

    def retry_job(self, job_id: str) -> JobRead:
        return cast(JobRead, self.request("POST", f"jobs/{job_id}/retry"))

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 600.0,
        interval: float = 2.0,
        raise_on_failure: bool = True,
    ) -> JobRead:
        """Poll until the job is `succeeded`, `failed` or `cancelled`.

        Raises `JobFailedError` for a job that did not succeed (unless
        `raise_on_failure` is false) and `JobTimeoutError` when `timeout` passes.
        """
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["status"] in TERMINAL_JOB_STATUSES:
                if raise_on_failure and job["status"] != "succeeded":
                    raise JobFailedError(job)
                return job
            if time.monotonic() >= deadline:
                raise JobTimeoutError(job, timeout)
            self._sleep(interval)


def _new_key() -> str:
    return uuid.uuid4().hex
