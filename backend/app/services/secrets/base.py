"""The secret-backend interface and reference parsing (AUTH-7).

A reference is a URI naming *where* a credential lives, never the credential
itself. One installation can draw from several stores at once:

    AZURE_STORAGE_KEY                     (bare name = environment variable)
    env:AZURE_STORAGE_KEY
    file:/run/secrets/storage_key
    azurekeyvault://acme-kv/storage-key?version=abc123
    awssecrets://prod/storage-key?region=eu-west-1&key=password
    gcpsecrets://acme-prod/storage-key?version=latest

A bare string with no scheme is an environment variable, so every
configuration written before secret stores existed keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable
from urllib.parse import parse_qsl


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A parsed reference: `scheme://locator?options` (or `scheme:locator`)."""

    raw: str
    scheme: str
    #: What identifies the secret within its store: the variable name for
    #: `env`, the path for `file`, "<vault>/<secret>" for a vault.
    locator: str
    options: dict[str, str] = field(default_factory=dict)

    @property
    def parts(self) -> list[str]:
        """The locator's `/`-separated segments, empty ones dropped."""
        return [part for part in self.locator.split("/") if part]


def parse_ref(raw: str) -> SecretRef:
    """Parse a reference. A bare name is an environment variable.

    Raises `ValueError` for an empty reference or one with nothing after its
    scheme.
    """
    text = raw.strip()
    if not text:
        raise ValueError("empty secret reference")
    options: dict[str, str] = {}
    if "://" in text:
        scheme, _, rest = text.partition("://")
        locator, _, query = rest.partition("?")
        options = dict(parse_qsl(query, keep_blank_values=False))
    elif ":" in text:
        scheme, _, locator = text.partition(":")
    else:
        scheme, locator = "env", text
    scheme = scheme.strip().lower()
    if not scheme or not locator.strip("/ "):
        raise ValueError(f"malformed secret reference {raw!r}")
    return SecretRef(raw=raw, scheme=scheme, locator=locator, options=options)


@runtime_checkable
class SecretBackend(Protocol):
    """One secret store. `get` returns the value or raises.

    Implementations raise rather than return ``None`` for a missing secret:
    ``None`` would reach a connector as "no credential" and try anonymous
    access, turning a configuration mistake into a puzzling 403 elsewhere.
    """

    scheme: ClassVar[str]

    async def get(self, ref: SecretRef) -> str: ...

    async def aclose(self) -> None: ...
