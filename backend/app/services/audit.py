"""Append-only audit log (SEC-3).

Every security-relevant change — who signed in, who was given which role,
what was annotated, exported or reconfigured — is one `audit_event` row
written in the *same transaction* as the change it describes, so an audit
entry can never exist for a change that was rolled back, and vice versa.
Rows are never updated or deleted.

Routers call :func:`record` after the change and before `commit()`. The
`before` / `after` snapshots are small dicts the caller chooses; they must
never contain a secret (`secret_ref` is a reference, not a secret, and may
appear; a password hash or token may not).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent


def record(
    session: AsyncSession,
    *,
    organization_id: UUID,
    actor_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Add one audit row to the caller's session. The caller commits.

    `action` is dotted `<target>.<verb>`: `auth.login`, `membership.create`,
    `annotation.review`, `job.create`, `connector.update`, … (the full list is
    in `docs/CONTRACTS.md` → "### audit_event"). `target_type` is the table
    name of the thing acted on.
    """
    event = AuditEvent(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        ip=ip,
    )
    session.add(event)
    return event


__all__ = ["record"]
