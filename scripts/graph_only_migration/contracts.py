from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Self


RUN_MAPPING_SCHEMA = "newsroom.graph-history-run-mapping/v1"
MIGRATION_PLAN_SCHEMA = "newsroom.graph-history-migration-plan/v1"
MIGRATION_INVENTORY_SCHEMA = "newsroom.graph-history-migration-inventory/v1"
QUARANTINE_RECORD_SCHEMA = "newsroom.graph-history-quarantine/v1"
GRAPH_EVENT_SCHEMA = "newsroom.graph-history-event/v1"
GRAPH_CHECKPOINT_SCHEMA = "newsroom.graph-history-checkpoint/v1"
GRAPH_REPLAY_BUNDLE_SCHEMA = "newsroom.graph-replay-bundle/v1"
GRAPH_ARTIFACT_INDEX_SCHEMA = "newsroom.graph-artifact-index-record/v1"
GRAPH_CONVERSATION_CURSOR_SCHEMA = "newsroom.graph-conversation-cursor/v1"
GRAPH_ITERATION_CHECKPOINT_SCHEMA = (
    "newsroom.graph-agent-iteration-checkpoint/v1"
)
GRAPH_TERMINAL_MANIFEST_SCHEMA = "newsroom.graph-terminal-manifest/v1"

ZERO_LIVE_SIDE_EFFECT_COUNTS = MappingProxyType(
    {
        "llm": 0,
        "tool_or_mcp": 0,
        "business_worker": 0,
        "retrieval": 0,
        "memory_write": 0,
        "publication": 0,
        "compensation": 0,
        "legacy_executor": 0,
    }
)

_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")
_MOVING_VERSIONS = frozenset({"current", "default", "latest", "stable"})
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "halted", "blocked"}
)


class LegacyRecordKind(StrEnum):
    RUN_MANIFEST = "run_manifest"
    WORKFLOW_EVENT = "workflow_event"
    WORKFLOW_CHECKPOINT = "workflow_checkpoint"
    REPLAY_BUNDLE = "replay_bundle"
    ARTIFACT_INDEX = "artifact_index"
    CONVERSATION_CURSOR = "conversation_cursor"
    ITERATION_CHECKPOINT = "iteration_checkpoint"


class QuarantineReasonCode(StrEnum):
    UNKNOWN_SCHEMA = "unknown_schema"
    MISSING_GRAPH_IDENTITY = "missing_graph_identity"
    MISSING_GATE_EVIDENCE = "missing_gate_evidence"
    MISSING_TERMINAL_EVIDENCE = "missing_terminal_evidence"
    ILLEGAL_SOURCE_PATH = "illegal_source_path"
    ILLEGAL_ARTIFACT_PATH = "illegal_artifact_path"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    EVENT_SEQUENCE_GAP = "event_sequence_gap"
    AMBIGUOUS_RECORD = "ambiguous_record"
    INCOMPATIBLE_CHECKPOINT = "incompatible_checkpoint"
    TARGET_CONFLICT = "target_conflict"
    MALFORMED_SOURCE = "malformed_source"


class ConversionStatus(StrEnum):
    CONVERTED = "converted"
    QUARANTINED = "quarantined"
    SKIPPED_IDEMPOTENT = "skipped_idempotent"


