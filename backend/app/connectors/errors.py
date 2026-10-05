"""Exception hierarchy for storage connectors.

All connector implementations raise from this hierarchy so callers can
catch failures without knowing which concrete connector produced them.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for every error raised by a storage connector."""


class ConnectorNotFound(ConnectorError):  # noqa: N818 - "path missing", not an error condition name
    """The requested path does not exist in the backing store."""


class ConnectorAuthError(ConnectorError):
    """The connector could not authenticate or authorise the operation."""


class ConnectorConfigError(ConnectorError):
    """The connector was constructed with an invalid or unusable configuration."""


class UnsupportedOperation(ConnectorError):  # noqa: N818 - matches builtin NotImplementedError style
    """The operation is not supported by this connector (e.g. a phase-1 stub)."""
