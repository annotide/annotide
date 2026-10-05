"""`services/secret_rotation.py`: re-seal stored secrets after an APP_SECRET_KEY rotation."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.core.security import UnsealError, seal, unseal
from app.db.base import Base
from app.models import Organization, User, Webhook
from app.services import mfa, webhooks
from app.services.secret_rotation import reseal_all


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def test_reseal_moves_secrets_to_the_new_key_and_counts_the_unreadable(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    settings_cache: None,
) -> None:
    old_key = get_settings().secret_key
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug="acme")
        session.add(org)
        await session.flush()
        user = User(organization_id=org.id, email="a@acme.example", display_name="A")
        user.totp_secret = seal("JBSWY3DPEHPK3PXP", purpose=mfa.SEAL_PURPOSE)
        broken = User(organization_id=org.id, email="b@acme.example", display_name="B")
        broken.totp_secret = "not-a-sealed-value"
        hook = Webhook(
            organization_id=org.id,
            url="https://hooks.example/in",
            events=["*"],
            secret=webhooks.seal_secret("s" * 64),
        )
        session.add_all([user, broken, hook])
        await session.commit()
        user_id, hook_id = user.id, hook.id

    monkeypatch.setenv("APP_SECRET_KEY", "the-rotated-secret-key")
    monkeypatch.setenv("APP_SECRET_KEY_PREVIOUS", str(old_key))
    get_settings.cache_clear()
    async with sessionmaker() as session:
        assert await reseal_all(session) == {"mfa": 1, "webhooks": 1, "unreadable": 1}

    # The previous key can go: everything opens with the new one alone.
    monkeypatch.delenv("APP_SECRET_KEY_PREVIOUS")
    get_settings.cache_clear()
    async with sessionmaker() as session:
        resealed = await session.get(User, user_id)
        stored_hook = await session.get(Webhook, hook_id)
    assert resealed is not None and resealed.totp_secret is not None
    assert stored_hook is not None
    assert unseal(resealed.totp_secret, purpose=mfa.SEAL_PURPOSE) == "JBSWY3DPEHPK3PXP"
    assert webhooks.signing_secret(stored_hook) == "s" * 64
    with pytest.raises(UnsealError):
        unseal("not-a-sealed-value", purpose=mfa.SEAL_PURPOSE)