class MigrationContractError(ValueError):
    def __init__(
        self,
        reason_code: QuarantineReasonCode | str,
        message: str,
    ) -> None:
        self.reason_code = QuarantineReasonCode(reason_code)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphReference:
    graph_id: str
    graph_version: str
    graph_schema_version: str
    compiler_version: str
    normalized_graph_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "graph_id", required_identifier(self.graph_id, "graph_id"))
        for field_name in (
            "graph_version",
            "graph_schema_version",
            "compiler_version",
        ):
            object.__setattr__(
                self,
                field_name,
                exact_version(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "normalized_graph_checksum",
            required_checksum(
                self.normalized_graph_checksum,
                "normalized_graph_checksum",
            ),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_schema_version": self.graph_schema_version,
            "compiler_version": self.compiler_version,
            "normalized_graph_checksum": self.normalized_graph_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            {
                "graph_id",
                "graph_version",
                "graph_schema_version",
                "compiler_version",
                "normalized_graph_checksum",
            },
            "graph_ref",
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class GraphNodeBinding:
    legacy_step_id: str
    node_id: str
    node_instance_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "legacy_step_id",
            "node_id",
            "node_instance_id",
            "attempt_id",
        ):
            object.__setattr__(
                self,
                field_name,
                required_identifier(getattr(self, field_name), field_name),
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "legacy_step_id": self.legacy_step_id,
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "attempt_id": self.attempt_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        return cls(
            **exact_keys(
                value,
                {"legacy_step_id", "node_id", "node_instance_id", "attempt_id"},
                "node_binding",
            )
        )


@dataclass(frozen=True, slots=True)
class RunGraphMapping:
    run_id: str
    legacy_workflow_id: str
    legacy_workflow_version: str
    tenant_id: str
    graph_ref: GraphReference
    node_bindings: tuple[GraphNodeBinding, ...]
    default_artifact_step_id: str
    checkpoint_refs: Mapping[str, str]
    terminal_status: str
    terminal_state_ref: str | None
    terminal_node_ids: tuple[str, ...]
    gate_evidence_refs: tuple[str, ...]
    publication_evidence: Mapping[str, Any] | None
    event_first_sequence: int
    owner: str
    schema_version: str = RUN_MAPPING_SCHEMA
    mapping_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != RUN_MAPPING_SCHEMA:
            raise ValueError("unsupported run mapping schema_version")
        for field_name in (
            "run_id",
            "legacy_workflow_id",
            "tenant_id",
        ):
            object.__setattr__(
                self,
                field_name,
                required_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "legacy_workflow_version",
            exact_version(self.legacy_workflow_version, "legacy_workflow_version"),
        )
        if not isinstance(self.graph_ref, GraphReference):
            raise TypeError("graph_ref must be a GraphReference")
        bindings = tuple(self.node_bindings)
        if any(not isinstance(item, GraphNodeBinding) for item in bindings):
            raise TypeError("node_bindings must contain GraphNodeBinding values")
        legacy_ids = [item.legacy_step_id for item in bindings]
        node_instances = [item.node_instance_id for item in bindings]
        if len(set(legacy_ids)) != len(legacy_ids):
            raise ValueError("node_bindings contain duplicate legacy_step_id")
        if len(set(node_instances)) != len(node_instances):
            raise ValueError("node_bindings contain duplicate node_instance_id")
        object.__setattr__(
            self,
            "node_bindings",
            tuple(sorted(bindings, key=lambda item: item.legacy_step_id)),
        )
        default_artifact_step_id = required_identifier(
            self.default_artifact_step_id,
            "default_artifact_step_id",
        )
        if default_artifact_step_id not in set(legacy_ids):
            raise ValueError(
                "default_artifact_step_id has no reviewed Graph node binding"
            )
        object.__setattr__(
            self,
            "default_artifact_step_id",
            default_artifact_step_id,
        )
        checkpoint_refs: dict[str, str] = {}
        checkpoint_prefix = f"graph://runs/{self.run_id}/checkpoints/"
        for raw_checkpoint_id, raw_reference in self.checkpoint_refs.items():
            checkpoint_id = required_identifier(
                raw_checkpoint_id,
                "legacy_checkpoint_id",
            )
            reference = required_reference(
                raw_reference,
                "graph_checkpoint_ref",
            )
            if not reference.startswith(checkpoint_prefix):
                raise ValueError(
                    "graph_checkpoint_ref must remain inside its mapped Graph run"
                )
            required_identifier(
                reference.removeprefix(checkpoint_prefix),
                "graph_checkpoint_id",
            )
            checkpoint_refs[checkpoint_id] = reference
        if len(set(checkpoint_refs.values())) != len(checkpoint_refs):
            raise ValueError("checkpoint_refs must map one-to-one")
        object.__setattr__(
            self,
            "checkpoint_refs",
            MappingProxyType(dict(sorted(checkpoint_refs.items()))),
        )
        status = required_text(self.terminal_status, "terminal_status")
        if status not in _TERMINAL_STATUSES:
            raise ValueError("run mapping terminal_status is not terminal")
        object.__setattr__(self, "terminal_status", status)
        if self.terminal_state_ref is not None:
            object.__setattr__(
                self,
                "terminal_state_ref",
                required_checksum(self.terminal_state_ref, "terminal_state_ref"),
            )
        object.__setattr__(
            self,
            "terminal_node_ids",
            stable_identifiers(self.terminal_node_ids, "terminal_node_ids"),
        )
        bound_node_ids = {item.node_id for item in self.node_bindings}
        if not set(self.terminal_node_ids).issubset(bound_node_ids):
            raise ValueError("terminal_node_ids contain an unmapped Graph node")
        object.__setattr__(
            self,
            "gate_evidence_refs",
            stable_checksums(self.gate_evidence_refs, "gate_evidence_refs"),
        )
        if self.publication_evidence is not None:
            publication = canonicalize_json(self.publication_evidence)
            if not isinstance(publication, Mapping):
                raise TypeError("publication_evidence must be an object")
            object.__setattr__(self, "publication_evidence", publication)
        if (
            isinstance(self.event_first_sequence, bool)
            or not isinstance(self.event_first_sequence, int)
            or self.event_first_sequence < 1
        ):
            raise ValueError("event_first_sequence must be a positive integer")
        object.__setattr__(self, "owner", required_text(self.owner, "owner"))
        object.__setattr__(self, "mapping_checksum", checksum_for(self.content_projection()))

    def content_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "legacy_workflow_id": self.legacy_workflow_id,
            "legacy_workflow_version": self.legacy_workflow_version,
            "tenant_id": self.tenant_id,
            "graph_ref": self.graph_ref.to_dict(),
            "node_bindings": [item.to_dict() for item in self.node_bindings],
            "default_artifact_step_id": self.default_artifact_step_id,
            "checkpoint_refs": dict(self.checkpoint_refs),
            "terminal_status": self.terminal_status,
            "terminal_state_ref": self.terminal_state_ref,
            "terminal_node_ids": list(self.terminal_node_ids),
            "gate_evidence_refs": list(self.gate_evidence_refs),
            "publication_evidence": (
                thaw_json(self.publication_evidence)
                if self.publication_evidence is not None
                else None
            ),
            "event_first_sequence": self.event_first_sequence,
            "owner": self.owner,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_projection(), "mapping_checksum": self.mapping_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            {
                "schema_version",
                "run_id",
                "legacy_workflow_id",
                "legacy_workflow_version",
                "tenant_id",
                "graph_ref",
                "node_bindings",
                "default_artifact_step_id",
                "checkpoint_refs",
                "terminal_status",
                "terminal_state_ref",
                "terminal_node_ids",
                "gate_evidence_refs",
                "publication_evidence",
                "event_first_sequence",
                "owner",
            },
            "run_mapping",
            optional={"mapping_checksum"},
        )
        raw_checksum = value.get("mapping_checksum")
        payload["graph_ref"] = GraphReference.from_dict(mapping(payload["graph_ref"], "graph_ref"))
        payload["node_bindings"] = tuple(
            GraphNodeBinding.from_dict(mapping(item, "node_binding"))
            for item in sequence(payload["node_bindings"], "node_bindings")
        )
        payload["checkpoint_refs"] = mapping(
            payload["checkpoint_refs"],
            "checkpoint_refs",
        )
        payload["terminal_node_ids"] = tuple(
            sequence(payload["terminal_node_ids"], "terminal_node_ids")
        )
        payload["gate_evidence_refs"] = tuple(
            sequence(payload["gate_evidence_refs"], "gate_evidence_refs")
        )
        if payload["publication_evidence"] is not None:
            payload["publication_evidence"] = mapping(
                payload["publication_evidence"],
                "publication_evidence",
            )
        result = cls(**payload)
        if raw_checksum is not None and raw_checksum != result.mapping_checksum:
            raise ValueError("run mapping checksum does not match canonical content")
        return result

    def node_binding(self, legacy_step_id: str) -> GraphNodeBinding:
        normalized = required_identifier(legacy_step_id, "legacy_step_id")
        for binding in self.node_bindings:
            if binding.legacy_step_id == normalized:
                return binding
        raise MigrationContractError(
            QuarantineReasonCode.MISSING_GRAPH_IDENTITY,
            f"no Graph node binding for legacy step {normalized}",
        )

    def checkpoint_ref(self, legacy_checkpoint_id: str) -> str:
        normalized = required_identifier(
            legacy_checkpoint_id,
            "legacy_checkpoint_id",
        )
        try:
            return self.checkpoint_refs[normalized]
        except KeyError as exc:
            raise MigrationContractError(
                QuarantineReasonCode.MISSING_GRAPH_IDENTITY,
                f"no Graph checkpoint binding for legacy checkpoint {normalized}",
            ) from exc


@dataclass(frozen=True, slots=True)
class LegacySourceDescriptor:
    environment: str
    source_store: str
    owner: str
    record_kind: LegacyRecordKind | str
    source_root: Path | str
    relative_path: str
    source_schema_version: str
    source_checksum: str

    def __post_init__(self) -> None:
        for field_name in ("environment", "source_store", "owner"):
            object.__setattr__(
                self,
                field_name,
                required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "record_kind", LegacyRecordKind(self.record_kind))
        object.__setattr__(self, "source_root", Path(self.source_root))
        object.__setattr__(
            self,
            "relative_path",
            required_text(self.relative_path, "relative_path"),
        )
        object.__setattr__(
            self,
            "source_schema_version",
            required_text(self.source_schema_version, "source_schema_version"),
        )
        object.__setattr__(
            self,
            "source_checksum",
            required_checksum(self.source_checksum, "source_checksum"),
        )

    @property
    def source_ref(self) -> str:
        # Built outside the f-string: a backslash in an f-string expression
        # is Python 3.12+ syntax, and the project supports 3.11.
        relative_path = self.relative_path.replace("\\", "/")
        return f"{self.source_store}:{relative_path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "source_store": self.source_store,
            "owner": self.owner,
            "record_kind": self.record_kind.value,
            "source_root": str(self.source_root),
            "relative_path": self.relative_path.replace("\\", "/"),
            "source_schema_version": self.source_schema_version,
            "source_checksum": self.source_checksum,
        }


