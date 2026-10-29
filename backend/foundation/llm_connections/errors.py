"""Model connection failures with stable, non-sensitive public codes."""
from __future__ import annotations

from enum import StrEnum

from backend.foundation.llm_connections.models import LLMPurpose


class ModelConnectionStoreReason(StrEnum):
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    OWNER_CAPACITY_EXCEEDED = "owner_capacity_exceeded"
    STORE_UNAVAILABLE = "store_unavailable"
    STORE_CORRUPT = "store_corrupt"


class ModelConnectionStoreError(RuntimeError):
    code = "model_connection_store_error"
    public_message = "Model connection storage operation failed."
    retryable = False

    def __init__(self, reason: ModelConnectionStoreReason) -> None:
        self.reason = ModelConnectionStoreReason(reason)
        super().__init__(self.public_message)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.public_message,
            "reason": self.reason.value,
            "retryable": self.retryable,
        }


class ModelConnectionNotFoundError(ModelConnectionStoreError, LookupError):
    code = "model_connection_not_found"
    public_message = "Model connection was not found."

    def __init__(self) -> None:
        super().__init__(ModelConnectionStoreReason.NOT_FOUND)


class ModelConnectionConflictError(ModelConnectionStoreError):
    code = "model_connection_conflict"
    public_message = "Model connection was changed by another request."

    def __init__(self, reason: ModelConnectionStoreReason = ModelConnectionStoreReason.REVISION_CONFLICT) -> None:
        super().__init__(reason)


class ModelConnectionStoreUnavailableError(ModelConnectionStoreError):
    code = "model_connection_store_unavailable"
    public_message = "Model connection storage is unavailable."
    retryable = True

    def __init__(self) -> None:
        super().__init__(ModelConnectionStoreReason.STORE_UNAVAILABLE)


class CredentialCipherError(RuntimeError):
    """A stored credential could not be opened (wrong key, tampering, retired key version)."""

    code = "model_connection_credential_unreadable"
    public_message = "The stored API key can no longer be read; enter it again."


class ModelConnectionUnavailableReason(StrEnum):
    NO_CALLER = "no_caller"
    NOT_ASSIGNED = "not_assigned"
    CREDENTIAL_UNREADABLE = "credential_unreadable"
    # A stored row no longer passes validation; it has to be edited or recreated.
    CONNECTION_INVALID = "connection_invalid"
    STORE_UNAVAILABLE = "store_unavailable"


class ModelConnectionUnavailable(RuntimeError):
    """A model call has no usable connection for its purpose. Nothing falls back to a shared key."""

    code = "model_connection_required"
    public_message = "Configure a model connection for this feature in settings."

    def __init__(self, purpose: LLMPurpose, reason: ModelConnectionUnavailableReason) -> None:
        self.purpose = LLMPurpose(purpose)
        self.reason = ModelConnectionUnavailableReason(reason)
        super().__init__(f"{self.public_message} (purpose={self.purpose.value}, reason={self.reason.value})")

    @property
    def retryable(self) -> bool:
        return self.reason is ModelConnectionUnavailableReason.STORE_UNAVAILABLE

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.public_message,
            "purpose": self.purpose.value,
            "reason": self.reason.value,
            "retryable": self.retryable,
        }


__all__ = [
    "CredentialCipherError",
    "ModelConnectionConflictError",
    "ModelConnectionNotFoundError",
    "ModelConnectionStoreError",
    "ModelConnectionStoreReason",
    "ModelConnectionStoreUnavailableError",
    "ModelConnectionUnavailable",
    "ModelConnectionUnavailableReason",
]
