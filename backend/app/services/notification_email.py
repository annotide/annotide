"""E-mail copies of in-app notifications (API-7).

The `notification` row stays the source of truth (WF-5); this mails the ones
not yet mailed to people who have not opted out, over the customer's own SMTP
server (`APP_SMTP_*`). Standard library only (`smtplib`, run in a thread).

Rules, all in `send_due`:

- nothing without `APP_SMTP_HOST`;
- only notifications younger than `APP_NOTIFICATION_EMAIL_MAX_AGE`, so
  turning e-mail on never mails the backlog;
- only to active, human, not erased users with `email_notifications`;
- a failed send leaves the row for the next tick, up to `MAX_ATTEMPTS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import smtplib
import ssl
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Notification, NotificationType, Project, User

log = structlog.get_logger(__name__)

#: A notification that failed this many times is not tried again.
MAX_ATTEMPTS = 5
#: Rows one tick mails; the rest wait for the next minute.
BATCH_SIZE = 100


class Mailer(Protocol):
    """Sends a batch; returns one error (or None) per message, in order."""

    async def send(self, messages: Sequence[EmailMessage]) -> list[str | None]: ...


class SmtpMailer:
    """One SMTP connection per batch, in a worker thread."""

    def __init__(self, settings: Settings, *, timeout: float = 30.0) -> None:
        self._settings = settings
        self._timeout = timeout

    def _connect(self) -> smtplib.SMTP:
        settings = self._settings
        host = str(settings.smtp_host)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if settings.smtp_security == "ssl":
            smtp = smtplib.SMTP_SSL(
                host, settings.smtp_port, timeout=self._timeout, context=context
            )
        else:
            smtp = smtplib.SMTP(host, settings.smtp_port, timeout=self._timeout)
            if settings.smtp_security == "starttls":
                smtp.starttls(context=context)
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password or "")
        return smtp

    def _send_all(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        try:
            smtp = self._connect()
        except (OSError, smtplib.SMTPException) as exc:
            error = f"{type(exc).__name__}: {exc}"
            return [error] * len(messages)
        results: list[str | None] = []
        try:
            for message in messages:
                try:
                    smtp.send_message(message)
                    results.append(None)
                except smtplib.SMTPException as exc:
                    results.append(f"{type(exc).__name__}: {exc}")
        finally:
            with contextlib.suppress(OSError, smtplib.SMTPException):
                smtp.quit()
        return results

    async def send(self, messages: Sequence[EmailMessage]) -> list[str | None]:
        return await asyncio.to_thread(self._send_all, messages)


def compose(
    notification: Notification,
    recipient: User,
    *,
    actor: str,
    project: str,
    settings: Settings,
) -> EmailMessage:
    """The e-mail for one notification: plain text, a subject and a link."""
    payload = notification.payload
    project_id = payload.get("project_id")
    item_id = payload.get("item_id")
    link = f"{settings.frontend_url.rstrip('/')}/projects/{project_id}/annotate/{item_id}"

    if notification.type is NotificationType.MENTION:
        subject = f"{actor} mentioned you in {project}"
        quote = payload.get("excerpt")
    elif notification.type is NotificationType.REPLY:
        subject = f"{actor} replied to your comment in {project}"
        quote = payload.get("excerpt")
    else:
        verdict = "approved" if payload.get("approve") else "returned for changes"
        subject = f"Your annotation was {verdict} in {project}"
        quote = payload.get("comment")

    # Names come from people: a line break would be a header injection, and
    # EmailMessage refuses it outright, failing the whole batch.
    subject = " ".join(subject.split())
    lines = [f"{subject}.", ""]
    if quote:
        lines += [f"> {line}" for line in str(quote).splitlines()] + [""]
    lines += [
        f"Open it: {link}",
        "",
        "You get these e-mails because notifications are on for your account.",
        f"Turn them off under Security: {settings.frontend_url.rstrip('/')}/settings/security",
    ]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = recipient.email
    message["Message-ID"] = make_msgid(domain="annotation.local")
    # The notification id lets a mail system thread or de-duplicate.
    message["X-Annotation-Notification"] = str(notification.id)
    message.set_content("\n".join(lines))
    return message


async def send_due(
    session: AsyncSession,
    settings: Settings,
    mailer: Mailer | None = None,
    *,
    now: datetime | None = None,
    mailer_factory: Callable[[Settings], Mailer] = SmtpMailer,
) -> dict[str, int]:
    """Mail every due notification once. Commits. Returns counts."""
    if not settings.smtp_host:
        return {"sent": 0, "failed": 0}
    now = now or datetime.now(UTC)
    rows = list(
        (
            await session.execute(
                select(Notification, User)
                .join(User, User.id == Notification.user_id)
                .where(
                    Notification.emailed_at.is_(None),
                    Notification.email_attempts < MAX_ATTEMPTS,
                    Notification.created_at
                    >= now - timedelta(seconds=settings.notification_email_max_age),
                    User.is_active.is_(True),
                    User.is_service.is_(False),
                    User.erased_at.is_(None),
                    User.email_notifications.is_(True),
                )
                .order_by(Notification.created_at)
                .limit(BATCH_SIZE)
                .with_for_update(of=Notification, skip_locked=True)
            )
        ).tuples()
    )
    if not rows:
        return {"sent": 0, "failed": 0}

    def ids(key: str) -> set[UUID]:
        return {UUID(str(n.payload[key])) for n, _ in rows if n.payload.get(key)}

    projects = dict(
        (
            await session.execute(
                select(Project.id, Project.name).where(Project.id.in_(ids("project_id")))
            )
        )
        .tuples()
        .all()
    )
    actors = dict(
        (
            await session.execute(
                select(User.id, User.display_name).where(User.id.in_(ids("actor_id")))
            )
        )
        .tuples()
        .all()
    )

    def name(table: dict[UUID, str], key: str, notification: Notification, default: str) -> str:
        raw = notification.payload.get(key)
        return table.get(UUID(str(raw)), default) if raw else default

    messages = [
        compose(
            notification,
            user,
            actor=name(actors, "actor_id", notification, "Someone"),
            project=name(projects, "project_id", notification, "a project"),
            settings=settings,
        )
        for notification, user in rows
    ]
    results = await (mailer or mailer_factory(settings)).send(messages)

    tally = {"sent": 0, "failed": 0}
    for (notification, _), error in zip(rows, results, strict=True):
        notification.email_attempts += 1
        if error is None:
            notification.emailed_at = now
            tally["sent"] += 1
        else:
            tally["failed"] += 1
            log.warning(
                "notification.email_failed",
                notification_id=str(notification.id),
                attempts=notification.email_attempts,
                error=error,
            )
    await session.commit()
    return tally
