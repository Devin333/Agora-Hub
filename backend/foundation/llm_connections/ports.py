"""Storage and encryption ports for model connections."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from backend.foundation.llm_connections.models import (
    ConnectionOwner,
    LLMPurpose,
    ModelConnection,
)
from framework.shared.secret_text import SecretText


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    """An API key as stored: AEAD ciphertext plus what is needed to open it."""

    ciphertext: bytes
    nonce: bytes
    key_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.ciphertext, bytes) or not self.ciphertext:
            raise ValueError("ciphertext must be non-empty bytes")
        if not isinstance(self.nonce, bytes) or not self.nonce:
            raise ValueError("nonce must be non-empty bytes")
        if not isinstance(self.key_version, int) or isinstance(self.key_version, bool) or self.key_version < 1:
            raise ValueError("key_version must be a positive integer")

    def __repr__(self) -> str:
        return f"EncryptedCredential(key_version={self.key_version})"


@dataclass(frozen=True, slots=True)
class CredentialBinding:
    """What a ciphertext is bound to; opening it under any other binding fails."""

    owner: ConnectionOwner
    connection_id: str

    def associated_data(self) -> bytes:
        return "\x1f".join(
            ("newsroom.llm-connection-credential.v1", self.owner.tenant_scope, self.owner.user_id, self.connection_id)
        ).encode("utf-8")


@runtime_checkable
class CredentialCipher(Protocol):
    def encrypt(self, secret: SecretText, *, binding: CredentialBinding) -> EncryptedCredential: ...

    def decrypt(self, credential: EncryptedCredential, *, binding: CredentialBinding) -> SecretText: ...


@dataclass(frozen=True, slots=True)
class StoredConnection:
    connection: ModelConnection
    credential: EncryptedCredential


@runtime_checkable
class ModelConnectionRepository(Protocol):
    """Owner-scoped persistence. Every write takes the revision it read (optimistic lock)."""

    def list_connections(self, owner: ConnectionOwner) -> list[ModelConnection]: ...

    def get_connection(self, owner: ConnectionOwner, connection_id: str) -> ModelConnection: ...

    def load_with_credential(self, owner: ConnectionOwner, connection_id: str) -> StoredConnection: ...

    def create_connection(self, connection: ModelConnection, credential: EncryptedCredential) -> ModelConnection: ...

    def update_connection(
        self,
        connection: ModelConnection,
        *,
        expected_revision: int,
        credential: EncryptedCredential | None = None,
    ) -> ModelConnection:
        """Writes ``connection`` as the next revision; ``credential=None`` keeps the stored key."""
        ...

    def mark_verified(
        self,
        owner: ConnectionOwner,
        connection_id: str,
        *,
        revision: int,
        verified_at: datetime,
    ) -> ModelConnection: ...

    def delete_connection(self, owner: ConnectionOwner, connection_id: str, *, expected_revision: int) -> None:
        """Also removes every purpose assignment pointing at the connection."""
        ...

    def list_assignments(self, owner: ConnectionOwner) -> dict[LLMPurpose, str]: ...

    def replace_assignments(
        self,
        owner: ConnectionOwner,
        assignments: Mapping[LLMPurpose, str],
    ) -> dict[LLMPurpose, str]:
        """Sets the full purpose map; purposes left out become unassigned."""
        ...

    def resolve_purpose(self, owner: ConnectionOwner, purpose: LLMPurpose) -> ModelConnection | None:
        """The connection assigned to ``purpose``, without its credential.

        Callers keep built clients keyed by ``deployment_id`` and only load and
        decrypt the credential when that revision is not cached yet.
        """
        ...


__all__ = [
    "CredentialBinding",
    "CredentialCipher",
    "EncryptedCredential",
    "ModelConnectionRepository",
    "StoredConnection",
]
