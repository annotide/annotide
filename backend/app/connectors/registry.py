"""Connector type registry (SRC-1).

Maps the `connector_type` strings from the `connector` table's enum
(`azure_blob`, `s3`, `gcs`, `local`, `http`) to their connector classes, and
builds a connector instance from a plain config dict — the shape of a
`connector` row's `config` JSONB column, plus its separately resolved
secret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from app.connectors.azure_blob import AzureBlobConnector
from app.connectors.base import StorageConnector
from app.connectors.databricks_volume import DatabricksVolumeConnector, build_databricks_volume
from app.connectors.errors import ConnectorConfigError
from app.connectors.gcs import GCSConnector, build_gcs
from app.connectors.http import HTTPConnector, build_http
from app.connectors.local import LocalConnector
from app.connectors.s3 import S3Connector, build_s3
from app.connectors.sharepoint import SharePointConnector, build_sharepoint

# Registered classes are stored as `type[Any]` rather than `type[StorageConnector]`.
# Every registered class implements `StorageConnector` structurally (each `list`
# method is a real `async def ... yield ...` async generator), but mypy infers a
# bare `async def list(...) -> AsyncIterator[T]: ...` *Protocol stub* (no `yield`)
# as returning `Coroutine[Any, Any, AsyncIterator[T]]` rather than `AsyncIterator[T]`
# directly, which makes it reject a real async-generator implementation as a
# structural match. `build_connector` below casts back to `StorageConnector` at its
# single return boundary, since the CONTRACTS.md interface must stay verbatim.
_REGISTRY: dict[str, type[Any]] = {}


def register(connector_type: str, cls: type[Any]) -> None:
    """Register a connector class under a `connector_type` string."""
    _REGISTRY[connector_type] = cls


def get_connector_class(connector_type: str) -> type[Any]:
    """Look up a connector class by its `connector_type` string.

    Raises :class:`ConnectorConfigError` for an unknown type.
    """
    try:
        return _REGISTRY[connector_type]
    except KeyError as exc:
        raise ConnectorConfigError(f"unknown connector type: {connector_type!r}") from exc


def _build_local(config: dict[str, Any]) -> LocalConnector:
    root = config.get("root")
    if not root:
        raise ConnectorConfigError("local connector config requires 'root'")
    return LocalConnector(
        root=Path(root),
        # Injected by `services.storage.open_storage` from the row's id, not
        # stored in the config JSONB: `signed_url` needs it to address and
        # sign the proxy route.
        connector_id=config.get("connector_id"),
        # Only set when something other than the browser fetches the media —
        # the model service reaching the API by its compose service name.
        public_base_url=config.get("public_base_url"),
        internal_base_url=config.get("internal_base_url"),
    )


def _build_azure_blob(config: dict[str, Any], secret: str | None) -> AzureBlobConnector:
    missing = [key for key in ("account_url", "container", "identity_type") if key not in config]
    if missing:
        raise ConnectorConfigError(
            f"azure_blob connector config missing required key(s): {', '.join(missing)}"
        )
    return AzureBlobConnector(
        account_url=config["account_url"],
        container=config["container"],
        identity_type=config["identity_type"],
        secret=secret,
        tenant_id=config.get("tenant_id"),
        client_id=config.get("client_id"),
        frontend_origin=config.get("frontend_origin"),
        # Set when the browser must use a different URL than the API does:
        # an emulator, a compose service name, a Private Endpoint.
        public_account_url=config.get("public_account_url"),
        # Explicit override for emulators, whose account name is in the URL
        # path rather than the hostname.
        account_name=config.get("account_name"),
    )


def build_connector(
    connector_type: str, config: dict[str, Any], secret: str | None = None
) -> StorageConnector:
    """Construct a connector instance from a `connector` row's config dict.

    `config` is the plain dict stored in `connector.config` (JSONB); `secret`
    is the already-resolved value of `connector.secret_ref` (never the ref
    itself). Raises :class:`ConnectorConfigError` for an unknown
    `connector_type` or an incomplete config.
    """
    cls = get_connector_class(connector_type)  # validates the type is known

    if connector_type == "local":
        return cast(StorageConnector, _build_local(config))
    if connector_type == "azure_blob":
        return cast(StorageConnector, _build_azure_blob(config, secret))

    if connector_type == "s3":
        return cast(StorageConnector, build_s3(config, secret))
    if connector_type == "gcs":
        return cast(StorageConnector, build_gcs(config, secret))
    if connector_type == "sharepoint":
        return cast(StorageConnector, build_sharepoint(config, secret))
    if connector_type == "databricks_volume":
        return cast(StorageConnector, build_databricks_volume(config, secret))

    if connector_type == "http":
        return cast(StorageConnector, build_http(config))

    raise ConnectorConfigError(f"no builder for connector type {connector_type!r}: {cls}")


register("local", LocalConnector)
register("azure_blob", AzureBlobConnector)
register("s3", S3Connector)
register("gcs", GCSConnector)
register("http", HTTPConnector)
register("sharepoint", SharePointConnector)
register("databricks_volume", DatabricksVolumeConnector)
