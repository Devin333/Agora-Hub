from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, Protocol, Self, runtime_checkable

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.transcript import (
    SUBAGENT_BUNDLE_SCHEMA_V3,
    SubAgentAttemptIdentity,
    SubAgentTranscriptReceipt,
    SubAgentTranscriptStoreError,
    subagent_context_ref,
)


SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1 = "newsroom.subagent-attempt-descriptor/v1"


class SubAgentAttemptMetadataIncompleteError(SubAgentTranscriptStoreError):
    """An attempt has only one of its immutable metadata and bundle files."""


@dataclass(frozen=True, slots=True)
class SubAgentAttemptDescriptor:
    """Trusted metadata for authorizing attempt refs without reading payload bytes."""

    identity: SubAgentAttemptIdentity
    receipt: SubAgentTranscriptReceipt
    artifact_refs: tuple[str, ...]
    bundle_checksum: str
    bundle_size_bytes: int
    bundle_schema: str = SUBAGENT_BUNDLE_SCHEMA_V3
    schema_version: str = SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1
    descriptor_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        if not isinstance(self.receipt, SubAgentTranscriptReceipt):
            raise TypeError("receipt must be SubAgentTranscriptReceipt")
        if self.schema_version != SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1:
            raise HarnessValidationError(
                "unsupported subagent attempt descriptor schema",
                code="subagent_attempt_descriptor_schema_unsupported",
            )
        if self.bundle_schema != SUBAGENT_BUNDLE_SCHEMA_V3:
            raise HarnessValidationError(
                "unsupported subagent attempt bundle schema",
                code="subagent_transcript_schema_unsupported",
            )
        artifact_refs = _refs(self.artifact_refs)
        object.__setattr__(self, "artifact_refs", artifact_refs)
        object.__setattr__(self, "bundle_checksum", _checksum(self.bundle_checksum))
        if (
            isinstance(self.bundle_size_bytes, bool)
            or not isinstance(self.bundle_size_bytes, int)
            or self.bundle_size_bytes <= 0
        ):
            raise HarnessValidationError(
                "bundle_size_bytes must be a positive integer",
                code="subagent_attempt_descriptor_invalid",
            )
        _validate_receipt_identity(self.identity, self.receipt)
        object.__setattr__(
            self,
            "descriptor_checksum",
            checksum_for(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "receipt": self.receipt.to_dict(),
            "artifact_refs": list(self.artifact_refs),
            "bundle_checksum": self.bundle_checksum,
            "bundle_size_bytes": self.bundle_size_bytes,
            "bundle_schema": self.bundle_schema,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "descriptor_checksum": self.descriptor_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = {
            "schema_version",
            "identity",
            "receipt",
            "artifact_refs",
            "bundle_checksum",
            "bundle_size_bytes",
            "bundle_schema",
            "descriptor_checksum",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise HarnessValidationError(
                "subagent attempt descriptor fields are invalid",
                code="subagent_attempt_descriptor_invalid",
            )
        payload = dict(value)
        supplied = _checksum(payload.pop("descriptor_checksum"))
        identity_value = payload.pop("identity")
        receipt_value = payload.pop("receipt")
        if not isinstance(identity_value, Mapping) or not isinstance(receipt_value, Mapping):
            raise HarnessValidationError(
                "subagent attempt descriptor identity and receipt must be objects",
                code="subagent_attempt_descriptor_invalid",
            )
        result = cls(
            identity=SubAgentAttemptIdentity.from_dict(identity_value),
            receipt=SubAgentTranscriptReceipt.from_dict(receipt_value),
            **payload,
        )
        if result.descriptor_checksum != supplied:
            raise HarnessValidationError(
                "descriptor_checksum does not match canonical content",
                code="subagent_attempt_descriptor_checksum_mismatch",
            )
        return result


@runtime_checkable
class SubAgentTranscriptDescriptorPort(Protocol):
    def describe_attempt(
        self,
        identity: SubAgentAttemptIdentity,
    ) -> SubAgentAttemptDescriptor | None: ...

    def describe_ref(self, ref: str) -> SubAgentAttemptDescriptor | None: ...


def descriptor_for_bundle(
    *,
    identity: SubAgentAttemptIdentity,
    receipt: SubAgentTranscriptReceipt,
    artifact_refs: Sequence[str],
    bundle: bytes,
) -> SubAgentAttemptDescriptor:
    if not isinstance(bundle, bytes) or not bundle:
        raise TypeError("bundle must be non-empty bytes")
    return SubAgentAttemptDescriptor(
        identity=identity,
        receipt=receipt,
        artifact_refs=tuple(artifact_refs),
        bundle_checksum=f"sha256:{sha256(bundle).hexdigest()}",
        bundle_size_bytes=len(bundle),
    )


def _validate_receipt_identity(
    identity: SubAgentAttemptIdentity,
    receipt: SubAgentTranscriptReceipt,
) -> None:
    expected_transcript_ref = (
        f"subagent-transcript://v3/{identity.parent_run_id}/{identity.transcript_id}"
    )
    expected_output_ref = (
        f"subagent-output://v3/{identity.parent_run_id}/{identity.transcript_id}"
    )
    if (
        receipt.identity_checksum != identity.identity_checksum
        or receipt.transcript_id != identity.transcript_id
        or receipt.invocation_id != identity.invocation_id
        or receipt.parent_run_id != identity.parent_run_id
        or receipt.child_run_id != identity.child_run_id
        or receipt.task_instance_id != identity.task_instance_id
        or receipt.attempt != identity.attempt
        or receipt.transcript_ref != expected_transcript_ref
        or receipt.context_ref != subagent_context_ref(identity)
        or receipt.output_ref != expected_output_ref
    ):
        raise HarnessValidationError(
            "subagent attempt descriptor receipt identity mismatch",
            code="subagent_transcript_identity_mismatch",
        )


def _refs(value: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HarnessValidationError(
            "artifact_refs must be an array",
            code="subagent_attempt_descriptor_invalid",
        )
    refs = tuple(value)
    if any(
        not isinstance(item, str)
        or not item
        or item != item.strip()
        or len(item) > 2048
        for item in refs
    ):
        raise HarnessValidationError(
            "artifact_refs must contain exact non-blank refs",
            code="subagent_attempt_descriptor_invalid",
        )
    if len(refs) != len(set(refs)):
        raise HarnessValidationError(
            "artifact_refs must contain unique refs",
            code="subagent_attempt_descriptor_invalid",
        )
    return refs


def _checksum(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise HarnessValidationError(
            "descriptor checksum fields must be sha256 checksums",
            code="subagent_attempt_descriptor_invalid",
        )
    return value


__all__ = [
    "SUBAGENT_ATTEMPT_DESCRIPTOR_SCHEMA_V1",
    "SubAgentAttemptDescriptor",
    "SubAgentAttemptMetadataIncompleteError",
    "SubAgentTranscriptDescriptorPort",
    "descriptor_for_bundle",
]
