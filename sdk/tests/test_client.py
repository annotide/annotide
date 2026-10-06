"""Client behaviour against a scripted fake of the API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from annotide import AnnotationError, ApiError, Client, JobFailedError, JobTimeoutError
from annotide.models import ModelVersionCreate
from tests.conftest import BASE, KEY, FakeApi, job, problem


def test_settings_fall_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANNOTIDE_URL", BASE)
    monkeypatch.setenv("ANNOTIDE_API_KEY", KEY)
    Client().close()


@pytest.mark.parametrize(
    ("url", "key", "message"),
    [(None, KEY, "ANNOTIDE_URL"), (BASE, None, "ANNOTIDE_API_KEY")],
)
def test_missing_settings_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, url: str | None, key: str | None, message: str
) -> None:
    monkeypatch.delenv("ANNOTIDE_URL", raising=False)
    monkeypatch.delenv("ANNOTIDE_API_KEY", raising=False)
    with pytest.raises(AnnotationError, match=message):
        Client(url, key)


def test_requests_carry_the_key_under_api_v1(api: FakeApi) -> None:
    api.json("GET", "/api/v1/auth/me", {"id": "u1"})
    with api.client() as client:
        assert client.me()["id"] == "u1"
    request = api.requests[0]
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert str(request.url) == f"{BASE}/api/v1/auth/me"


def test_a_site_under_a_path_keeps_its_prefix(api: FakeApi) -> None:
    api.json("GET", "/tools/annotate/api/v1/auth/me", {"id": "u1"})
    client = Client(f"{BASE}/tools/annotate", KEY, transport=httpx.MockTransport(api.handle))
    assert client.me()["id"] == "u1"


def test_lists_follow_the_cursor_and_drop_unset_filters(api: FakeApi) -> None:
    api.json(
        "GET",
        "/api/v1/projects/p1/items",
        {"items": [{"id": "i1"}, {"id": "i2"}], "next_cursor": "c2"},
        {"items": [{"id": "i3"}], "next_cursor": None},
    )
    with api.client() as client:
        ids = [item["id"] for item in client.list_items("p1", status="new")]
    assert ids == ["i1", "i2", "i3"]
    first, second = api.requests
    assert dict(first.url.params) == {"status": "new", "limit": "100"}
    assert dict(second.url.params) == {"status": "new", "limit": "100", "cursor": "c2"}


def test_problem_details_become_api_errors(api: FakeApi) -> None:
    api.json(
        "GET",
        "/api/v1/jobs/nope",
        problem(404, "Not Found", "Job nope does not exist."),
        status=404,
    )
    with api.client() as client, pytest.raises(ApiError) as caught:
        client.get_job("nope")
    error = caught.value
    assert (error.status, error.title, error.detail) == (
        404,
        "Not Found",
        "Job nope does not exist.",
    )
    assert str(error) == "GET jobs/nope: 404 Job nope does not exist."


def test_validation_errors_keep_their_detail(api: FakeApi) -> None:
    detail = [{"loc": ["body", "format"], "msg": "Field required"}]
    api.json("POST", "/api/v1/projects/p1/exports", {"detail": detail}, status=422)
    with api.client() as client, pytest.raises(ApiError) as caught:
        client.create_export("p1", "")
    assert caught.value.status == 422
    assert "Field required" in (caught.value.detail or "")


def test_a_non_json_error_still_raises(api: FakeApi) -> None:
    api.on("GET", "/api/v1/jobs/j1", httpx.Response(500, text="boom"))
    with api.client() as client, pytest.raises(ApiError) as caught:
        client.get_job("j1")
    assert (caught.value.status, caught.value.detail) == (500, "boom")


def test_rate_limits_wait_for_retry_after(api: FakeApi) -> None:
    limited = httpx.Response(429, headers={"Retry-After": "7"}, json=problem(429, "Too Many"))
    api.on("POST", "/api/v1/jobs/j1/cancel", limited, httpx.Response(200, json=job("cancelled")))
    with api.client() as client:
        assert client.cancel_job("j1")["status"] == "cancelled"
    # A 429 is answered before any handler runs, so even a plain POST repeats.
    assert api.sleeps == [7.0]
    assert len(api.sent("POST", "/api/v1/jobs/j1/cancel")) == 2


def test_retry_after_is_capped(api: FakeApi) -> None:
    limited = httpx.Response(429, headers={"Retry-After": "3600"}, json=problem(429, "Too Many"))
    api.on("GET", "/api/v1/jobs/j1", limited, httpx.Response(200, json=job()))
    with api.client() as client:
        client.get_job("j1")
    assert api.sleeps == [60.0]


def test_reads_retry_gateway_errors_with_backoff(api: FakeApi) -> None:
    api.on(
        "GET",
        "/api/v1/jobs/j1",
        httpx.Response(503),
        httpx.Response(502),
        httpx.Response(200, json=job()),
    )
    with api.client() as client:
        assert client.get_job("j1")["status"] == "succeeded"
    assert api.sleeps == [0.5, 1.0]


def test_retries_stop_at_max_retries(api: FakeApi) -> None:
    api.on("GET", "/api/v1/jobs/j1", httpx.Response(503))
    with api.client(max_retries=2) as client, pytest.raises(ApiError) as caught:
        client.get_job("j1")
    assert caught.value.status == 503
    assert len(api.requests) == 3


def test_creates_send_one_idempotency_key_across_retries(api: FakeApi) -> None:
    api.on(
        "POST",
        "/api/v1/projects/p1/exports",
        httpx.Response(504),
        httpx.Response(202, json=job("queued")),
    )
    with api.client() as client:
        client.create_export("p1", "coco", snapshot_id="s1")
    first, second = api.requests
    assert first.headers["Idempotency-Key"]
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert json.loads(second.content) == {"format": "coco", "snapshot_id": "s1"}


def test_a_caller_key_is_sent_as_given(api: FakeApi) -> None:
    api.json("POST", "/api/v1/projects", {"id": "p1", "name": "Cars"})
    with api.client() as client:
        client.create_project("Cars", idempotency_key="nightly-2026-09-25", description="x")
    request = api.requests[0]
    assert request.headers["Idempotency-Key"] == "nightly-2026-09-25"
    assert json.loads(request.content) == {"name": "Cars", "description": "x"}


def test_plain_posts_do_not_retry_gateway_errors(api: FakeApi) -> None:
    api.on("POST", "/api/v1/jobs/j1/retry", httpx.Response(502))
    with api.client() as client, pytest.raises(ApiError):
        client.retry_job("j1")
    assert len(api.requests) == 1
    assert "Idempotency-Key" not in api.requests[0].headers


def test_connect_errors_retry_even_for_plain_posts(api: FakeApi) -> None:
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=job("queued"))

    api.on("POST", "/api/v1/jobs/j1/retry", flaky)
    with api.client() as client:
        assert client.retry_job("j1")["status"] == "queued"
    assert calls == 2


def test_a_read_timeout_on_a_plain_post_is_not_repeated(api: FakeApi) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api.on("POST", "/api/v1/jobs/j1/retry", slow)
    with api.client() as client, pytest.raises(AnnotationError, match="slow"):
        client.retry_job("j1")
    assert len(api.requests) == 1


def test_request_returns_none_for_no_content(api: FakeApi) -> None:
    api.on("DELETE", "/api/v1/webhooks/w1", httpx.Response(204))
    with api.client() as client:
        assert client.request("delete", "/webhooks/w1") is None


def test_wait_for_job_polls_until_done(api: FakeApi) -> None:
    api.json("GET", "/api/v1/jobs/j1", job("queued"), job("running"), job("succeeded"))
    with api.client() as client:
        assert client.wait_for_job("j1", interval=5)["status"] == "succeeded"
    assert api.sleeps == [5, 5]


def test_wait_for_job_raises_for_a_failed_job(api: FakeApi) -> None:
    api.json("GET", "/api/v1/jobs/j1", job("failed", error="bad format"))
    with api.client() as client:
        with pytest.raises(JobFailedError, match="bad format") as caught:
            client.wait_for_job("j1")
        assert caught.value.job["status"] == "failed"
        assert client.wait_for_job("j1", raise_on_failure=False)["status"] == "failed"


def test_wait_for_job_gives_up_at_the_timeout(api: FakeApi) -> None:
    api.json("GET", "/api/v1/jobs/j1", job("running"))
    with api.client() as client, pytest.raises(JobTimeoutError) as caught:
        client.wait_for_job("j1", timeout=0)
    assert caught.value.job["status"] == "running"


def test_take_snapshot_waits_and_returns_the_snapshot(api: FakeApi) -> None:
    api.json("POST", "/api/v1/projects/p1/snapshots", job("queued", type="snapshot"))
    api.json("GET", "/api/v1/jobs/j1", job(type="snapshot", result={"snapshot_id": "s1"}))
    api.json("GET", "/api/v1/projects/p1/snapshots/s1", {"id": "s1", "name": "q3"})
    split = {"train": 0.8, "val": 0.1, "test": 0.1, "seed": 0, "group_by": None}
    with api.client() as client:
        assert client.take_snapshot("p1", "q3", split=split)["id"] == "s1"
    body = json.loads(api.sent("POST", "/api/v1/projects/p1/snapshots")[0].content)
    assert body == {"name": "q3", "split": split}


def test_take_snapshot_without_a_snapshot_id_fails_loudly(api: FakeApi) -> None:
    api.json("POST", "/api/v1/projects/p1/snapshots", job("queued", type="snapshot"))
    api.json("GET", "/api/v1/jobs/j1", job(type="snapshot", result={}))
    with api.client() as client, pytest.raises(AnnotationError, match="no snapshot_id"):
        client.take_snapshot("p1", "q3")


def _serve_export(api: FakeApi, url: str, body: bytes = b"PK-archive") -> None:
    api.json("POST", "/api/v1/projects/p1/exports", job("queued"))
    api.json("GET", "/api/v1/jobs/j1", job())
    api.json("GET", "/api/v1/jobs/j1/download", {"url": url, "expires_in": 900})
    path = (
        httpx.URL(
            url,
        ).path
        if url.startswith("http")
        else url.split("?")[0]
    )
    api.on("GET", path, httpx.Response(200, content=body))


def test_export_downloads_without_sending_the_key(api: FakeApi, tmp_path: Path) -> None:
    _serve_export(api, "/api/v1/storage/local/c1/exports/j1/coco.zip?expires=1&sig=x")
    with api.client() as client:
        written = client.export("p1", "coco", tmp_path / "dataset.zip", snapshot_id="s1")
    assert written == tmp_path / "dataset.zip"
    assert written.read_bytes() == b"PK-archive"
    assert not (tmp_path / "dataset.zip.part").exists()
    download = api.requests[-1]
    # A relative URL resolves against the site root, and the signature is the
    # only authorisation the storage host gets.
    assert str(download.url).startswith(f"{BASE}/api/v1/storage/local/c1/")
    assert "Authorization" not in download.headers


def test_export_into_a_directory_uses_the_archive_name(api: FakeApi, tmp_path: Path) -> None:
    _serve_export(api, "https://blob.example.net/results/exports/j1/coco.zip?sv=1&sig=x")
    with api.client() as client:
        written = client.export("p1", "coco", tmp_path)
    assert written == tmp_path / "coco.zip"


def test_a_refused_download_leaves_no_file(api: FakeApi, tmp_path: Path) -> None:
    api.json(
        "GET",
        "/api/v1/jobs/j1/download",
        {"url": "https://blob.example.net/x.zip", "expires_in": 1},
    )
    api.on("GET", "/x.zip", httpx.Response(403, text="AuthenticationFailed"))
    with api.client() as client, pytest.raises(ApiError) as caught:
        client.download_export("j1", tmp_path / "out.zip")
    assert caught.value.status == 403
    assert list(tmp_path.iterdir()) == []


def test_create_import_sends_only_set_fields(api: FakeApi) -> None:
    api.json("POST", "/api/v1/projects/p1/imports", job("queued", type="import"))
    with api.client() as client:
        client.create_import("p1", "coco", "labels/train.json", class_mapping={"car": "vehicle"})
    body = json.loads(api.requests[0].content)
    assert body == {
        "format": "coco",
        "path": "labels/train.json",
        "class_mapping": {"car": "vehicle"},
        "dry_run": False,
    }


def test_import_file_uploads_and_waits(api: FakeApi, tmp_path: Path) -> None:
    source = tmp_path / "labels.json"
    source.write_text('{"images": []}')
    api.json("POST", "/api/v1/projects/p1/imports/upload", job("queued", type="import"))
    api.json("GET", "/api/v1/jobs/j1", job(type="import", result={"items_matched": 0}))
    with api.client() as client:
        done = client.import_file(
            "p1", source, "coco", class_mapping={"car": None}, status="draft", dry_run=True
        )
    assert done["result"] == {"items_matched": 0}
    upload = api.sent("POST", "/api/v1/projects/p1/imports/upload")[0]
    content = upload.content.decode()
    assert 'filename="labels.json"' in content
    assert '{"images": []}' in content
    assert 'name="dry_run"\r\n\r\ntrue' in content
    assert 'name="status"\r\n\r\ndraft' in content
    assert '{"car": null}' in content
    # The upload route takes no Idempotency-Key: a repeat could import twice.
    assert "Idempotency-Key" not in upload.headers


def test_an_upload_is_resent_whole_after_a_rate_limit(api: FakeApi, tmp_path: Path) -> None:
    source = tmp_path / "labels.json"
    source.write_text("0123456789")
    limited = httpx.Response(429, headers={"Retry-After": "1"}, json=problem(429, "Too Many"))
    api.on(
        "POST",
        "/api/v1/projects/p1/imports/upload",
        limited,
        httpx.Response(202, json=job("queued")),
    )
    with api.client() as client:
        client.upload_import("p1", source, "coco")
    first, second = api.requests
    assert b"0123456789" in first.content
    assert b"0123456789" in second.content


def test_simple_reads_hit_their_routes(api: FakeApi) -> None:
    api.json("GET", "/api/v1/projects", {"items": [{"id": "p1"}], "next_cursor": None})
    api.json("GET", "/api/v1/projects/p1", {"id": "p1"})
    api.json("GET", "/api/v1/projects/p1/stats", {"items": {}})
    api.json("GET", "/api/v1/items/i1", {"id": "i1"})
    api.json("GET", "/api/v1/items/i1/annotations", [{"id": "a1"}])
    api.json("GET", "/api/v1/projects/p1/snapshots", {"items": [], "next_cursor": None})
    api.json("GET", "/api/v1/projects/p1/jobs", {"items": [job()], "next_cursor": None})
    api.json("POST", "/api/v1/projects/p1/scan", job("queued", type="scan_source"))
    with api.client() as client:
        assert [p["id"] for p in client.list_projects()] == ["p1"]
        assert client.get_project("p1")["id"] == "p1"
        assert "items" in client.get_stats("p1")
        assert client.get_item("i1")["id"] == "i1"
        assert client.list_annotations("i1")[0]["id"] == "a1"
        assert list(client.list_snapshots("p1")) == []
        assert len(list(client.list_jobs("p1", status="succeeded"))) == 1
        assert client.scan("p1")["type"] == "scan_source"
    assert api.sent("GET", "/api/v1/projects/p1/jobs")[0].url.params["status"] == "succeeded"
    assert api.sent("POST", "/api/v1/projects/p1/scan")[0].headers["Idempotency-Key"]


def test_create_model_version_posts_the_lineage(api: FakeApi) -> None:
    api.json("POST", "/api/v1/models/m1/versions", {"id": "v1", "version": 3}, status=201)
    body: ModelVersionCreate = {
        "snapshot_id": "s1",
        "snapshot_digest": "d" * 64,
        "metrics": {"map50": 0.5},
        "training_run": {"id": "r1"},
    }
    with api.client() as client:
        assert client.create_model_version("m1", body)["version"] == 3
    request = api.requests[0]
    assert json.loads(request.content) == body
    assert "Idempotency-Key" not in request.headers


def test_job_errors_tolerate_a_partial_body(api: FakeApi) -> None:
    api.json("GET", "/api/v1/jobs/j1", {"id": "j1", "status": "failed"})
    with api.client() as client, pytest.raises(JobFailedError, match="job j1 ended failed"):
        client.wait_for_job("j1")


def test_a_422_names_the_bad_fields(api: FakeApi) -> None:
    api.json(
        "POST",
        "/api/v1/items/i1/prelabels",
        {
            "title": "Request body failed validation",
            "status": 422,
            "detail": "One or more fields are invalid.",
            "errors": [
                {"loc": ["body", "result", "shapes", 0, "id"], "msg": "Input should be a UUID"}
            ],
        },
        status=422,
    )
    with api.client() as client, pytest.raises(ApiError) as caught:
        client.create_prelabel("i1", model_version_id="m", result={})
    assert "result.shapes.0.id: Input should be a UUID" in str(caught.value)
