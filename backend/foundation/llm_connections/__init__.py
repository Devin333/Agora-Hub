"""User-configured model connections (BYOK) and per-purpose routing."""
from __future__ import annotations

from backend.foundation.llm_connections.errors import (
    CredentialCipherError,
    ModelConnectionConflictError,
    ModelConnectionNotFoundError,
    ModelConnectionStoreError,
    ModelConnectionStoreReason,
    ModelConnectionStoreUnavailableError,
    ModelConnectionUnavailable,
    ModelConnectionUnavailableReason,
)
from backend.foundation.llm_connections.models import (
    MAX_CONNECTIONS_PER_OWNER,
    ConnectionOwner,
    LLMPurpose,
    ModelCapabilities,
    ModelConnection,
    PurposeAssignment,
)
from backend.foundation.llm_connections.routing import (
    ModelCallWindow,
    ModelConnectionResolver,
    PurposeRoutedLLMClient,
    ResolvedModelConnection,
    connection_call_window,
)
from backend.foundation.llm_connections.scope import (
    current_llm_owner,
    llm_call_scope,
    owner_from_actor_metadata,
)
from backend.foundation.llm_connections.ports import (
    CredentialBinding,
    CredentialCipher,
    EncryptedCredential,
    ModelConnectionRepository,
    StoredConnection,
)

__all__ = [
    "MAX_CONNECTIONS_PER_OWNER",
    "ConnectionOwner",
    "CredentialBinding",
    "CredentialCipher",
    "CredentialCipherError",
    "EncryptedCredential",
    "LLMPurpose",
    "ModelCapabilities",
    "ModelConnection",
    "ModelConnectionConflictError",
    "ModelConnectionNotFoundError",
    "ModelConnectionRepository",
    "ModelConnectionStoreError",
    "ModelConnectionStoreReason",
    "ModelConnectionStoreUnavailableError",
    "ModelConnectionUnavailable",
    "ModelConnectionUnavailableReason",
    "ModelCallWindow",
    "ModelConnectionResolver",
    "PurposeAssignment",
    "PurposeRoutedLLMClient",
    "ResolvedModelConnection",
    "StoredConnection",
    "connection_call_window",
    "current_llm_owner",
    "llm_call_scope",
    "owner_from_actor_metadata",
]
