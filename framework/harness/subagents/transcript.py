from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

from framework.events.canonical import checksum_for, normalize_canonical_json
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.canonical import exact_reference
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from framework.shared.json import stable_json_dumps, to_jsonable
from framework.shared.redaction import redact_sensitive_values
from framework.shared.time import format_datetime, parse_datetime, utc_now

if TYPE_CHECKING:
    from framework.harness.subagents.transcript_metadata import SubAgentAttemptDescriptor


SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3 = "newsroom.subagent-attempt-identity/v3"
SUBAGENT_CONTEXT_SCHEMA_V3 = "newsroom.subagent-context-evidence/v3"
SUBAGENT_OUTPUT_SCHEMA_V3 = "newsroom.subagent-output-document/v3"
SUBAGENT_TRANSCRIPT_SCHEMA_V3 = "newsroom.subagent-transcript/v3"
SUBAGENT_RECEIPT_SCHEMA_V3 = "newsroom.subagent-transcript-receipt/v3"
SUBAGENT_BUNDLE_SCHEMA_V3 = "newsroom.subagent-attempt-bundle/v3"
SUBAGENT_CONTEXT_SCHEMA = SUBAGENT_CONTEXT_SCHEMA_V3
SUBAGENT_OUTPUT_SCHEMA = SUBAGENT_OUTPUT_SCHEMA_V3
SUBAGENT_TRANSCRIPT_SCHEMA = SUBAGENT_TRANSCRIPT_SCHEMA_V3
SUBAGENT_RECEIPT_SCHEMA = SUBAGENT_RECEIPT_SCHEMA_V3
SUBAGENT_BUNDLE_SCHEMA = SUBAGENT_BUNDLE_SCHEMA_V3
DEFAULT_MAX_TRANSCRIPT_BYTES = 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_BUNDLE_BYTES = 12 * 1024 * 1024
MAX_PARENT_QUERY = 256
_REASON_CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_EXACT_SCHEMA_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*/v[1-9][0-9]*\Z")
_IDENTITY_FIELDS = frozenset({
    "schema_version", "invocation_id", "parent_run_id", "child_run_id",
    "graph_id", "graph_version", "graph_ref", "graph_schema_version",
    "compiler_version", "condition_policy_version", "graph_checksum", "stage_id",
    "stage_binding_checksum", "stage_identity_schema", "stage_identity_checksum",
    "plan_id", "plan_version", "plan_checksum", "task_id",
    "task_definition_checksum", "context_envelope_id", "context_envelope_checksum",
    "node_id", "node_instance_id", "activity_id", "activity_attempt",
    "task_instance_id", "attempt", "subagent_id",
})
_FORBIDDEN_KEYS = frozenset({
    "parent_raw_messages", "sibling_raw_history", "sibling_private_notes",
    "hidden_prompt", "raw_payload", "full_text", "authorization", "apikey",
    "cookie", "api_key", "access_token", "refresh_token", "password", "secret",
    "client_secret", "credential", "private_key", "token", "dsn",
})


class SubAgentTranscriptStoreError(HarnessValidationError):
    """Stable typed failure from a transcript persistence boundary."""


class SubAgentTranscriptConflictError(SubAgentTranscriptStoreError):
    pass


class SubAgentTranscriptCorruptError(SubAgentTranscriptStoreError):
    pass


