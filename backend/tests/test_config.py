"""Settings parsing and `.env.example` drift (OPS-1)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CONTRACTS = REPO_ROOT / "docs" / "CONTRACTS.md"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://t:t@localhost/t",
        "secret_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        if line.startswith("APP_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


@pytest.mark.parametrize(
    "key",
    ["", "short-key", "x" * 31, "dev-only-insecure-secret-key-change-me", "dev-only-" + "x" * 40],
)
def test_production_refuses_a_weak_secret_key(key: str) -> None:
    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        _settings(env="production", secret_key=key)


@pytest.mark.parametrize("value", ["*", "http://a.example, *", ["*"]])
def test_a_wildcard_cors_origin_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        _settings(cors_origins=value)


def test_explicit_cors_origins_are_accepted() -> None:
    assert _settings(cors_origins="http://a.example,http://b.example").cors_origins == [
        "http://a.example",
        "http://b.example",
    ]


def test_production_accepts_a_long_random_secret_key() -> None:
    assert _settings(env="production", secret_key="a1" * 32).env == "production"


def test_development_accepts_a_short_secret_key() -> None:
    assert _settings(env="development", secret_key="short").secret_key == "short"


@pytest.mark.parametrize(
    "field",
    ["tile_work_dir", "oidc_issuer", "oidc_admin_groups", "secret_file_root", "aws_region"],
)
def test_an_empty_value_means_unset(field: str) -> None:
    # docker compose passes `${APP_X:-}` as "", and `tempfile` would read a
    # "" work dir as the current directory rather than the system temp dir.
    assert getattr(_settings(**{field: ""}), field) is None
    assert getattr(_settings(**{field: "  "}), field) is None


@pytest.mark.skipif(not ENV_EXAMPLE.is_file(), reason="repository checkout not available")
def test_env_example_lists_every_contract_variable_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    documented = set(re.findall(r"^\| `(APP_[A-Z0-9_]+)`", CONTRACTS.read_text(), re.MULTILINE))
    example = _example_values()
    assert documented - example.keys() == set(), "missing from .env.example"
    assert example.keys() - documented == set(), "in .env.example but not in CONTRACTS.md"

    for key, value in example.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    assert settings.tile_work_dir is None
    assert settings.oidc_enabled is False
