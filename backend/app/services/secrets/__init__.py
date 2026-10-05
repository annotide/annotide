"""Secret references and their resolution (AUTH-7, CONTRACTS.md "Secrets").

A `secret_ref` names where a credential lives, never the credential itself.
Anything that builds a connector or calls a model resolves the reference here
first; passing the reference on as if it were the secret authenticates with a
key *name* and fails silently (images that never load).

A reference is a URI naming its store, so one installation can draw from
several at once:

- `AZURE_STORAGE_KEY` or `env:AZURE_STORAGE_KEY` — an environment variable
- `file:/run/secrets/storage_key` — a Docker / Kubernetes mounted secret
- `azurekeyvault://vault/name?version=` — Azure Key Vault
- `awssecrets://name?region=&key=` — AWS Secrets Manager
- `gcpsecrets://project/name?version=` — GCP Secret Manager

`resolve_secret` raises rather than returning None for an unresolvable
reference (None would read as "no credential needed" and try anonymous
access), caches values by TTL so a vault is not called per request, never
caches a failure, and never logs or reports a value: errors carry the
reference only. Cloud SDKs are imported lazily and are optional installs.

Layout: `errors` (exception types), `base` (`SecretRef`, `parse_ref`, the
`SecretBackend` protocol), `backends` (one class per store), `registry`
(`configure`, the cache and `resolve_secret`). Import from the package.
"""

from __future__ import annotations

from app.services.secrets.backends import (
    AwsSecretsBackend,
    AzureKeyVaultBackend,
    EnvBackend,
    FileBackend,
    GcpSecretsBackend,
)
from app.services.secrets.base import SecretBackend, SecretRef, parse_ref
from app.services.secrets.errors import (
    SecretBackendError,
    SecretError,
    SecretResolutionError,
    UnknownSecretSchemeError,
)
from app.services.secrets.registry import (
    DEFAULT_CACHE_TTL,
    aclose_backends,
    clear_cache,
    configure,
    register,
    resolve_secret,
)

configure()

__all__ = [
    "DEFAULT_CACHE_TTL",
    "AwsSecretsBackend",
    "AzureKeyVaultBackend",
    "EnvBackend",
    "FileBackend",
    "GcpSecretsBackend",
    "SecretBackend",
    "SecretBackendError",
    "SecretError",
    "SecretRef",
    "SecretResolutionError",
    "UnknownSecretSchemeError",
    "aclose_backends",
    "clear_cache",
    "configure",
    "parse_ref",
    "register",
    "resolve_secret",
]
