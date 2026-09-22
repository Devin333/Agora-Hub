"""Durable evidence owner for tool calls made by one admitted child attempt."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any, Protocol, runtime_checkable

from framework.events import (
    EventStoreContentionError,
    TransactionalStateReaderPort,
    TransactionalStateRuntimePort,
    TransactionalStateSnapshot,
    thaw_canonical_json,
)
from framework.agent.artifacts.models import (
    ARTIFACT_SCOPE_GRAPH,
    ArtifactRef,
    ArtifactWriteRequest,
)
from framework.harness.task_plan.durable_store import TaskPlanArtifactStorePort
from framework.shared.attempts import (
    AttemptIdentity,
    AttemptOutcome,
    AttemptState,
    current_attempt_context,
    derive_idempotency_key,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps, to_jsonable
from framework.shared.time import format_datetime, parse_datetime, utc_now
from framework.tool.models import ToolCall, ToolDefinition, ToolObservation, ToolStatus
from framework.tool.governance.redaction import redact_sensitive_values
from framework.tool.runtime.evidence import (
    ToolEvidencePersistenceError,
    ToolExecutionEvidencePort,
    ToolRunnerTurnEvidence,
)


CHILD_TOOL_EVIDENCE_STATE_NAMESPACE = "newsroom.harness-child-tool-evidence/v1"
_ARTIFACT_TYPE_PREFIX = "newsroom.child-tool-evidence"
_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}\Z")
_ARTIFACT_CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
_MAX_CAS_RETRIES = 16


class ChildToolEvidenceOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ChildToolInvocationState(StrEnum):
    NOT_INVOKED = "NOT_INVOKED"
    INVOKED = "INVOKED"
    UNKNOWN = "UNKNOWN"


class ChildToolEvidenceCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True, slots=True)
class ChildToolEvidenceLimits:
    max_logical_calls: int
    max_physical_attempts: int
    max_runner_turn_bytes: int = 64 * 1024
    max_request_bytes: int = 64 * 1024
    max_receipt_bytes: int = 1024 * 1024
    max_index_bytes: int = 64 * 1024
    max_total_evidence_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_logical_calls",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in (
            "max_runner_turn_bytes",
            "max_request_bytes",
            "max_receipt_bytes",
            "max_index_bytes",
            "max_total_evidence_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.max_physical_attempts, bool)
            or not isinstance(self.max_physical_attempts, int)
            or self.max_physical_attempts < 0
        ):
            raise ValueError("max_physical_attempts must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in (
                "max_logical_calls",
                "max_physical_attempts",
                "max_runner_turn_bytes",
                "max_request_bytes",
                "max_receipt_bytes",
                "max_index_bytes",
                "max_total_evidence_bytes",
            )
        }


@dataclass(frozen=True, slots=True)
class ChildToolEvidenceScope:
    parent_graph_identity: GraphExecutionIdentity
    stage_id: str
    plan_id: str
    plan_version: int
    group_id: str
    wave_id: str
    task_id: str
    task_instance_id: str
    task_attempt: int
    task_instance_checksum: str
    binding_ref: str
    binding_checksum: str
    spawn_operation_key: str
    owner_scope: str
    input_grant_ref: str
    input_grant_checksum: str
    policy_checksum: str
    reservation_key: str
    reservation_revision: int
    allocation_checksum: str
    limits: ChildToolEvidenceLimits
    tenant_id: str | None = None
    schema_version: str = field(default="newsroom.child-tool-evidence-scope/v1")

    def __post_init__(self) -> None:
        if not isinstance(self.parent_graph_identity, GraphExecutionIdentity):
            raise TypeError("parent_graph_identity must be GraphExecutionIdentity")
        if not isinstance(self.limits, ChildToolEvidenceLimits):
            raise TypeError("limits must be ChildToolEvidenceLimits")
        for name in (
            "schema_version",
            "stage_id",
            "plan_id",
            "group_id",
            "wave_id",
            "task_id",
            "task_instance_id",
            "binding_ref",
            "spawn_operation_key",
            "owner_scope",
            "input_grant_ref",
            "reservation_key",
        ):
            _required_text(getattr(self, name), name)
        if self.tenant_id is not None:
            _required_text(self.tenant_id, "tenant_id")
        for name in (
            "task_instance_checksum",
            "binding_checksum",
            "input_grant_checksum",
            "policy_checksum",
            "allocation_checksum",
        ):
            _required_checksum(getattr(self, name), name)
        for name in ("plan_version", "task_attempt", "reservation_revision"):
            _positive_int(getattr(self, name), name)
        if self.schema_version != "newsroom.child-tool-evidence-scope/v1":
            raise ValueError("unsupported child tool evidence scope version")

    @property
    def run_id(self) -> str:
        return self.parent_graph_identity.run_id

    @property
    def child_scope_key(self) -> str:
        return _checksum(
            {
                "run_id": self.run_id,
                "stage_id": self.stage_id,
                "plan_id": self.plan_id,
                "plan_version": self.plan_version,
                "group_id": self.group_id,
                "wave_id": self.wave_id,
                "task_instance_id": self.task_instance_id,
                "task_attempt": self.task_attempt,
                "binding_ref": self.binding_ref,
                "spawn_operation_key": self.spawn_operation_key,
            }
        )

    @property
    def scope_checksum(self) -> str:
        return _checksum(self.to_dict(include_checksum=False))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "parent_graph_identity": self.parent_graph_identity.to_dict(),
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "group_id": self.group_id,
            "wave_id": self.wave_id,
            "task_id": self.task_id,
            "task_instance_id": self.task_instance_id,
            "task_attempt": self.task_attempt,
            "task_instance_checksum": self.task_instance_checksum,
            "binding_ref": self.binding_ref,
            "binding_checksum": self.binding_checksum,
            "spawn_operation_key": self.spawn_operation_key,
            "owner_scope": self.owner_scope,
            "tenant_id": self.tenant_id,
            "input_grant_ref": self.input_grant_ref,
            "input_grant_checksum": self.input_grant_checksum,
            "policy_checksum": self.policy_checksum,
            "reservation_key": self.reservation_key,
            "reservation_revision": self.reservation_revision,
            "allocation_checksum": self.allocation_checksum,
            "limits": self.limits.to_dict(),
            "child_scope_key": self.child_scope_key,
        }
        if include_checksum:
            value["scope_checksum"] = _checksum(value)
        return value


@dataclass(frozen=True, slots=True)
class ChildToolEvidenceIndex:
    ref: str
    checksum: str
    content_checksum: str
    byte_size: int
    revision: int
    completeness: ChildToolEvidenceCompleteness
    disposition: str
    registered_logical_calls: int
    rejected_logical_calls: int
    admitted_physical_attempts: int
    known_terminal_attempts: int
    unresolved_attempts: int


@runtime_checkable
class ChildToolEvidenceAuthorizationPort(Protocol):
    """Authorize metadata-bound artifact access before any payload is read."""

    def authorize_artifact_read(
        self,
        scope: ChildToolEvidenceScope,
        *,
        artifact_ref: ArtifactRef,
        artifact_type: str,
        expected_artifact_checksum: str,
        expected_byte_size: int,
    ) -> None: ...


class MetadataFirstChildToolEvidenceAuthorization:
    """Validate the integrity-protected immutable reference before body reads."""

    def authorize_artifact_read(
        self,
        scope: ChildToolEvidenceScope,
        *,
        artifact_ref: ArtifactRef,
        artifact_type: str,
        expected_artifact_checksum: str,
        expected_byte_size: int,
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef):
            raise ToolEvidencePersistenceError("tool evidence artifact ref is invalid")
        metadata = {
            "tenant_id": scope.tenant_id,
            "owner_scope": scope.owner_scope,
            "child_scope_key": scope.child_scope_key,
            "scope_checksum": scope.scope_checksum,
            "input_grant_ref": scope.input_grant_ref,
        }
        if (
            artifact_ref.run_id != scope.run_id
            or artifact_ref.scope_kind != ARTIFACT_SCOPE_GRAPH
            or artifact_ref.graph_id != scope.parent_graph_identity.graph_id
            or artifact_ref.graph_version
            != scope.parent_graph_identity.graph_version
            or artifact_ref.graph_ref != scope.parent_graph_identity.graph_ref
            or artifact_ref.graph_checksum
            != scope.parent_graph_identity.graph_checksum
            or artifact_ref.node_id != scope.parent_graph_identity.node_id
            or artifact_ref.node_instance_id
            != scope.parent_graph_identity.node_instance_id
            or artifact_ref.activity_id
            != scope.parent_graph_identity.activity_id
            or artifact_ref.attempt != scope.parent_graph_identity.attempt
            or artifact_ref.artifact_type != artifact_type
            or artifact_ref.content_type != "application/json"
            or artifact_ref.checksum != expected_artifact_checksum
            or artifact_ref.size_bytes != expected_byte_size
            or artifact_ref.metadata != metadata
        ):
            raise ToolEvidencePersistenceError(
                "tool evidence artifact metadata does not match child authority"
            )


class DurableChildToolEvidenceOwner(ToolExecutionEvidencePort):
    """CAS-owned, restart-safe evidence capability for one child attempt."""

    is_durable = True

    def __init__(
        self,
        scope: ChildToolEvidenceScope,
        *,
        state_runtime: TransactionalStateRuntimePort,
        state_reader: TransactionalStateReaderPort,
        artifact_store: TaskPlanArtifactStorePort,
        authorization_port: ChildToolEvidenceAuthorizationPort,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(scope, ChildToolEvidenceScope):
            raise TypeError("scope must be ChildToolEvidenceScope")
        if not isinstance(state_runtime, TransactionalStateRuntimePort):
            raise TypeError("state_runtime must implement TransactionalStateRuntimePort")
        if not isinstance(state_reader, TransactionalStateReaderPort):
            raise TypeError("state_reader must implement TransactionalStateReaderPort")
        if not isinstance(artifact_store, TaskPlanArtifactStorePort):
            raise TypeError("artifact_store must implement TaskPlanArtifactStorePort")
        if not isinstance(authorization_port, ChildToolEvidenceAuthorizationPort):
            raise TypeError(
                "authorization_port must implement ChildToolEvidenceAuthorizationPort"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._scope = scope
        self._runtime = state_runtime
        self._reader = state_reader
        self._artifacts = artifact_store
        self._authorization = authorization_port
        self._clock = clock

    @property
    def scope(self) -> ChildToolEvidenceScope:
        return self._scope

    def open_scope(self) -> None:
        for _ in range(_MAX_CAS_RETRIES):
            current = self._reader.load_transactional_state(
                CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
                self._scope.child_scope_key,
            )
            if current is not None:
                self._validated_state(current)
                return
            initial = TransactionalStateSnapshot.create(
                namespace=CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
                key=self._scope.child_scope_key,
                revision=1,
                payload={
                    "schema_version": "newsroom.child-tool-evidence-state/v1",
                    "scope": self._scope.to_dict(),
                    "status": "OPEN",
                    "artifact_reservations": {},
                    "runner_turns": {},
                    "logical_calls": {},
                    "physical_attempt_count": 0,
                    "known_terminal_count": 0,
                    "total_evidence_bytes": 0,
                    "index": None,
                },
            )
            try:
                self._runtime.compare_and_swap_transactional_state(
                    initial,
                    expected_revision=None,
                    expected_checksum=None,
                )
            except EventStoreContentionError:
                continue
            except ToolEvidencePersistenceError:
                raise
            except BaseException as exc:  # noqa: BLE001 - required durable boundary
                raise ToolEvidencePersistenceError(
                    "child tool scope open state write failed"
                ) from exc
            return
        raise ToolEvidencePersistenceError("child tool scope open CAS did not converge")

    def commit_runner_turn(self, turn: Mapping[str, Any]) -> ToolRunnerTurnEvidence:
        body = _mapping(turn, "runner turn")
        content_checksum = _checksum(to_jsonable(body))
        state = self._require_open_state()
        existing = _dict(state.payload["runner_turns"], "runner_turns").get(
            content_checksum
        )
        if existing is not None:
            self._read_verified_artifact(
                existing["ref"],
                artifact_type=self._artifact_type("runner-turn"),
                expected_artifact_checksum=existing["artifact_checksum"],
                expected_content_checksum=content_checksum,
                expected_byte_size=existing["byte_size"],
            )
            return ToolRunnerTurnEvidence(
                ref=existing["ref"],
                checksum=content_checksum,
            )
        descriptor = self._write_verified_artifact(
            "runner-turn",
            body,
            max_bytes=self._scope.limits.max_runner_turn_bytes,
        )

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            turns = _dict(payload["runner_turns"], "runner_turns")
            current = turns.get(content_checksum)
            if current is not None:
                return payload, False
            turns[content_checksum] = descriptor
            payload["runner_turns"] = turns
            return payload, True

        persisted = self._cas_mutate(mutate)
        accepted = _dict(persisted["runner_turns"], "runner_turns")[content_checksum]
        self._read_verified_artifact(
            accepted["ref"],
            artifact_type=self._artifact_type("runner-turn"),
            expected_artifact_checksum=accepted["artifact_checksum"],
            expected_content_checksum=content_checksum,
            expected_byte_size=accepted["byte_size"],
        )
        return ToolRunnerTurnEvidence(
            ref=accepted["ref"],
            checksum=content_checksum,
        )

    def register_call(self, call: ToolCall) -> None:
        self._validate_call_scope(call)
        logical_id, runner_ref, runner_checksum, ordinal = self._logical_identity(call)
        state = self._require_open_state()
        runner_descriptor = _dict(state.payload["runner_turns"], "runner_turns").get(
            runner_checksum
        )
        if runner_descriptor is None or runner_descriptor.get("ref") != runner_ref:
            raise ToolEvidencePersistenceError(
                "logical tool call references an uncommitted runner turn"
            )
        self._read_verified_artifact(
            runner_ref,
            artifact_type=self._artifact_type("runner-turn"),
            expected_artifact_checksum=runner_descriptor["artifact_checksum"],
            expected_content_checksum=runner_checksum,
            expected_byte_size=runner_descriptor["byte_size"],
        )
        arguments = to_jsonable(call.arguments)
        request_identity = {
            "schema_version": "newsroom.child-tool-call-request/v1",
            "child_scope_key": self._scope.child_scope_key,
            "scope_checksum": self._scope.scope_checksum,
            "logical_call_id": logical_id,
            "runner_turn_ref": runner_ref,
            "runner_turn_checksum": runner_checksum,
            "call_ordinal": ordinal,
            "model_tool_call_id": _optional_bounded_text(
                call.metadata.get("model_tool_call_id"),
                "model_tool_call_id",
                maximum=512,
            ),
            "runtime_call_id": _required_text(call.call_id, "call_id"),
            "requested_tool_name": _required_text(call.tool_name, "tool_name"),
            "requested_by_agent_id": call.requested_by_agent_id or None,
            "arguments_checksum": _checksum(arguments),
            "operation_key": _tool_operation_key(call),
        }
        request_checksum = _checksum(request_identity)
        request_body = {
            **request_identity,
            "request_checksum": request_checksum,
            "arguments_descriptor": redact_sensitive_values(arguments),
        }
        descriptor = self._write_verified_artifact(
            "call-request",
            request_body,
            max_bytes=self._scope.limits.max_request_bytes,
        )

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            known_turn = _dict(payload["runner_turns"], "runner_turns").get(
                runner_checksum
            )
            if known_turn is None or known_turn.get("ref") != runner_ref:
                raise ToolEvidencePersistenceError(
                    "logical tool call references an uncommitted runner turn"
                )
            calls = _dict(payload["logical_calls"], "logical_calls")
            existing = calls.get(logical_id)
            if existing is not None:
                if existing.get("request_checksum") != request_checksum:
                    raise ToolEvidencePersistenceError(
                        "logical tool call identity conflicts with its durable request"
                    )
                return payload, False
            if len(calls) >= self._scope.limits.max_logical_calls:
                raise ToolEvidencePersistenceError("logical tool call budget exhausted")
            calls[logical_id] = {
                "logical_call_id": logical_id,
                "runtime_call_id": call.call_id,
                "requested_tool_name": call.tool_name,
                "runner_turn_ref": runner_ref,
                "runner_turn_checksum": runner_checksum,
                "call_ordinal": ordinal,
                "request_checksum": request_checksum,
                "operation_key": request_identity["operation_key"],
                "request_artifact": descriptor,
                "tool_binding": None,
                "attempts": [],
                "receipt": None,
                "closed": False,
            }
            payload["logical_calls"] = calls
            return payload, True

        self._cas_mutate(mutate)

    def bind_tool(self, call: ToolCall, definition: ToolDefinition) -> None:
        self._validate_call_scope(call)
        if not isinstance(definition, ToolDefinition):
            raise TypeError("definition must be ToolDefinition")
        logical_id = self._logical_identity(call)[0]
        binding = {
            "tool_ref": f"tool://{definition.name}@{definition.version}",
            "tool_checksum": _checksum(to_jsonable(definition)),
            "tool_name": definition.name,
            "tool_version": definition.version,
            "side_effect": definition.side_effect_value,
            "concurrency_safe": definition.concurrency_safe,
        }

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            record = self._logical_record(payload, logical_id, call)
            existing = record.get("tool_binding")
            if existing is not None:
                if existing != binding:
                    raise ToolEvidencePersistenceError(
                        "logical tool call resolved to a conflicting tool binding"
                    )
                return payload, False
            record["tool_binding"] = binding
            return payload, True

        self._cas_mutate(mutate)

    def admit_attempt(self, call: ToolCall, identity: AttemptIdentity) -> None:
        self._validate_call_scope(call)
        if not isinstance(identity, AttemptIdentity):
            raise TypeError("identity must be AttemptIdentity")
        if identity.operation_kind != "tool_call":
            raise ToolEvidencePersistenceError("tool attempt operation kind is invalid")
        logical_id = self._logical_identity(call)[0]
        physical_id = _checksum(
            {
                "logical_call_id": logical_id,
                "local_attempt_no": identity.local_attempt_no,
                "attempt_id": identity.attempt_id,
            }
        )

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            record = self._logical_record(payload, logical_id, call)
            if record.get("closed"):
                raise ToolEvidencePersistenceError("logical tool call is already closed")
            if record.get("tool_binding") is None:
                raise ToolEvidencePersistenceError(
                    "physical tool attempt cannot precede tool binding"
                )
            attempts = _list(record["attempts"], "attempts")
            if any(item.get("physical_attempt_id") == physical_id for item in attempts):
                raise ToolEvidencePersistenceError(
                    "physical tool intent already exists; execution ownership cannot be reacquired"
                )
            if attempts and attempts[-1].get("terminal") is None:
                raise ToolEvidencePersistenceError(
                    "previous physical tool intent is unresolved"
                )
            expected_ordinal = len(attempts) + 1
            if identity.local_attempt_no != expected_ordinal:
                raise ToolEvidencePersistenceError(
                    "physical tool attempt ordinal is not contiguous"
                )
            count = int(payload["physical_attempt_count"])
            if count >= self._scope.limits.max_physical_attempts:
                raise ToolEvidencePersistenceError("physical tool attempt budget exhausted")
            if (
                identity.operation_id != record.get("operation_key")
                or identity.idempotency_key != record.get("operation_key")
            ):
                raise ToolEvidencePersistenceError(
                    "physical tool attempt operation identity conflicts with logical call"
                )
            intent = {
                "physical_attempt_id": physical_id,
                "physical_attempt_no": identity.local_attempt_no,
                "attempt_id": identity.attempt_id,
                "operation_id": identity.operation_id,
                "operation_kind": identity.operation_kind,
                "idempotency_key": identity.idempotency_key,
                "retry_credit_id": identity.retry_credit_id,
                "parent_attempt_id": identity.parent_attempt_id,
                "reservation_key": self._scope.reservation_key,
                "reservation_revision": self._scope.reservation_revision,
                "allocation_checksum": self._scope.allocation_checksum,
                "admitted_at": _clock_timestamp(self._clock),
                "terminal": None,
            }
            intent["intent_checksum"] = _checksum(intent)
            attempts.append(intent)
            record["attempts"] = attempts
            payload["physical_attempt_count"] = count + 1
            return payload, True

        self._cas_mutate(mutate)

    def record_attempt_terminal(
        self,
        call: ToolCall,
        outcome: AttemptOutcome[Any],
    ) -> None:
        self._validate_call_scope(call)
        if not isinstance(outcome, AttemptOutcome) or outcome.context is None:
            raise TypeError("outcome must be a started AttemptOutcome")
        logical_id = self._logical_identity(call)[0]
        terminal_body = _terminal_body(self._scope, logical_id, outcome)
        descriptor = self._write_verified_artifact(
            "attempt-terminal",
            terminal_body,
            max_bytes=self._scope.limits.max_receipt_bytes,
        )

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            record = self._logical_record(payload, logical_id, call)
            attempts = _list(record["attempts"], "attempts")
            match = next(
                (
                    item
                    for item in attempts
                    if item.get("attempt_id") == outcome.context.attempt_id
                    and item.get("physical_attempt_no")
                    == outcome.context.local_attempt_no
                ),
                None,
            )
            if match is None:
                raise ToolEvidencePersistenceError(
                    "terminal tool outcome has no admitted physical intent"
                )
            if (
                terminal_body["operation_id"] != match.get("operation_id")
                or terminal_body["idempotency_key"] != match.get("idempotency_key")
            ):
                raise ToolEvidencePersistenceError(
                    "terminal tool outcome operation identity conflicts with intent"
                )
            existing = match.get("terminal")
            if existing is not None:
                if existing.get("content_checksum") != descriptor["content_checksum"]:
                    raise ToolEvidencePersistenceError(
                        "physical tool outcome conflicts with its durable terminal"
                    )
                return payload, False
            match["terminal"] = {
                **descriptor,
                "outcome": terminal_body["outcome"],
                "invocation_state": terminal_body["invocation_state"],
                "termination_confirmed": terminal_body["termination_confirmed"],
            }
            record["attempts"] = attempts
            payload["known_terminal_count"] = int(payload["known_terminal_count"]) + 1
            return payload, True

        self._cas_mutate(mutate)

    def commit_observation(
        self,
        observation: ToolObservation,
        definition: ToolDefinition | None,
    ) -> str:
        if not isinstance(observation, ToolObservation):
            raise TypeError("observation must be ToolObservation")
        self._validate_call_scope(observation.call)
        logical_id = self._logical_identity(observation.call)[0]
        body = self._observation_body(observation, definition, logical_id)
        descriptor = self._write_verified_artifact(
            "observation-receipt",
            body,
            max_bytes=self._scope.limits.max_receipt_bytes,
        )

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            record = self._logical_record(payload, logical_id, observation.call)
            existing = record.get("receipt")
            if existing is not None:
                if existing.get("content_checksum") != descriptor["content_checksum"]:
                    raise ToolEvidencePersistenceError(
                        "logical tool receipt conflicts with its durable observation"
                    )
                return payload, False
            attempts = _list(record["attempts"], "attempts")
            binding = record.get("tool_binding")
            definition_checksum = (
                _checksum(to_jsonable(definition)) if definition is not None else None
            )
            if binding is None:
                if definition_checksum is not None:
                    raise ToolEvidencePersistenceError(
                        "tool observation has an unregistered definition binding"
                    )
            elif definition_checksum != binding.get("tool_checksum"):
                raise ToolEvidencePersistenceError(
                    "tool observation definition conflicts with its binding"
                )
            _validate_observation_against_attempts(observation, attempts)
            record["receipt"] = {
                **descriptor,
                "outcome": body["outcome"],
                "invocation_state": body["invocation_state"],
                "termination_confirmed": body["termination_confirmed"],
            }
            record["closed"] = True
            return payload, True

        persisted = self._cas_mutate(mutate)
        record = self._logical_record(persisted, logical_id, observation.call)
        receipt = _dict(record["receipt"], "receipt")
        self._read_verified_artifact(
            receipt["ref"],
            artifact_type=self._artifact_type("observation-receipt"),
            expected_artifact_checksum=receipt["artifact_checksum"],
            expected_content_checksum=receipt["content_checksum"],
            expected_byte_size=receipt["byte_size"],
        )
        return str(receipt["ref"])

    def close_scope(self) -> ChildToolEvidenceIndex:
        current = self._require_state()
        payload = self._validated_state(current)
        if payload["status"] == "CLOSED":
            return self._index_from_payload(payload)

        for _ in range(_MAX_CAS_RETRIES):
            current = self._require_state()
            payload = self._validated_state(current)
            if payload["status"] == "CLOSED":
                return self._index_from_payload(payload)
            close_basis = _close_basis_checksum(payload)
            index_body = self._build_index_body(payload, revision=current.revision)
            encoded_size = _encoded_size(index_body)
            if encoded_size > self._scope.limits.max_index_bytes:
                raise ToolEvidencePersistenceError("child tool evidence index is too large")
            descriptor = self._write_verified_artifact(
                "evidence-index",
                index_body,
                max_bytes=self._scope.limits.max_index_bytes,
            )
            latest = self._require_state()
            latest_payload = self._validated_state(latest)
            if latest_payload["status"] == "CLOSED":
                return self._index_from_payload(latest_payload)
            if _close_basis_checksum(latest_payload) != close_basis:
                continue
            next_payload = deepcopy(latest_payload)
            next_payload["status"] = "CLOSED"
            next_payload["index"] = {
                **descriptor,
                "revision": index_body["revision"],
                "completeness": index_body["completeness"],
                "disposition": index_body["disposition"],
                "counts": index_body["counts"],
            }
            next_state = TransactionalStateSnapshot.create(
                namespace=latest.namespace,
                key=latest.key,
                revision=latest.revision + 1,
                payload=next_payload,
            )
            try:
                persisted = self._runtime.compare_and_swap_transactional_state(
                    next_state,
                    expected_revision=latest.revision,
                    expected_checksum=latest.checksum,
                )
            except EventStoreContentionError:
                continue
            except ToolEvidencePersistenceError:
                raise
            except BaseException as exc:  # noqa: BLE001 - required durable boundary
                raise ToolEvidencePersistenceError(
                    "child tool scope close state write failed"
                ) from exc
            return self._index_from_payload(self._validated_state(persisted))
        raise ToolEvidencePersistenceError("child tool scope close CAS did not converge")

    def _observation_body(
        self,
        observation: ToolObservation,
        definition: ToolDefinition | None,
        logical_id: str,
    ) -> dict[str, Any]:
        status = observation.status
        rejected = status in {
            ToolStatus.BLOCKED,
            ToolStatus.DENIED,
            ToolStatus.APPROVAL_REQUIRED,
            ToolStatus.SKIPPED,
        }
        unknown = bool(observation.result.indeterminate) or (
            observation.result.termination_confirmed is False
        )
        if rejected:
            outcome = ChildToolEvidenceOutcome.REJECTED
            invocation_state = ChildToolInvocationState.NOT_INVOKED
            termination_confirmed = True
        elif unknown:
            outcome = ChildToolEvidenceOutcome.UNKNOWN
            invocation_state = ChildToolInvocationState.UNKNOWN
            termination_confirmed = False
        elif status is ToolStatus.SUCCEEDED:
            outcome = ChildToolEvidenceOutcome.SUCCEEDED
            invocation_state = ChildToolInvocationState.INVOKED
            termination_confirmed = observation.result.termination_confirmed is True
            if not termination_confirmed:
                raise ToolEvidencePersistenceError(
                    "successful tool observation lacks confirmed termination"
                )
        else:
            outcome = ChildToolEvidenceOutcome.FAILED
            invocation_state = ChildToolInvocationState.INVOKED
            termination_confirmed = observation.result.termination_confirmed is True
            if not termination_confirmed:
                outcome = ChildToolEvidenceOutcome.UNKNOWN
                invocation_state = ChildToolInvocationState.UNKNOWN
        return {
            "schema_version": "newsroom.child-tool-receipt/v1",
            "child_scope_key": self._scope.child_scope_key,
            "scope_checksum": self._scope.scope_checksum,
            "logical_call_id": logical_id,
            "outcome": outcome.value,
            "invocation_state": invocation_state.value,
            "termination_confirmed": termination_confirmed,
            "definition_checksum": (
                _checksum(to_jsonable(definition)) if definition is not None else None
            ),
            "observation": observation.to_dict(),
        }

    def _build_index_body(
        self,
        payload: dict[str, Any],
        *,
        revision: int,
    ) -> dict[str, Any]:
        if any(
            reservation.get("status") == "RESERVED"
            for reservation in _dict(
                payload["artifact_reservations"], "artifact_reservations"
            ).values()
        ):
            raise ToolEvidencePersistenceError(
                "child tool evidence has an unresolved artifact write"
            )
        calls = _dict(payload["logical_calls"], "logical_calls")
        ordered = sorted(
            calls.values(),
            key=lambda item: (
                str(item["runner_turn_ref"]),
                int(item["call_ordinal"]),
                str(item["logical_call_id"]),
            ),
        )
        unresolved = 0
        rejected = 0
        receipts: list[dict[str, Any]] = []
        call_entries: list[dict[str, Any]] = []
        runner_turns = _dict(payload["runner_turns"], "runner_turns")
        for record in ordered:
            request_artifact = _dict(record["request_artifact"], "request_artifact")
            runner_artifact = _dict(
                runner_turns.get(record["runner_turn_checksum"]),
                "runner turn artifact",
            )
            if runner_artifact.get("ref") != record["runner_turn_ref"]:
                raise ToolEvidencePersistenceError(
                    "logical tool call runner turn attribution mismatch"
                )
            self._read_verified_artifact(
                record["runner_turn_ref"],
                artifact_type=self._artifact_type("runner-turn"),
                expected_artifact_checksum=runner_artifact["artifact_checksum"],
                expected_content_checksum=record["runner_turn_checksum"],
                expected_byte_size=runner_artifact["byte_size"],
            )
            request_document = self._read_verified_artifact(
                request_artifact["ref"],
                artifact_type=self._artifact_type("call-request"),
                expected_artifact_checksum=request_artifact["artifact_checksum"],
                expected_content_checksum=request_artifact["content_checksum"],
                expected_byte_size=request_artifact["byte_size"],
            )
            request_body = _artifact_body(request_document)
            if (
                request_body.get("logical_call_id") != record["logical_call_id"]
                or request_body.get("request_checksum") != record["request_checksum"]
                or request_body.get("runner_turn_ref") != record["runner_turn_ref"]
                or request_body.get("runner_turn_checksum")
                != record["runner_turn_checksum"]
                or request_body.get("call_ordinal") != record["call_ordinal"]
                or request_body.get("operation_key") != record["operation_key"]
                or request_body.get("runtime_call_id") != record["runtime_call_id"]
                or request_body.get("requested_tool_name")
                != record["requested_tool_name"]
            ):
                raise ToolEvidencePersistenceError(
                    "logical tool request artifact attribution mismatch"
                )
            attempts = _list(record["attempts"], "attempts")
            for attempt in attempts:
                terminal = attempt.get("terminal")
                if terminal is not None:
                    terminal_document = self._read_verified_artifact(
                        terminal["ref"],
                        artifact_type=self._artifact_type("attempt-terminal"),
                        expected_artifact_checksum=terminal["artifact_checksum"],
                        expected_content_checksum=terminal["content_checksum"],
                        expected_byte_size=terminal["byte_size"],
                    )
                    terminal_body = _artifact_body(terminal_document)
                    if (
                        terminal_body.get("logical_call_id")
                        != record["logical_call_id"]
                        or terminal_body.get("attempt_id") != attempt["attempt_id"]
                        or terminal_body.get("physical_attempt_no")
                        != attempt["physical_attempt_no"]
                        or terminal_body.get("operation_id")
                        != attempt["operation_id"]
                        or terminal_body.get("idempotency_key")
                        != attempt["idempotency_key"]
                        or terminal_body.get("outcome") != terminal.get("outcome")
                    ):
                        raise ToolEvidencePersistenceError(
                            "physical tool terminal artifact attribution mismatch"
                        )
            pending = [item for item in attempts if item.get("terminal") is None]
            unresolved += len(pending)
            unresolved += sum(
                1
                for item in attempts
                if item.get("terminal") is not None
                and item["terminal"].get("outcome")
                == ChildToolEvidenceOutcome.UNKNOWN.value
            )
            receipt = record.get("receipt")
            if receipt is None or not record.get("closed"):
                unresolved += 1
            else:
                receipt_document = self._read_verified_artifact(
                    receipt["ref"],
                    artifact_type=self._artifact_type("observation-receipt"),
                    expected_artifact_checksum=receipt["artifact_checksum"],
                    expected_content_checksum=receipt["content_checksum"],
                    expected_byte_size=receipt["byte_size"],
                )
                receipt_body = _artifact_body(receipt_document)
                if (
                    receipt_body.get("logical_call_id")
                    != record["logical_call_id"]
                    or receipt_body.get("outcome") != receipt.get("outcome")
                    or receipt_body.get("invocation_state")
                    != receipt.get("invocation_state")
                ):
                    raise ToolEvidencePersistenceError(
                        "logical tool receipt artifact attribution mismatch"
                    )
                receipts.append(_artifact_projection(receipt))
                if receipt.get("outcome") == ChildToolEvidenceOutcome.UNKNOWN.value:
                    unresolved += 1
                if receipt.get("outcome") == ChildToolEvidenceOutcome.REJECTED.value:
                    rejected += 1
            call_entries.append(
                {
                    "logical_call_id": record["logical_call_id"],
                    "request_checksum": record["request_checksum"],
                    "runner_turn_ref": record["runner_turn_ref"],
                    "runner_turn_checksum": record["runner_turn_checksum"],
                    "call_ordinal": record["call_ordinal"],
                    "tool_binding": record["tool_binding"],
                    "attempts": [
                        {
                            "physical_attempt_id": item["physical_attempt_id"],
                            "physical_attempt_no": item["physical_attempt_no"],
                            "intent_checksum": item["intent_checksum"],
                            "terminal": (
                                _artifact_projection(item["terminal"])
                                if item.get("terminal") is not None
                                else None
                            ),
                            "current_outcome": (
                                item["terminal"].get("outcome")
                                if item.get("terminal") is not None
                                else ChildToolEvidenceOutcome.UNKNOWN.value
                            ),
                            "current_invocation_state": (
                                item["terminal"].get("invocation_state")
                                if item.get("terminal") is not None
                                else ChildToolInvocationState.UNKNOWN.value
                            ),
                        }
                        for item in attempts
                    ],
                    "receipt": (
                        _artifact_projection(receipt) if receipt is not None else None
                    ),
                }
            )
        registered = len(ordered)
        completeness = (
            ChildToolEvidenceCompleteness.COMPLETE
            if unresolved == 0
            else ChildToolEvidenceCompleteness.INCOMPLETE
        )
        physical = int(payload["physical_attempt_count"])
        if registered == 0:
            disposition = "NO_TOOL_REQUESTS"
        elif physical == 0 and rejected == registered and unresolved == 0:
            disposition = "NO_TOOL_INVOCATIONS"
        elif completeness is ChildToolEvidenceCompleteness.INCOMPLETE:
            disposition = "UNKNOWN"
        else:
            disposition = "TOOL_INVOCATIONS_RECORDED"
        return {
            "schema_version": "newsroom.child-tool-evidence-index/v1",
            "child_scope_key": self._scope.child_scope_key,
            "scope_checksum": self._scope.scope_checksum,
            "revision": revision,
            "completeness": completeness.value,
            "disposition": disposition,
            "counts": {
                "registered_logical_calls": registered,
                "rejected_logical_calls": rejected,
                "admitted_physical_attempts": physical,
                "known_terminal_attempts": int(payload["known_terminal_count"]),
                "unresolved_attempts": unresolved,
            },
            "logical_calls": call_entries,
            "receipt_manifest": receipts,
        }

    def _index_from_payload(self, payload: dict[str, Any]) -> ChildToolEvidenceIndex:
        index = _dict(payload.get("index"), "index")
        document = self._read_verified_artifact(
            index["ref"],
            artifact_type=self._artifact_type("evidence-index"),
            expected_artifact_checksum=index["artifact_checksum"],
            expected_content_checksum=index["content_checksum"],
            expected_byte_size=index["byte_size"],
        )
        counts = _dict(index["counts"], "index counts")
        body = _artifact_body(document)
        rebuilt = self._build_index_body(
            payload,
            revision=int(index["revision"]),
        )
        if (
            body != rebuilt
            or body.get("completeness") != index.get("completeness")
            or body.get("disposition") != index.get("disposition")
            or body.get("counts") != counts
        ):
            raise ToolEvidencePersistenceError(
                "child tool evidence index attribution mismatch"
            )
        return ChildToolEvidenceIndex(
            ref=index["ref"],
            checksum=index["artifact_checksum"],
            content_checksum=index["content_checksum"],
            byte_size=int(index["byte_size"]),
            revision=int(index["revision"]),
            completeness=ChildToolEvidenceCompleteness(index["completeness"]),
            disposition=str(index["disposition"]),
            registered_logical_calls=int(counts["registered_logical_calls"]),
            rejected_logical_calls=int(counts["rejected_logical_calls"]),
            admitted_physical_attempts=int(counts["admitted_physical_attempts"]),
            known_terminal_attempts=int(counts["known_terminal_attempts"]),
            unresolved_attempts=int(counts["unresolved_attempts"]),
        )

    def _cas_mutate(
        self,
        mutation: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]],
    ) -> dict[str, Any]:
        for _ in range(_MAX_CAS_RETRIES):
            current = self._require_state()
            payload = deepcopy(self._validated_state(current))
            next_payload, changed = mutation(payload)
            if not changed:
                return next_payload
            next_state = TransactionalStateSnapshot.create(
                namespace=current.namespace,
                key=current.key,
                revision=current.revision + 1,
                payload=next_payload,
            )
            try:
                persisted = self._runtime.compare_and_swap_transactional_state(
                    next_state,
                    expected_revision=current.revision,
                    expected_checksum=current.checksum,
                )
            except EventStoreContentionError:
                continue
            except ToolEvidencePersistenceError:
                raise
            except BaseException as exc:  # noqa: BLE001 - required durable boundary
                raise ToolEvidencePersistenceError(
                    "child tool evidence state write failed"
                ) from exc
            return deepcopy(self._validated_state(persisted))
        raise ToolEvidencePersistenceError("child tool evidence CAS did not converge")

    def _require_state(self) -> TransactionalStateSnapshot:
        state = self._reader.load_transactional_state(
            CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
            self._scope.child_scope_key,
        )
        if state is None:
            raise ToolEvidencePersistenceError("child tool evidence scope is not open")
        return state

    def _require_open_state(self) -> TransactionalStateSnapshot:
        state = self._require_state()
        self._require_open_payload(self._validated_state(state))
        return state

    def _validated_state(self, state: TransactionalStateSnapshot) -> dict[str, Any]:
        if not isinstance(state, TransactionalStateSnapshot):
            raise ToolEvidencePersistenceError("invalid child tool evidence state")
        if (state.namespace, state.key) != (
            CHILD_TOOL_EVIDENCE_STATE_NAMESPACE,
            self._scope.child_scope_key,
        ):
            raise ToolEvidencePersistenceError("child tool evidence state identity mismatch")
        payload = thaw_canonical_json(state.payload)
        if not isinstance(payload, dict):
            raise ToolEvidencePersistenceError("invalid child tool evidence payload")
        if payload.get("schema_version") != "newsroom.child-tool-evidence-state/v1":
            raise ToolEvidencePersistenceError("unsupported child tool evidence state")
        expected_state_fields = {
            "schema_version",
            "scope",
            "status",
            "artifact_reservations",
            "runner_turns",
            "logical_calls",
            "physical_attempt_count",
            "known_terminal_count",
            "total_evidence_bytes",
            "index",
        }
        if set(payload) != expected_state_fields:
            raise ToolEvidencePersistenceError("child tool evidence state fields are invalid")
        if payload.get("scope") != self._scope.to_dict():
            raise ToolEvidencePersistenceError("child tool evidence scope ownership conflict")
        if payload.get("status") not in {"OPEN", "CLOSED"}:
            raise ToolEvidencePersistenceError("child tool evidence state is corrupt")
        if (payload["status"] == "CLOSED") != (payload.get("index") is not None):
            raise ToolEvidencePersistenceError("child tool evidence close state is corrupt")
        calls = _dict(payload.get("logical_calls"), "logical_calls")
        turns = _dict(payload.get("runner_turns"), "runner_turns")
        if len(calls) > self._scope.limits.max_logical_calls:
            raise ToolEvidencePersistenceError("logical tool call count exceeds budget")
        accounted_bytes = 0
        committed_descriptors: dict[str, dict[str, Any]] = {}
        reservations = _dict(
            payload.get("artifact_reservations"), "artifact_reservations"
        )
        for reservation_id, reservation_value in reservations.items():
            _required_checksum(reservation_id, "artifact reservation id")
            reservation = _dict(reservation_value, "artifact reservation")
            if set(reservation) != {
                "reservation_id",
                "artifact_id",
                "artifact_type",
                "content_checksum",
                "byte_size",
                "created_at",
                "status",
                "descriptor",
            }:
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reservation fields are invalid"
                )
            if reservation.get("reservation_id") != reservation_id:
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reservation key is corrupt"
                )
            _required_text(reservation.get("artifact_id"), "artifact_id")
            _required_text(reservation.get("artifact_type"), "artifact_type")
            _required_checksum(
                reservation.get("content_checksum"), "artifact content checksum"
            )
            try:
                created_at = parse_datetime(reservation.get("created_at"))
            except (TypeError, ValueError) as exc:
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reservation timestamp is invalid"
                ) from exc
            if created_at is None:
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reservation timestamp is invalid"
                )
            reserved_bytes = _non_negative_int(
                reservation.get("byte_size"), "artifact byte_size"
            )
            accounted_bytes += reserved_bytes
            status = reservation.get("status")
            descriptor_value = reservation.get("descriptor")
            if status == "RESERVED":
                if descriptor_value is not None:
                    raise ToolEvidencePersistenceError(
                        "reserved tool evidence artifact has a committed descriptor"
                    )
                continue
            if status != "COMMITTED":
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reservation status is invalid"
                )
            descriptor = _dict(descriptor_value, "artifact descriptor")
            self._validate_committed_descriptor(reservation, descriptor)
            ref = str(descriptor["ref"])
            if ref in committed_descriptors:
                raise ToolEvidencePersistenceError(
                    "tool evidence artifact reference is not unique"
                )
            committed_descriptors[ref] = descriptor

        def require_committed_descriptor(value: Any, field_name: str) -> dict[str, Any]:
            descriptor = _dict(value, field_name)
            committed = committed_descriptors.get(str(descriptor.get("ref")))
            if committed is None or any(
                descriptor.get(name) != committed.get(name)
                for name in committed
            ):
                raise ToolEvidencePersistenceError(
                    f"{field_name} is not backed by a committed artifact reservation"
                )
            return descriptor

        for checksum, descriptor in turns.items():
            _required_checksum(checksum, "runner turn checksum")
            descriptor = require_committed_descriptor(
                descriptor, "runner turn descriptor"
            )
            if descriptor.get("content_checksum") != checksum:
                raise ToolEvidencePersistenceError("runner turn descriptor is corrupt")
        physical_count = 0
        terminal_count = 0
        for logical_id, record in calls.items():
            record = _dict(record, "logical call")
            if record.get("logical_call_id") != logical_id:
                raise ToolEvidencePersistenceError("logical tool call key is corrupt")
            operation_key = _required_text(
                record.get("operation_key"), "logical tool operation key"
            )
            require_committed_descriptor(
                record.get("request_artifact"), "request artifact"
            )
            attempts = _list(record.get("attempts"), "attempts")
            for expected_ordinal, attempt in enumerate(attempts, start=1):
                physical_count += 1
                if attempt.get("physical_attempt_no") != expected_ordinal:
                    raise ToolEvidencePersistenceError(
                        "physical tool attempt ordinal is corrupt"
                    )
                expected_physical_id = _checksum(
                    {
                        "logical_call_id": logical_id,
                        "local_attempt_no": expected_ordinal,
                        "attempt_id": attempt.get("attempt_id"),
                    }
                )
                if attempt.get("physical_attempt_id") != expected_physical_id:
                    raise ToolEvidencePersistenceError(
                        "physical tool attempt identity is corrupt"
                    )
                if (
                    attempt.get("operation_id") != operation_key
                    or attempt.get("idempotency_key") != operation_key
                ):
                    raise ToolEvidencePersistenceError(
                        "physical tool attempt operation identity is corrupt"
                    )
                intent_projection = {
                    name: value
                    for name, value in attempt.items()
                    if name not in {"intent_checksum", "terminal"}
                }
                intent_projection["terminal"] = None
                if attempt.get("intent_checksum") != _checksum(intent_projection):
                    raise ToolEvidencePersistenceError(
                        "physical tool intent checksum is corrupt"
                    )
                terminal = attempt.get("terminal")
                if terminal is not None:
                    terminal_count += 1
                    require_committed_descriptor(
                        terminal, "terminal artifact"
                    )
            receipt = record.get("receipt")
            if receipt is not None:
                require_committed_descriptor(
                    receipt, "receipt artifact"
                )
        index = payload.get("index")
        if index is not None:
            index = _dict(index, "index")
            require_committed_descriptor(index, "index artifact")
        if _non_negative_int(
            payload.get("physical_attempt_count"), "physical_attempt_count"
        ) != physical_count:
            raise ToolEvidencePersistenceError("physical tool attempt count is corrupt")
        if _non_negative_int(
            payload.get("known_terminal_count"), "known_terminal_count"
        ) != terminal_count:
            raise ToolEvidencePersistenceError("terminal tool attempt count is corrupt")
        if _non_negative_int(
            payload.get("total_evidence_bytes"), "total_evidence_bytes"
        ) != accounted_bytes:
            raise ToolEvidencePersistenceError("tool evidence byte count is corrupt")
        if physical_count > self._scope.limits.max_physical_attempts:
            raise ToolEvidencePersistenceError("physical tool attempt count exceeds budget")
        if accounted_bytes > self._scope.limits.max_total_evidence_bytes:
            raise ToolEvidencePersistenceError("tool evidence bytes exceed capacity")
        return payload

    @staticmethod
    def _require_open_payload(payload: dict[str, Any]) -> None:
        if payload["status"] != "OPEN":
            raise ToolEvidencePersistenceError("child tool evidence scope is closed")

    def _logical_record(
        self,
        payload: dict[str, Any],
        logical_id: str,
        call: ToolCall,
    ) -> dict[str, Any]:
        record = _dict(payload["logical_calls"], "logical_calls").get(logical_id)
        if record is None:
            raise ToolEvidencePersistenceError("logical tool call is not registered")
        if (
            record.get("runtime_call_id") != call.call_id
            or record.get("requested_tool_name") != call.tool_name
        ):
            raise ToolEvidencePersistenceError("logical tool call attribution conflict")
        return record

    def _validate_call_scope(self, call: ToolCall) -> None:
        if not isinstance(call, ToolCall):
            raise TypeError("call must be ToolCall")
        if call.graph_identity != self._scope.parent_graph_identity:
            raise ToolEvidencePersistenceError(
                "tool call Graph identity does not match its child scope"
            )

    def _logical_identity(self, call: ToolCall) -> tuple[str, str, str, int]:
        runner_ref = _required_text(
            call.metadata.get("runner_turn_ref"), "runner_turn_ref"
        )
        runner_checksum = _required_checksum(
            call.metadata.get("runner_turn_checksum"), "runner_turn_checksum"
        )
        ordinal = call.metadata.get("call_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise ToolEvidencePersistenceError("call_ordinal must be a non-negative integer")
        logical_id = _checksum(
            {
                "child_scope_key": self._scope.child_scope_key,
                "runner_turn_ref": runner_ref,
                "call_ordinal": ordinal,
            }
        )
        return logical_id, runner_ref, runner_checksum, ordinal

    def _write_verified_artifact(
        self,
        kind: str,
        body: Mapping[str, Any],
        *,
        max_bytes: int,
    ) -> dict[str, Any]:
        canonical_body = to_jsonable(dict(body))
        if _encoded_size(canonical_body) > max_bytes:
            raise ToolEvidencePersistenceError(f"{kind} artifact exceeds its size limit")
        content_checksum = _checksum(canonical_body)
        artifact_type = self._artifact_type(kind)
        metadata = self._artifact_metadata()
        document = {
            "artifact_type": artifact_type,
            "payload": {
                "schema_version": "newsroom.child-tool-evidence-artifact/v1",
                "kind": kind,
                "child_scope_key": self._scope.child_scope_key,
                "scope_checksum": self._scope.scope_checksum,
                "content_checksum": content_checksum,
                "body": canonical_body,
            },
            "media_type": "application/json",
            "metadata": metadata,
        }
        content = stable_json_dumps(document).encode("utf-8")
        byte_size = len(content)
        reservation_id = _checksum(
            {
                "child_scope_key": self._scope.child_scope_key,
                "artifact_type": artifact_type,
                "content_checksum": content_checksum,
            }
        )
        artifact_id = f"child-tool-evidence-{reservation_id.removeprefix('sha256:')}"
        reservation = self._reserve_artifact(
            reservation_id=reservation_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            content_checksum=content_checksum,
            byte_size=byte_size,
        )
        request = ArtifactWriteRequest(
            run_id=self._scope.run_id,
            scope_kind=ARTIFACT_SCOPE_GRAPH,
            graph_id=self._scope.parent_graph_identity.graph_id,
            graph_version=self._scope.parent_graph_identity.graph_version,
            graph_ref=self._scope.parent_graph_identity.graph_ref,
            graph_checksum=self._scope.parent_graph_identity.graph_checksum,
            node_id=self._scope.parent_graph_identity.node_id,
            node_instance_id=self._scope.parent_graph_identity.node_instance_id,
            activity_id=self._scope.parent_graph_identity.activity_id,
            attempt=self._scope.parent_graph_identity.attempt,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            content=content,
            content_type="application/json",
            redacted=True,
            created_at=parse_datetime(reservation["created_at"]) or self._clock(),
            metadata=metadata,
        )
        try:
            ref = self._artifacts.write(request)
        except BaseException as exc:  # noqa: BLE001 - required durable boundary
            raise ToolEvidencePersistenceError(f"failed to write {kind} artifact") from exc
        if not isinstance(ref, ArtifactRef):
            raise ToolEvidencePersistenceError("artifact writer returned an invalid reference")
        expected_artifact_checksum = sha256(content).hexdigest()
        if (
            ref.artifact_id != artifact_id
            or ref.run_id != self._scope.run_id
            or ref.artifact_type != artifact_type
            or ref.content_type != "application/json"
            or ref.checksum != expected_artifact_checksum
            or ref.size_bytes != byte_size
            or ref.metadata != metadata
        ):
            raise ToolEvidencePersistenceError("artifact writer changed immutable metadata")
        descriptor = {
            "ref": ref.artifact_id,
            "artifact_ref": ref.to_dict(),
            "artifact_checksum": expected_artifact_checksum,
            "content_checksum": content_checksum,
            "byte_size": byte_size,
            "artifact_type": artifact_type,
            "reservation_id": reservation_id,
        }
        self._read_verified_artifact(
            ref.artifact_id,
            artifact_type=artifact_type,
            expected_artifact_checksum=expected_artifact_checksum,
            expected_content_checksum=content_checksum,
            expected_byte_size=byte_size,
            expected_document=document,
            provisional_ref=ref,
        )
        self._commit_artifact_reservation(reservation_id, descriptor)
        return descriptor

    def _reserve_artifact(
        self,
        *,
        reservation_id: str,
        artifact_id: str,
        artifact_type: str,
        content_checksum: str,
        byte_size: int,
    ) -> dict[str, Any]:
        immutable = {
            "reservation_id": reservation_id,
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "content_checksum": content_checksum,
            "byte_size": byte_size,
        }

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            reservations = _dict(
                payload["artifact_reservations"], "artifact_reservations"
            )
            existing = reservations.get(reservation_id)
            if existing is not None:
                for name, value in immutable.items():
                    if existing.get(name) != value:
                        raise ToolEvidencePersistenceError(
                            "tool evidence artifact reservation identity conflict"
                        )
                return payload, False
            total = int(payload["total_evidence_bytes"]) + byte_size
            if total > self._scope.limits.max_total_evidence_bytes:
                raise ToolEvidencePersistenceError(
                    "child tool evidence capacity exhausted"
                )
            reservations[reservation_id] = {
                **immutable,
                "created_at": _clock_timestamp(self._clock),
                "status": "RESERVED",
                "descriptor": None,
            }
            payload["artifact_reservations"] = reservations
            payload["total_evidence_bytes"] = total
            return payload, True

        persisted = self._cas_mutate(mutate)
        return _dict(
            _dict(persisted["artifact_reservations"], "artifact_reservations").get(
                reservation_id
            ),
            "artifact reservation",
        )

    def _commit_artifact_reservation(
        self,
        reservation_id: str,
        descriptor: Mapping[str, Any],
    ) -> None:
        canonical_descriptor = to_jsonable(dict(descriptor))

        def mutate(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            self._require_open_payload(payload)
            reservations = _dict(
                payload["artifact_reservations"], "artifact_reservations"
            )
            reservation = _dict(
                reservations.get(reservation_id),
                "artifact reservation",
            )
            existing = reservation.get("descriptor")
            if existing is not None:
                if existing != canonical_descriptor:
                    raise ToolEvidencePersistenceError(
                        "tool evidence artifact reservation commit conflicts"
                    )
                return payload, False
            reservation["status"] = "COMMITTED"
            reservation["descriptor"] = canonical_descriptor
            reservations[reservation_id] = reservation
            payload["artifact_reservations"] = reservations
            return payload, True

        self._cas_mutate(mutate)

    def _read_verified_artifact(
        self,
        ref: str,
        *,
        artifact_type: str,
        expected_artifact_checksum: str,
        expected_content_checksum: str,
        expected_byte_size: int,
        expected_document: Mapping[str, Any] | None = None,
        provisional_ref: ArtifactRef | None = None,
    ) -> dict[str, Any]:
        artifact_ref = provisional_ref or self._committed_artifact_ref(ref)
        try:
            self._authorization.authorize_artifact_read(
                self._scope,
                artifact_ref=artifact_ref,
                artifact_type=artifact_type,
                expected_artifact_checksum=expected_artifact_checksum,
                expected_byte_size=expected_byte_size,
            )
        except ToolEvidencePersistenceError:
            raise
        except BaseException as exc:  # noqa: BLE001 - authorization is mandatory
            raise ToolEvidencePersistenceError(
                "tool evidence artifact read is not authorized"
            ) from exc
        try:
            raw_value = self._artifacts.read(artifact_ref)
        except BaseException as exc:  # noqa: BLE001 - required durable boundary
            raise ToolEvidencePersistenceError("tool evidence artifact is unreadable") from exc
        if not isinstance(raw_value, bytes):
            raise ToolEvidencePersistenceError("tool evidence artifact body is invalid")
        if len(raw_value) != expected_byte_size:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact is unreadable: size mismatch"
            )
        if sha256(raw_value).hexdigest() != expected_artifact_checksum:
            raise ToolEvidencePersistenceError("tool evidence artifact checksum mismatch")
        try:
            import json

            value = json.loads(raw_value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact is not canonical JSON"
            ) from exc
        if stable_json_dumps(value).encode("utf-8") != raw_value:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact is not canonical JSON"
            )
        document = _mapping(value, "artifact document")
        if set(document) != {"artifact_type", "payload", "media_type", "metadata"}:
            raise ToolEvidencePersistenceError("tool evidence artifact fields are invalid")
        if expected_document is not None and document != to_jsonable(expected_document):
            raise ToolEvidencePersistenceError("tool evidence artifact read-back mismatch")
        if (
            document.get("artifact_type") != artifact_type
            or document.get("media_type") != "application/json"
        ):
            raise ToolEvidencePersistenceError("tool evidence artifact type mismatch")
        payload = _mapping(document.get("payload"), "artifact payload")
        metadata = _mapping(document.get("metadata"), "artifact metadata")
        if (
            set(payload)
            != {
                "schema_version",
                "kind",
                "child_scope_key",
                "scope_checksum",
                "content_checksum",
                "body",
            }
            or payload.get("schema_version")
            != "newsroom.child-tool-evidence-artifact/v1"
            or payload.get("kind")
            != artifact_type.removeprefix(f"{_ARTIFACT_TYPE_PREFIX}.").removesuffix(
                ".v1"
            )
            or payload.get("child_scope_key") != self._scope.child_scope_key
            or payload.get("scope_checksum") != self._scope.scope_checksum
            or payload.get("content_checksum") != expected_content_checksum
            or _checksum(payload.get("body")) != expected_content_checksum
        ):
            raise ToolEvidencePersistenceError("tool evidence artifact integrity mismatch")
        if metadata != {
            "tenant_id": self._scope.tenant_id,
            "owner_scope": self._scope.owner_scope,
            "child_scope_key": self._scope.child_scope_key,
            "scope_checksum": self._scope.scope_checksum,
            "input_grant_ref": self._scope.input_grant_ref,
        }:
            raise ToolEvidencePersistenceError("tool evidence artifact ownership mismatch")
        return dict(document)

    def _committed_artifact_ref(self, ref: str) -> ArtifactRef:
        payload = self._validated_state(self._require_state())
        matches = []
        for reservation in _dict(
            payload["artifact_reservations"], "artifact_reservations"
        ).values():
            if reservation.get("status") != "COMMITTED":
                continue
            descriptor = _dict(reservation.get("descriptor"), "artifact descriptor")
            if descriptor.get("ref") == ref:
                matches.append(descriptor)
        if len(matches) != 1:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact reference is not uniquely committed"
            )
        try:
            return ArtifactRef.from_dict(
                _dict(matches[0].get("artifact_ref"), "artifact reference")
            )
        except (TypeError, ValueError) as exc:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact reference is corrupt"
            ) from exc

    def _validate_committed_descriptor(
        self,
        reservation: Mapping[str, Any],
        descriptor: Mapping[str, Any],
    ) -> None:
        expected_fields = {
            "ref",
            "artifact_ref",
            "artifact_checksum",
            "content_checksum",
            "byte_size",
            "artifact_type",
            "reservation_id",
        }
        if set(descriptor) != expected_fields:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact descriptor fields are invalid"
            )
        if (
            descriptor.get("ref") != reservation.get("artifact_id")
            or descriptor.get("reservation_id") != reservation.get("reservation_id")
            or descriptor.get("artifact_type") != reservation.get("artifact_type")
            or descriptor.get("content_checksum")
            != reservation.get("content_checksum")
            or descriptor.get("byte_size") != reservation.get("byte_size")
            or not isinstance(descriptor.get("artifact_checksum"), str)
            or _ARTIFACT_CHECKSUM.fullmatch(descriptor["artifact_checksum"]) is None
        ):
            raise ToolEvidencePersistenceError(
                "tool evidence artifact descriptor conflicts with its reservation"
            )
        try:
            artifact_ref = ArtifactRef.from_dict(
                _dict(descriptor.get("artifact_ref"), "artifact reference")
            )
        except (TypeError, ValueError) as exc:
            raise ToolEvidencePersistenceError(
                "tool evidence artifact reference is corrupt"
            ) from exc
        try:
            self._authorization.authorize_artifact_read(
                self._scope,
                artifact_ref=artifact_ref,
                artifact_type=str(descriptor["artifact_type"]),
                expected_artifact_checksum=str(descriptor["artifact_checksum"]),
                expected_byte_size=int(descriptor["byte_size"]),
            )
        except ToolEvidencePersistenceError:
            raise
        except BaseException as exc:  # noqa: BLE001 - metadata authority is required
            raise ToolEvidencePersistenceError(
                "tool evidence artifact metadata is not authorized"
            ) from exc

    def _artifact_metadata(self) -> dict[str, Any]:
        return {
            "tenant_id": self._scope.tenant_id,
            "owner_scope": self._scope.owner_scope,
            "child_scope_key": self._scope.child_scope_key,
            "scope_checksum": self._scope.scope_checksum,
            "input_grant_ref": self._scope.input_grant_ref,
        }

    @staticmethod
    def _artifact_type(kind: str) -> str:
        return f"{_ARTIFACT_TYPE_PREFIX}.{kind}.v1"


def _terminal_body(
    scope: ChildToolEvidenceScope,
    logical_id: str,
    outcome: AttemptOutcome[Any],
) -> dict[str, Any]:
    context = outcome.context
    if context is None:
        raise ToolEvidencePersistenceError("tool terminal is missing attempt context")
    if outcome.indeterminate or outcome.state is AttemptState.INDETERMINATE:
        result = ChildToolEvidenceOutcome.UNKNOWN
        invocation = ChildToolInvocationState.UNKNOWN
    elif outcome.state is AttemptState.SUCCEEDED:
        if outcome.termination_confirmed is not True:
            raise ToolEvidencePersistenceError(
                "successful tool attempt lacks confirmed termination"
            )
        result = ChildToolEvidenceOutcome.SUCCEEDED
        invocation = ChildToolInvocationState.INVOKED
    elif outcome.state is AttemptState.REJECTED:
        raise ToolEvidencePersistenceError(
            "an admitted physical tool intent cannot terminate as not invoked"
        )
    elif outcome.termination_confirmed is True:
        result = ChildToolEvidenceOutcome.FAILED
        invocation = ChildToolInvocationState.INVOKED
    else:
        result = ChildToolEvidenceOutcome.UNKNOWN
        invocation = ChildToolInvocationState.UNKNOWN
    return {
        "schema_version": "newsroom.child-tool-attempt-terminal/v1",
        "child_scope_key": scope.child_scope_key,
        "scope_checksum": scope.scope_checksum,
        "logical_call_id": logical_id,
        "attempt_id": context.attempt_id,
        "physical_attempt_no": context.local_attempt_no,
        "operation_id": context.operation_id,
        "idempotency_key": context.idempotency_key,
        "outcome": result.value,
        "invocation_state": invocation.value,
        "termination_confirmed": outcome.termination_confirmed,
        "indeterminate": outcome.indeterminate,
        "timed_out": outcome.timed_out,
        "reason_code": outcome.reason_code,
        "elapsed_seconds": outcome.elapsed_seconds,
    }


def _validate_observation_against_attempts(
    observation: ToolObservation,
    attempts: list[dict[str, Any]],
) -> None:
    rejected = observation.status in {
        ToolStatus.BLOCKED,
        ToolStatus.DENIED,
        ToolStatus.APPROVAL_REQUIRED,
        ToolStatus.SKIPPED,
    }
    if rejected:
        if attempts:
            raise ToolEvidencePersistenceError(
                "zero-invocation rejection conflicts with admitted physical intent"
            )
        return
    if not attempts:
        raise ToolEvidencePersistenceError(
            "tool observation has no admitted physical intent"
        )
    latest_terminal = attempts[-1].get("terminal")
    if latest_terminal is None:
        if not observation.result.indeterminate:
            raise ToolEvidencePersistenceError(
                "tool observation lacks a durable physical terminal"
            )
        return
    if observation.status is ToolStatus.SUCCEEDED and (
        latest_terminal.get("outcome") != ChildToolEvidenceOutcome.SUCCEEDED.value
        or latest_terminal.get("termination_confirmed") is not True
    ):
        raise ToolEvidencePersistenceError(
            "successful observation conflicts with physical terminal evidence"
        )


def _artifact_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value.get(name)
        for name in (
            "ref",
            "artifact_checksum",
            "content_checksum",
            "byte_size",
            "artifact_type",
            "outcome",
            "invocation_state",
            "termination_confirmed",
        )
        if name in value
    }


def _tool_operation_key(call: ToolCall) -> str:
    parent_context = current_attempt_context()
    if parent_context is not None:
        child_id = str(call.metadata.get("idempotency_key") or call.call_id)
        return _required_text(
            derive_idempotency_key(
                parent_context.idempotency_key,
                "tool",
                child_id,
            ),
            "tool operation key",
        )
    configured = call.metadata.get("idempotency_key")
    return _required_text(
        str(configured or f"tool:{call.call_id}"), "tool operation key"
    )


def _artifact_body(document: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(document.get("payload"), "artifact payload")
    return _mapping(payload.get("body"), "artifact body")


def _close_basis_checksum(payload: Mapping[str, Any]) -> str:
    return _checksum(
        {
            "runner_turns": payload.get("runner_turns"),
            "logical_calls": payload.get("logical_calls"),
            "physical_attempt_count": payload.get("physical_attempt_count"),
            "known_terminal_count": payload.get("known_terminal_count"),
        }
    )


def _checksum(value: Any) -> str:
    return "sha256:" + sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def _encoded_size(value: Any) -> int:
    return len(stable_json_dumps(value).encode("utf-8"))


def _clock_timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime):
        raise ToolEvidencePersistenceError("tool evidence clock returned an invalid value")
    formatted = format_datetime(value)
    if formatted is None:  # pragma: no cover - datetime input is authoritative
        raise ToolEvidencePersistenceError("tool evidence clock returned no timestamp")
    return formatted


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2048:
        raise ToolEvidencePersistenceError(f"{name} must be a bounded canonical string")
    return value


def _optional_bounded_text(value: Any, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum:
        raise ToolEvidencePersistenceError(f"{name} must be a bounded canonical string")
    return value


def _required_checksum(value: Any, name: str) -> str:
    if not isinstance(value, str) or _CHECKSUM.fullmatch(value) is None:
        raise ToolEvidencePersistenceError(f"{name} must be a canonical sha256 checksum")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ToolEvidencePersistenceError(f"{name} must be a non-negative integer")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ToolEvidencePersistenceError(f"{name} must be an object")
    return dict(value)


def _dict(value: Any, name: str) -> dict[str, Any]:
    return _mapping(value, name)


def _list(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ToolEvidencePersistenceError(f"{name} must be an object list")
    return [dict(item) for item in value]


__all__ = [
    "CHILD_TOOL_EVIDENCE_STATE_NAMESPACE",
    "ChildToolEvidenceAuthorizationPort",
    "ChildToolEvidenceCompleteness",
    "ChildToolEvidenceIndex",
    "ChildToolEvidenceLimits",
    "ChildToolEvidenceOutcome",
    "ChildToolEvidenceScope",
    "ChildToolInvocationState",
    "DurableChildToolEvidenceOwner",
    "MetadataFirstChildToolEvidenceAuthorization",
]
