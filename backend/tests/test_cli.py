"""Tests for the ``app.cli`` bootstrap commands.

No PostgreSQL is available here, so the database layer is always mocked: an
``AsyncMock`` stands in for the ``AsyncSession``, and ``get_sessionmaker`` is
patched to hand back an async context manager wrapping it. What is under test
is the CLI's own logic — argument parsing, password sourcing, the
create-or-reuse / create-or-refuse rules, and error-to-exit-code mapping —
not SQLAlchemy or a real database.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cli import (
    DEFAULT_ORG_NAME,
    DEFAULT_ORG_SLUG,
    MIN_PASSWORD_LENGTH,
    CliError,
    build_parser,
    main,
)
from app.models import Organization, User

SECRET = "correct-horse-battery-staple"  # 27 chars, well over the minimum
assert len(SECRET) >= MIN_PASSWORD_LENGTH


class _FakeSessionContext:
    """A minimal async context manager standing in for ``sessionmaker()``."""

    def __init__(self, session: MagicMock) -> None:
        self._session = session

    async def __aenter__(self) -> MagicMock:
        return self._session

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _make_session(*, scalar_results: list[Any]) -> MagicMock:
    """A fake ``AsyncSession`` whose ``scalar`` calls return ``scalar_results`` in order.

    ``add`` is deliberately left as an ordinary (synchronous) MagicMock
    attribute, matching the real, non-async ``Session.add`` — only the
    genuinely async methods are ``AsyncMock``.
    """
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=scalar_results)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    return session


@pytest.fixture
def patched_sessionmaker() -> Iterator[Callable[[MagicMock], None]]:
    """Patch ``app.cli.get_sessionmaker`` to serve a given fake session."""
    holder: dict[str, MagicMock] = {}

    def _sessionmaker_factory() -> _FakeSessionContext:
        return _FakeSessionContext(holder["session"])

    def _set(session: MagicMock) -> None:
        holder["session"] = session

    with patch("app.cli.get_sessionmaker", return_value=_sessionmaker_factory):
        yield _set


class TestArgumentParsing:
    def test_create_superuser_requires_email(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["create-superuser"])

    def test_reseal_secrets_takes_no_arguments_and_has_a_handler(self) -> None:
        from app.cli import _HANDLERS

        args = build_parser().parse_args(["reseal-secrets"])
        assert args.command == "reseal-secrets"
        assert "reseal-secrets" in _HANDLERS

    def test_create_superuser_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["create-superuser", "--email", "a@example.com"])
        assert args.command == "create-superuser"
        assert args.email == "a@example.com"
        assert args.password is None
        assert args.display_name is None
        assert args.org_name == DEFAULT_ORG_NAME
        assert args.org_slug == DEFAULT_ORG_SLUG
        assert args.update_password is False

    def test_create_superuser_all_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "create-superuser",
                "--email",
                "a@example.com",
                "--password",
                "hunter2hunter2",
                "--display-name",
                "Ada",
                "--org-name",
                "Acme",
                "--org-slug",
                "acme",
                "--update-password",
            ]
        )
        assert args.email == "a@example.com"
        assert args.password == "hunter2hunter2"
        assert args.display_name == "Ada"
        assert args.org_name == "Acme"
        assert args.org_slug == "acme"
        assert args.update_password is True

    def test_create_user_requires_email(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["create-user"])

    def test_create_user_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["create-user", "--email", "b@example.com"])
        assert args.command == "create-user"
        assert args.org_slug == DEFAULT_ORG_SLUG
        assert args.display_name is None
        assert args.update_password is False

    def test_create_user_has_no_org_name_flag(self) -> None:
        # create-user never creates an organization, so it has nothing to name.
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["create-user", "--email", "b@example.com", "--org-name", "Acme"])

    def test_unknown_command_exits(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["bogus-command"])

    def test_no_command_exits(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


class TestPasswordSourcing:
    def test_resolve_password_raises_cli_error_without_any_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.cli import _resolve_password

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(CliError):
            _resolve_password(None)

    def test_explicit_password_beats_admin_password_env(
        self, monkeypatch: pytest.MonkeyPatch, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        monkeypatch.setenv("ADMIN_PASSWORD", "env-password-1234")
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        code = main(
            [
                "create-superuser",
                "--email",
                "admin@example.com",
                "--password",
                SECRET,
            ]
        )

        assert code == 0
        created_user = session.add.call_args.args[0]
        assert isinstance(created_user, User)
        # The hash must be derived from the explicit --password, not the env var.
        from app.core.security import verify_password

        assert verify_password(SECRET, created_user.password_hash)
        assert not verify_password("env-password-1234", created_user.password_hash)

    def test_admin_password_env_used_when_flag_absent(
        self, monkeypatch: pytest.MonkeyPatch, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        monkeypatch.setenv("ADMIN_PASSWORD", SECRET)
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        code = main(["create-superuser", "--email", "admin@example.com"])

        assert code == 0
        created_user = session.add.call_args.args[0]
        from app.core.security import verify_password

        assert verify_password(SECRET, created_user.password_hash)

    def test_missing_password_non_interactive_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        code = main(["create-superuser", "--email", "admin@example.com"])

        assert code != 0

    def test_interactive_prompt_used_when_no_other_source(
        self, monkeypatch: pytest.MonkeyPatch, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("app.cli.getpass.getpass", lambda _prompt: SECRET)
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        code = main(["create-superuser", "--email", "admin@example.com"])

        assert code == 0

    def test_interactive_prompt_mismatch_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        responses = iter([SECRET, "a-different-password"])
        monkeypatch.setattr("app.cli.getpass.getpass", lambda _prompt: next(responses))

        code = main(["create-superuser", "--email", "admin@example.com"])

        assert code != 0

    @pytest.mark.parametrize(
        ("password", "expect_ok"),
        [
            pytest.param("short123456", False, id="11-chars-too-short"),
            pytest.param("exactly12chr", True, id="12-chars-is-enough"),
        ],
    )
    def test_minimum_length_boundary(
        self,
        password: str,
        expect_ok: bool,
        patched_sessionmaker: Callable[[MagicMock], None],
    ) -> None:
        assert len(password) == 11 + int(expect_ok)
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        code = main(["create-superuser", "--email", "admin@example.com", "--password", password])

        assert (code == 0) is expect_ok


class TestCreateSuperuser:
    def test_creates_organization_and_superuser(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        # No existing organization (None), no existing user (None): both are created.
        session = _make_session(scalar_results=[None, None])
        patched_sessionmaker(session)

        code = main(
            [
                "create-superuser",
                "--email",
                "admin@example.com",
                "--password",
                SECRET,
                "--org-slug",
                "acme",
                "--org-name",
                "Acme Inc",
            ]
        )

        assert code == 0
        add_calls = [call.args[0] for call in session.add.call_args_list]
        organizations = [obj for obj in add_calls if isinstance(obj, Organization)]
        users = [obj for obj in add_calls if isinstance(obj, User)]
        assert len(organizations) == 1
        assert organizations[0].slug == "acme"
        assert len(users) == 1
        assert users[0].is_superuser is True
        assert users[0].is_active is True
        assert users[0].email == "admin@example.com"
        assert users[0].display_name == "admin"  # defaulted from the email local part
        session.commit.assert_awaited()

    def test_reuses_existing_organization(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        existing_org = Organization(name="Acme Inc", slug="acme")
        session = _make_session(scalar_results=[existing_org, None])
        patched_sessionmaker(session)

        code = main(
            [
                "create-superuser",
                "--email",
                "admin@example.com",
                "--password",
                SECRET,
                "--org-slug",
                "acme",
            ]
        )

        assert code == 0
        # Only the user is added; the organization was reused, not recreated.
        add_calls = [call.args[0] for call in session.add.call_args_list]
        assert not any(isinstance(obj, Organization) for obj in add_calls)
        assert any(isinstance(obj, User) for obj in add_calls)

    def test_existing_email_without_update_flag_is_refused(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        existing_user = User(
            organization_id=org.id,
            email="admin@example.com",
            display_name="Admin",
            password_hash="old-hash",
            is_active=True,
            is_superuser=True,
        )
        session = _make_session(scalar_results=[org, existing_user])
        patched_sessionmaker(session)

        code = main(["create-superuser", "--email", "admin@example.com", "--password", SECRET])

        assert code != 0
        session.commit.assert_not_awaited()
        assert existing_user.password_hash == "old-hash"

    def test_existing_email_with_update_flag_updates_password_only(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        existing_user = User(
            organization_id=org.id,
            email="admin@example.com",
            display_name="Admin",
            password_hash="old-hash",
            is_active=True,
            is_superuser=True,
        )
        session = _make_session(scalar_results=[org, existing_user])
        patched_sessionmaker(session)

        code = main(
            [
                "create-superuser",
                "--email",
                "admin@example.com",
                "--password",
                SECRET,
                "--update-password",
            ]
        )

        assert code == 0
        assert existing_user.password_hash != "old-hash"
        from app.core.security import verify_password

        assert verify_password(SECRET, existing_user.password_hash)
        session.commit.assert_awaited()

    def test_integrity_error_is_reported_cleanly(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        session.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("dupe")))
        patched_sessionmaker(session)

        code = main(["create-superuser", "--email", "admin@example.com", "--password", SECRET])

        assert code != 0
        session.rollback.assert_awaited()


class TestCreateUser:
    def test_requires_existing_organization(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        session = _make_session(scalar_results=[None])  # organization lookup: not found
        patched_sessionmaker(session)

        code = main(
            [
                "create-user",
                "--email",
                "alice@example.com",
                "--password",
                SECRET,
                "--org-slug",
                "missing-org",
            ]
        )

        assert code != 0
        session.add.assert_not_called()

    def test_creates_non_superuser_in_existing_organization(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        code = main(["create-user", "--email", "alice@example.com", "--password", SECRET])

        assert code == 0
        created_user = session.add.call_args.args[0]
        assert isinstance(created_user, User)
        assert created_user.is_superuser is False
        assert created_user.is_active is True

    def test_existing_email_without_update_flag_is_refused(
        self, patched_sessionmaker: Callable[[MagicMock], None]
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        existing_user = User(
            organization_id=org.id,
            email="alice@example.com",
            display_name="Alice",
            password_hash="old-hash",
            is_active=True,
            is_superuser=False,
        )
        session = _make_session(scalar_results=[org, existing_user])
        patched_sessionmaker(session)

        code = main(["create-user", "--email", "alice@example.com", "--password", SECRET])

        assert code != 0


class TestMainExitCodes:
    def test_success_returns_zero(self, patched_sessionmaker: Callable[[MagicMock], None]) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        assert main(["create-superuser", "--email", "a@example.com", "--password", SECRET]) == 0

    def test_argparse_errors_raise_system_exit_with_nonzero_code(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            main(["create-superuser"])  # missing --email
        assert exc_info.value.code != 0

    def test_cli_error_path_returns_nonzero_not_system_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        # A CliError is turned into a return code, not a SystemExit / traceback.
        code = main(["create-superuser", "--email", "a@example.com"])
        assert isinstance(code, int)
        assert code != 0


class TestPasswordNeverLeaked:
    def test_password_absent_from_output_on_success(
        self,
        capsys: pytest.CaptureFixture[str],
        patched_sessionmaker: Callable[[MagicMock], None],
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        main(["create-superuser", "--email", "admin@example.com", "--password", SECRET])

        captured = capsys.readouterr()
        assert SECRET not in captured.out
        assert SECRET not in captured.err

    def test_password_absent_from_output_on_refusal(
        self,
        capsys: pytest.CaptureFixture[str],
        patched_sessionmaker: Callable[[MagicMock], None],
    ) -> None:
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        existing_user = User(
            organization_id=org.id,
            email="admin@example.com",
            display_name="Admin",
            password_hash="old-hash",
            is_active=True,
            is_superuser=True,
        )
        session = _make_session(scalar_results=[org, existing_user])
        patched_sessionmaker(session)

        main(["create-superuser", "--email", "admin@example.com", "--password", SECRET])

        captured = capsys.readouterr()
        assert SECRET not in captured.out
        assert SECRET not in captured.err

    def test_admin_password_env_value_absent_from_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        patched_sessionmaker: Callable[[MagicMock], None],
    ) -> None:
        monkeypatch.setenv("ADMIN_PASSWORD", SECRET)
        org = Organization(name=DEFAULT_ORG_NAME, slug=DEFAULT_ORG_SLUG)
        session = _make_session(scalar_results=[org, None])
        patched_sessionmaker(session)

        main(["create-superuser", "--email", "admin@example.com"])

        captured = capsys.readouterr()
        assert SECRET not in captured.out
        assert SECRET not in captured.err


class TestHandlerSignature:
    def test_main_accepts_none_and_uses_sys_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["prog", "create-superuser"])
        with pytest.raises(SystemExit):
            main(None)  # missing --email still exits non-zero via argparse

    def test_build_parser_returns_argument_parser(self) -> None:
        assert isinstance(build_parser(), argparse.ArgumentParser)
