"""Tests for secret-reference resolution across backends.

The bug these guard against: passing a stored ``secret_ref`` straight to a
connector as if it were the secret. That fails authentication rather than
raising anything obvious, and because callers turn connector failures into
"no preview available", it shows up as images silently never loading.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.secrets import (
    AwsSecretsBackend,
    AzureKeyVaultBackend,
    EnvBackend,
    FileBackend,
    GcpSecretsBackend,
    SecretBackendError,
    SecretError,
    SecretRef,
    SecretResolutionError,
    UnknownSecretSchemeError,
    aclose_backends,
    clear_cache,
    configure,
    parse_ref,
    register,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path: Path) -> Any:
    """Every test starts with standard backends and an empty cache."""
    configure()
    clear_cache()
    yield
    clear_cache()


class TestParseRef:
    def test_bare_string_is_an_environment_variable(self) -> None:
        # Backwards compatibility: this is what every existing config uses.
        ref = parse_ref("AZURE_STORAGE_KEY")
        assert ref.scheme == "env"
        assert ref.locator == "AZURE_STORAGE_KEY"

    @pytest.mark.parametrize(
        ("raw", "scheme", "locator"),
        [
            ("env:MY_VAR", "env", "MY_VAR"),
            ("file:/run/secrets/key", "file", "/run/secrets/key"),
            ("azurekeyvault://acme-kv/storage-key", "azurekeyvault", "acme-kv/storage-key"),
            ("awssecrets://prod/storage-key", "awssecrets", "prod/storage-key"),
            ("gcpsecrets://acme/storage-key", "gcpsecrets", "acme/storage-key"),
        ],
    )
    def test_schemes(self, raw: str, scheme: str, locator: str) -> None:
        ref = parse_ref(raw)
        assert (ref.scheme, ref.locator) == (scheme, locator)

    def test_query_options_are_parsed(self) -> None:
        ref = parse_ref("awssecrets://prod/db?region=eu-west-1&key=password")
        assert ref.options == {"region": "eu-west-1", "key": "password"}

    def test_parts_splits_the_locator(self) -> None:
        assert parse_ref("azurekeyvault://acme-kv/storage-key").parts == [
            "acme-kv",
            "storage-key",
        ]

    @pytest.mark.parametrize("raw", ["", "   ", "env:", "azurekeyvault://"])
    def test_malformed_references_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_ref(raw)


class TestResolveSecret:
    async def test_none_reference_resolves_to_none(self) -> None:
        # A managed-identity connector has no secret; normal, not an error.
        assert await resolve_secret(None) is None

    async def test_resolves_bare_name_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AZURE_STORAGE_KEY", "the-actual-secret")
        assert await resolve_secret("AZURE_STORAGE_KEY") == "the-actual-secret"

    async def test_resolves_explicit_env_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_STORAGE_KEY", "the-actual-secret")
        assert await resolve_secret("env:AZURE_STORAGE_KEY") == "the-actual-secret"

    async def test_unresolvable_reference_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        with pytest.raises(SecretResolutionError) as exc:
            await resolve_secret("MISSING_SECRET")
        assert exc.value.secret_ref == "MISSING_SECRET"

    async def test_unresolvable_never_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returning None would make the connector attempt anonymous access."""
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        with pytest.raises(SecretResolutionError):
            await resolve_secret("MISSING_SECRET")

    async def test_never_returns_the_reference_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original bug, pinned.

        A resolver that handed back the reference would authenticate with the
        literal string 'AZURE_STORAGE_KEY'.
        """
        monkeypatch.setenv("AZURE_STORAGE_KEY", "the-actual-secret")
        assert await resolve_secret("AZURE_STORAGE_KEY") != "AZURE_STORAGE_KEY"

    async def test_empty_value_is_honoured_not_treated_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EMPTY_SECRET", "")
        assert await resolve_secret("EMPTY_SECRET") == ""

    async def test_error_message_names_the_reference_not_the_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        with pytest.raises(SecretResolutionError, match="MISSING_SECRET"):
            await resolve_secret("MISSING_SECRET")

    async def test_unknown_scheme_is_reported_clearly(self) -> None:
        with pytest.raises(UnknownSecretSchemeError, match="vault9000"):
            await resolve_secret("vault9000://somewhere/secret")


class TestFileBackend:
    async def test_reads_a_mounted_secret(self, tmp_path: Path) -> None:
        secret_file = tmp_path / "storage_key"
        secret_file.write_text("the-actual-secret")
        assert await resolve_secret(f"file:{secret_file}") == "the-actual-secret"

    async def test_strips_exactly_one_trailing_newline(self, tmp_path: Path) -> None:
        # `echo secret > file` adds one; it is not part of the credential.
        secret_file = tmp_path / "k"
        secret_file.write_text("the-actual-secret\n")
        assert await resolve_secret(f"file:{secret_file}") == "the-actual-secret"

    async def test_keeps_a_second_trailing_newline(self, tmp_path: Path) -> None:
        # Only one is an artefact; more than one was deliberate.
        secret_file = tmp_path / "k"
        secret_file.write_text("value\n\n")
        assert await resolve_secret(f"file:{secret_file}") == "value\n"

    async def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(SecretResolutionError, match="no such file"):
            await resolve_secret(f"file:{tmp_path / 'nope'}")

    async def test_path_outside_the_allowed_root_is_refused(self, tmp_path: Path) -> None:
        allowed = tmp_path / "secrets"
        allowed.mkdir()
        outside = tmp_path / "private.pem"
        outside.write_text("a private key")

        register(FileBackend(allowed_root=str(allowed)))
        # A connector row is editable by an org admin; without the root check it
        # could name any file the process can read.
        with pytest.raises(SecretResolutionError, match="outside the permitted"):
            await resolve_secret(f"file:{outside}")

    async def test_traversal_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        allowed = tmp_path / "secrets"
        allowed.mkdir()
        (tmp_path / "private.pem").write_text("a private key")

        register(FileBackend(allowed_root=str(allowed)))
        with pytest.raises(SecretResolutionError):
            await resolve_secret(f"file:{allowed}/../private.pem")


class TestCaching:
    async def test_second_lookup_is_served_from_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CACHED", "first")
        assert await resolve_secret("CACHED") == "first"

        # Changing the source has no effect while the entry is still fresh —
        # which is the point: a vault is not hit once per request.
        monkeypatch.setenv("CACHED", "second")
        assert await resolve_secret("CACHED") == "first"

    async def test_clearing_the_cache_refetches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHED", "first")
        await resolve_secret("CACHED")
        monkeypatch.setenv("CACHED", "second")
        clear_cache()
        assert await resolve_secret("CACHED") == "second"

    async def test_cache_can_be_bypassed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHED", "first")
        await resolve_secret("CACHED")
        monkeypatch.setenv("CACHED", "second")
        assert await resolve_secret("CACHED", use_cache=False) == "second"

    async def test_zero_ttl_disables_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHED", "first")
        await resolve_secret("CACHED", cache_ttl=0)
        monkeypatch.setenv("CACHED", "second")
        assert await resolve_secret("CACHED", cache_ttl=0) == "second"

    async def test_a_failed_lookup_is_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LATER", raising=False)
        with pytest.raises(SecretResolutionError):
            await resolve_secret("LATER")

        # Caching a failure would keep an install broken after the operator
        # fixed the configuration, until a restart.
        monkeypatch.setenv("LATER", "now-present")
        assert await resolve_secret("LATER") == "now-present"


class TestAzureKeyVault:
    async def test_reads_a_secret_from_the_vault(self) -> None:
        client = MagicMock()
        client.get_secret = AsyncMock(return_value=MagicMock(value="from-the-vault"))

        backend = AzureKeyVaultBackend()
        with patch.object(backend, "_client", AsyncMock(return_value=client)):
            register(backend)
            assert await resolve_secret("azurekeyvault://acme-kv/storage-key") == "from-the-vault"

        client.get_secret.assert_awaited_once_with("storage-key", version=None)

    async def test_version_option_is_passed_through(self) -> None:
        client = MagicMock()
        client.get_secret = AsyncMock(return_value=MagicMock(value="v2"))

        backend = AzureKeyVaultBackend()
        with patch.object(backend, "_client", AsyncMock(return_value=client)):
            register(backend)
            await resolve_secret("azurekeyvault://acme-kv/key?version=abc123")

        client.get_secret.assert_awaited_once_with("key", version="abc123")

    async def test_default_vault_allows_a_short_reference(self) -> None:
        client = MagicMock()
        client.get_secret = AsyncMock(return_value=MagicMock(value="x"))

        backend = AzureKeyVaultBackend(default_vault="acme-kv")
        with patch.object(backend, "_client", AsyncMock(return_value=client)) as factory:
            register(backend)
            await resolve_secret("azurekeyvault://storage-key")
        factory.assert_awaited_once_with("acme-kv")

    async def test_vault_failure_becomes_a_resolution_error(self) -> None:
        client = MagicMock()
        client.get_secret = AsyncMock(side_effect=RuntimeError("ServiceRequestError"))

        backend = AzureKeyVaultBackend()
        with patch.object(backend, "_client", AsyncMock(return_value=client)):
            register(backend)
            with pytest.raises(SecretResolutionError):
                await resolve_secret("azurekeyvault://acme-kv/storage-key")

    async def test_missing_sdk_names_the_package_to_install(self) -> None:
        backend = AzureKeyVaultBackend()
        with patch.dict("sys.modules", {"azure.keyvault.secrets.aio": None}):
            register(backend)
            with pytest.raises((SecretBackendError, SecretResolutionError)):
                await resolve_secret("azurekeyvault://acme-kv/storage-key")


class TestNoLeaks:
    async def test_the_secret_value_never_reaches_a_log(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("LEAKY", "super-secret-value")
        with caplog.at_level("DEBUG"):
            await resolve_secret("LEAKY")
        assert "super-secret-value" not in caplog.text

    async def test_error_text_carries_the_reference_but_no_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRESENT", "super-secret-value")
        monkeypatch.delenv("ABSENT", raising=False)
        try:
            await resolve_secret("ABSENT")
        except SecretResolutionError as exc:
            assert "ABSENT" in str(exc)
            assert "super-secret-value" not in str(exc)


class TestBackendRegistration:
    def test_env_backend_declares_its_scheme(self) -> None:
        assert EnvBackend.scheme == "env"
        assert FileBackend.scheme == "file"


class SlowBackend:
    """Counts lookups and holds each one open until released."""

    scheme: ClassVar[str] = "slow"

    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()

    async def get(self, ref: SecretRef) -> str:
        self.calls += 1
        await self.release.wait()
        return f"value-{self.calls}"

    async def aclose(self) -> None:
        return None


class TestConcurrency:
    async def test_concurrent_misses_share_one_lookup(self) -> None:
        backend = SlowBackend()
        register(backend)
        waiters = [asyncio.create_task(resolve_secret("slow://a")) for _ in range(5)]
        await asyncio.sleep(0)
        backend.release.set()
        assert await asyncio.gather(*waiters) == ["value-1"] * 5
        assert backend.calls == 1

    async def test_an_uncached_lookup_does_not_wait_for_the_lock(self) -> None:
        backend = SlowBackend()
        backend.release.set()
        register(backend)
        assert await resolve_secret("slow://a", use_cache=False) == "value-1"
        assert await resolve_secret("slow://a", use_cache=False) == "value-2"


class TestErrorHygiene:
    async def test_a_store_error_is_neither_quoted_nor_chained(self) -> None:
        client = MagicMock()
        client.get_secret = AsyncMock(side_effect=RuntimeError("token=super-secret-value"))
        backend = AzureKeyVaultBackend()
        with patch.object(backend, "_client", AsyncMock(return_value=client)):
            register(backend)
            with pytest.raises(SecretResolutionError) as caught:
                await resolve_secret("azurekeyvault://acme-kv/storage-key")
        assert "super-secret-value" not in str(caught.value)
        assert "RuntimeError" in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__

    async def test_file_root_error_does_not_reveal_the_root(self, tmp_path: Path) -> None:
        configure(file_root=str(tmp_path / "vault"))
        with pytest.raises(SecretResolutionError) as caught:
            await resolve_secret("file:/etc/hostname")
        assert str(tmp_path) not in str(caught.value)

    def test_every_error_is_a_secret_error(self) -> None:
        assert issubclass(SecretResolutionError, SecretError)
        assert issubclass(SecretBackendError, SecretError)
        assert issubclass(UnknownSecretSchemeError, SecretResolutionError)

    async def test_aclose_keeps_the_configured_backends(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(file_root=str(tmp_path))
        await aclose_backends()
        with pytest.raises(SecretResolutionError, match="outside the permitted"):
            await resolve_secret("file:/etc/hostname")


class TestAwsSecrets:
    def _backend(self, response: dict[str, Any]) -> tuple[AwsSecretsBackend, MagicMock]:
        client = MagicMock()
        client.get_secret_value.return_value = response
        backend = AwsSecretsBackend(default_region="eu-west-1")
        backend._clients["eu-west-1"] = client
        register(backend)
        return backend, client

    async def test_reads_a_string_secret_and_reuses_the_client(self) -> None:
        _, client = self._backend({"SecretString": "s3cret"})
        assert await resolve_secret("awssecrets://prod/key", use_cache=False) == "s3cret"
        assert await resolve_secret("awssecrets://prod/key", use_cache=False) == "s3cret"
        assert client.get_secret_value.call_count == 2
        client.get_secret_value.assert_called_with(SecretId="prod/key")

    async def test_picks_a_json_field(self) -> None:
        self._backend({"SecretString": '{"password": "pw", "user": "u"}'})
        assert await resolve_secret("awssecrets://db?key=password") == "pw"
        with pytest.raises(SecretResolutionError, match="no JSON field"):
            await resolve_secret("awssecrets://db?key=missing")

    async def test_reads_a_binary_secret(self) -> None:
        self._backend({"SecretBinary": b"binary-value"})
        assert await resolve_secret("awssecrets://bin") == "binary-value"


class TestGcpSecrets:
    async def test_reads_latest_and_reuses_the_client(self) -> None:
        client = MagicMock()
        client.access_secret_version.return_value.payload.data = b"gcp-value"
        backend = GcpSecretsBackend()
        backend._client = client
        register(backend)
        assert await resolve_secret("gcpsecrets://acme/key", use_cache=False) == "gcp-value"
        assert await resolve_secret("gcpsecrets://acme/key?version=3") == "gcp-value"
        names = [c.kwargs["request"]["name"] for c in client.access_secret_version.call_args_list]
        assert names == [
            "projects/acme/secrets/key/versions/latest",
            "projects/acme/secrets/key/versions/3",
        ]

    async def test_rejects_a_reference_without_a_project(self) -> None:
        with pytest.raises(SecretResolutionError, match="expected gcpsecrets"):
            await resolve_secret("gcpsecrets://just-a-name")