@dataclass(frozen=True, slots=True)
class LegacyRecord:
    source: LegacySourceDescriptor
    source_record_ref: str
    ordinal: int
    value: Mapping[str, Any]
    source_record_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, LegacySourceDescriptor):
            raise TypeError("source must be a LegacySourceDescriptor")
        object.__setattr__(
            self,
            "source_record_ref",
            required_text(self.source_record_ref, "source_record_ref"),
        )
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise ValueError("ordinal must be a positive integer")
        normalized = canonicalize_json(self.value)
        if not isinstance(normalized, Mapping):
            raise TypeError("legacy record must be a JSON object")
        object.__setattr__(self, "value", normalized)
        object.__setattr__(self, "source_record_checksum", checksum_for(normalized))

    @property
    def record_kind(self) -> LegacyRecordKind:
        return self.source.record_kind


@dataclass(frozen=True, slots=True)
class MigrationProvenance:
    environment: str
    source_store: str
    source_record_ref: str
    source_schema_version: str
    source_checksum: str
    source_record_checksum: str
    mapping_checksum: str | None

    def __post_init__(self) -> None:
        for field_name in (
            "environment",
            "source_store",
            "source_record_ref",
            "source_schema_version",
        ):
            object.__setattr__(
                self,
                field_name,
                required_text(getattr(self, field_name), field_name),
            )
        for field_name in ("source_checksum", "source_record_checksum"):
            object.__setattr__(
                self,
                field_name,
                required_checksum(getattr(self, field_name), field_name),
            )
        if self.mapping_checksum is not None:
            object.__setattr__(
                self,
                "mapping_checksum",
                required_checksum(self.mapping_checksum, "mapping_checksum"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "source_store": self.source_store,
            "source_record_ref": self.source_record_ref,
            "source_schema_version": self.source_schema_version,
            "source_checksum": self.source_checksum,
            "source_record_checksum": self.source_record_checksum,
            "mapping_checksum": self.mapping_checksum,
        }


@dataclass(frozen=True, slots=True)
class TransformedGraphRecord:
    record_kind: LegacyRecordKind | str
    run_id: str | None
    target_ref: str
    target_schema_version: str
    payload: Mapping[str, Any]
    target_checksum: str
    provenance: MigrationProvenance
    authority_mode: str = "staging_only"
    stream_id: str | None = None
    stream_sequence: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "record_kind", LegacyRecordKind(self.record_kind))
        if self.run_id is not None:
            object.__setattr__(self, "run_id", required_identifier(self.run_id, "run_id"))
        object.__setattr__(self, "target_ref", required_reference(self.target_ref, "target_ref"))
        object.__setattr__(
            self,
            "target_schema_version",
            required_text(self.target_schema_version, "target_schema_version"),
        )
        normalized = canonicalize_json(self.payload)
        if not isinstance(normalized, Mapping):
            raise TypeError("Graph migration payload must be an object")
        object.__setattr__(self, "payload", normalized)
        object.__setattr__(
            self,
            "target_checksum",
            required_checksum(self.target_checksum, "target_checksum"),
        )
        checksum_field = (
            "manifest_hash"
            if self.target_schema_version == GRAPH_TERMINAL_MANIFEST_SCHEMA
            else "record_checksum"
        )
        if normalized.get(checksum_field) != self.target_checksum:
            raise ValueError("Graph migration target checksum field does not match")
        checksum_projection = thaw_json(normalized)
        checksum_projection.pop(checksum_field, None)
        if checksum_for(checksum_projection) != self.target_checksum:
            raise ValueError("Graph migration target checksum is not canonical")
        if not isinstance(self.provenance, MigrationProvenance):
            raise TypeError("provenance must be MigrationProvenance")
        if self.authority_mode != "staging_only":
            raise ValueError("Gate A target records must remain staging-only")
        if self.stream_id is not None:
            object.__setattr__(self, "stream_id", required_text(self.stream_id, "stream_id"))
        if self.stream_sequence is not None and (
            isinstance(self.stream_sequence, bool)
            or not isinstance(self.stream_sequence, int)
            or self.stream_sequence < 1
        ):
            raise ValueError("stream_sequence must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_kind": self.record_kind.value,
            "run_id": self.run_id,
            "target_ref": self.target_ref,
            "target_schema_version": self.target_schema_version,
            "target_checksum": self.target_checksum,
            "authority_mode": self.authority_mode,
            "stream_id": self.stream_id,
            "stream_sequence": self.stream_sequence,
            "payload": thaw_json(self.payload),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    environment: str
    source_store: str
    source_record_ref: str
    source_schema_version: str
    source_checksum: str
    source_record_checksum: str | None
    record_kind: LegacyRecordKind | str
    reason_code: QuarantineReasonCode | str
    owner: str
    detail: str
    schema_version: str = QUARANTINE_RECORD_SCHEMA
    disposition: str = "read_only_no_execution"
    quarantine_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != QUARANTINE_RECORD_SCHEMA:
            raise ValueError("unsupported quarantine record schema_version")
        for field_name in (
            "environment",
            "source_store",
            "source_record_ref",
            "source_schema_version",
            "owner",
            "detail",
        ):
            object.__setattr__(
                self,
                field_name,
                required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "source_checksum",
            required_checksum(self.source_checksum, "source_checksum"),
        )
        if self.source_record_checksum is not None:
            object.__setattr__(
                self,
                "source_record_checksum",
                required_checksum(
                    self.source_record_checksum,
                    "source_record_checksum",
                ),
            )
        object.__setattr__(self, "record_kind", LegacyRecordKind(self.record_kind))
        object.__setattr__(self, "reason_code", QuarantineReasonCode(self.reason_code))
        if self.disposition != "read_only_no_execution":
            raise ValueError("quarantine disposition must prohibit execution")
        object.__setattr__(
            self,
            "quarantine_checksum",
            checksum_for(self.content_projection()),
        )

    def content_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment,
            "source_store": self.source_store,
            "source_record_ref": self.source_record_ref,
            "source_schema_version": self.source_schema_version,
            "source_checksum": self.source_checksum,
            "source_record_checksum": self.source_record_checksum,
            "record_kind": self.record_kind.value,
            "reason_code": self.reason_code.value,
            "owner": self.owner,
            "disposition": self.disposition,
            "resume_allowed": False,
            "replay_execution_allowed": False,
            "publication_allowed": False,
            "detail": self.detail,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_projection(),
            "quarantine_checksum": self.quarantine_checksum,
        }


