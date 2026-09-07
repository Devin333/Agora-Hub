from __future__ import annotations

import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.artifacts.catalog import (
        ArtifactCatalogClaim,
        ArtifactCatalogEntry,
        ArtifactCatalogGcDetachReceipt,
        ArtifactCatalogGcDetachRequest,
        ArtifactCatalogGcPlan,
        ArtifactCatalogReconciliationPlan,
        ArtifactCatalogRegistrationRequest,
        ArtifactCatalogRegistrationResult,
        ArtifactCatalogSnapshot,
        ArtifactLogicalReference,
        ArtifactReferenceRetirementReceipt,
        ArtifactReferenceRetirementRequest,
        ArtifactVerificationReceipt,
    )

from framework.harness.control_plane.errors import HarnessValidationError
from framework.shared.json import to_jsonable


_SHA256_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ArtifactWriteRequest:
    artifact_type: str
    payload: dict[str, Any]
    media_type: str = "application/json"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.artifact_type).strip():
            raise HarnessValidationError("artifact_type is required")
        if not str(self.media_type).strip():
            raise HarnessValidationError("media_type is required")
        object.__setattr__(self, "artifact_type", str(self.artifact_type))
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "payload": to_jsonable(self.payload),
            "media_type": self.media_type,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class ArtifactRef:
    ref: str
    artifact_type: str
    checksum: str
    media_type: str = "application/json"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.ref).strip():
            raise HarnessValidationError("artifact ref is required")
        if not str(self.artifact_type).strip():
            raise HarnessValidationError("artifact_type is required")
        if not str(self.checksum).strip():
            raise HarnessValidationError("checksum is required")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "artifact_type": self.artifact_type,
            "checksum": self.checksum,
            "media_type": self.media_type,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ArtifactReferenceDescriptor:
    """Trusted immutable metadata for one exact artifact reference.

    The descriptor deliberately excludes artifact payload and arbitrary artifact
    metadata. Optional ownership fields are populated only when the backing
    manifest or catalog records them as integrity-protected authority data.
    """

    ref: str
    run_id: str
    artifact_type: str
    checksum: str
    byte_size: int
    media_type: str
    tenant_id: str | None = None
    graph_id: str | None = None
    node_id: str | None = None
    attempt_id: str | None = None
    identity_scope_ref: str | None = None
    subject_scope_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("ref", "run_id", "artifact_type", "media_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise HarnessValidationError(f"{field_name} is required")
        if (
            not isinstance(self.checksum, str)
            or _SHA256_CHECKSUM_PATTERN.fullmatch(self.checksum) is None
        ):
            raise HarnessValidationError(
                "checksum must be a lowercase sha256 checksum"
            )
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise HarnessValidationError("byte_size must be a non-negative integer")
        for field_name in ("tenant_id", "graph_id", "node_id", "attempt_id"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise HarnessValidationError(f"{field_name} must be non-empty")
        for field_name in ("identity_scope_ref", "subject_scope_ref"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str)
                or _SHA256_CHECKSUM_PATTERN.fullmatch(value) is None
            ):
                raise HarnessValidationError(
                    f"{field_name} must be a lowercase sha256 checksum"
                )


@runtime_checkable
class ArtifactPort(Protocol):
    def write_artifact(self, request: ArtifactWriteRequest) -> ArtifactRef:
        ...

    def read_artifact(self, ref: str) -> dict[str, Any]:
        ...


@runtime_checkable
class ArtifactReferenceVerifierPort(Protocol):
    """Verify one canonical artifact ref without returning its payload."""

    def verify_artifact_ref(self, ref: str, *, expected_run_id: str) -> None:
        ...


@runtime_checkable
class ArtifactReferenceDescriptorPort(Protocol):
    """Describe one artifact through trusted metadata without returning payload."""

    def describe_artifact_ref(
        self,
        ref: str,
        *,
        expected_run_id: str,
        expected_tenant_id: str | None = None,
    ) -> ArtifactReferenceDescriptor:
        ...


@runtime_checkable
class GraphResultArtifactReadPort(Protocol):
    """Read an unpublished graph-result body through its ref-only boundary."""

    def read_graph_result_artifact(
        self,
        ref: str,
        *,
        expected_run_id: str,
    ) -> dict[str, Any]:
        ...


@runtime_checkable
class RunBoundArtifactPort(ArtifactPort, Protocol):
    def bind_run(self, run_id: str) -> AbstractContextManager[str]:
        ...


@runtime_checkable
class ArtifactCatalogPort(Protocol):
    """Metadata authority for verified physical artifacts and logical owners."""

    def register(
        self,
        request: "ArtifactCatalogRegistrationRequest",
    ) -> "ArtifactCatalogRegistrationResult":
        ...

    def get(self, entry_id: str) -> "ArtifactCatalogEntry":
        ...

    def get_by_ref(
        self,
        *,
        tenant_id: str,
        ref: str,
    ) -> "ArtifactCatalogEntry":
        ...

    def get_claim(
        self,
        *,
        tenant_id: str,
        run_id: str,
        artifact_id: str,
    ) -> "ArtifactCatalogClaim":
        ...

    def find_by_checksum(
        self,
        *,
        tenant_id: str,
        content_checksum: str,
        media_type: str | None = None,
        producer_revision: str | None = None,
    ) -> tuple["ArtifactCatalogEntry", ...]:
        ...

    def list_by_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> tuple["ArtifactCatalogEntry", ...]:
        ...

    def list_claims_by_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> tuple["ArtifactCatalogClaim", ...]:
        ...

    def list_references(
        self,
        entry_id: str,
    ) -> tuple["ArtifactLogicalReference", ...]:
        ...

    def add_reference(
        self,
        reference: "ArtifactLogicalReference",
    ) -> "ArtifactLogicalReference":
        ...

    def remove_reference(
        self,
        *,
        tenant_id: str,
        reference_id: str,
    ) -> bool:
        ...

    def snapshot(
        self,
        *,
        captured_at: datetime,
        tenant_id: str | None = None,
    ) -> "ArtifactCatalogSnapshot":
        ...

    def retire_reference(
        self,
        request: "ArtifactReferenceRetirementRequest",
    ) -> "ArtifactReferenceRetirementReceipt":
        ...

    def detach_gc_candidate(
        self,
        request: "ArtifactCatalogGcDetachRequest",
    ) -> "ArtifactCatalogGcDetachReceipt":
        ...

    def plan_gc(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
    ) -> "ArtifactCatalogGcPlan":
        ...

    def reconcile(
        self,
        *,
        now: datetime,
        tenant_id: str | None = None,
        physical_inventory: tuple["ArtifactVerificationReceipt", ...] | None = None,
    ) -> "ArtifactCatalogReconciliationPlan":
        ...


__all__ = [
    "ArtifactPort",
    "ArtifactCatalogPort",
    "ArtifactReferenceDescriptor",
    "ArtifactReferenceDescriptorPort",
    "ArtifactReferenceVerifierPort",
    "GraphResultArtifactReadPort",
    "ArtifactRef",
    "ArtifactWriteRequest",
    "RunBoundArtifactPort",
]
