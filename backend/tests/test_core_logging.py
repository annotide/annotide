"""`core/logging.py`: the redaction processor."""

from __future__ import annotations

import json

import pytest
import structlog

from app.core.logging import REDACTED, configure_logging, redact_sensitive


def _run(**event: object) -> dict[str, object]:
    return dict(redact_sensitive(None, "info", {"event": "x", **event}))


@pytest.mark.parametrize(
    "key",
    [
        "authorization",
        "Authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "api_key",
        "x-api-key",
        "apikey",
        "API_KEY",
        "set-cookie",
        "Cookie",
    ],
)
def test_sensitive_keys_are_masked(key: str) -> None:
    assert _run(**{key: "hunter2"})[key] == REDACTED


def test_other_keys_pass_through() -> None:
    out = _run(user="ana", count=3, path="/x")
    assert out == {"event": "x", "user": "ana", "count": 3, "path": "/x"}


def test_dict_values_are_masked_one_level_down() -> None:
    out = _run(headers={"Authorization": "Bearer abc", "Accept": "json", "n": {"token": "deep"}})
    assert out["headers"] == {
        "Authorization": REDACTED,
        "Accept": "json",
        "n": {"token": "deep"},  # only one level
    }


def test_non_string_keys_in_nested_dicts_are_kept() -> None:
    assert _run(extra={1: "a", "token": "b"})["extra"] == {1: "a", "token": REDACTED}


def test_configured_pipeline_redacts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_LOG_FORMAT", "json")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        configure_logging()
        structlog.get_logger("t").info("login", password="p", user="ana", headers={"cookie": "c"})
    finally:
        get_settings.cache_clear()
        structlog.reset_defaults()
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert record["password"] == REDACTED
    assert record["user"] == "ana"
    assert record["headers"] == {"cookie": REDACTED}