def _required(value: Any, field_name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise HarnessValidationError(f"{field_name} must be a non-blank trimmed string", code="subagent_transcript_invalid_field", details={"field": field_name})
    if len(value) > max_length:
        raise HarnessValidationError(f"{field_name} exceeds its maximum length", code="subagent_transcript_field_too_long", details={"field": field_name, "max_length": max_length})
    return value


def _optional(value: Any, field_name: str, *, max_length: int = 512) -> str | None:
    return None if value is None else _required(value, field_name, max_length=max_length)


def _positive(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HarnessValidationError(f"{field_name} must be a positive integer", code="subagent_transcript_invalid_field", details={"field": field_name})
    return value


def _checksum(value: Any, field_name: str) -> str:
    text = _required(value, field_name, max_length=71)
    if not text.startswith("sha256:") or len(text) != 71:
        raise HarnessValidationError(f"{field_name} must be a sha256 checksum", code="subagent_transcript_invalid_checksum", details={"field": field_name})
    return text


def _refs(value: Sequence[str], field_name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HarnessValidationError(f"{field_name} must be an array", code="subagent_transcript_invalid_refs")
    refs = tuple(_required(str(item), field_name, max_length=2048) for item in value)
    if not allow_empty and not refs:
        raise HarnessValidationError(f"{field_name} must not be empty", code="subagent_transcript_invalid_refs")
    if len(refs) != len(set(refs)):
        raise HarnessValidationError(f"{field_name} must contain unique refs", code="subagent_transcript_duplicate_ref")
    return refs


def _mapping(value: Any, field_name: str, *, max_bytes: int | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(f"{field_name} must be an object", code="subagent_transcript_invalid_mapping")
    try:
        payload = normalize_canonical_json(_sanitize_value(dict(value), path=f"$.{field_name}"), path=f"$.{field_name}")
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError(f"{field_name} must be canonical JSON", code="subagent_transcript_noncanonical_payload") from exc
    if not isinstance(payload, Mapping):
        raise HarnessValidationError(f"{field_name} must remain an object", code="subagent_transcript_invalid_mapping")
    if max_bytes is not None and len(stable_json_dumps(payload).encode("utf-8")) > max_bytes:
        raise HarnessValidationError(f"{field_name} exceeds its size limit", code="subagent_transcript_size_exceeded", details={"field": field_name, "max_bytes": max_bytes})
    return payload


def sanitize_subagent_payload(value: Mapping[str, Any], *, field_name: str = "output", max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> dict[str, Any]:
    payload = to_jsonable(_mapping(value, field_name, max_bytes=max_bytes))
    if not isinstance(payload, dict):
        raise HarnessValidationError(f"{field_name} must remain an object", code="subagent_transcript_invalid_mapping")
    return payload


def _sanitize_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            normalized = text.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or normalized.endswith("_raw"):
                raise HarnessValidationError("subagent evidence contains forbidden private fields", code="subagent_transcript_private_content_rejected", details={"field": f"{path}.{key}"})
            if text in result:
                raise HarnessValidationError("subagent evidence contains colliding canonical keys", code="subagent_transcript_noncanonical_payload", details={"field": f"{path}.{text}"})
            result[text] = _sanitize_value(item, path=f"{path}.{text}")
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, str):
        if redact_sensitive_values(value) != value:
            raise HarnessValidationError("subagent evidence contains a secret-like value", code="subagent_transcript_secret_content_rejected", details={"field": path})
    return value


def _timestamp(value: Any, field_name: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise HarnessValidationError(f"{field_name} must be a timezone-aware timestamp", code="subagent_transcript_invalid_timestamp")
    return parsed


def _verify_checksum(payload: Mapping[str, Any], supplied: str, field_name: str) -> None:
    if checksum_for(payload) != supplied:
        raise HarnessValidationError(f"{field_name} does not match canonical content", code="subagent_transcript_checksum_mismatch", details={"field": field_name})


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], model: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(f"{model} must be an object", code="subagent_transcript_invalid_payload")
    actual = set(value)
    if actual != expected:
        raise HarnessValidationError(f"{model} fields are invalid", code="subagent_transcript_invalid_payload", details={"missing": sorted(expected - actual), "unexpected": sorted(str(item) for item in actual - expected)})
    return dict(value)


@dataclass(frozen=True, slots=True)
class SubAgentAttemptIdentity:
    invocation_id: str
    parent_run_id: str
    child_run_id: str
    graph_id: str
    graph_version: str
    graph_ref: str
    graph_schema_version: str
    compiler_version: str
    condition_policy_version: str
    graph_checksum: str
    stage_id: str
    stage_binding_checksum: str
    stage_identity_schema: str
    stage_identity_checksum: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    task_id: str
    task_definition_checksum: str
    context_envelope_id: str
    context_envelope_checksum: str
    node_id: str
    node_instance_id: str
    activity_id: str
    activity_attempt: int
    task_instance_id: str
    attempt: int
    subagent_id: str
    schema_version: str = SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3

    def __post_init__(self) -> None:
        if self.schema_version != SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3:
            raise HarnessValidationError("unsupported SubAgent attempt identity schema", code="subagent_transcript_schema_unsupported")
        for field_name in (
            "invocation_id", "parent_run_id", "child_run_id", "graph_id", "graph_version",
            "graph_ref", "graph_schema_version", "compiler_version", "condition_policy_version",
            "stage_id", "stage_identity_schema", "plan_id", "task_id", "context_envelope_id",
            "node_id", "node_instance_id", "activity_id", "task_instance_id", "subagent_id",
        ):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        for field_name in (
            "graph_checksum", "stage_binding_checksum", "stage_identity_checksum", "plan_checksum",
            "task_definition_checksum", "context_envelope_checksum",
        ):
            object.__setattr__(self, field_name, _checksum(getattr(self, field_name), field_name))
        object.__setattr__(self, "attempt", _positive(self.attempt, "attempt"))
        object.__setattr__(
            self,
            "activity_attempt",
            _positive(self.activity_attempt, "activity_attempt"),
        )
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version <= 0:
            raise HarnessValidationError("plan_version must be a positive integer", code="subagent_transcript_identity_mismatch")
        if exact_reference(self.graph_ref, "graph_ref") != f"{self.graph_id}@{self.graph_version}":
            raise HarnessValidationError("Graph-only SubAgent attempt ref does not match its Graph identity", code="subagent_transcript_identity_mismatch")
        if self.graph_schema_version != GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA or self.compiler_version != HARNESS_GRAPH_ONLY_COMPILER_VERSION or self.condition_policy_version != HARNESS_CONDITION_POLICY_VERSION:
            raise HarnessValidationError("Graph-only SubAgent attempt uses an unsupported Graph authority", code="subagent_transcript_schema_unsupported")
        if _EXACT_SCHEMA_PATTERN.fullmatch(self.stage_identity_schema) is None:
            raise HarnessValidationError("Graph-only SubAgent attempt requires an exact stage identity schema", code="subagent_transcript_schema_unsupported")

    @property
    def is_graph_only(self) -> bool:
        return True

    def to_dict(self) -> dict[str, Any]:
        return {field_name: getattr(self, field_name) for field_name in _IDENTITY_FIELDS}

    @property
    def identity_checksum(self) -> str:
        return checksum_for(self.to_dict())

    @property
    def transcript_id(self) -> str:
        return f"sat_{self.identity_checksum.removeprefix('sha256:')}"

    @property
    def result_attempt_id(self) -> str:
        return f"subagent_{self.identity_checksum.removeprefix('sha256:')}"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        return cls(**_require_exact_fields(value, _IDENTITY_FIELDS, cls.__name__))


@dataclass(frozen=True, slots=True)
class SubAgentEvidenceSchemas:
    context: str = SUBAGENT_CONTEXT_SCHEMA_V3
    output: str = SUBAGENT_OUTPUT_SCHEMA_V3
    transcript: str = SUBAGENT_TRANSCRIPT_SCHEMA_V3
    receipt: str = SUBAGENT_RECEIPT_SCHEMA_V3
    bundle: str = SUBAGENT_BUNDLE_SCHEMA_V3


def subagent_evidence_schemas(identity: SubAgentAttemptIdentity) -> SubAgentEvidenceSchemas:
    if not isinstance(identity, SubAgentAttemptIdentity):
        raise TypeError("identity must be SubAgentAttemptIdentity")
    return SubAgentEvidenceSchemas()


def _ref_version(schema_version: str) -> str:
    if schema_version.endswith("/v3"):
        return "v3"
    raise HarnessValidationError("unsupported SubAgent evidence ref schema", code="subagent_transcript_schema_unsupported")


def _context_ref(identity: SubAgentAttemptIdentity, schema_version: str = SUBAGENT_CONTEXT_SCHEMA_V3) -> str:
    if schema_version != SUBAGENT_CONTEXT_SCHEMA_V3:
        raise HarnessValidationError("unsupported subagent context schema", code="subagent_transcript_schema_unsupported")
    return f"subagent-context://v3/{identity.parent_run_id}/{identity.transcript_id}"


def subagent_context_ref(identity: SubAgentAttemptIdentity) -> str:
    return _context_ref(identity)


@dataclass(frozen=True, slots=True)
class SubAgentContextEvidence:
    identity: SubAgentAttemptIdentity
    context_envelope_ref: str
    input_refs: tuple[str, ...]
    memory_context_refs: tuple[str, ...]
    redaction_report: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SUBAGENT_CONTEXT_SCHEMA_V3
    context_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SubAgentAttemptIdentity) or self.schema_version != SUBAGENT_CONTEXT_SCHEMA_V3:
            raise HarnessValidationError("unsupported subagent context schema", code="subagent_transcript_schema_unsupported")
        object.__setattr__(self, "context_envelope_ref", _required(self.context_envelope_ref, "context_envelope_ref", max_length=2048))
        object.__setattr__(self, "input_refs", _refs(self.input_refs, "input_refs", allow_empty=False))
        object.__setattr__(self, "memory_context_refs", _refs(self.memory_context_refs, "memory_context_refs"))
        object.__setattr__(self, "redaction_report", _mapping(self.redaction_report, "redaction_report"))
        object.__setattr__(self, "context_checksum", checksum_for(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "identity": self.identity.to_dict(), "context_envelope_ref": self.context_envelope_ref, "input_refs": list(self.input_refs), "memory_context_refs": list(self.memory_context_refs), "redaction_report": to_jsonable(self.redaction_report)}

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "context_checksum": self.context_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = frozenset({"schema_version", "identity", "context_envelope_ref", "input_refs", "memory_context_refs", "redaction_report", "context_checksum"})
        payload = _require_exact_fields(value, expected, cls.__name__)
        supplied = _checksum(payload.pop("context_checksum"), "context_checksum")
        identity = SubAgentAttemptIdentity.from_dict(payload.pop("identity"))
        result = cls(identity=identity, **payload)
        _verify_checksum(result.checksum_projection(), supplied, "context_checksum")
        return result


@dataclass(frozen=True, slots=True)
class SubAgentOutputDocument:
    identity: SubAgentAttemptIdentity
    status: str
    output: Mapping[str, Any]
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    schema_version: str = SUBAGENT_OUTPUT_SCHEMA_V3
    output_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SubAgentAttemptIdentity) or self.schema_version != SUBAGENT_OUTPUT_SCHEMA_V3:
            raise HarnessValidationError("unsupported subagent output schema", code="subagent_transcript_schema_unsupported")
        object.__setattr__(self, "status", _required(self.status, "status", max_length=32))
        if self.status not in {"succeeded", "failed", "halted", "blocked"}:
            raise HarnessValidationError("unsupported subagent output status", code="subagent_transcript_invalid_status")
        object.__setattr__(self, "output", _mapping(self.output, "output", max_bytes=DEFAULT_MAX_OUTPUT_BYTES))
        object.__setattr__(self, "artifact_refs", _refs(self.artifact_refs, "artifact_refs"))
        object.__setattr__(self, "error_code", _optional(self.error_code, "error_code", max_length=128))
        object.__setattr__(self, "output_checksum", checksum_for(self.checksum_projection()))

    @property
    def ref(self) -> str:
        return f"subagent-output://v3/{self.identity.parent_run_id}/{self.identity.transcript_id}"

    def checksum_projection(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "identity": self.identity.to_dict(), "status": self.status, "output": to_jsonable(self.output), "artifact_refs": list(self.artifact_refs), "error_code": self.error_code}

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "output_checksum": self.output_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = frozenset({"schema_version", "identity", "status", "output", "artifact_refs", "error_code", "output_checksum"})
        payload = _require_exact_fields(value, expected, cls.__name__)
        supplied = _checksum(payload.pop("output_checksum"), "output_checksum")
        identity = SubAgentAttemptIdentity.from_dict(payload.pop("identity"))
        result = cls(identity=identity, **payload)
        _verify_checksum(result.checksum_projection(), supplied, "output_checksum")
        return result


def _bounded_codes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) > 128:
        raise HarnessValidationError(f"{field_name} must be a bounded array", code="subagent_transcript_invalid_codes")
    result = tuple(_required(str(value), field_name, max_length=128) for value in values)
    if any(_REASON_CODE_PATTERN.fullmatch(item) is None for item in result):
        raise HarnessValidationError(f"{field_name} must contain stable reason codes", code="subagent_transcript_invalid_codes")
    return result


def _gate_evidence(value: Mapping[str, Any]) -> Mapping[str, Any]:
    evidence = _mapping(value, "gate_results")
    expected = {"gate_id", "gate_version", "input_checksum", "passed", "reason_code", "evidence_checksum"}
    if set(evidence) != expected or not isinstance(evidence["passed"], bool):
        raise HarnessValidationError("subagent gate evidence fields are invalid", code="subagent_transcript_gate_evidence_invalid")
    projection = {"gate_id": _required(evidence["gate_id"], "gate_id", max_length=128), "gate_version": _required(evidence["gate_version"], "gate_version", max_length=32), "input_checksum": _checksum(evidence["input_checksum"], "input_checksum"), "passed": evidence["passed"], "reason_code": _bounded_codes((str(evidence["reason_code"]),), "reason_code")[0]}
    supplied = _checksum(evidence["evidence_checksum"], "evidence_checksum")
    _verify_checksum(projection, supplied, "evidence_checksum")
    return normalize_canonical_json({**projection, "evidence_checksum": supplied}, path="$.gate_results")


@dataclass(frozen=True, slots=True)
class SubAgentTranscript:
    identity: SubAgentAttemptIdentity
    context_envelope_ref: str
    input_refs: tuple[str, ...] = ()
    tool_call_refs: tuple[str, ...] = ()
    memory_context_refs: tuple[str, ...] = ()
    output_ref: str | None = None
    output_checksum: str | None = None
    artifact_refs: tuple[str, ...] = ()
    gate_results: tuple[Mapping[str, Any], ...] = ()
    budget_snapshot: Mapping[str, Any] = field(default_factory=dict)
    redaction_report: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    events: tuple[Mapping[str, Any], ...] = ()
    observed_at: datetime = field(default_factory=utc_now)
    schema_version: str = SUBAGENT_TRANSCRIPT_SCHEMA_V3
    transcript_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SubAgentAttemptIdentity) or self.schema_version != SUBAGENT_TRANSCRIPT_SCHEMA_V3:
            raise HarnessValidationError("unsupported subagent transcript schema", code="subagent_transcript_schema_unsupported")
        object.__setattr__(self, "context_envelope_ref", _required(self.context_envelope_ref, "context_envelope_ref", max_length=2048))
        object.__setattr__(self, "input_refs", _refs(self.input_refs, "input_refs", allow_empty=False))
        object.__setattr__(self, "tool_call_refs", _refs(self.tool_call_refs, "tool_call_refs"))
        object.__setattr__(self, "memory_context_refs", _refs(self.memory_context_refs, "memory_context_refs"))
        object.__setattr__(self, "output_ref", _optional(self.output_ref, "output_ref", max_length=2048))
        if self.output_checksum is not None:
            object.__setattr__(self, "output_checksum", _checksum(self.output_checksum, "output_checksum"))
        object.__setattr__(self, "artifact_refs", _refs(self.artifact_refs, "artifact_refs"))
        object.__setattr__(self, "gate_results", tuple(_gate_evidence(item) for item in self.gate_results))
        object.__setattr__(self, "budget_snapshot", _mapping(self.budget_snapshot, "budget_snapshot"))
        object.__setattr__(self, "redaction_report", _mapping(self.redaction_report, "redaction_report"))
        object.__setattr__(self, "warnings", _bounded_codes(self.warnings, "warnings"))
        object.__setattr__(self, "errors", _bounded_codes(self.errors, "errors"))
        object.__setattr__(self, "events", tuple(_mapping(item, "events") for item in self.events))
        object.__setattr__(self, "observed_at", _timestamp(self.observed_at, "observed_at"))
        object.__setattr__(self, "transcript_checksum", checksum_for(self.checksum_projection()))
        if len(stable_json_dumps(self.to_dict(include_checksum=False)).encode("utf-8")) > DEFAULT_MAX_TRANSCRIPT_BYTES:
            raise HarnessValidationError("subagent transcript exceeds size limit", code="subagent_transcript_size_exceeded")

    @property
    def transcript_id(self) -> str:
        return self.identity.transcript_id

    @property
    def invocation_id(self) -> str:
        return self.identity.invocation_id

    @property
    def parent_run_id(self) -> str:
        return self.identity.parent_run_id

    @property
    def child_run_id(self) -> str:
        return self.identity.child_run_id

    @property
    def subagent_id(self) -> str:
        return self.identity.subagent_id

    @property
    def ref(self) -> str:
        return f"subagent-transcript://v3/{self.identity.parent_run_id}/{self.transcript_id}"

    def checksum_projection(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "identity": self.identity.to_dict(), "context_envelope_ref": self.context_envelope_ref, "input_refs": list(self.input_refs), "tool_call_refs": list(self.tool_call_refs), "memory_context_refs": list(self.memory_context_refs), "output_ref": self.output_ref, "output_checksum": self.output_checksum, "artifact_refs": list(self.artifact_refs), "gate_results": to_jsonable(list(self.gate_results)), "budget_snapshot": to_jsonable(self.budget_snapshot), "redaction_report": to_jsonable(self.redaction_report), "warnings": list(self.warnings), "errors": list(self.errors), "events": to_jsonable(list(self.events)), "observed_at": format_datetime(self.observed_at)}

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload = self.checksum_projection()
        if include_checksum:
            payload["transcript_checksum"] = self.transcript_checksum
        return {**payload, "transcript_id": self.transcript_id, "ref": self.ref}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = frozenset({"schema_version", "identity", "context_envelope_ref", "input_refs", "tool_call_refs", "memory_context_refs", "output_ref", "output_checksum", "artifact_refs", "gate_results", "budget_snapshot", "redaction_report", "warnings", "errors", "events", "observed_at", "transcript_checksum", "transcript_id", "ref"})
        payload = _require_exact_fields(value, expected, cls.__name__)
        supplied_id = _required(payload.pop("transcript_id"), "transcript_id")
        supplied_ref = _required(payload.pop("ref"), "ref", max_length=2048)
        supplied = _checksum(payload.pop("transcript_checksum"), "transcript_checksum")
        identity = SubAgentAttemptIdentity.from_dict(payload.pop("identity"))
        result = cls(identity=identity, **payload)
        _verify_checksum(result.checksum_projection(), supplied, "transcript_checksum")
        if supplied_id != result.transcript_id or supplied_ref != result.ref:
            raise HarnessValidationError("subagent transcript ref identity does not match canonical content", code="subagent_transcript_identity_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class SubAgentTranscriptReceipt:
    transcript_ref: str
    transcript_checksum: str
    transcript_id: str
    invocation_id: str
    parent_run_id: str
    child_run_id: str
    task_instance_id: str
    attempt: int
    context_ref: str
    context_checksum: str
    output_ref: str
    output_checksum: str
    storage_revision: str
    committed_at: datetime
    identity_checksum: str
    schema_version: str = SUBAGENT_RECEIPT_SCHEMA_V3
    receipt_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != SUBAGENT_RECEIPT_SCHEMA_V3:
            raise HarnessValidationError("unsupported subagent receipt schema", code="subagent_transcript_schema_unsupported")
        for field_name in ("transcript_ref", "context_ref", "output_ref", "storage_revision"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name, max_length=2048))
        for field_name in ("transcript_checksum", "context_checksum", "output_checksum", "identity_checksum"):
            object.__setattr__(self, field_name, _checksum(getattr(self, field_name), field_name))
        for field_name in ("transcript_id", "invocation_id", "parent_run_id", "child_run_id", "task_instance_id"):
            object.__setattr__(self, field_name, _required(getattr(self, field_name), field_name))
        object.__setattr__(self, "attempt", _positive(self.attempt, "attempt"))
        object.__setattr__(self, "committed_at", _timestamp(self.committed_at, "committed_at"))
        object.__setattr__(self, "receipt_checksum", checksum_for(self.checksum_projection()))

    @property
    def is_graph_only(self) -> bool:
        return True

    def checksum_projection(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "transcript_ref": self.transcript_ref, "transcript_checksum": self.transcript_checksum, "transcript_id": self.transcript_id, "invocation_id": self.invocation_id, "parent_run_id": self.parent_run_id, "child_run_id": self.child_run_id, "task_instance_id": self.task_instance_id, "attempt": self.attempt, "context_ref": self.context_ref, "context_checksum": self.context_checksum, "output_ref": self.output_ref, "output_checksum": self.output_checksum, "storage_revision": self.storage_revision, "committed_at": format_datetime(self.committed_at), "identity_checksum": self.identity_checksum}

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload = self.checksum_projection()
        if include_checksum:
            payload["receipt_checksum"] = self.receipt_checksum
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = frozenset({"schema_version", "transcript_ref", "transcript_checksum", "transcript_id", "invocation_id", "parent_run_id", "child_run_id", "task_instance_id", "attempt", "context_ref", "context_checksum", "output_ref", "output_checksum", "storage_revision", "committed_at", "identity_checksum", "receipt_checksum"})
        payload = _require_exact_fields(value, expected, cls.__name__)
        supplied = _checksum(payload.pop("receipt_checksum"), "receipt_checksum")
        result = cls(**payload)
        _verify_checksum(result.checksum_projection(), supplied, "receipt_checksum")
        return result


@runtime_checkable
class SubAgentTranscriptStorePort(Protocol):
    def write(self, context: SubAgentContextEvidence, output: SubAgentOutputDocument, transcript: SubAgentTranscript) -> SubAgentTranscriptReceipt: ...
    def read(self, transcript_ref: str) -> SubAgentTranscript: ...
    def read_context(self, context_ref: str) -> SubAgentContextEvidence: ...
    def read_output(self, output_ref: str) -> SubAgentOutputDocument: ...
    def verify(self, receipt: SubAgentTranscriptReceipt) -> SubAgentTranscriptReceipt: ...
    def refs_for_parent(self, parent_run_id: str, *, limit: int = MAX_PARENT_QUERY) -> tuple[str, ...]: ...
    def find_by_identity(self, identity: SubAgentAttemptIdentity) -> SubAgentTranscriptReceipt | None: ...


def _validate_bundle_identity(context: SubAgentContextEvidence, output: SubAgentOutputDocument, transcript: SubAgentTranscript) -> None:
    if context.identity != output.identity or output.identity != transcript.identity:
        raise HarnessValidationError("subagent evidence identity mismatch", code="subagent_transcript_identity_mismatch")
    if (context.schema_version, output.schema_version, transcript.schema_version) != (SUBAGENT_CONTEXT_SCHEMA_V3, SUBAGENT_OUTPUT_SCHEMA_V3, SUBAGENT_TRANSCRIPT_SCHEMA_V3):
        raise HarnessValidationError("subagent evidence schemas do not match attempt identity", code="subagent_transcript_identity_schema_mismatch")
    if transcript.output_ref != output.ref or transcript.output_checksum != output.output_checksum or transcript.artifact_refs != output.artifact_refs:
        raise HarnessValidationError("subagent transcript does not match output document", code="subagent_result_scope_mismatch")


def _bundle_schema_for_identity(identity: SubAgentAttemptIdentity) -> str:
    if not isinstance(identity, SubAgentAttemptIdentity):
        raise TypeError("identity must be SubAgentAttemptIdentity")
    return SUBAGENT_BUNDLE_SCHEMA_V3


def _receipt_matches_bundle(receipt: SubAgentTranscriptReceipt, context: SubAgentContextEvidence, output: SubAgentOutputDocument, transcript: SubAgentTranscript) -> bool:
    identity = transcript.identity
    return (
        context.identity == identity and output.identity == identity and receipt.schema_version == SUBAGENT_RECEIPT_SCHEMA_V3
        and receipt.identity_checksum == identity.identity_checksum and receipt.transcript_id == identity.transcript_id
        and receipt.invocation_id == identity.invocation_id and receipt.parent_run_id == identity.parent_run_id
        and receipt.child_run_id == identity.child_run_id and receipt.task_instance_id == identity.task_instance_id
        and receipt.attempt == identity.attempt and receipt.transcript_ref == transcript.ref
        and receipt.context_ref == _context_ref(identity) and receipt.output_ref == output.ref
        and receipt.transcript_checksum == transcript.transcript_checksum and receipt.context_checksum == context.context_checksum
        and receipt.output_checksum == output.output_checksum
    )


class FakeSubAgentTranscriptStore:
    """Explicit test-only store implementing immutable Graph v3 bundle semantics."""
    is_durable = False

    def __init__(self) -> None:
        self.contexts: dict[str, SubAgentContextEvidence] = {}
        self.outputs: dict[str, SubAgentOutputDocument] = {}
        self.transcripts: dict[str, SubAgentTranscript] = {}
        self.receipts: dict[str, SubAgentTranscriptReceipt] = {}
        self.descriptors: dict[str, SubAgentAttemptDescriptor] = {}

    def write(self, context: SubAgentContextEvidence, output: SubAgentOutputDocument, transcript: SubAgentTranscript) -> SubAgentTranscriptReceipt:
        _validate_bundle_identity(context, output, transcript)
        identity = transcript.identity
        existing = self.find_by_identity(identity)
        if existing is not None:
            if self.contexts.get(existing.context_ref) != context or self.outputs.get(existing.output_ref) != output or self.transcripts.get(existing.transcript_ref) != transcript:
                raise SubAgentTranscriptConflictError("subagent transcript identity already has different content", code="subagent_transcript_conflict")
            return existing
        receipt = SubAgentTranscriptReceipt(
            transcript_ref=transcript.ref, transcript_checksum=transcript.transcript_checksum,
            transcript_id=identity.transcript_id, invocation_id=identity.invocation_id,
            parent_run_id=identity.parent_run_id, child_run_id=identity.child_run_id,
            task_instance_id=identity.task_instance_id, attempt=identity.attempt,
            context_ref=_context_ref(identity), context_checksum=context.context_checksum,
            output_ref=output.ref, output_checksum=output.output_checksum,
            storage_revision=f"memory:{identity.transcript_id}:v3", committed_at=utc_now(),
            identity_checksum=identity.identity_checksum,
        )
        self.contexts[receipt.context_ref] = context
        self.outputs[receipt.output_ref] = output
        self.transcripts[receipt.transcript_ref] = transcript
        self.receipts[receipt.transcript_ref] = receipt
        from framework.harness.subagents.transcript_metadata import descriptor_for_bundle

        bundle = (
            stable_json_dumps(
                {
                    "schema_version": _bundle_schema_for_identity(identity),
                    "context": context.to_dict(),
                    "output": output.to_dict(),
                    "transcript": transcript.to_dict(),
                    "receipt": receipt.to_dict(),
                }
            )
            + "\n"
        ).encode("utf-8")
        descriptor = descriptor_for_bundle(
            identity=identity,
            receipt=receipt,
            artifact_refs=output.artifact_refs,
            bundle=bundle,
        )
        self.descriptors[receipt.transcript_ref] = descriptor
        return receipt

    def read(self, transcript_ref: str) -> SubAgentTranscript:
        try:
            return self.transcripts[transcript_ref]
        except KeyError as exc:
            raise SubAgentTranscriptStoreError("subagent transcript was not found", code="subagent_transcript_not_found") from exc

    def read_context(self, context_ref: str) -> SubAgentContextEvidence:
        try:
            return self.contexts[context_ref]
        except KeyError as exc:
            raise SubAgentTranscriptStoreError("subagent context evidence was not found", code="subagent_context_not_found") from exc

    def read_output(self, output_ref: str) -> SubAgentOutputDocument:
        try:
            return self.outputs[output_ref]
        except KeyError as exc:
            raise SubAgentTranscriptStoreError("subagent output was not found", code="subagent_output_not_found") from exc

    def verify(self, receipt: SubAgentTranscriptReceipt) -> SubAgentTranscriptReceipt:
        if not isinstance(receipt, SubAgentTranscriptReceipt) or self.receipts.get(receipt.transcript_ref) != receipt:
            raise SubAgentTranscriptCorruptError("subagent receipt is not current", code="subagent_transcript_corrupt")
        context, output, transcript = self.read_context(receipt.context_ref), self.read_output(receipt.output_ref), self.read(receipt.transcript_ref)
        _validate_bundle_identity(context, output, transcript)
        if not _receipt_matches_bundle(receipt, context, output, transcript):
            raise SubAgentTranscriptCorruptError("subagent receipt checksum mismatch", code="subagent_transcript_checksum_mismatch")
        return receipt

    def refs_for_parent(self, parent_run_id: str, *, limit: int = MAX_PARENT_QUERY) -> tuple[str, ...]:
        parent = _required(parent_run_id, "parent_run_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0 or limit > MAX_PARENT_QUERY:
            raise HarnessValidationError("transcript parent query limit is invalid", code="subagent_transcript_query_limit_invalid")
        return tuple(sorted(ref for ref, item in self.transcripts.items() if item.identity.parent_run_id == parent)[:limit])

    def find_by_identity(self, identity: SubAgentAttemptIdentity) -> SubAgentTranscriptReceipt | None:
        receipt = self.receipts.get(f"subagent-transcript://v3/{identity.parent_run_id}/{identity.transcript_id}")
        if receipt is None:
            return None
        try:
            transcript = self.read(receipt.transcript_ref)
        except SubAgentTranscriptStoreError as exc:
            raise SubAgentTranscriptCorruptError(
                "subagent identity lookup found a dangling receipt",
                code="subagent_transcript_corrupt",
            ) from exc
        if transcript.identity != identity:
            raise SubAgentTranscriptCorruptError("subagent identity lookup resolved a different attempt", code="subagent_transcript_identity_mismatch")
        return receipt

    def describe_attempt(
        self,
        identity: SubAgentAttemptIdentity,
    ) -> SubAgentAttemptDescriptor | None:
        if not isinstance(identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        ref = f"subagent-transcript://v3/{identity.parent_run_id}/{identity.transcript_id}"
        descriptor = self.descriptors.get(ref)
        receipt = self.receipts.get(ref)
        payload_present = (
            receipt is not None
            and receipt.context_ref in self.contexts
            and receipt.output_ref in self.outputs
            and receipt.transcript_ref in self.transcripts
        )
        if descriptor is None and receipt is None and not payload_present:
            return None
        if descriptor is None or receipt is None or not payload_present:
            from framework.harness.subagents.transcript_metadata import (
                SubAgentAttemptMetadataIncompleteError,
            )

            raise SubAgentAttemptMetadataIncompleteError(
                "subagent attempt metadata is incomplete",
                code="subagent_attempt_metadata_incomplete",
            )
        if descriptor.identity != identity or descriptor.receipt != receipt:
            raise SubAgentTranscriptCorruptError(
                "subagent attempt descriptor resolved a different attempt",
                code="subagent_transcript_identity_mismatch",
            )
        return descriptor

    def describe_ref(self, ref: str) -> SubAgentAttemptDescriptor | None:
        if not isinstance(ref, str):
            raise TypeError("ref must be str")
        for kind in ("subagent-transcript", "subagent-context", "subagent-output"):
            prefix = f"{kind}://v3/"
            if ref.startswith(prefix):
                parts = ref.removeprefix(prefix).split("/")
                if len(parts) != 2:
                    break
                canonical = f"subagent-transcript://v3/{parts[0]}/{parts[1]}"
                descriptor = self.descriptors.get(canonical)
                receipt = self.receipts.get(canonical)
                payload_present = (
                    receipt is not None
                    and receipt.context_ref in self.contexts
                    and receipt.output_ref in self.outputs
                    and receipt.transcript_ref in self.transcripts
                )
                if descriptor is None and receipt is None and not payload_present:
                    return None
                if descriptor is None or receipt is None or not payload_present:
                    from framework.harness.subagents.transcript_metadata import (
                        SubAgentAttemptMetadataIncompleteError,
                    )

                    raise SubAgentAttemptMetadataIncompleteError(
                        "subagent attempt metadata is incomplete",
                        code="subagent_attempt_metadata_incomplete",
                    )
                expected = {
                    "subagent-transcript": descriptor.receipt.transcript_ref,
                    "subagent-context": descriptor.receipt.context_ref,
                    "subagent-output": descriptor.receipt.output_ref,
                }[kind]
                if ref != expected:
                    raise SubAgentTranscriptCorruptError(
                        "subagent evidence ref does not match its descriptor",
                        code="subagent_transcript_identity_mismatch",
                    )
                return descriptor
        raise SubAgentTranscriptStoreError(
            "subagent evidence ref is invalid",
            code="subagent_transcript_identity_mismatch",
        )


__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES", "DEFAULT_MAX_OUTPUT_BYTES", "DEFAULT_MAX_TRANSCRIPT_BYTES",
    "FakeSubAgentTranscriptStore", "MAX_PARENT_QUERY", "SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3",
    "SUBAGENT_BUNDLE_SCHEMA", "SUBAGENT_BUNDLE_SCHEMA_V3", "SUBAGENT_CONTEXT_SCHEMA",
    "SUBAGENT_CONTEXT_SCHEMA_V3", "SUBAGENT_OUTPUT_SCHEMA", "SUBAGENT_OUTPUT_SCHEMA_V3",
    "SUBAGENT_RECEIPT_SCHEMA", "SUBAGENT_RECEIPT_SCHEMA_V3", "SUBAGENT_TRANSCRIPT_SCHEMA",
    "SUBAGENT_TRANSCRIPT_SCHEMA_V3", "SubAgentAttemptIdentity", "SubAgentContextEvidence",
    "SubAgentEvidenceSchemas", "SubAgentOutputDocument", "SubAgentTranscript",
    "SubAgentTranscriptConflictError", "SubAgentTranscriptCorruptError", "SubAgentTranscriptReceipt",
    "SubAgentTranscriptStoreError", "SubAgentTranscriptStorePort", "_bundle_schema_for_identity",
    "_context_ref", "_receipt_matches_bundle", "_validate_bundle_identity", "sanitize_subagent_payload",
    "subagent_context_ref", "subagent_evidence_schemas",
]
