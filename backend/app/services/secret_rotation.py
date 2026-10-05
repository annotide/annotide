"""Re-seal stored secrets under the current `APP_SECRET_KEY` (rotation step).

During a rotation `APP_SECRET_KEY_PREVIOUS` keeps values sealed with the old
key readable. This moves every sealed value — MFA seeds (`user.totp_secret`)
and webhook signing secrets (`webhook.secret`) — onto the current key, so the
previous one can be removed. Idempotent: running it twice only renews nonces.
A value no key opens is left as it is and counted, never cleared: that
user resets MFA, that hook rotates its secret.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import UnsealError, seal, unseal
from app.models import User, Webhook
from app.services import mfa, webhooks


async def reseal_all(session: AsyncSession) -> dict[str, int]:
    """Re-seal every stored secret; returns counts. Commits."""
    tally = {"mfa": 0, "webhooks": 0, "unreadable": 0}
    for user in await session.scalars(select(User).where(User.totp_secret.is_not(None))):
        assert user.totp_secret is not None
        try:
            plain = unseal(user.totp_secret, purpose=mfa.SEAL_PURPOSE)
        except UnsealError:
            tally["unreadable"] += 1
            continue
        user.totp_secret = seal(plain, purpose=mfa.SEAL_PURPOSE)
        tally["mfa"] += 1
    for hook in await session.scalars(select(Webhook)):
        try:
            plain = webhooks.signing_secret(hook)
        except UnsealError:
            tally["unreadable"] += 1
            continue
        hook.secret = webhooks.seal_secret(plain)
        tally["webhooks"] += 1
    await session.commit()
    return tally
