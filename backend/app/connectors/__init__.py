"""Storage connector plugin interface (SRC-1, ARC-5).

See ``docs/CONTRACTS.md`` ("Storage connector interface" and "Blob layout")
for the full contract. This package imports ``core/`` + ``schemas/`` only,
per the layering rule in that document — in practice the connectors below
take their configuration as explicit constructor arguments and do not
import from either, so they stay independently testable.
"""

from __future__ import annotations

from app.connectors.azure_blob import AzureBlobConnector
from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo, StorageConnector
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
    UnsupportedOperation,
)
from app.connectors.gcs import GCSConnector
from app.connectors.http import HTTPConnector
from app.connectors.local import LocalConnector
from app.connectors.registry import build_connector, get_connector_class, register
from app.connectors.s3 import S3Connector

__all__ = [
    "AzureBlobConnector",
    "BaseStorageConnector",
    "ConnectorAuthError",
    "ConnectorCheck",
    "ConnectorConfigError",
    "ConnectorError",
    "ConnectorNotFound",
    "GCSConnector",
    "HTTPConnector",
    "LocalConnector",
    "ObjectInfo",
    "S3Connector",
    "StorageConnector",
    "UnsupportedOperation",
    "build_connector",
    "get_connector_class",
    "register",
]
