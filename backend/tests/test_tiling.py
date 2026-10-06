"""Tile pyramid grid math and `POST /items/{id}/tiles/sign` (IMG-1).

CONTRACTS.md `tile_image` and the REST row for `/items/{id}/tiles/sign`. The
job itself is covered in `tests/test_worker.py` (`TestTileImage`).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import Item, Project, ProjectRole
from app.services.tiling import grid_dims, level_dims, max_level_for, validate_tile_coordinate
from tests import test_api_annotations as _shared
from tests.test_parallel_tasks import Maker, _project

app = _shared.app
client = _shared.client
sessionmaker = _shared.sessionmaker

_META: dict[str, Any] = {
    "format": "dzi",
    "tile_size": 256,
    "overlap": 0,
    "suffix": "jpeg",
    "max_level": 12,
    "width": 3000,
    "height": 2000,
}


class TestGridMath:
    def test_levels_follow_deep_zoom(self) -> None:
        assert max_level_for(1, 1) == 0
        assert max_level_for(1000, 700) == 10
        assert max_level_for(3000, 2000) == 12
        assert max_level_for(100_000, 100_000) == 17
        assert level_dims(3000, 2000, 12, 12) == (3000, 2000)
        assert level_dims(3000, 2000, 12, 11) == (1500, 1000)
        assert level_dims(3000, 2000, 12, 0) == (1, 1)
        assert grid_dims(3000, 2000) == (12, 8)
        assert grid_dims(1, 1) == (1, 1)

    @pytest.mark.parametrize(
        ("level", "col", "row"),
        [(13, 0, 0), (-1, 0, 0), (12, 12, 0), (12, 0, 8), (11, 6, 0), (0, 1, 0)],
    )
    def test_out_of_grid_is_refused(self, level: int, col: int, row: int) -> None:
        with pytest.raises(ValueError):
            validate_tile_coordinate(_META, level, col, row)

    def test_edges_of_the_grid_are_fine(self) -> None:
        validate_tile_coordinate(_META, 12, 11, 7)
        validate_tile_coordinate(_META, 11, 5, 3)
        validate_tile_coordinate(_META, 0, 0, 0)


async def _tiled_item(sessionmaker: Maker, *, tiled: bool = True) -> tuple[Any, UUID]:
    project = await _project(sessionmaker)
    async with sessionmaker() as session:
        row = await session.get(Project, project.id)
        assert row is not None
        row.result_connector_id = project.connector_id
        item = Item(
            project_id=project.id,
            connector_id=project.connector_id,
            path="big/slide.tif",
            media_type="image",
            size_bytes=1,
            width=3000,
            height=2000,
            meta={"tiles": {**_META, "path": "cache/tiles/x/"}} if tiled else {},
            status="new",
        )
        session.add(item)
        await session.commit()
        return project, item.id


def _sign(client: TestClient, item_id: UUID, tiles: list[list[int]]) -> Any:
    return client.post(f"/api/v1/items/{item_id}/tiles/sign", json={"tiles": tiles})


class TestSignTiles:
    async def test_returns_one_url_per_tile_in_order(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id = await _tiled_item(sessionmaker)
        _shared._sign_in(app, await project.member(sessionmaker, ProjectRole.ANNOTATOR))

        response = _sign(client, item_id, [[12, 11, 7], [0, 0, 0]])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expires_in"] > 0
        first, second = body["urls"]
        assert f"cache/tiles/{item_id}/image_files/12/11_7.jpeg" in first
        assert f"cache/tiles/{item_id}/image_files/0/0_0.jpeg" in second

    async def test_out_of_grid_is_422_and_untiled_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id = await _tiled_item(sessionmaker)
        _shared._sign_in(app, await project.member(sessionmaker, ProjectRole.ANNOTATOR))
        assert _sign(client, item_id, [[13, 0, 0]]).status_code == 422
        assert _sign(client, item_id, [[12, 12, 0]]).status_code == 422
        assert _sign(client, item_id, []).status_code == 422
        assert _sign(client, item_id, [[0, 0, 0]] * 513).status_code == 422

        _, plain = await _tiled_item(sessionmaker, tiled=False)
        # Another project: the caller is not a member there.
        assert _sign(client, plain, [[0, 0, 0]]).status_code in (403, 404)

    async def test_untiled_item_is_409_for_a_member(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id = await _tiled_item(sessionmaker, tiled=False)
        _shared._sign_in(app, await project.member(sessionmaker, ProjectRole.ANNOTATOR))
        assert _sign(client, item_id, [[0, 0, 0]]).status_code == 409
