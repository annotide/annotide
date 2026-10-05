"""Bootstrap CLI for creating the first accounts in a fresh installation.

A freshly migrated database has no users, and there is no other way to sign
in (AUTH-2): the API only ever mints a token for an existing account. This
module is the escape hatch — run once, by an operator with shell access to
the backend container, to create the first superuser and any subsequent
accounts.

Usage::

    python -m app.cli create-superuser --email admin@example.com
    python -m app.cli create-user --email alice@example.com --org-slug default
    python -m app.cli reseal-secrets   # after rotating APP_SECRET_KEY

Every command is a thin argparse wrapper around an async handler that talks
to the database through the same session machinery as the API
(``app.db.session``). Handlers never raise for expected, user-facing failures
— they raise :class:`CliError`, which :func:`main` turns into a message on
stderr and a non-zero exit code, so a bad email or a stale password never
prints a Python traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import get_sessionmaker
from app.models import Organization, User

#: The account created by ``create-superuser`` owns every project in the
#: installation, so it is held to a higher bar than a typical login policy.
MIN_PASSWORD_LENGTH = 12

DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Default"


class CliError(Exception):
    """A user-facing failure: reported as a message, never a traceback."""


def _display_name_from_email(email: str) -> str:
    """Default display name: the local part of the email address."""
    local_part, _, _ = email.partition("@")
    return local_part


def _resolve_password(explicit: str | None) -> str:
    """Resolve the password to hash, in order: ``--password``, ``ADMIN_PASSWORD``, prompt.

    Never invents or defaults a password. Raises :class:`CliError` when none
    of the three sources yields one, and when the result is shorter than
    :data:`MIN_PASSWORD_LENGTH`.
    """
    password: str | None = explicit if explicit is not None else os.environ.get("ADMIN_PASSWORD")

    if password is None:
        if not sys.stdin.isatty():
            raise CliError(
                "No password supplied. Pass --password, set the ADMIN_PASSWORD "
                "environment variable, or run this in an interactive terminal."
            )
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            raise CliError("Passwords do not match.")
        password = first

    if len(password) < MIN_PASSWORD_LENGTH:
        raise CliError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    return password


async def _get_or_create_organization(
    session: AsyncSession, *, slug: str, name: str
) -> Organization:
    """Return the organization with ``slug``, creating it if none exists."""
    organization = await session.scalar(select(Organization).where(Organization.slug == slug))
    if organization is not None:
        return organization
    organization = Organization(name=name, slug=slug)
    session.add(organization)
    # Flush (not commit) so organization.id is populated for the user's FK,
    # while staying in the same transaction as the user insert below.
    await session.flush()
    return organization


async def _get_organization_or_raise(session: AsyncSession, *, slug: str) -> Organization:
    """Return the organization with ``slug``, or raise :class:`CliError`."""
    organization = await session.scalar(select(Organization).where(Organization.slug == slug))
    if organization is None:
        raise CliError(
            f"No organization with slug {slug!r} exists. Create it first "
            "(for example with create-superuser, which creates its organization)."
        )
    return organization


async def _create_or_update_user(
    session: AsyncSession,
    *,
    organization: Organization,
    email: str,
    password: str,
    display_name: str,
    is_superuser: bool,
    update_password: bool,
) -> str:
    """Create a user in ``organization``, or update just its password if it exists.

    Never overwrites an existing account silently: a pre-existing email is a
    :class:`CliError` unless ``update_password`` was explicitly requested.
    """
    existing = await session.scalar(select(User).where(User.email == email))
    password_hash = hash_password(password)

    if existing is not None:
        if not update_password:
            raise CliError(
                f"A user with email {email!r} already exists. Pass --update-password "
                "to update just its password hash."
            )
        existing.password_hash = password_hash
        await session.commit()
        return f"Updated password for {email!r} in organization {organization.slug!r}."

    user = User(
        organization_id=organization.id,
        email=email,
        display_name=display_name,
        password_hash=password_hash,
        is_active=True,
        is_superuser=is_superuser,
    )
    session.add(user)
    await session.commit()

    kind = "superuser" if is_superuser else "user"
    return f"Created {kind} {email!r} in organization {organization.slug!r}."


async def _run_create_superuser(args: argparse.Namespace) -> str:
    email: str = args.email
    password = _resolve_password(args.password)
    display_name: str = args.display_name or _display_name_from_email(email)
    org_slug: str = args.org_slug
    org_name: str = args.org_name
    update_password: bool = args.update_password

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            organization = await _get_or_create_organization(session, slug=org_slug, name=org_name)
            message = await _create_or_update_user(
                session,
                organization=organization,
                email=email,
                password=password,
                display_name=display_name,
                is_superuser=True,
                update_password=update_password,
            )
        except IntegrityError as exc:
            await session.rollback()
            raise CliError(
                "Could not complete the operation: a conflicting record already exists "
                "(likely created concurrently). Try again."
            ) from exc
    return message


async def _run_create_user(args: argparse.Namespace) -> str:
    email: str = args.email
    password = _resolve_password(args.password)
    display_name: str = args.display_name or _display_name_from_email(email)
    org_slug: str = args.org_slug
    update_password: bool = args.update_password

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            organization = await _get_organization_or_raise(session, slug=org_slug)
            message = await _create_or_update_user(
                session,
                organization=organization,
                email=email,
                password=password,
                display_name=display_name,
                is_superuser=False,
                update_password=update_password,
            )
        except IntegrityError as exc:
            await session.rollback()
            raise CliError(
                "Could not complete the operation: a conflicting record already exists "
                "(likely created concurrently). Try again."
            ) from exc
    return message


async def _run_reseal_secrets(args: argparse.Namespace) -> str:
    from app.services.secret_rotation import reseal_all

    async with get_sessionmaker()() as session:
        tally = await reseal_all(session)
    message = (
        f"Re-sealed {tally['mfa']} MFA seed(s) and {tally['webhooks']} webhook secret(s) "
        "under the current APP_SECRET_KEY."
    )
    if tally["unreadable"]:
        message += (
            f" {tally['unreadable']} value(s) opened with no key and were left as they are: "
            "those users reset MFA, those webhooks rotate their secret."
        )
    return message


_HANDLERS: dict[str, Callable[[argparse.Namespace], Coroutine[Any, Any, str]]] = {
    "create-superuser": _run_create_superuser,
    "create-user": _run_create_user,
    "reseal-secrets": _run_reseal_secrets,
}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for ``python -m app.cli``."""
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Bootstrap accounts for a fresh installation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_superuser = subparsers.add_parser(
        "create-superuser",
        help="Create (or reuse) an organization and create a superuser in it.",
    )
    create_superuser.add_argument("--email", required=True, help="Login email (unique).")
    create_superuser.add_argument(
        "--password",
        default=None,
        help="Plaintext password. Falls back to ADMIN_PASSWORD, then an interactive prompt.",
    )
    create_superuser.add_argument(
        "--display-name",
        default=None,
        help="Defaults to the local part of --email.",
    )
    create_superuser.add_argument("--org-name", default=DEFAULT_ORG_NAME)
    create_superuser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    create_superuser.add_argument(
        "--update-password",
        action="store_true",
        help="If the email already exists, update just its password hash.",
    )

    create_user = subparsers.add_parser(
        "create-user",
        help="Create a non-superuser user in an existing organization.",
    )
    create_user.add_argument("--email", required=True, help="Login email (unique).")
    create_user.add_argument(
        "--password",
        default=None,
        help="Plaintext password. Falls back to ADMIN_PASSWORD, then an interactive prompt.",
    )
    create_user.add_argument(
        "--display-name",
        default=None,
        help="Defaults to the local part of --email.",
    )
    create_user.add_argument(
        "--org-slug",
        default=DEFAULT_ORG_SLUG,
        help="Must already exist; this command never creates an organization.",
    )
    create_user.add_argument(
        "--update-password",
        action="store_true",
        help="If the email already exists, update just its password hash.",
    )

    subparsers.add_parser(
        "reseal-secrets",
        help=(
            "After rotating APP_SECRET_KEY (old one in APP_SECRET_KEY_PREVIOUS): move sealed "
            "MFA seeds and webhook secrets onto the new key."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse arguments, run the handler, and return an exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _HANDLERS[args.command]

    try:
        message = asyncio.run(handler(args))
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
