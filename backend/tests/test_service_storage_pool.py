"""The process-wide connector pool behind `storage_for`."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.models import Connector, ConnectorIdentity, ConnectorType
from app.services import storage as storage_module
from app.services.storage import ConnectorPool, storage_for


class Tracked:
    """A connector stand-in that records when it is closed."""

    def __init__(self, config: dict[str, Any], secret: str | None) -> None:
        self.config = config
        self.secret = secret
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[Tracked]:
    instances: list[Tracked] = []

    def build(connector_type: str, config: dict[str, Any], secret: str | None) -> Tracked:
        instance = Tracked(config, secret)
        instances.append(instance)
        return instance

    monkeypatch.setattr(storage_module, "build_connector", build)
    return instances


def _row(tmp_path: Path, **config: Any) -> Connector:
    return Connector(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="media",
        type=ConnectorType.LOCAL,
        identity_type=ConnectorIdentity.NONE,
        config={"root": str(tmp_path), **config},
        secret_ref=None,
    )


async def test_leases_share_one_instance_until_the_row_changes(
    tmp_path: Path, built: list[Tracked]
) -> None:
    row = _row(tmp_path)
    async with storage_for(row) as first:
        pass
    async with storage_for(row) as second:
        pass
    assert first is second
    assert len(built) == 1
    assert not built[0].closed

    row.config = {**row.config, "prefix": "other/"}
    async with storage_for(row) as third:
        assert third is not first
    assert built[0].closed, "the replaced instance is closed"
    assert not built[1].closed


async def test_a_replaced_instance_closes_only_after_its_last_lease(
    tmp_path: Path, built: list[Tracked]
) -> None:
    row = _row(tmp_path)
    async with storage_for(row) as held:
        row.config = {**row.config, "prefix": "other/"}
        async with storage_for(row):
            pass
        assert not built[0].closed, "still leased: not closed under the holder"
    assert held is built[0]
    assert built[0].closed


async def test_the_secret_value_is_part_of_the_key(
    tmp_path: Path, built: list[Tracked], monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = iter(["old-key", "old-key", "rotated-key"])

    async def resolve(ref: str | None) -> str:
        return next(secrets)

    monkeypatch.setattr(storage_module, "resolve_secret", resolve)
    row = _row(tmp_path)
    for _ in range(3):
        async with storage_for(row):
            pass
    assert [b.secret for b in built] == ["old-key", "rotated-key"]


async def test_least_recently_used_idle_instances_are_evicted(
    tmp_path: Path, built: list[Tracked]
) -> None:
    pool = ConnectorPool(max_size=2)
    rows = [_row(tmp_path / str(i)) for i in range(3)]
    for row in rows:
        async with pool.lease(row):
            pass
    assert len(pool) == 2
    assert [b.closed for b in built] == [True, False, False]


async def test_discard_and_shutdown_close_instances(tmp_path: Path, built: list[Tracked]) -> None:
    pool = ConnectorPool()
    kept, dropped = _row(tmp_path / "a"), _row(tmp_path / "b")
    async with pool.lease(kept), pool.lease(dropped):
        pool.discard(dropped.id)
    assert built[1].closed
    assert not built[0].closed

    await pool.aclose()
    assert built[0].closed
    assert len(pool) == 0


async def test_concurrent_leases_build_once(tmp_path: Path, built: list[Tracked]) -> None:
    row = _row(tmp_path)

    async def use() -> object:
        async with storage_for(row) as storage:
            await asyncio.sleep(0)
            return storage

    results = await asyncio.gather(*(use() for _ in range(5)))
    assert len({id(r) for r in results}) == 1
    assert len(built) == 1
