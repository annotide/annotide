"""The demo seeder: storage CORS rule (SRC-7) and the model family (EXP-8)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import cast

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import demo
from app.core.config import get_settings
from app.db.base import Base
from app.models import Annotation, Item, Model, ModelVersion, Project, Snapshot


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("address", ["admin", "admin@localhost", "a b@acme.com", "@acme.com"])
async def test_seed_refuses_an_admin_email_that_is_not_an_address(address: str) -> None:
    assert await demo._seed(1, address, "pw") == 2


def test_demo_cors_origins_are_the_app_origins_never_a_wildcard(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("APP_CORS_ORIGINS", "http://localhost:5173,https://annotate.example/")
    monkeypatch.setenv("APP_FRONTEND_URL", "http://localhost:5173")

    origins = demo.demo_cors_origins()

    assert origins == [
        "http://localhost:5173",
        "https://annotate.example",
        demo.E2E_DEV_ORIGIN,
    ]
    assert "*" not in origins


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = cast(
        "list[Table]",
        [t.__table__ for t in (Model, ModelVersion, Project, Item, Annotation, Snapshot)],
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def test_the_model_family_is_teacher_then_distilled_then_quantized_once(
    session: AsyncSession,
) -> None:
    org = uuid.uuid4()
    await demo._seed_model_family(session, org)
    await demo._seed_model_family(session, org)  # idempotent
    await session.commit()

    rows = (
        await session.execute(
            select(ModelVersion, Model.name)
            .join(Model, Model.id == ModelVersion.model_id)
            .order_by(ModelVersion.created_at, Model.name, ModelVersion.version)
        )
    ).all()
    by_derivation = {version.derivation: (version, name) for version, name in rows}
    assert len(rows) == 3
    teacher, teacher_model = by_derivation["trained"]
    student, student_model = by_derivation["distilled"]
    small, _ = by_derivation["quantized"]
    assert teacher_model == demo.DEMO_TEACHER_MODEL_NAME
    assert student_model == demo.DEMO_STUDENT_MODEL_NAME
    assert student.parent_version_id == teacher.id
    assert small.parent_version_id == student.id
    assert small.metrics["dtype"] == "int8"
    assert small.metrics["size_bytes"] < student.metrics["size_bytes"]
    # External producers: prelabelling never offers them.
    models = (await session.scalars(select(Model))).all()
    assert all(model.endpoint_url is None for model in models)
