"""`make dev-licence`: a throwaway Business licence the local and CI stack trust."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services.licensing import devlicence
from app.services.licensing.license import BUSINESS, InvalidLicenseError, parse_and_verify


def _trusted_keys(keys_py: Path) -> dict[str, bytes]:
    namespace: dict[str, object] = {}
    exec(keys_py.read_text(encoding="utf-8"), namespace)
    keys = namespace["VENDOR_PUBLIC_KEYS"]
    assert isinstance(keys, dict)
    assert namespace["REVOKED_LICENSES"] == {}
    return keys


def _licence_key(override: Path) -> str:
    lines = [line.strip() for line in override.read_text(encoding="utf-8").splitlines()]
    values = {
        line.split(": ", 1)[1].strip('"') for line in lines if line.startswith("APP_LICENSE_KEY:")
    }
    assert len(values) == 1, "backend and worker get the same key"
    return values.pop()


def test_writes_a_business_key_only_the_generated_keys_trust(tmp_path: Path) -> None:
    today = datetime(2026, 10, 6, tzinfo=UTC)

    override = devlicence.write(tmp_path / ".dev", today=today)

    keys = _trusted_keys(tmp_path / ".dev" / "keys.py")
    assert len(keys) == 1
    (kid,) = keys
    assert kid.startswith("vdev")
    licence = parse_and_verify(_licence_key(override), public_keys=keys)
    assert licence.tier == BUSINESS
    assert licence.seats == devlicence.SEATS
    assert licence.hosts == ()
    assert licence.expires_at == today.date() + timedelta(days=devlicence.VALID_DAYS)


def test_the_production_keys_do_not_trust_the_dev_key(tmp_path: Path) -> None:
    override = devlicence.write(tmp_path)

    with pytest.raises(InvalidLicenseError):
        parse_and_verify(_licence_key(override), public_keys={"vb4929e71": bytes(32)})


def test_override_mounts_the_generated_keys_into_backend_and_worker(tmp_path: Path) -> None:
    text = devlicence.write(tmp_path).read_text(encoding="utf-8")

    for target in devlicence._CONTAINER_PATHS:
        assert text.count(f"- {devlicence.MOUNT_SOURCE}:{target}:ro") == 2
    assert "\n  backend:\n" in text
    assert "\n  worker:\n" in text


def test_main_reports_the_override(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    devlicence.main(["--out", str(tmp_path)])

    assert "compose.licence.yml" in capsys.readouterr().out
    assert (tmp_path / "keys.py").is_file()
