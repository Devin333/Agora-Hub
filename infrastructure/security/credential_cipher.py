"""AES-256-GCM encryption for stored model API keys.

The key ring holds one current key, used for every new ciphertext, and any
number of retired keys that can still open older rows. Rotation is: add the new
key as current, move the old one to the retired list, re-encrypt rows whose
``key_version`` is not current (``needs_reencryption``), then drop the retired
key.
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.foundation.llm_connections.errors import CredentialCipherError
from backend.foundation.llm_connections.ports import CredentialBinding, EncryptedCredential
from framework.shared.secret_text import SecretText

CREDENTIAL_KEY_ENV = "NEWS_LLM_CREDENTIAL_ENCRYPTION_KEY"
CREDENTIAL_KEY_VERSION_ENV = "NEWS_LLM_CREDENTIAL_ENCRYPTION_KEY_VERSION"
RETIRED_CREDENTIAL_KEYS_ENV = "NEWS_LLM_CREDENTIAL_RETIRED_KEYS"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class CredentialKeyConfigurationError(ValueError):
    """The encryption key ring is missing or malformed. Messages never include key material."""


@dataclass(frozen=True)
class CredentialKeyRing:
    current_version: int
    keys: Mapping[int, bytes]

    def __post_init__(self) -> None:
        if not isinstance(self.current_version, int) or self.current_version < 1:
            raise CredentialKeyConfigurationError("current key version must be a positive integer")
        if self.current_version not in self.keys:
            raise CredentialKeyConfigurationError("current key version has no key")
        for version, key in self.keys.items():
            if not isinstance(version, int) or version < 1:
                raise CredentialKeyConfigurationError("key versions must be positive integers")
            if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
                raise CredentialKeyConfigurationError(f"key version {version} must be {_KEY_BYTES} bytes")
        object.__setattr__(self, "keys", dict(self.keys))

    def __repr__(self) -> str:
        return f"CredentialKeyRing(current_version={self.current_version}, versions={sorted(self.keys)})"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> CredentialKeyRing:
        environ = os.environ if environ is None else environ
        raw_current = (environ.get(CREDENTIAL_KEY_ENV) or "").strip()
        if not raw_current:
            raise CredentialKeyConfigurationError(f"{CREDENTIAL_KEY_ENV} is not set")
        version = _parse_version(environ.get(CREDENTIAL_KEY_VERSION_ENV) or "1", CREDENTIAL_KEY_VERSION_ENV)
        keys = {version: decode_credential_key(raw_current, name=CREDENTIAL_KEY_ENV)}
        for entry in (environ.get(RETIRED_CREDENTIAL_KEYS_ENV) or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            raw_version, separator, raw_key = entry.partition(":")
            if not separator:
                raise CredentialKeyConfigurationError(
                    f"{RETIRED_CREDENTIAL_KEYS_ENV} entries must look like <version>:<key>"
                )
            retired_version = _parse_version(raw_version, RETIRED_CREDENTIAL_KEYS_ENV)
            if retired_version in keys:
                raise CredentialKeyConfigurationError(f"key version {retired_version} is configured twice")
            keys[retired_version] = decode_credential_key(raw_key, name=RETIRED_CREDENTIAL_KEYS_ENV)
        return cls(current_version=version, keys=keys)


class AesGcmCredentialCipher:
    def __init__(self, key_ring: CredentialKeyRing) -> None:
        self._current_version = key_ring.current_version
        self._aeads = {version: AESGCM(key) for version, key in key_ring.keys.items()}

    def __repr__(self) -> str:
        return f"AesGcmCredentialCipher(current_version={self._current_version})"

    @property
    def current_version(self) -> int:
        return self._current_version

    def encrypt(self, secret: SecretText, *, binding: CredentialBinding) -> EncryptedCredential:
        if not isinstance(secret, SecretText):
            raise TypeError("secret must be SecretText")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = self._aeads[self._current_version].encrypt(
            nonce, secret.reveal().encode("utf-8"), binding.associated_data()
        )
        return EncryptedCredential(ciphertext=ciphertext, nonce=nonce, key_version=self._current_version)

    def decrypt(self, credential: EncryptedCredential, *, binding: CredentialBinding) -> SecretText:
        aead = self._aeads.get(credential.key_version)
        if aead is None:
            raise CredentialCipherError(f"credential key version {credential.key_version} is not configured")
        if len(credential.nonce) != _NONCE_BYTES:
            raise CredentialCipherError("credential nonce has the wrong length")
        try:
            plaintext = aead.decrypt(credential.nonce, credential.ciphertext, binding.associated_data())
            return SecretText(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise CredentialCipherError("credential failed authentication") from exc

    def needs_reencryption(self, credential: EncryptedCredential) -> bool:
        return credential.key_version != self._current_version


def decode_credential_key(value: str, *, name: str = CREDENTIAL_KEY_ENV) -> bytes:
    encoded = value.strip().encode("ascii", errors="strict") if value.isascii() else None
    if encoded is None:
        raise CredentialKeyConfigurationError(f"{name} must be URL-safe base64")
    try:
        decoded = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CredentialKeyConfigurationError(f"{name} must be URL-safe base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise CredentialKeyConfigurationError(f"{name} must decode to exactly {_KEY_BYTES} bytes")
    return decoded


def generate_credential_key() -> str:
    """A fresh key in the format ``decode_credential_key`` accepts."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode("ascii").rstrip("=")


def _parse_version(raw: str, name: str) -> int:
    try:
        version = int(raw.strip())
    except ValueError as exc:
        raise CredentialKeyConfigurationError(f"{name} key version must be an integer") from exc
    if version < 1:
        raise CredentialKeyConfigurationError(f"{name} key version must be positive")
    return version


__all__ = [
    "CREDENTIAL_KEY_ENV",
    "CREDENTIAL_KEY_VERSION_ENV",
    "RETIRED_CREDENTIAL_KEYS_ENV",
    "AesGcmCredentialCipher",
    "CredentialKeyConfigurationError",
    "CredentialKeyRing",
    "decode_credential_key",
    "generate_credential_key",
]
