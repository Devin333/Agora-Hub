"""AES-256-GCM sealing of per-account model API keys."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from interfaces.services.model_connections.errors import ModelConnectionConfigurationError

ENCRYPTION_KEY_ENV = "NEWSROOM_CREDENTIAL_ENCRYPTION_KEY"
_NONCE_BYTES = 12


class SecretKeyMismatchError(RuntimeError):
    """The secret was sealed under a different master key."""


class SecretTamperedError(RuntimeError):
    """The secret does not authenticate for the binding it was opened with."""


@dataclass(frozen=True, slots=True)
class SealedSecret:
    key_id: str
    nonce: str
    ciphertext: str


@dataclass(frozen=True, slots=True)
class SecretBinding:
    """What a ciphertext is bound to; opening it under any other binding fails.

    The binding is the AEAD associated data, so a ciphertext copied into
    another account or onto another connection does not decrypt.
    """

    domain: str
    parts: tuple[str, ...]

    def associated_data(self) -> bytes:
        return "|".join((self.domain, *self.parts)).encode("utf-8")


def connection_binding(user_id: str, connection_id: str) -> SecretBinding:
    return SecretBinding("agora.model-connection.v2", (user_id, connection_id))


def single_key_credential_binding(user_id: str, provider_id: str) -> SecretBinding:
    """Binding of the single per-account key stored before connections existed."""
    return SecretBinding("agora.model-credential.v1", (user_id, provider_id))


class CredentialCipher:
    """Seal secrets under one master key; ``key_id`` tells a key change from tampering."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ModelConnectionConfigurationError(
                f"{ENCRYPTION_KEY_ENV} must decode to exactly 32 bytes"
            )
        self._aead = AESGCM(master_key)
        self.key_id = hashlib.sha256(master_key).hexdigest()[:16]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CredentialCipher:
        values = env if env is not None else os.environ
        raw = str(values.get(ENCRYPTION_KEY_ENV) or "").strip()
        if not raw:
            raise ModelConnectionConfigurationError(
                f"{ENCRYPTION_KEY_ENV} is required in the hosted profile"
            )
        try:
            master_key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ModelConnectionConfigurationError(f"{ENCRYPTION_KEY_ENV} must be base64") from exc
        return cls(master_key)

    def seal(self, secret: str, binding: SecretBinding) -> SealedSecret:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, secret.encode("utf-8"), binding.associated_data())
        return SealedSecret(key_id=self.key_id, nonce=_b64(nonce), ciphertext=_b64(ciphertext))

    def open(self, sealed: SealedSecret, binding: SecretBinding) -> str:
        if sealed.key_id != self.key_id:
            raise SecretKeyMismatchError("secret was sealed under another master key")
        try:
            plaintext = self._aead.decrypt(
                base64.b64decode(sealed.nonce, validate=True),
                base64.b64decode(sealed.ciphertext, validate=True),
                binding.associated_data(),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise SecretTamperedError("secret does not authenticate") from exc


def generate_master_key() -> str:
    """Return a new base64 master key for ``NEWSROOM_CREDENTIAL_ENCRYPTION_KEY``."""

    return _b64(os.urandom(32))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


__all__ = [
    "CredentialCipher",
    "ENCRYPTION_KEY_ENV",
    "SealedSecret",
    "SecretBinding",
    "SecretKeyMismatchError",
    "SecretTamperedError",
    "connection_binding",
    "generate_master_key",
    "single_key_credential_binding",
]