@dataclass(frozen=True, slots=True)
class MigrationInventoryRecord:
    environment: str
    source_store: str
    source_record_ref: str
    source_schema_version: str
    source_checksum: str
    record_kind: LegacyRecordKind | str
    conversion_status: ConversionStatus | str
    target_ref: str | None
    target_checksum: str | None
    quarantine_reason: QuarantineReasonCode | str | None
    owner: str
    schema_version: str = MIGRATION_INVENTORY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != MIGRATION_INVENTORY_SCHEMA:
            raise ValueError("unsupported migration inventory schema_version")
        for field_name in (
            "environment",
            "source_store",
            "source_record_ref",
            "source_schema_version",
            "owner",
        ):
            object.__setattr__(
                self,
                field_name,
                required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "source_checksum",
            required_checksum(self.source_checksum, "source_checksum"),
        )
        object.__setattr__(self, "record_kind", LegacyRecordKind(self.record_kind))
        object.__setattr__(self, "conversion_status", ConversionStatus(self.conversion_status))
        if self.target_ref is not None:
            object.__setattr__(self, "target_ref", required_reference(self.target_ref, "target_ref"))
        if self.target_checksum is not None:
            object.__setattr__(
                self,
                "target_checksum",
                required_checksum(self.target_checksum, "target_checksum"),
            )
        if self.quarantine_reason is not None:
            object.__setattr__(
                self,
                "quarantine_reason",
                QuarantineReasonCode(self.quarantine_reason),
            )
        if self.conversion_status is ConversionStatus.QUARANTINED:
            if self.quarantine_reason is None:
                raise ValueError("quarantined inventory record requires a reason")
        elif self.target_ref is None or self.target_checksum is None:
            raise ValueError("converted/skipped inventory record requires a target")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment,
            "source_store": self.source_store,
            "source_record_ref": self.source_record_ref,
            "source_schema_version": self.source_schema_version,
            "source_checksum": self.source_checksum,
            "record_kind": self.record_kind.value,
            "conversion_status": self.conversion_status.value,
            "target_ref": self.target_ref,
            "target_checksum": self.target_checksum,
            "quarantine_reason": (
                self.quarantine_reason.value
                if self.quarantine_reason is not None
                else None
            ),
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class MigrationPlanItem:
    inventory: MigrationInventoryRecord
    target: TransformedGraphRecord | None = None
    quarantine: QuarantineRecord | None = None

    def __post_init__(self) -> None:
        if self.inventory.conversion_status is ConversionStatus.QUARANTINED:
            if self.target is not None or self.quarantine is None:
                raise ValueError("quarantined item must contain only quarantine evidence")
        elif self.target is None or self.quarantine is not None:
            raise ValueError("converted/skipped item must contain only a target")

    def to_dict(self) -> dict[str, Any]:
        return {
            "inventory": self.inventory.to_dict(),
            "target": self.target.to_dict() if self.target is not None else None,
            "quarantine": (
                self.quarantine.to_dict() if self.quarantine is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    items: tuple[MigrationPlanItem, ...]
    source_aggregate_checksum: str
    mapping_aggregate_checksum: str
    schema_version: str = MIGRATION_PLAN_SCHEMA
    dry_run: bool = True
    plan_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != MIGRATION_PLAN_SCHEMA:
            raise ValueError("unsupported migration plan schema_version")
        if self.dry_run is not True:
            raise ValueError("Gate A migration plan must remain dry-run only")
        ordered = tuple(
            sorted(
                self.items,
                key=lambda item: (
                    item.inventory.environment,
                    item.inventory.source_store,
                    item.inventory.source_record_ref,
                ),
            )
        )
        object.__setattr__(self, "items", ordered)
        object.__setattr__(
            self,
            "source_aggregate_checksum",
            required_checksum(
                self.source_aggregate_checksum,
                "source_aggregate_checksum",
            ),
        )
        object.__setattr__(
            self,
            "mapping_aggregate_checksum",
            required_checksum(
                self.mapping_aggregate_checksum,
                "mapping_aggregate_checksum",
            ),
        )
        object.__setattr__(self, "plan_checksum", checksum_for(self.content_projection()))

    @property
    def counts(self) -> dict[str, int]:
        converted = sum(
            item.inventory.conversion_status is ConversionStatus.CONVERTED
            for item in self.items
        )
        quarantined = sum(
            item.inventory.conversion_status is ConversionStatus.QUARANTINED
            for item in self.items
        )
        skipped = sum(
            item.inventory.conversion_status is ConversionStatus.SKIPPED_IDEMPOTENT
            for item in self.items
        )
        return {
            "inventory": len(self.items),
            "converted": converted,
            "quarantined": quarantined,
            "skipped_idempotent": skipped,
        }

    def content_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dry_run": self.dry_run,
            "source_aggregate_checksum": self.source_aggregate_checksum,
            "mapping_aggregate_checksum": self.mapping_aggregate_checksum,
            "counts": self.counts,
            "side_effect_counts": dict(ZERO_LIVE_SIDE_EFFECT_COUNTS),
            "items": [item.to_dict() for item in self.items],
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_projection(), "plan_checksum": self.plan_checksum}


def provenance_for(
    record: LegacyRecord,
    mapping_value: RunGraphMapping | None,
) -> MigrationProvenance:
    return MigrationProvenance(
        environment=record.source.environment,
        source_store=record.source.source_store,
        source_record_ref=record.source_record_ref,
        source_schema_version=record.source.source_schema_version,
        source_checksum=record.source.source_checksum,
        source_record_checksum=record.source_record_checksum,
        mapping_checksum=(
            mapping_value.mapping_checksum if mapping_value is not None else None
        ),
    )


def canonicalize_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 64:
        raise ValueError("JSON value exceeds maximum nesting depth")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON value contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > 100_000:
            raise ValueError("JSON object exceeds maximum member count")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized[key] = canonicalize_json(item, depth=depth + 1)
        return MappingProxyType(dict(sorted(normalized.items())))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > 100_000:
            raise ValueError("JSON array exceeds maximum item count")
        return tuple(canonicalize_json(item, depth=depth + 1) for item in value)
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        thaw_json(canonicalize_json(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def checksum_for(value: Any) -> str:
    return f"sha256:{sha256(canonical_json_bytes(value)).hexdigest()}"


def required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} contains a control character")
    return value


def required_identifier(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} is not a valid identifier")
    return normalized


def exact_version(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if normalized.lower() in _MOVING_VERSIONS or _VERSION.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be an exact version")
    return normalized


def required_checksum(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if _CHECKSUM.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return normalized


def normalize_checksum(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name).lower()
    if re.fullmatch(r"[0-9a-f]{64}", normalized):
        normalized = f"sha256:{normalized}"
    return required_checksum(normalized, field_name)


def required_reference(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if len(normalized) > 2048 or any(character.isspace() for character in normalized):
        raise ValueError(f"{field_name} is not a valid reference")
    return normalized


def stable_identifiers(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    normalized = tuple(required_identifier(item, field_name) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} contains duplicates")
    return tuple(sorted(normalized))


def stable_checksums(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    normalized = tuple(required_checksum(item, field_name) for item in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{field_name} contains duplicates")
    return tuple(sorted(normalized))


def exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    model_name: str,
    *,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{model_name} must be an object")
    optional = optional or set()
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise ValueError(
            f"{model_name} keys are invalid; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    return {key: value[key] for key in required}


def mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object")
    return value


def sequence(value: Any, field_name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be an array")
    return value


def aware_datetime(value: Any, field_name: str) -> datetime:
    text = required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include an offset")
    return parsed


__all__ = [
    "ConversionStatus",
    "GRAPH_ARTIFACT_INDEX_SCHEMA",
    "GRAPH_CHECKPOINT_SCHEMA",
    "GRAPH_CONVERSATION_CURSOR_SCHEMA",
    "GRAPH_EVENT_SCHEMA",
    "GRAPH_ITERATION_CHECKPOINT_SCHEMA",
    "GRAPH_REPLAY_BUNDLE_SCHEMA",
    "GRAPH_TERMINAL_MANIFEST_SCHEMA",
    "GraphNodeBinding",
    "GraphReference",
    "LegacyRecord",
    "LegacyRecordKind",
    "LegacySourceDescriptor",
    "MIGRATION_INVENTORY_SCHEMA",
    "MIGRATION_PLAN_SCHEMA",
    "MigrationContractError",
    "MigrationInventoryRecord",
    "MigrationPlan",
    "MigrationPlanItem",
    "MigrationProvenance",
    "QUARANTINE_RECORD_SCHEMA",
    "QuarantineReasonCode",
    "QuarantineRecord",
    "RUN_MAPPING_SCHEMA",
    "RunGraphMapping",
    "TransformedGraphRecord",
    "ZERO_LIVE_SIDE_EFFECT_COUNTS",
    "aware_datetime",
    "canonical_json_bytes",
    "canonicalize_json",
    "checksum_for",
    "exact_keys",
    "mapping",
    "normalize_checksum",
    "provenance_for",
    "required_checksum",
    "required_identifier",
    "required_reference",
    "required_text",
    "sequence",
    "thaw_json",
]
