"""Secret-store backends.

Cloud SDKs are imported lazily inside each backend, so an installation that
never uses a vault neither pays the import nor needs the package; a missing SDK
raises `SecretBackendError` naming the extra to install. Store clients are
built once and reused: a vault client per call costs a TLS handshake and a
token fetch every time the cache misses.

Backends let the store's own exceptions propagate; `resolve_secret` turns them
into a `SecretResolutionError` carrying only the exception's type, since a
store's error text is not ours to trust not to echo a value.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
from typing import Any, ClassVar

from app.services.secrets.base import SecretRef
from app.services.secrets.errors import SecretBackendError, SecretResolutionError


class EnvBackend:
    """Environment variables. An empty value is a value, not a missing one."""

    scheme: ClassVar[str] = "env"

    async def get(self, ref: SecretRef) -> str:
        value = os.environ.get(ref.locator)
        if value is None:
            raise SecretResolutionError(ref.raw, "environment variable is not set")
        return value

    async def aclose(self) -> None:
        return None


class FileBackend:
    """A file per secret, as Docker and Kubernetes mount them.

    Preferred over environment variables in production: a file is not
    inherited by child processes and is not in ``/proc/<pid>/environ``.
    Exactly one trailing newline is stripped (`echo value > file` adds it).
    With `allowed_root`, a path outside it — directly or through `..` or a
    symlink — is refused: a connector row is editable by an org admin and must
    not be able to name any file the process can read.
    """

    scheme: ClassVar[str] = "file"

    def __init__(self, allowed_root: str | None = None) -> None:
        self.allowed_root = Path(allowed_root).resolve() if allowed_root else None

    async def get(self, ref: SecretRef) -> str:
        path = Path(ref.locator).resolve()
        if self.allowed_root is not None and not path.is_relative_to(self.allowed_root):
            # The root itself is not named: the org admin reading this error
            # has no business learning the server's layout.
            raise SecretResolutionError(ref.raw, "path is outside the permitted secret root")
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError:
            raise SecretResolutionError(ref.raw, "no such file") from None
        except OSError as exc:
            raise SecretResolutionError(ref.raw, f"cannot read file: {exc.strerror}") from None
        except UnicodeDecodeError:
            raise SecretResolutionError(ref.raw, "file is not UTF-8 text") from None
        return text[:-1] if text.endswith("\n") else text

    async def aclose(self) -> None:
        return None


class AzureKeyVaultBackend:
    """Azure Key Vault through `azure-keyvault-secrets` and `DefaultAzureCredential`.

    `azurekeyvault://vault/name` or, with `default_vault`, `azurekeyvault://name`.
    `?version=` pins a secret version. `client_id` picks a user-assigned
    managed identity; without it the credential's own lookup applies
    (`AZURE_CLIENT_ID`, then the system-assigned identity).
    """

    scheme: ClassVar[str] = "azurekeyvault"

    def __init__(self, default_vault: str | None = None, client_id: str | None = None) -> None:
        self.default_vault = default_vault
        self.client_id = client_id
        self._clients: dict[str, Any] = {}
        self._credential: Any = None

    async def _client(self, vault: str) -> Any:
        if vault in self._clients:
            return self._clients[vault]
        try:
            from azure.identity.aio import DefaultAzureCredential
            from azure.keyvault.secrets.aio import SecretClient
        except ImportError as exc:
            raise SecretBackendError(
                "azurekeyvault:// references need azure-keyvault-secrets "
                "(install the backend with the `keyvault` extra)"
            ) from exc
        if self._credential is None:
            # Only when set: an explicit None would hide `AZURE_CLIENT_ID`.
            options = {"managed_identity_client_id": self.client_id} if self.client_id else {}
            self._credential = DefaultAzureCredential(**options)
        client = SecretClient(
            vault_url=f"https://{vault}.vault.azure.net", credential=self._credential
        )
        self._clients[vault] = client
        return client

    async def get(self, ref: SecretRef) -> str:
        parts = ref.parts
        if len(parts) >= 2:
            vault, name = parts[0], "/".join(parts[1:])
        elif len(parts) == 1 and self.default_vault:
            vault, name = self.default_vault, parts[0]
        else:
            raise SecretResolutionError(
                ref.raw, "name a vault (azurekeyvault://vault/name) or set APP_AZURE_KEY_VAULT_NAME"
            )
        client = await self._client(vault)
        secret = await client.get_secret(name, version=ref.options.get("version"))
        if secret.value is None:
            raise SecretResolutionError(ref.raw, "the vault returned no value")
        return str(secret.value)

    async def aclose(self) -> None:
        # Shutdown must not fail because a client was already closed.
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        if self._credential is not None:
            with contextlib.suppress(Exception):
                await self._credential.close()
            self._credential = None


class AwsSecretsBackend:
    """AWS Secrets Manager through boto3, run in a thread (boto3 is synchronous).

    `awssecrets://name?region=&key=`: `key` picks one field of a JSON secret,
    which is how AWS itself stores database credentials. Binary secrets are
    read as UTF-8.
    """

    scheme: ClassVar[str] = "awssecrets"

    def __init__(self, default_region: str | None = None) -> None:
        self.default_region = default_region
        self._clients: dict[str | None, Any] = {}

    def _client(self, region: str | None) -> Any:
        # boto3 clients are thread-safe; one per region is enough.
        if region not in self._clients:
            try:
                import boto3
            except ImportError as exc:
                raise SecretBackendError("awssecrets:// references need boto3") from exc
            self._clients[region] = boto3.client("secretsmanager", region_name=region)
        return self._clients[region]

    async def get(self, ref: SecretRef) -> str:
        client = self._client(ref.options.get("region") or self.default_region)

        def fetch() -> str:
            response = client.get_secret_value(SecretId=ref.locator.strip("/"))
            if response.get("SecretString") is not None:
                return str(response["SecretString"])
            if response.get("SecretBinary") is not None:
                binary = response["SecretBinary"]
                raw = binary if isinstance(binary, bytes) else base64.b64decode(binary)
                return raw.decode("utf-8")
            raise SecretResolutionError(ref.raw, "the secret has no value")

        value = await asyncio.to_thread(fetch)
        key = ref.options.get("key")
        if key is None:
            return value
        try:
            field_value = json.loads(value)[key]
        except (ValueError, KeyError, TypeError):
            raise SecretResolutionError(ref.raw, f"the secret has no JSON field {key!r}") from None
        return str(field_value)

    async def aclose(self) -> None:
        self._clients.clear()


class GcpSecretsBackend:
    """GCP Secret Manager: `gcpsecrets://project/name?version=` (default `latest`)."""

    scheme: ClassVar[str] = "gcpsecrets"

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise SecretBackendError(
                    "gcpsecrets:// references need google-cloud-secret-manager "
                    "(install the backend with the `gcp-secrets` extra)"
                ) from exc
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    async def get(self, ref: SecretRef) -> str:
        parts = ref.parts
        if len(parts) != 2:
            raise SecretResolutionError(ref.raw, "expected gcpsecrets://project/name")
        client = self._get_client()
        name = (
            f"projects/{parts[0]}/secrets/{parts[1]}/versions/"
            f"{ref.options.get('version') or 'latest'}"
        )

        def fetch() -> str:
            response = client.access_secret_version(request={"name": name})
            return str(response.payload.data.decode("utf-8"))

        return await asyncio.to_thread(fetch)

    async def aclose(self) -> None:
        self._client = None
