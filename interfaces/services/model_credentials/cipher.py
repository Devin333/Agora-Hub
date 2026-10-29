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

from interfaces.services.model_credentials.errors import ModelCredentialConfigurationError

ENCRYPTION_KEY_ENV = "NEWSROOM_CREDENTIAL_ENCRYPTION_KEY"
_AAD_PREFIX = "agora.model-credential.v1"
_NONCE_BYTES = 12


class CredentialKeyMismatchError(RuntimeError):
    """The record was sealed under a different master key."""


class CredentialTamperedError(RuntimeError):
    """The record does not authenticate for this account and provider."""


@dataclass(frozen=True, slots=True)
class SealedSecret:
    key_id: str
    nonce: str
    ciphertext: str


class CredentialCipher:
    """Seal secrets bound to ``(user_id, provider_id)``.

    The associated data ties every ciphertext to its owner and provider, so a
    record copied into another account, or relabelled with another provider,
    fails authentication instead of decrypting. ``key_id`` identifies the
    master key so a key change is reported as such rather than as tampering.
    """

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ModelCredentialConfigurationError(
                f"{ENCRYPTION_KEY_ENV} must decode to exactly 32 bytes"
            )
        self._aead = AESGCM(master_key)
        self.key_id = hashlib.sha256(master_key).hexdigest()[:16]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CredentialCipher":
        values = env if env is not None else os.environ
        raw = str(values.get(ENCRYPTION_KEY_ENV) or "").strip()
        if not raw:
            raise ModelCredentialConfigurationError(
                f"{ENCRYPTION_KEY_ENV} is required in the hosted profile"
            )
        try:
            master_key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ModelCredentialConfigurationError(
                f"{ENCRYPTION_KEY_ENV} must be base64"
            ) from exc
        return cls(master_key)

    def seal(self, secret: str, *, user_id: str, provider_id: str) -> SealedSecret:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(
            nonce, secret.encode("utf-8"), _associated_data(user_id, provider_id)
        )
        return SealedSecret(
            key_id=self.key_id,
            nonce=_b64(nonce),
            ciphertext=_b64(ciphertext),
        )

    def open(self, sealed: SealedSecret, *, user_id: str, provider_id: str) -> str:
        if sealed.key_id != self.key_id:
            raise CredentialKeyMismatchError("credential was sealed under another master key")
        try:
            plaintext = self._aead.decrypt(
                base64.b64decode(sealed.nonce, validate=True),
                base64.b64decode(sealed.ciphertext, validate=True),
                _associated_data(user_id, provider_id),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise CredentialTamperedError("credential does not authenticate") from exc


def generate_master_key() -> str:
    """Return a new base64 master key for ``NEWSROOM_CREDENTIAL_ENCRYPTION_KEY``."""

    return _b64(os.urandom(32))


def _associated_data(user_id: str, provider_id: str) -> bytes:
    return f"{_AAD_PREFIX}|{user_id}|{provider_id}".encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


__all__ = [
    "CredentialCipher",
    "CredentialKeyMismatchError",
    "CredentialTamperedError",
    "ENCRYPTION_KEY_ENV",
    "SealedSecret",
    "generate_master_key",
]
