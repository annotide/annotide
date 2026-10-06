"""The `annotation` command line, over the same fake API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from annotide import Client
from annotide.cli import main
from tests.conftest import FakeApi, job, problem


def run(api: FakeApi, *argv: str) -> int:
    def factory(url: str | None) -> Client:
        assert url is None
        return api.client()

    return main(list(argv), client_factory=factory)


def test_lists_print_json_lines(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json(
        "GET",
        "/api/v1/projects",
        {
            "items": [{"id": "p1", "name": "Cars"}, {"id": "p2", "name": "Ålands"}],
            "next_cursor": None,
        },
    )
    assert run(api, "projects", "list") == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["p1", "p2"]
    assert "Ålands" in lines[1]


def test_whoami_prints_one_object(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json("GET", "/api/v1/auth/me", {"id": "svc1", "is_service": True})
    assert run(api, "whoami") == 0
    assert json.loads(capsys.readouterr().out) == {"id": "svc1", "is_service": True}


def test_snapshot_create_builds_the_split(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json("POST", "/api/v1/projects/p1/snapshots", job("queued", type="snapshot"))
    api.json("GET", "/api/v1/jobs/j1", job(type="snapshot", result={"snapshot_id": "s1"}))
    api.json("GET", "/api/v1/projects/p1/snapshots/s1", {"id": "s1"})
    code = run(
        api,
        "snapshots",
        "create",
        "p1",
        "q3",
        "--split",
        "0.7,0.2,0.1",
        "--seed",
        "4",
        "--group-by",
        "folder",
    )
    assert code == 0
    body = json.loads(api.sent("POST", "/api/v1/projects/p1/snapshots")[0].content)
    assert body["split"] == {"train": 0.7, "val": 0.2, "test": 0.1, "seed": 4, "group_by": "folder"}
    assert json.loads(capsys.readouterr().out) == {"id": "s1"}


def test_export_prints_the_written_path(
    api: FakeApi, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api.json("POST", "/api/v1/projects/p1/exports", job("queued"))
    api.json("GET", "/api/v1/jobs/j1", job())
    api.json("GET", "/api/v1/jobs/j1/download", {"url": "/files/coco.zip", "expires_in": 9})
    api.on("GET", "/files/coco.zip", httpx.Response(200, content=b"zip"))
    out = tmp_path / "d.zip"
    code = run(
        api,
        "export",
        "p1",
        "--format",
        "coco",
        "--snapshot",
        "s1",
        "--split",
        "val",
        "-o",
        str(out),
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == str(out)
    body = json.loads(api.sent("POST", "/api/v1/projects/p1/exports")[0].content)
    assert body == {"format": "coco", "snapshot_id": "s1", "split": "val"}


def test_import_parses_the_class_map(api: FakeApi, tmp_path: Path) -> None:
    source = tmp_path / "l.json"
    source.write_text("{}")
    api.json("POST", "/api/v1/projects/p1/imports/upload", job("queued", type="import"))
    api.json("GET", "/api/v1/jobs/j1", job(type="import"))
    code = run(
        api,
        "import",
        "p1",
        str(source),
        "--format",
        "coco",
        "--dry-run",
        "--class-map",
        '{"car": "vehicle"}',
    )
    assert code == 0
    assert '{"car": "vehicle"}' in api.requests[0].content.decode()


@pytest.mark.parametrize("value", ["not json", "[1, 2]"])
def test_a_bad_class_map_is_a_usage_error(api: FakeApi, value: str) -> None:
    with pytest.raises(SystemExit) as caught:
        run(api, "import", "p1", "f.json", "--format", "coco", "--class-map", value)
    assert caught.value.code == 2


def test_a_bad_split_is_a_usage_error(api: FakeApi) -> None:
    with pytest.raises(SystemExit) as caught:
        run(api, "snapshots", "create", "p1", "q3", "--split", "0.8,0.2")
    assert caught.value.code == 2


def test_jobs_commands(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json("GET", "/api/v1/projects/p1/jobs", {"items": [job()], "next_cursor": None})
    api.json("GET", "/api/v1/jobs/j1", job())
    assert run(api, "jobs", "list", "p1", "--type", "export") == 0
    assert run(api, "jobs", "get", "j1") == 0
    assert run(api, "jobs", "wait", "j1", "--timeout", "5") == 0
    assert api.sent("GET", "/api/v1/projects/p1/jobs")[0].url.params["type"] == "export"
    assert capsys.readouterr().out.count('"id": "j1"') == 3


def test_other_reads(api: FakeApi) -> None:
    api.json("GET", "/api/v1/projects/p1/stats", {"items": {}})
    api.json("GET", "/api/v1/projects/p1/items", {"items": [], "next_cursor": None})
    api.json("GET", "/api/v1/projects/p1/snapshots", {"items": [], "next_cursor": None})
    assert run(api, "projects", "stats", "p1") == 0
    assert run(api, "items", "list", "p1", "--status", "new") == 0
    assert run(api, "snapshots", "list", "p1") == 0


def test_errors_go_to_stderr_with_exit_1(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json(
        "GET", "/api/v1/jobs/j1", problem(404, "Not Found", "Job j1 does not exist."), status=404
    )
    assert run(api, "jobs", "get", "j1") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Job j1 does not exist." in captured.err


def test_a_failed_job_exits_1(api: FakeApi, capsys: pytest.CaptureFixture[str]) -> None:
    api.json("GET", "/api/v1/jobs/j1", job("failed", error="no annotations"))
    assert run(api, "jobs", "wait", "j1") == 1
    assert "no annotations" in capsys.readouterr().err


def test_missing_configuration_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANNOTIDE_URL", raising=False)
    monkeypatch.setenv("ANNOTIDE_API_KEY", "k")
    assert main(["whoami"]) == 1
    assert "ANNOTIDE_URL" in capsys.readouterr().err


def test_mcp_without_the_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    import annotide
    from annotide.cli import main

    # A None entry makes the import fail as if `mcp` were not installed; the
    # package attribute goes too, or an earlier import would be reused.
    monkeypatch.setitem(sys.modules, "annotide.mcp_server", None)
    monkeypatch.delattr(annotide, "mcp_server", raising=False)
    assert main(["mcp"]) == 2
    assert 'pip install "annotide[mcp]"' in capsys.readouterr().err
