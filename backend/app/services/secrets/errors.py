"""Failures that can occur while resolving a secret reference.

Every message carries the *reference* (``env:AZURE_STORAGE_KEY``,
``azurekeyvault://acme-kv/storage-key``), never a value: the reference is safe
to show an operator, and it is what makes a misconfiguration diagnosable.
"""

from __future__ import annotations


class SecretError(Exception):
    """Base class for everything this package raises."""


class SecretBackendError(SecretError):
    """A secret store cannot be used at all — typically its SDK is not installed.

    Distinct from :class:`SecretResolutionError` inside a backend: "the vault
    is unusable" and "the vault has no such secret" call for different operator
    responses. `resolve_secret` wraps it into a `SecretResolutionError`, so
    callers only ever catch one type.
    """


class SecretResolutionError(SecretError):
    """A reference could not be turned into a value. Names the reference, never a value."""

    def __init__(self, secret_ref: str, reason: str) -> None:
        super().__init__(f"cannot resolve secret {secret_ref!r}: {reason}")
        self.secret_ref = secret_ref
        self.reason = reason


class UnknownSecretSchemeError(SecretResolutionError):
    """The reference names a store no backend is registered for."""
