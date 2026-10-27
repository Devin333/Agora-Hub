from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.state import HarnessRunStatus, HarnessStepStatus
from framework.harness.waits.models import (
    HarnessSignalInboxEntry,
    HarnessSignalInboxEntryStatus,
)
from framework.harness.graph.canonical import (
    canonical_checksum,
    exact_reference,
    freeze_json,
    optional_text,
    required_text,
    thaw_json,
)
from framework.harness.graph.dsl import WaitKind
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
    HarnessGraphNodeKind,
    NormalizedHarnessGraph,
)
from framework.harness.graph.reference import (
    HarnessGraphReference as _HarnessGraphReference,
)
from framework.harness.graph.versioning import (
    GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA,
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_GRAPH_RUNTIME_VERSION,
)


_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class RunLifecycle(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    HALTED = "halted"


class RunOutcome(StrEnum):
    NONE = "none"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    INDETERMINATE = "indeterminate"


class HarnessNodeInstanceStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    CANCEL_REQUESTED = "cancel_requested"
    COMPENSATING = "compensating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    HALTED = "halted"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"


class HarnessEvidenceKind(StrEnum):
    ACTIVITY_RESULT = "activity_result"
    GATE_RESULT = "gate_result"
    SIDE_EFFECT_OUTCOME = "side_effect_outcome"
    APPROVAL = "approval"
    SIGNAL = "signal"
    TIMER = "timer"
    MERGE_RESULT = "merge_result"


class HarnessPendingSideEffectScope(StrEnum):
    NODE_INSTANCE = "node_instance"
    TERMINAL_RUN = "terminal_run"


class HarnessPendingSideEffectStatus(StrEnum):
    PREPARED = "prepared"
    OUTCOME_RECORDED = "outcome_recorded"
    FAILED = "failed"


class HarnessJoinKind(StrEnum):
    ALL = "all"
    ANY = "any"


class HarnessJoinStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    SATISFIED = "satisfied"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"


class HarnessLoopStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    EXITED = "exited"
    EXHAUSTED = "exhausted"


class HarnessWaitStatus(StrEnum):
    REGISTERED = "registered"
    RESUMED = "resumed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class HarnessCompensationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


_RUNNING_NODE_STATUSES = frozenset(
    {
        HarnessNodeInstanceStatus.RUNNING,
        HarnessNodeInstanceStatus.CANCEL_REQUESTED,
        HarnessNodeInstanceStatus.COMPENSATING,
    }
)
_ACTIVE_NODE_STATUSES = frozenset(
    {
        HarnessNodeInstanceStatus.READY,
        HarnessNodeInstanceStatus.RUNNING,
        HarnessNodeInstanceStatus.WAITING,
        HarnessNodeInstanceStatus.CANCEL_REQUESTED,
        HarnessNodeInstanceStatus.COMPENSATING,
    }
)
_TERMINAL_NODE_STATUSES = frozenset(
    {
        HarnessNodeInstanceStatus.SUCCEEDED,
        HarnessNodeInstanceStatus.FAILED,
        HarnessNodeInstanceStatus.CANCELLED,
        HarnessNodeInstanceStatus.HALTED,
        HarnessNodeInstanceStatus.SKIPPED,
        HarnessNodeInstanceStatus.COMPENSATED,
    }
)
_UNRESOLVED_WAIT_STATUSES = frozenset({HarnessWaitStatus.REGISTERED})
_NODE_STEP_STATUS_COMPATIBILITY = {
    HarnessNodeInstanceStatus.PENDING: frozenset({HarnessStepStatus.PENDING}),
    HarnessNodeInstanceStatus.READY: frozenset({HarnessStepStatus.PENDING}),
    HarnessNodeInstanceStatus.RUNNING: frozenset(
        {
            HarnessStepStatus.PLANNING,
            HarnessStepStatus.PLAN_VERIFIED,
            HarnessStepStatus.RUNNING,
            HarnessStepStatus.VERIFYING,
            HarnessStepStatus.RETRYING,
            HarnessStepStatus.REPLANNING,
        }
    ),
    HarnessNodeInstanceStatus.WAITING: frozenset({HarnessStepStatus.WAITING_APPROVAL}),
    HarnessNodeInstanceStatus.CANCEL_REQUESTED: frozenset(
        {
            HarnessStepStatus.PLANNING,
            HarnessStepStatus.PLAN_VERIFIED,
            HarnessStepStatus.RUNNING,
            HarnessStepStatus.VERIFYING,
            HarnessStepStatus.RETRYING,
            HarnessStepStatus.REPLANNING,
            HarnessStepStatus.WAITING_APPROVAL,
        }
    ),
    HarnessNodeInstanceStatus.COMPENSATING: frozenset(
        {
            HarnessStepStatus.PENDING,
            HarnessStepStatus.PLANNING,
            HarnessStepStatus.PLAN_VERIFIED,
            HarnessStepStatus.RUNNING,
            HarnessStepStatus.VERIFYING,
            HarnessStepStatus.RETRYING,
            HarnessStepStatus.REPLANNING,
        }
    ),
    HarnessNodeInstanceStatus.SUCCEEDED: frozenset({HarnessStepStatus.SUCCEEDED}),
    HarnessNodeInstanceStatus.FAILED: frozenset({HarnessStepStatus.FAILED}),
    HarnessNodeInstanceStatus.CANCELLED: frozenset(
        {HarnessStepStatus.SKIPPED, HarnessStepStatus.HALTED}
    ),
    HarnessNodeInstanceStatus.HALTED: frozenset({HarnessStepStatus.HALTED}),
    HarnessNodeInstanceStatus.SKIPPED: frozenset({HarnessStepStatus.SKIPPED}),
    HarnessNodeInstanceStatus.COMPENSATED: frozenset({HarnessStepStatus.SUCCEEDED}),
}


@dataclass(frozen=True, slots=True, order=True)
class HarnessLoopIteration:
    loop_id: str
    iteration: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "loop_id", required_text(self.loop_id, "iteration.loop_id")
        )
        _nonnegative_int(self.iteration, "iteration.iteration")

    def to_dict(self) -> dict[str, Any]:
        return {"loop_id": self.loop_id, "iteration": self.iteration}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessLoopIteration":
        _exact_keys(value, {"loop_id", "iteration"}, "loop iteration")
        return cls(loop_id=value["loop_id"], iteration=value["iteration"])


@dataclass(frozen=True, slots=True)
class HarnessNodeInstanceIdentity:
    run_id: str
    graph_checksum: str
    node_id: str
    branch_path: tuple[str, ...] = ()
    iteration_vector: tuple[HarnessLoopIteration, ...] = ()
    activation_ordinal: int = 0
    instance_id: str = field(init=False)

    def __post_init__(self) -> None:
        run_id = required_text(self.run_id, "node_identity.run_id")
        graph_checksum = _checksum(
            self.graph_checksum,
            "node_identity.graph_checksum",
        )
        node_id = required_text(self.node_id, "node_identity.node_id")
        branch_path = _ordered_text_tuple(
            self.branch_path,
            "node_identity.branch_path",
        )
        iteration_vector = tuple(self.iteration_vector)
        if not all(isinstance(item, HarnessLoopIteration) for item in iteration_vector):
            raise TypeError("iteration_vector must contain HarnessLoopIteration values")
        loop_ids = [item.loop_id for item in iteration_vector]
        if len(loop_ids) != len(set(loop_ids)):
            raise HarnessValidationError(
                "iteration vector cannot repeat one loop identity",
                code="duplicate_loop_iteration_identity",
            )
        _nonnegative_int(self.activation_ordinal, "node_identity.activation_ordinal")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "graph_checksum", graph_checksum)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "branch_path", branch_path)
        object.__setattr__(self, "iteration_vector", iteration_vector)
        digest = canonical_checksum(self.identity_projection()).removeprefix("sha256:")
        object.__setattr__(self, "instance_id", f"hni_{digest}")

    def identity_projection(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph_checksum": self.graph_checksum,
            "node_id": self.node_id,
            "branch_path": list(self.branch_path),
            "iteration_vector": [item.to_dict() for item in self.iteration_vector],
            "activation_ordinal": self.activation_ordinal,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_projection(), "instance_id": self.instance_id}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessNodeInstanceIdentity":
        _exact_keys(
            value,
            {
                "run_id",
                "graph_checksum",
                "node_id",
                "branch_path",
                "iteration_vector",
                "activation_ordinal",
                "instance_id",
            },
            "node identity",
        )
        identity = cls(
            run_id=value["run_id"],
            graph_checksum=value["graph_checksum"],
            node_id=value["node_id"],
            branch_path=tuple(
                _array(value["branch_path"], "node_identity.branch_path")
            ),
            iteration_vector=tuple(
                HarnessLoopIteration.from_dict(item)
                for item in _array(
                    value["iteration_vector"],
                    "node_identity.iteration_vector",
                )
            ),
            activation_ordinal=value["activation_ordinal"],
        )
        if value["instance_id"] != identity.instance_id:
            raise HarnessValidationError(
                "node instance identity does not match its canonical inputs",
                code="node_instance_identity_mismatch",
                details={
                    "expected": identity.instance_id,
                    "actual": str(value["instance_id"]),
                },
            )
        return identity


@dataclass(frozen=True, slots=True)
class HarnessBranchOutputReference:
    branch_id: str
    output_namespace: str
    branch_priority: int
    producer_node_id: str
    producer_declaration_order: int
    producer_node_instance_id: str
    producer_activation_ordinal: int
    attempt: int
    iteration_vector: tuple[HarnessLoopIteration, ...]
    output_key: str
    payload_ref: str
    producer_terminal_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "branch_id",
            "output_namespace",
            "producer_node_id",
            "producer_node_instance_id",
            "output_key",
        ):
            object.__setattr__(
                self,
                field_name,
                required_text(
                    getattr(self, field_name),
                    f"branch_output.{field_name}",
                ),
            )
        _nonnegative_int(self.branch_priority, "branch_output.branch_priority")
        _nonnegative_int(
            self.producer_declaration_order,
            "branch_output.producer_declaration_order",
        )
        _nonnegative_int(
            self.producer_activation_ordinal,
            "branch_output.producer_activation_ordinal",
        )
        _nonnegative_int(self.attempt, "branch_output.attempt")
        vector = tuple(self.iteration_vector)
        if not all(isinstance(item, HarnessLoopIteration) for item in vector):
            raise TypeError(
                "branch output iteration_vector must contain HarnessLoopIteration values"
            )
        object.__setattr__(self, "iteration_vector", vector)
        object.__setattr__(
            self,
            "payload_ref",
            _checksum(self.payload_ref, "branch_output.payload_ref"),
        )
        object.__setattr__(
            self,
            "producer_terminal_ref",
            _checksum(
                self.producer_terminal_ref,
                "branch_output.producer_terminal_ref",
            ),
        )

    @property
    def stable_order_key(self) -> tuple[Any, ...]:
        return (
            self.branch_priority,
            self.branch_id,
            self.producer_declaration_order,
            tuple((item.loop_id, item.iteration) for item in self.iteration_vector),
            self.producer_activation_ordinal,
            self.output_key,
            self.producer_node_instance_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch_id": self.branch_id,
            "output_namespace": self.output_namespace,
            "branch_priority": self.branch_priority,
            "producer_node_id": self.producer_node_id,
            "producer_declaration_order": self.producer_declaration_order,
            "producer_node_instance_id": self.producer_node_instance_id,
            "producer_activation_ordinal": self.producer_activation_ordinal,
            "attempt": self.attempt,
            "iteration_vector": [item.to_dict() for item in self.iteration_vector],
            "output_key": self.output_key,
            "payload_ref": self.payload_ref,
            "producer_terminal_ref": self.producer_terminal_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessBranchOutputReference":
        _exact_keys(
            value,
            {
                "branch_id",
                "output_namespace",
                "branch_priority",
                "producer_node_id",
                "producer_declaration_order",
                "producer_node_instance_id",
                "producer_activation_ordinal",
                "attempt",
                "iteration_vector",
                "output_key",
                "payload_ref",
                "producer_terminal_ref",
            },
            "branch output reference",
        )
        return cls(
            branch_id=value["branch_id"],
            output_namespace=value["output_namespace"],
            branch_priority=value["branch_priority"],
            producer_node_id=value["producer_node_id"],
            producer_declaration_order=value["producer_declaration_order"],
            producer_node_instance_id=value["producer_node_instance_id"],
            producer_activation_ordinal=value["producer_activation_ordinal"],
            attempt=value["attempt"],
            iteration_vector=tuple(
                HarnessLoopIteration.from_dict(item)
                for item in _array(
                    value["iteration_vector"],
                    "branch_output.iteration_vector",
                )
            ),
            output_key=value["output_key"],
            payload_ref=value["payload_ref"],
            producer_terminal_ref=value["producer_terminal_ref"],
        )


@dataclass(frozen=True, slots=True)
class HarnessAttemptEvidenceReference:
    evidence_ref: str
    kind: HarnessEvidenceKind | str
    node_instance_id: str
    attempt: int
    event_sequence: int
    contract_ref: HarnessContractReference | None = None
    payload_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_ref",
            _checksum(self.evidence_ref, "evidence.evidence_ref"),
        )
        object.__setattr__(self, "kind", HarnessEvidenceKind(self.kind))
        object.__setattr__(
            self,
            "node_instance_id",
            required_text(self.node_instance_id, "evidence.node_instance_id"),
        )
        _nonnegative_int(self.attempt, "evidence.attempt")
        _nonnegative_int(self.event_sequence, "evidence.event_sequence")
        if (self.contract_ref is None) != (self.payload_ref is None):
            raise HarnessValidationError(
                "evidence contract_ref and payload_ref must be declared together",
                code="incomplete_evidence_binding",
            )
        if self.contract_ref is not None:
            if not isinstance(self.contract_ref, HarnessContractReference):
                raise TypeError(
                    "evidence.contract_ref must be HarnessContractReference"
                )
            allowed_contract_kinds = {
                HarnessEvidenceKind.ACTIVITY_RESULT: frozenset(
                    {
                        HarnessContractKind.STEP,
                        HarnessContractKind.WORKER,
                        HarnessContractKind.ACTIVITY,
                    }
                ),
                HarnessEvidenceKind.GATE_RESULT: frozenset({HarnessContractKind.GATE}),
                HarnessEvidenceKind.SIDE_EFFECT_OUTCOME: frozenset(
                    {HarnessContractKind.SIDE_EFFECT}
                ),
                HarnessEvidenceKind.MERGE_RESULT: frozenset(
                    {HarnessContractKind.MERGE}
                ),
            }.get(self.kind)
            if (
                allowed_contract_kinds is not None
                and self.contract_ref.contract_kind not in allowed_contract_kinds
            ):
                raise HarnessValidationError(
                    "evidence contract kind does not match its evidence kind",
                    code="evidence_contract_kind_mismatch",
                    details={
                        "evidence_kind": self.kind.value,
                        "contract_kind": self.contract_ref.contract_kind.value,
                    },
                )
            object.__setattr__(
                self,
                "payload_ref",
                _checksum(self.payload_ref, "evidence.payload_ref"),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_ref": self.evidence_ref,
            "kind": self.kind.value,
            "node_instance_id": self.node_instance_id,
            "attempt": self.attempt,
            "event_sequence": self.event_sequence,
            "contract_ref": (
                None if self.contract_ref is None else self.contract_ref.to_dict()
            ),
            "payload_ref": self.payload_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessAttemptEvidenceReference":
        _exact_keys(
            value,
            {
                "evidence_ref",
                "kind",
                "node_instance_id",
                "attempt",
                "event_sequence",
                "contract_ref",
                "payload_ref",
            },
            "attempt evidence reference",
        )
        return cls(
            evidence_ref=value["evidence_ref"],
            kind=value["kind"],
            node_instance_id=value["node_instance_id"],
            attempt=value["attempt"],
            event_sequence=value["event_sequence"],
            contract_ref=(
                None
                if value["contract_ref"] is None
                else HarnessContractReference.from_dict(value["contract_ref"])
            ),
            payload_ref=value["payload_ref"],
        )


@dataclass(frozen=True, slots=True)
class HarnessPendingSideEffectState:
    scope: HarnessPendingSideEffectScope | str
    prepare_decision_ref: str
    prepare_sequence: int
    handler_ref: HarnessContractReference
    node_id: str
    node_instance_id: str
    attempt: int
    status: HarnessPendingSideEffectStatus | str = (
        HarnessPendingSideEffectStatus.PREPARED
    )
    authorization_ref: str | None = None
    outcome_ref: str | None = None
    failure_ref: str | None = None
    reason_code: str | None = None
    observation_sequence: int | None = None

    def __post_init__(self) -> None:
        scope = HarnessPendingSideEffectScope(self.scope)
        status = HarnessPendingSideEffectStatus(self.status)
        object.__setattr__(
            self,
            "prepare_decision_ref",
            _checksum(
                self.prepare_decision_ref,
                "pending_side_effect.prepare_decision_ref",
            ),
        )
        _positive_int(
            self.prepare_sequence,
            "pending_side_effect.prepare_sequence",
        )
        if not isinstance(self.handler_ref, HarnessContractReference):
            raise TypeError("pending side-effect handler_ref must be a contract reference")
        _require_contract_kind(
            self.handler_ref,
            HarnessContractKind.SIDE_EFFECT,
            "pending_side_effect.handler_ref",
        )
        for field_name in ("node_id", "node_instance_id"):
            object.__setattr__(
                self,
                field_name,
                required_text(
                    getattr(self, field_name),
                    f"pending_side_effect.{field_name}",
                ),
            )
        _positive_int(self.attempt, "pending_side_effect.attempt")
        authorization_ref = (
            None
            if self.authorization_ref is None
            else _checksum(
                self.authorization_ref,
                "pending_side_effect.authorization_ref",
            )
        )
        outcome_ref = (
            None
            if self.outcome_ref is None
            else _checksum(self.outcome_ref, "pending_side_effect.outcome_ref")
        )
        failure_ref = (
            None
            if self.failure_ref is None
            else _checksum(self.failure_ref, "pending_side_effect.failure_ref")
        )
        reason_code = optional_text(
            self.reason_code,
            "pending_side_effect.reason_code",
        )
        observation_sequence = self.observation_sequence
        if observation_sequence is not None:
            _positive_int(
                observation_sequence,
                "pending_side_effect.observation_sequence",
            )
            if observation_sequence <= self.prepare_sequence:
                raise HarnessValidationError(
                    "side-effect observation must follow its durable preparation",
                    code="pending_side_effect_sequence_mismatch",
                )
        if status is HarnessPendingSideEffectStatus.PREPARED:
            if any(
                value is not None
                for value in (
                    authorization_ref,
                    outcome_ref,
                    failure_ref,
                    reason_code,
                    observation_sequence,
                )
            ):
                raise HarnessValidationError(
                    "prepared side effect cannot contain outcome or failure evidence",
                    code="invalid_pending_side_effect_state",
                )
        elif status is HarnessPendingSideEffectStatus.OUTCOME_RECORDED:
            if (
                authorization_ref is None
                or outcome_ref is None
                or observation_sequence is None
                or failure_ref is not None
                or reason_code is not None
            ):
                raise HarnessValidationError(
                    "recorded side-effect outcome requires exact authorization and outcome evidence",
                    code="invalid_pending_side_effect_state",
                )
        elif (
            authorization_ref is None
            or failure_ref is None
            or reason_code is None
            or observation_sequence is None
            or outcome_ref is not None
        ):
            raise HarnessValidationError(
                "failed side effect requires exact authorization and failure evidence",
                code="invalid_pending_side_effect_state",
            )
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "authorization_ref", authorization_ref)
        object.__setattr__(self, "outcome_ref", outcome_ref)
        object.__setattr__(self, "failure_ref", failure_ref)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "observation_sequence", observation_sequence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "prepare_decision_ref": self.prepare_decision_ref,
            "prepare_sequence": self.prepare_sequence,
            "handler_ref": self.handler_ref.to_dict(),
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "attempt": self.attempt,
            "status": self.status.value,
            "authorization_ref": self.authorization_ref,
            "outcome_ref": self.outcome_ref,
            "failure_ref": self.failure_ref,
            "reason_code": self.reason_code,
            "observation_sequence": self.observation_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessPendingSideEffectState":
        _exact_keys(
            value,
            {
                "scope",
                "prepare_decision_ref",
                "prepare_sequence",
                "handler_ref",
                "node_id",
                "node_instance_id",
                "attempt",
                "status",
                "authorization_ref",
                "outcome_ref",
                "failure_ref",
                "reason_code",
                "observation_sequence",
            },
            "pending side effect",
        )
        return cls(
            scope=value["scope"],
            prepare_decision_ref=value["prepare_decision_ref"],
            prepare_sequence=value["prepare_sequence"],
            handler_ref=HarnessContractReference.from_dict(value["handler_ref"]),
            node_id=value["node_id"],
            node_instance_id=value["node_instance_id"],
            attempt=value["attempt"],
            status=value["status"],
            authorization_ref=value["authorization_ref"],
            outcome_ref=value["outcome_ref"],
            failure_ref=value["failure_ref"],
            reason_code=value["reason_code"],
            observation_sequence=value["observation_sequence"],
        )


@dataclass(frozen=True, slots=True)
class HarnessActiveActivityState:
    activity_id: str
    activity_ref: HarnessContractReference
    node_instance_id: str
    attempt: int
    idempotency_key: str
    fencing_generation: int
    dispatched_sequence: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "activity_id",
            required_text(self.activity_id, "activity.activity_id"),
        )
        if not isinstance(self.activity_ref, HarnessContractReference):
            raise TypeError("activity_ref must be HarnessContractReference")
        if self.activity_ref.contract_kind is not HarnessContractKind.ACTIVITY:
            raise HarnessValidationError(
                "active activity reference must use activity contract kind",
                code="graph_state_contract_kind_mismatch",
            )
        object.__setattr__(
            self,
            "node_instance_id",
            required_text(self.node_instance_id, "activity.node_instance_id"),
        )
        _positive_int(self.attempt, "activity.attempt")
        object.__setattr__(
            self,
            "idempotency_key",
            required_text(self.idempotency_key, "activity.idempotency_key"),
        )
        _nonnegative_int(self.fencing_generation, "activity.fencing_generation")
        _nonnegative_int(self.dispatched_sequence, "activity.dispatched_sequence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "activity_id": self.activity_id,
            "activity_ref": self.activity_ref.to_dict(),
            "node_instance_id": self.node_instance_id,
            "attempt": self.attempt,
            "idempotency_key": self.idempotency_key,
            "fencing_generation": self.fencing_generation,
            "dispatched_sequence": self.dispatched_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessActiveActivityState":
        _exact_keys(
            value,
            {
                "activity_id",
                "activity_ref",
                "node_instance_id",
                "attempt",
                "idempotency_key",
                "fencing_generation",
                "dispatched_sequence",
            },
            "active activity",
        )
        return cls(
            activity_id=value["activity_id"],
            activity_ref=HarnessContractReference.from_dict(value["activity_ref"]),
            node_instance_id=value["node_instance_id"],
            attempt=value["attempt"],
            idempotency_key=value["idempotency_key"],
            fencing_generation=value["fencing_generation"],
            dispatched_sequence=value["dispatched_sequence"],
        )


@dataclass(frozen=True, slots=True)
class HarnessNodeInstanceState:
    identity: HarnessNodeInstanceIdentity
    node_kind: HarnessGraphNodeKind | str
    status: HarnessNodeInstanceStatus | str
    step_id: str | None = None
    step_ref: HarnessContractReference | None = None
    step_status: HarnessStepStatus | str | None = None
    attempt: int = 0
    replans: int = 0
    output_refs: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[HarnessAttemptEvidenceReference, ...] = ()
    error_code: str | None = None
    terminal_reason: str | None = None
    activation_sequence: int = 0
    last_event_sequence: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.identity, HarnessNodeInstanceIdentity):
            raise TypeError("identity must be HarnessNodeInstanceIdentity")
        node_kind = HarnessGraphNodeKind(self.node_kind)
        status = HarnessNodeInstanceStatus(self.status)
        step_id = optional_text(self.step_id, "node_state.step_id")
        step_status = (
            None if self.step_status is None else HarnessStepStatus(self.step_status)
        )
        if node_kind is HarnessGraphNodeKind.EXECUTABLE:
            if step_id is None or self.step_ref is None or step_status is None:
                raise HarnessValidationError(
                    "executable node state requires exact step identity and phase status",
                    code="invalid_executable_node_state",
                )
            _require_contract_kind(
                self.step_ref,
                HarnessContractKind.STEP,
                "node_state.step_ref",
            )
            _validate_node_step_status(status, step_status)
        elif (
            step_id is not None or self.step_ref is not None or step_status is not None
        ):
            raise HarnessValidationError(
                "control node state cannot carry executable step phase",
                code="invalid_control_node_state",
            )
        _nonnegative_int(self.attempt, "node_state.attempt")
        _nonnegative_int(self.replans, "node_state.replans")
        if node_kind is not HarnessGraphNodeKind.EXECUTABLE and (
            self.attempt != 0 or self.replans != 0
        ):
            raise HarnessValidationError(
                "control node cannot carry executable attempt or replan progress",
                code="invalid_control_node_state",
            )
        if status in {
            HarnessNodeInstanceStatus.PENDING,
            HarnessNodeInstanceStatus.READY,
        }:
            if self.attempt != 0 or self.replans != 0:
                raise HarnessValidationError(
                    "pending or ready node cannot carry attempt or replan progress",
                    code="invalid_node_attempt_state",
                )
        output_refs = freeze_json(self.output_refs, "node_state.output_refs")
        if not isinstance(output_refs, Mapping):
            raise HarnessValidationError(
                "node output refs must be an object",
                code="invalid_graph_state_projection",
            )
        raw_evidence_refs = tuple(self.evidence_refs)
        if not all(
            isinstance(item, HarnessAttemptEvidenceReference)
            for item in raw_evidence_refs
        ):
            raise TypeError(
                "evidence_refs must contain HarnessAttemptEvidenceReference values"
            )
        evidence_refs = tuple(
            sorted(
                raw_evidence_refs,
                key=lambda item: (
                    item.event_sequence,
                    item.kind.value,
                    item.evidence_ref,
                ),
            )
        )
        evidence_identities = [
            (item.kind.value, item.evidence_ref, item.event_sequence)
            for item in evidence_refs
        ]
        if len(evidence_identities) != len(set(evidence_identities)):
            raise HarnessValidationError(
                "node evidence references must be unique",
                code="duplicate_node_evidence_reference",
            )
        for evidence in evidence_refs:
            if evidence.node_instance_id != self.identity.instance_id:
                raise HarnessValidationError(
                    "node evidence belongs to another node instance",
                    code="cross_node_evidence_rejected",
                )
            if evidence.attempt > self.attempt:
                raise HarnessValidationError(
                    "node evidence belongs to another attempt",
                    code="cross_attempt_evidence_rejected",
                )
            if node_kind is not HarnessGraphNodeKind.EXECUTABLE and evidence.kind in {
                HarnessEvidenceKind.ACTIVITY_RESULT,
                HarnessEvidenceKind.GATE_RESULT,
                HarnessEvidenceKind.SIDE_EFFECT_OUTCOME,
            }:
                raise HarnessValidationError(
                    "control node cannot carry executable activity, Gate, or side-effect evidence",
                    code="invalid_control_node_evidence",
                )
        if (
            status
            in {HarnessNodeInstanceStatus.PENDING, HarnessNodeInstanceStatus.READY}
            and evidence_refs
        ):
            raise HarnessValidationError(
                "pending or ready node cannot carry accepted attempt evidence",
                code="invalid_node_evidence_state",
            )
        _nonnegative_int(self.activation_sequence, "node_state.activation_sequence")
        _nonnegative_int(self.last_event_sequence, "node_state.last_event_sequence")
        if self.last_event_sequence < self.activation_sequence:
            raise HarnessValidationError(
                "node last event sequence cannot precede activation",
                code="graph_state_sequence_regression",
            )
        if (
            evidence_refs
            and max(item.event_sequence for item in evidence_refs)
            > self.last_event_sequence
        ):
            raise HarnessValidationError(
                "node evidence sequence exceeds the node projection sequence",
                code="graph_state_sequence_regression",
            )
        if (
            evidence_refs
            and min(item.event_sequence for item in evidence_refs)
            < self.activation_sequence
        ):
            raise HarnessValidationError(
                "node evidence sequence cannot precede node activation",
                code="graph_state_sequence_regression",
            )
        metadata = freeze_json(self.metadata, "node_state.metadata")
        if not isinstance(metadata, Mapping):
            raise HarnessValidationError(
                "node metadata must be an object",
                code="invalid_graph_state_projection",
            )
        raw_pending_side_effect = metadata.get("pending_side_effect")
        if raw_pending_side_effect is not None:
            if not isinstance(raw_pending_side_effect, Mapping):
                raise HarnessValidationError(
                    "pending side-effect node metadata must be an object",
                    code="invalid_pending_side_effect_state",
                )
            pending_side_effect = HarnessPendingSideEffectState.from_dict(
                raw_pending_side_effect
            )
            if (
                pending_side_effect.scope
                is not HarnessPendingSideEffectScope.NODE_INSTANCE
                or pending_side_effect.node_id != self.identity.node_id
                or pending_side_effect.node_instance_id != self.identity.instance_id
                or pending_side_effect.attempt != self.attempt
                or pending_side_effect.prepare_sequence > self.last_event_sequence
                or (
                    pending_side_effect.observation_sequence is not None
                    and pending_side_effect.observation_sequence
                    > self.last_event_sequence
                )
            ):
                raise HarnessValidationError(
                    "pending side effect does not match its node projection",
                    code="pending_side_effect_identity_mismatch",
                )
        object.__setattr__(self, "node_kind", node_kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "step_status", step_status)
        object.__setattr__(self, "output_refs", output_refs)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(
            self,
            "error_code",
            optional_text(self.error_code, "node_state.error_code"),
        )
        object.__setattr__(
            self,
            "terminal_reason",
            optional_text(self.terminal_reason, "node_state.terminal_reason"),
        )
        object.__setattr__(self, "metadata", metadata)

    @property
    def instance_id(self) -> str:
        return self.identity.instance_id

    @property
    def is_ready(self) -> bool:
        return self.status is HarnessNodeInstanceStatus.READY

    @property
    def is_running(self) -> bool:
        return self.status in _RUNNING_NODE_STATUSES

    @property
    def is_waiting(self) -> bool:
        return self.status is HarnessNodeInstanceStatus.WAITING

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_NODE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "node_kind": self.node_kind.value,
            "status": self.status.value,
            "step_id": self.step_id,
            "step_ref": None if self.step_ref is None else self.step_ref.to_dict(),
            "step_status": None if self.step_status is None else self.step_status.value,
            "attempt": self.attempt,
            "replans": self.replans,
            "output_refs": thaw_json(self.output_refs),
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "error_code": self.error_code,
            "terminal_reason": self.terminal_reason,
            "activation_sequence": self.activation_sequence,
            "last_event_sequence": self.last_event_sequence,
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessNodeInstanceState":
        _exact_keys(
            value,
            {
                "identity",
                "node_kind",
                "status",
                "step_id",
                "step_ref",
                "step_status",
                "attempt",
                "replans",
                "output_refs",
                "evidence_refs",
                "error_code",
                "terminal_reason",
                "activation_sequence",
                "last_event_sequence",
                "metadata",
            },
            "node instance state",
        )
        return cls(
            identity=HarnessNodeInstanceIdentity.from_dict(value["identity"]),
            node_kind=value["node_kind"],
            status=value["status"],
            step_id=value["step_id"],
            step_ref=(
                None
                if value["step_ref"] is None
                else HarnessContractReference.from_dict(value["step_ref"])
            ),
            step_status=value["step_status"],
            attempt=value["attempt"],
            replans=value["replans"],
            output_refs=value["output_refs"],
            evidence_refs=tuple(
                HarnessAttemptEvidenceReference.from_dict(item)
                for item in _array(value["evidence_refs"], "node_state.evidence_refs")
            ),
            error_code=value["error_code"],
            terminal_reason=value["terminal_reason"],
            activation_sequence=value["activation_sequence"],
            last_event_sequence=value["last_event_sequence"],
            metadata=value["metadata"],
        )


@dataclass(frozen=True, slots=True)
class HarnessJoinState:
    join_instance_id: str
    fork_instance_id: str
    join_kind: HarnessJoinKind | str
    status: HarnessJoinStatus | str
    required_branch_ids: tuple[str, ...]
    completed_branch_instances: Mapping[str, Any] = field(default_factory=dict)
    terminal_event_refs: Mapping[str, Any] = field(default_factory=dict)
    winner_branch_id: str | None = None
    last_event_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "join_instance_id",
            required_text(self.join_instance_id, "join.join_instance_id"),
        )
        object.__setattr__(
            self,
            "fork_instance_id",
            required_text(self.fork_instance_id, "join.fork_instance_id"),
        )
        join_kind = HarnessJoinKind(self.join_kind)
        status = HarnessJoinStatus(self.status)
        required = _stable_text_tuple(
            self.required_branch_ids,
            "join.required_branch_ids",
            allow_empty=False,
        )
        completed = _freeze_text_mapping(
            self.completed_branch_instances,
            "join.completed_branch_instances",
        )
        terminal_refs = _freeze_text_mapping(
            self.terminal_event_refs,
            "join.terminal_event_refs",
        )
        terminal_refs = freeze_json(
            {
                branch_id: _checksum(reference, "join.terminal_event_refs")
                for branch_id, reference in terminal_refs.items()
            },
            "join.terminal_event_refs",
        )
        completed_keys = set(completed)
        required_keys = set(required)
        if not completed_keys.issubset(required_keys):
            raise HarnessValidationError(
                "join completion contains an unknown branch",
                code="join_branch_identity_mismatch",
            )
        if set(terminal_refs) != completed_keys:
            raise HarnessValidationError(
                "join branch completion and terminal evidence must align",
                code="join_evidence_identity_mismatch",
            )
        winner = optional_text(self.winner_branch_id, "join.winner_branch_id")
        if join_kind is HarnessJoinKind.ALL and winner is not None:
            raise HarnessValidationError(
                "Parallel-All join cannot declare a winner",
                code="invalid_join_winner",
            )
        if winner is not None and winner not in completed_keys:
            raise HarnessValidationError(
                "join winner must reference a completed branch",
                code="invalid_join_winner",
            )
        if winner is not None and status is not HarnessJoinStatus.SATISFIED:
            raise HarnessValidationError(
                "join winner can be projected only after the join is satisfied",
                code="invalid_join_winner",
            )
        if status is HarnessJoinStatus.SATISFIED:
            if join_kind is HarnessJoinKind.ALL and completed_keys != required_keys:
                raise HarnessValidationError(
                    "satisfied Parallel-All join requires every branch",
                    code="incomplete_parallel_all_join",
                )
            if join_kind is HarnessJoinKind.ANY and winner is None:
                raise HarnessValidationError(
                    "satisfied Parallel-Any join requires a winner",
                    code="parallel_any_winner_missing",
                )
        _nonnegative_int(self.last_event_sequence, "join.last_event_sequence")
        object.__setattr__(self, "join_kind", join_kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "required_branch_ids", required)
        object.__setattr__(self, "completed_branch_instances", completed)
        object.__setattr__(self, "terminal_event_refs", terminal_refs)
        object.__setattr__(self, "winner_branch_id", winner)

    def to_dict(self) -> dict[str, Any]:
        return {
            "join_instance_id": self.join_instance_id,
            "fork_instance_id": self.fork_instance_id,
            "join_kind": self.join_kind.value,
            "status": self.status.value,
            "required_branch_ids": list(self.required_branch_ids),
            "completed_branch_instances": thaw_json(self.completed_branch_instances),
            "terminal_event_refs": thaw_json(self.terminal_event_refs),
            "winner_branch_id": self.winner_branch_id,
            "last_event_sequence": self.last_event_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessJoinState":
        _exact_keys(
            value,
            {
                "join_instance_id",
                "fork_instance_id",
                "join_kind",
                "status",
                "required_branch_ids",
                "completed_branch_instances",
                "terminal_event_refs",
                "winner_branch_id",
                "last_event_sequence",
            },
            "join state",
        )
        return cls(
            join_instance_id=value["join_instance_id"],
            fork_instance_id=value["fork_instance_id"],
            join_kind=value["join_kind"],
            status=value["status"],
            required_branch_ids=tuple(
                _array(value["required_branch_ids"], "join.required_branch_ids")
            ),
            completed_branch_instances=value["completed_branch_instances"],
            terminal_event_refs=value["terminal_event_refs"],
            winner_branch_id=value["winner_branch_id"],
            last_event_sequence=value["last_event_sequence"],
        )


@dataclass(frozen=True, slots=True)
class HarnessLoopCounterState:
    loop_id: str
    branch_path: tuple[str, ...]
    parent_iteration_vector: tuple[HarnessLoopIteration, ...]
    completed_iterations: int
    max_iterations: int
    status: HarnessLoopStatus | str = HarnessLoopStatus.PENDING
    last_event_sequence: int = 0
    counter_id: str = field(init=False)

    def __post_init__(self) -> None:
        loop_id = required_text(self.loop_id, "loop_counter.loop_id")
        branch_path = _ordered_text_tuple(
            self.branch_path,
            "loop_counter.branch_path",
        )
        vector = tuple(self.parent_iteration_vector)
        if not all(isinstance(item, HarnessLoopIteration) for item in vector):
            raise TypeError(
                "parent_iteration_vector must contain HarnessLoopIteration values"
            )
        _nonnegative_int(
            self.completed_iterations,
            "loop_counter.completed_iterations",
        )
        _positive_int(self.max_iterations, "loop_counter.max_iterations")
        if self.completed_iterations > self.max_iterations:
            raise HarnessValidationError(
                "loop iteration counter exceeds its pinned maximum",
                code="loop_iteration_bound_exceeded",
            )
        status = HarnessLoopStatus(self.status)
        if (
            status is HarnessLoopStatus.EXHAUSTED
            and self.completed_iterations != self.max_iterations
        ):
            raise HarnessValidationError(
                "exhausted loop must reach its maximum iteration count",
                code="invalid_loop_counter_state",
            )
        _nonnegative_int(self.last_event_sequence, "loop_counter.last_event_sequence")
        object.__setattr__(self, "loop_id", loop_id)
        object.__setattr__(self, "branch_path", branch_path)
        object.__setattr__(self, "parent_iteration_vector", vector)
        object.__setattr__(self, "status", status)
        projection = {
            "loop_id": loop_id,
            "branch_path": list(branch_path),
            "parent_iteration_vector": [item.to_dict() for item in vector],
        }
        digest = canonical_checksum(projection).removeprefix("sha256:")
        object.__setattr__(self, "counter_id", f"hlc_{digest}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "counter_id": self.counter_id,
            "loop_id": self.loop_id,
            "branch_path": list(self.branch_path),
            "parent_iteration_vector": [
                item.to_dict() for item in self.parent_iteration_vector
            ],
            "completed_iterations": self.completed_iterations,
            "max_iterations": self.max_iterations,
            "status": self.status.value,
            "last_event_sequence": self.last_event_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessLoopCounterState":
        _exact_keys(
            value,
            {
                "counter_id",
                "loop_id",
                "branch_path",
                "parent_iteration_vector",
                "completed_iterations",
                "max_iterations",
                "status",
                "last_event_sequence",
            },
            "loop counter state",
        )
        counter = cls(
            loop_id=value["loop_id"],
            branch_path=tuple(_array(value["branch_path"], "loop_counter.branch_path")),
            parent_iteration_vector=tuple(
                HarnessLoopIteration.from_dict(item)
                for item in _array(
                    value["parent_iteration_vector"],
                    "loop_counter.parent_iteration_vector",
                )
            ),
            completed_iterations=value["completed_iterations"],
            max_iterations=value["max_iterations"],
            status=value["status"],
            last_event_sequence=value["last_event_sequence"],
        )
        if value["counter_id"] != counter.counter_id:
            raise HarnessValidationError(
                "loop counter identity does not match its scope",
                code="loop_counter_identity_mismatch",
            )
        return counter


@dataclass(frozen=True, slots=True)
class HarnessWaitRegistration:
    wait_id: str
    node_instance_id: str
    kind: WaitKind | str
    correlation_ref: str
    tenant_scope_ref: str
    identity_scope_ref: str
    signal_schema_ref: str
    registered_sequence: int
    status: HarnessWaitStatus | str = HarnessWaitStatus.REGISTERED
    deadline_ref: str | None = None
    resolution_event_ref: str | None = None
    last_event_sequence: int = 0
    # Present only for re-entrant approval Waits: the exact grant the resumed
    # node redeems and the node it re-enters.  Omitted otherwise so historical
    # projections keep their canonical checksum.
    approval_id: str | None = None
    resume_node_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "wait_id", required_text(self.wait_id, "wait.wait_id"))
        object.__setattr__(
            self,
            "node_instance_id",
            required_text(self.node_instance_id, "wait.node_instance_id"),
        )
        object.__setattr__(self, "kind", WaitKind(self.kind))
        object.__setattr__(
            self,
            "correlation_ref",
            _checksum(self.correlation_ref, "wait.correlation_ref"),
        )
        object.__setattr__(
            self,
            "tenant_scope_ref",
            _checksum(self.tenant_scope_ref, "wait.tenant_scope_ref"),
        )
        object.__setattr__(
            self,
            "identity_scope_ref",
            _checksum(self.identity_scope_ref, "wait.identity_scope_ref"),
        )
        object.__setattr__(
            self,
            "signal_schema_ref",
            exact_reference(self.signal_schema_ref, "wait.signal_schema_ref"),
        )
        _nonnegative_int(self.registered_sequence, "wait.registered_sequence")
        status = HarnessWaitStatus(self.status)
        deadline_ref = optional_text(self.deadline_ref, "wait.deadline_ref")
        if deadline_ref is not None:
            deadline_ref = _checksum(deadline_ref, "wait.deadline_ref")
        if self.kind is WaitKind.TIMER and deadline_ref is None:
            raise HarnessValidationError(
                "timer Wait registration requires a persisted deadline reference",
                code="timer_deadline_missing",
            )
        resolution = optional_text(
            self.resolution_event_ref,
            "wait.resolution_event_ref",
        )
        if resolution is not None:
            resolution = _checksum(resolution, "wait.resolution_event_ref")
        if status in _UNRESOLVED_WAIT_STATUSES and resolution is not None:
            raise HarnessValidationError(
                "unresolved Wait cannot carry resolution evidence",
                code="invalid_wait_registration_state",
            )
        if status not in _UNRESOLVED_WAIT_STATUSES and resolution is None:
            raise HarnessValidationError(
                "resolved Wait requires durable resolution evidence",
                code="wait_resolution_evidence_missing",
            )
        _nonnegative_int(self.last_event_sequence, "wait.last_event_sequence")
        if self.last_event_sequence < self.registered_sequence:
            raise HarnessValidationError(
                "Wait last event sequence cannot precede registration",
                code="graph_state_sequence_regression",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "deadline_ref", deadline_ref)
        object.__setattr__(self, "resolution_event_ref", resolution)
        approval_id = optional_text(self.approval_id, "wait.approval_id")
        resume_node_id = optional_text(self.resume_node_id, "wait.resume_node_id")
        if (approval_id is None) != (resume_node_id is None) or (
            approval_id is not None and self.kind is not WaitKind.APPROVAL
        ):
            raise HarnessValidationError(
                "re-entrant Wait must bind an approval grant and its resume node",
                code="invalid_wait_registration_state",
            )
        object.__setattr__(self, "approval_id", approval_id)
        object.__setattr__(self, "resume_node_id", resume_node_id)

    @property
    def unresolved(self) -> bool:
        return self.status in _UNRESOLVED_WAIT_STATUSES

    @property
    def reentrant(self) -> bool:
        return self.resume_node_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id,
            "node_instance_id": self.node_instance_id,
            "kind": self.kind.value,
            "correlation_ref": self.correlation_ref,
            "tenant_scope_ref": self.tenant_scope_ref,
            "identity_scope_ref": self.identity_scope_ref,
            "signal_schema_ref": self.signal_schema_ref,
            "registered_sequence": self.registered_sequence,
            "status": self.status.value,
            "deadline_ref": self.deadline_ref,
            "resolution_event_ref": self.resolution_event_ref,
            "last_event_sequence": self.last_event_sequence,
            **(
                {}
                if self.resume_node_id is None
                else {
                    "approval_id": self.approval_id,
                    "resume_node_id": self.resume_node_id,
                }
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessWaitRegistration":
        fields = {
            "wait_id",
            "node_instance_id",
            "kind",
            "correlation_ref",
            "tenant_scope_ref",
            "identity_scope_ref",
            "signal_schema_ref",
            "registered_sequence",
            "status",
            "deadline_ref",
            "resolution_event_ref",
            "last_event_sequence",
        }
        if "resume_node_id" in value or "approval_id" in value:
            fields.update({"approval_id", "resume_node_id"})
        _exact_keys(value, fields, "Wait registration")
        return cls(
            wait_id=value["wait_id"],
            node_instance_id=value["node_instance_id"],
            kind=value["kind"],
            correlation_ref=value["correlation_ref"],
            tenant_scope_ref=value["tenant_scope_ref"],
            identity_scope_ref=value["identity_scope_ref"],
            signal_schema_ref=value["signal_schema_ref"],
            registered_sequence=value["registered_sequence"],
            status=value["status"],
            deadline_ref=value["deadline_ref"],
            resolution_event_ref=value["resolution_event_ref"],
            last_event_sequence=value["last_event_sequence"],
            approval_id=value.get("approval_id"),
            resume_node_id=value.get("resume_node_id"),
        )


@dataclass(frozen=True, slots=True)
class HarnessCompensationEntry:
    entry_id: str
    origin_node_instance_id: str
    effect_outcome_ref: str
    effect_commit_sequence: int
    handler_ref: HarnessContractReference
    activity_ref: HarnessContractReference
    idempotency_key: str
    fencing_generation: int
    status: HarnessCompensationStatus | str = HarnessCompensationStatus.PENDING
    compensation_node_instance_id: str | None = None
    outcome_ref: str | None = None
    last_event_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "entry_id", required_text(self.entry_id, "compensation.entry_id")
        )
        object.__setattr__(
            self,
            "origin_node_instance_id",
            required_text(
                self.origin_node_instance_id,
                "compensation.origin_node_instance_id",
            ),
        )
        object.__setattr__(
            self,
            "effect_outcome_ref",
            _checksum(self.effect_outcome_ref, "compensation.effect_outcome_ref"),
        )
        _nonnegative_int(
            self.effect_commit_sequence,
            "compensation.effect_commit_sequence",
        )
        _require_contract_kind(
            self.handler_ref,
            HarnessContractKind.COMPENSATION,
            "compensation.handler_ref",
        )
        _require_contract_kind(
            self.activity_ref,
            HarnessContractKind.ACTIVITY,
            "compensation.activity_ref",
        )
        object.__setattr__(
            self,
            "idempotency_key",
            required_text(
                self.idempotency_key,
                "compensation.idempotency_key",
            ),
        )
        _nonnegative_int(
            self.fencing_generation,
            "compensation.fencing_generation",
        )
        status = HarnessCompensationStatus(self.status)
        node_instance_id = optional_text(
            self.compensation_node_instance_id,
            "compensation.compensation_node_instance_id",
        )
        outcome_ref = optional_text(self.outcome_ref, "compensation.outcome_ref")
        if outcome_ref is not None:
            outcome_ref = _checksum(outcome_ref, "compensation.outcome_ref")
        if status is HarnessCompensationStatus.PENDING:
            if node_instance_id is not None or outcome_ref is not None:
                raise HarnessValidationError(
                    "pending compensation cannot carry execution outcome",
                    code="invalid_compensation_entry_state",
                )
        elif status is HarnessCompensationStatus.RUNNING:
            if node_instance_id is None or outcome_ref is not None:
                raise HarnessValidationError(
                    "running compensation requires its node instance only",
                    code="invalid_compensation_entry_state",
                )
        elif node_instance_id is None or outcome_ref is None:
            raise HarnessValidationError(
                "terminal compensation requires node instance and durable outcome",
                code="compensation_outcome_evidence_missing",
            )
        _nonnegative_int(
            self.last_event_sequence,
            "compensation.last_event_sequence",
        )
        if self.last_event_sequence < self.effect_commit_sequence:
            raise HarnessValidationError(
                "compensation state cannot precede its original effect",
                code="graph_state_sequence_regression",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "compensation_node_instance_id", node_instance_id)
        object.__setattr__(self, "outcome_ref", outcome_ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "origin_node_instance_id": self.origin_node_instance_id,
            "effect_outcome_ref": self.effect_outcome_ref,
            "effect_commit_sequence": self.effect_commit_sequence,
            "handler_ref": self.handler_ref.to_dict(),
            "activity_ref": self.activity_ref.to_dict(),
            "idempotency_key": self.idempotency_key,
            "fencing_generation": self.fencing_generation,
            "status": self.status.value,
            "compensation_node_instance_id": self.compensation_node_instance_id,
            "outcome_ref": self.outcome_ref,
            "last_event_sequence": self.last_event_sequence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessCompensationEntry":
        _exact_keys(
            value,
            {
                "entry_id",
                "origin_node_instance_id",
                "effect_outcome_ref",
                "effect_commit_sequence",
                "handler_ref",
                "activity_ref",
                "idempotency_key",
                "fencing_generation",
                "status",
                "compensation_node_instance_id",
                "outcome_ref",
                "last_event_sequence",
            },
            "compensation entry",
        )
        return cls(
            entry_id=value["entry_id"],
            origin_node_instance_id=value["origin_node_instance_id"],
            effect_outcome_ref=value["effect_outcome_ref"],
            effect_commit_sequence=value["effect_commit_sequence"],
            handler_ref=HarnessContractReference.from_dict(value["handler_ref"]),
            activity_ref=HarnessContractReference.from_dict(value["activity_ref"]),
            idempotency_key=value["idempotency_key"],
            fencing_generation=value["fencing_generation"],
            status=value["status"],
            compensation_node_instance_id=value["compensation_node_instance_id"],
            outcome_ref=value["outcome_ref"],
            last_event_sequence=value["last_event_sequence"],
        )


@dataclass(frozen=True, slots=True, order=True)
class HarnessBudgetCounterState:
    name: str
    limit: int
    used: int = 0
    reserved: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "name", required_text(self.name, "budget_counter.name")
        )
        _nonnegative_int(self.limit, "budget_counter.limit")
        _nonnegative_int(self.used, "budget_counter.used")
        _nonnegative_int(self.reserved, "budget_counter.reserved")
        if self.used + self.reserved > self.limit:
            raise HarnessValidationError(
                "budget usage and reservations exceed the pinned limit",
                code="graph_budget_exceeded",
                details={
                    "name": self.name,
                    "limit": self.limit,
                    "used": self.used,
                    "reserved": self.reserved,
                },
            )

    @property
    def remaining(self) -> int:
        return self.limit - self.used - self.reserved

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limit": self.limit,
            "used": self.used,
            "reserved": self.reserved,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessBudgetCounterState":
        _exact_keys(value, {"name", "limit", "used", "reserved"}, "budget counter")
        return cls(
            name=value["name"],
            limit=value["limit"],
            used=value["used"],
            reserved=value["reserved"],
        )


@dataclass(frozen=True, slots=True)
class HarnessGraphBudgetState:
    counters: tuple[HarnessBudgetCounterState, ...]

    def __post_init__(self) -> None:
        raw_counters = tuple(self.counters)
        if not all(
            isinstance(item, HarnessBudgetCounterState) for item in raw_counters
        ):
            raise TypeError("counters must contain HarnessBudgetCounterState values")
        counters = tuple(sorted(raw_counters, key=lambda item: item.name))
        names = [item.name for item in counters]
        if len(names) != len(set(names)):
            raise HarnessValidationError(
                "graph budget counters must have unique names",
                code="duplicate_graph_budget_counter",
            )
        object.__setattr__(self, "counters", counters)

    def get(self, name: str) -> HarnessBudgetCounterState | None:
        normalized = required_text(name, "budget_counter.name")
        return next((item for item in self.counters if item.name == normalized), None)

    def require(self, name: str) -> HarnessBudgetCounterState:
        counter = self.get(name)
        if counter is None:
            raise HarnessValidationError(
                "required graph budget counter is missing",
                code="graph_budget_counter_missing",
                details={"name": name},
            )
        return counter

    def to_dict(self) -> dict[str, Any]:
        return {"counters": [item.to_dict() for item in self.counters]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessGraphBudgetState":
        _exact_keys(value, {"counters"}, "graph budget state")
        return cls(
            counters=tuple(
                HarnessBudgetCounterState.from_dict(item)
                for item in _array(value["counters"], "graph_budget.counters")
            )
        )


@dataclass(frozen=True, slots=True)
class HarnessGraphState:
    run_id: str
    graph_ref: _HarnessGraphReference
    lifecycle: RunLifecycle | str
    outcome: RunOutcome | str = RunOutcome.NONE
    node_instances: tuple[HarnessNodeInstanceState, ...] = ()
    active_activities: tuple[HarnessActiveActivityState, ...] = ()
    join_states: tuple[HarnessJoinState, ...] = ()
    loop_counters: tuple[HarnessLoopCounterState, ...] = ()
    wait_registrations: tuple[HarnessWaitRegistration, ...] = ()
    signal_inbox: tuple[HarnessSignalInboxEntry, ...] = ()
    compensation_stack: tuple[HarnessCompensationEntry, ...] = ()
    budgets: HarnessGraphBudgetState = field(
        default_factory=lambda: HarnessGraphBudgetState(())
    )
    last_event_sequence: int = 0
    terminal_reason_code: str | None = None
    terminal_evidence_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA
    runtime_version: str = HARNESS_GRAPH_RUNTIME_VERSION
    projection_checksum: str | None = None

    def __post_init__(self) -> None:
        run_id = required_text(self.run_id, "graph_state.run_id")
        if not isinstance(self.graph_ref, _HarnessGraphReference):
            raise TypeError("graph_ref must be HarnessGraphReference")
        lifecycle = RunLifecycle(self.lifecycle)
        outcome = RunOutcome(self.outcome)
        _validate_lifecycle_outcome(lifecycle, outcome)
        raw_nodes = tuple(self.node_instances)
        _require_type_tuple(raw_nodes, HarnessNodeInstanceState, "node_instances")
        nodes = tuple(
            sorted(
                raw_nodes,
                key=lambda item: (
                    item.identity.activation_ordinal,
                    item.identity.instance_id,
                ),
            )
        )
        _require_unique(nodes, lambda item: item.instance_id, "node instance")
        ordinals = [item.identity.activation_ordinal for item in nodes]
        if len(ordinals) != len(set(ordinals)):
            raise HarnessValidationError(
                "node activation ordinals must be unique within one run",
                code="duplicate_node_activation_ordinal",
            )
        for node in nodes:
            if node.identity.run_id != run_id:
                raise HarnessValidationError(
                    "node instance belongs to another run",
                    code="cross_run_node_instance_rejected",
                )
            if node.identity.graph_checksum != self.graph_ref.checksum:
                raise HarnessValidationError(
                    "node instance belongs to another graph",
                    code="cross_graph_node_instance_rejected",
                )
        nodes_by_id = {item.instance_id: item for item in nodes}

        raw_activities = tuple(self.active_activities)
        _require_type_tuple(
            raw_activities,
            HarnessActiveActivityState,
            "active_activities",
        )
        activities = tuple(sorted(raw_activities, key=lambda item: item.activity_id))
        _require_unique(activities, lambda item: item.activity_id, "active activity")
        _require_unique(
            activities,
            lambda item: (item.node_instance_id, item.attempt),
            "active activity attempt",
            code="duplicate_active_activity_attempt",
        )
        for activity in activities:
            node = nodes_by_id.get(activity.node_instance_id)
            if node is None:
                raise HarnessValidationError(
                    "active activity belongs to an unknown node instance",
                    code="cross_node_activity_rejected",
                )
            if node.node_kind is not HarnessGraphNodeKind.EXECUTABLE:
                raise HarnessValidationError(
                    "active activity requires an executable node instance",
                    code="activity_node_kind_mismatch",
                )
            if activity.attempt != node.attempt:
                raise HarnessValidationError(
                    "active activity belongs to another attempt",
                    code="cross_attempt_activity_rejected",
                )
            retains_uncertain_activity = (
                node.status is HarnessNodeInstanceStatus.HALTED
                and lifecycle is RunLifecycle.HALTED
                and outcome is RunOutcome.INDETERMINATE
            )
            if not node.is_running and not retains_uncertain_activity:
                raise HarnessValidationError(
                    "active activity requires a running node instance",
                    code="activity_node_state_mismatch",
                )
            if not (
                node.activation_sequence
                <= activity.dispatched_sequence
                <= node.last_event_sequence
            ):
                raise HarnessValidationError(
                    "activity dispatch sequence must be within its node projection",
                    code="graph_state_sequence_regression",
                )
        active_node_ids = {item.node_instance_id for item in activities}
        for node in nodes:
            if (
                node.status is HarnessNodeInstanceStatus.CANCEL_REQUESTED
                and node.instance_id not in active_node_ids
            ):
                raise HarnessValidationError(
                    "cancel-requested node requires one active activity",
                    code="cancel_requested_activity_missing",
                    details={"node_instance_id": node.instance_id},
                )

        raw_joins = tuple(self.join_states)
        _require_type_tuple(raw_joins, HarnessJoinState, "join_states")
        joins = tuple(sorted(raw_joins, key=lambda item: item.join_instance_id))
        _require_unique(joins, lambda item: item.join_instance_id, "join state")
        raw_loops = tuple(self.loop_counters)
        _require_type_tuple(raw_loops, HarnessLoopCounterState, "loop_counters")
        loops = tuple(sorted(raw_loops, key=lambda item: item.counter_id))
        _require_unique(loops, lambda item: item.counter_id, "loop counter")
        raw_waits = tuple(self.wait_registrations)
        _require_type_tuple(raw_waits, HarnessWaitRegistration, "wait_registrations")
        waits = tuple(
            sorted(
                raw_waits,
                key=lambda item: (
                    item.registered_sequence,
                    item.node_instance_id,
                    item.wait_id,
                ),
            )
        )
        _require_unique(
            waits,
            lambda item: item.node_instance_id,
            "Wait node registration",
            code="duplicate_wait_node_registration",
        )
        waits_by_node = {item.node_instance_id: item for item in waits}
        for wait in waits:
            node = nodes_by_id.get(wait.node_instance_id)
            if node is None:
                raise HarnessValidationError(
                    "Wait registration belongs to an unknown node instance",
                    code="cross_node_wait_rejected",
                )
            if node.node_kind is not HarnessGraphNodeKind.WAIT:
                raise HarnessValidationError(
                    "Wait registration requires a Wait node instance",
                    code="wait_node_kind_mismatch",
                )
            if wait.unresolved and not node.is_waiting:
                raise HarnessValidationError(
                    "unresolved Wait requires a waiting node instance",
                    code="wait_node_state_mismatch",
                )
            if not (
                node.activation_sequence
                <= wait.registered_sequence
                <= wait.last_event_sequence
                <= node.last_event_sequence
            ):
                raise HarnessValidationError(
                    "Wait registration sequence must be within its node projection",
                    code="graph_state_sequence_regression",
                )
            _validate_wait_node_status(wait, node)
        for node in nodes:
            if (
                node.node_kind is HarnessGraphNodeKind.WAIT
                and node.status is HarnessNodeInstanceStatus.WAITING
                and node.instance_id not in waits_by_node
            ):
                raise HarnessValidationError(
                    "waiting Wait node requires a durable registration",
                    code="wait_registration_missing",
                )
        raw_signals = tuple(self.signal_inbox)
        _require_type_tuple(raw_signals, HarnessSignalInboxEntry, "signal_inbox")
        signals = tuple(
            sorted(
                raw_signals,
                key=lambda item: (
                    item.signal.received_sequence,
                    item.signal.signal_id,
                    item.signal.signal_ref,
                ),
            )
        )
        _require_unique(
            signals,
            lambda item: (
                item.signal.scope.tenant_scope_ref,
                item.signal.signal_id,
            ),
            "Wait signal identity",
            code="duplicate_wait_signal_identity",
        )
        for entry in signals:
            signal = entry.signal
            scope = signal.scope
            node = nodes_by_id.get(scope.node_instance_id)
            if scope.run_id != run_id:
                raise HarnessValidationError(
                    "Wait signal belongs to another run",
                    code="wait_signal_run_scope_mismatch",
                )
            if node is None or node.node_kind is not HarnessGraphNodeKind.WAIT:
                raise HarnessValidationError(
                    "Wait signal requires an existing Wait control node instance",
                    code="wait_signal_node_scope_mismatch",
                )
            if node.identity.node_id != scope.wait_id:
                raise HarnessValidationError(
                    "Wait signal definition does not match its node instance",
                    code="wait_signal_wait_mismatch",
                )
            if signal.received_sequence > self.last_event_sequence:
                raise HarnessValidationError(
                    "Wait signal sequence exceeds the state high watermark",
                    code="graph_state_sequence_regression",
                )
            if signal.received_sequence < node.activation_sequence:
                raise HarnessValidationError(
                    "Wait signal cannot precede its node activation",
                    code="wait_signal_sequence_regression",
                )
            if entry.status is HarnessSignalInboxEntryStatus.MATCHED:
                registration = waits_by_node.get(scope.node_instance_id)
                if (
                    registration is None
                    or registration.status is not HarnessWaitStatus.RESUMED
                    or registration.resolution_event_ref != signal.signal_ref
                    or entry.match is None
                    or entry.match.matched_sequence > self.last_event_sequence
                ):
                    raise HarnessValidationError(
                        "matched Wait signal lacks exact durable registration evidence",
                        code="wait_signal_match_registration_mismatch",
                    )
        for join in joins:
            join_node = nodes_by_id.get(join.join_instance_id)
            fork_node = nodes_by_id.get(join.fork_instance_id)
            expected_join_kind = (
                HarnessGraphNodeKind.JOIN_ALL
                if join.join_kind is HarnessJoinKind.ALL
                else HarnessGraphNodeKind.JOIN_ANY
            )
            expected_fork_kind = (
                HarnessGraphNodeKind.FORK_ALL
                if join.join_kind is HarnessJoinKind.ALL
                else HarnessGraphNodeKind.FORK_ANY
            )
            if join_node is None or fork_node is None:
                raise HarnessValidationError(
                    "join state requires known fork and join node instances",
                    code="cross_node_join_state_rejected",
                )
            if (
                join_node.node_kind is not expected_join_kind
                or fork_node.node_kind is not expected_fork_kind
            ):
                raise HarnessValidationError(
                    "join state fork/join node kinds do not match its policy",
                    code="join_node_kind_mismatch",
                )
            _validate_join_node_status(join, join_node)
            for branch_id, instance_id in join.completed_branch_instances.items():
                node = nodes_by_id.get(instance_id)
                if node is None:
                    raise HarnessValidationError(
                        "join evidence belongs to an unknown node instance",
                        code="cross_node_join_evidence_rejected",
                        details={"branch_id": branch_id},
                    )
                if not node.is_terminal:
                    raise HarnessValidationError(
                        "join completion requires a terminal node instance",
                        code="nonterminal_join_evidence_rejected",
                        details={"branch_id": branch_id},
                    )
                if branch_id not in node.identity.branch_path:
                    raise HarnessValidationError(
                        "join completion belongs to another branch scope",
                        code="join_branch_scope_mismatch",
                        details={"branch_id": branch_id},
                    )
                # A branch can receive later durable evidence (for example a
                # side-effect outcome) after the Join recorded its terminal
                # branch evidence.  ``node.last_event_sequence`` is therefore
                # not the branch-terminal sequence and cannot be compared to
                # the Join cursor.  The branch decision checksum in
                # ``terminal_event_refs`` remains the authoritative ordering
                # evidence during replay.
            if join.winner_branch_id is not None:
                winner_instance_id = join.completed_branch_instances[
                    join.winner_branch_id
                ]
                winner = nodes_by_id[winner_instance_id]
                if winner.status is not HarnessNodeInstanceStatus.SUCCEEDED:
                    raise HarnessValidationError(
                        "Parallel-Any winner must be a successfully verified node",
                        code="parallel_any_winner_state_mismatch",
                    )
        raw_compensations = tuple(self.compensation_stack)
        _require_type_tuple(
            raw_compensations,
            HarnessCompensationEntry,
            "compensation_stack",
        )
        compensations = tuple(
            sorted(
                raw_compensations,
                key=lambda item: (item.effect_commit_sequence, item.entry_id),
            )
        )
        _require_unique(compensations, lambda item: item.entry_id, "compensation entry")
        _require_unique(
            compensations,
            lambda item: item.effect_commit_sequence,
            "compensation effect sequence",
        )
        assigned_compensations = tuple(
            item
            for item in compensations
            if item.compensation_node_instance_id is not None
        )
        _require_unique(
            assigned_compensations,
            lambda item: item.compensation_node_instance_id,
            "compensation node assignment",
            code="duplicate_compensation_node_assignment",
        )
        for entry in compensations:
            origin = nodes_by_id.get(entry.origin_node_instance_id)
            if origin is None:
                raise HarnessValidationError(
                    "compensation entry belongs to an unknown origin node",
                    code="cross_node_compensation_rejected",
                )
            if origin.status is not HarnessNodeInstanceStatus.SUCCEEDED:
                raise HarnessValidationError(
                    "compensation entry requires a successfully verified origin node",
                    code="compensation_origin_state_mismatch",
                )
            if origin.node_kind is not HarnessGraphNodeKind.EXECUTABLE:
                raise HarnessValidationError(
                    "compensation entry origin must be an executable node",
                    code="compensation_origin_node_kind_mismatch",
                )
            matching_effects = tuple(
                evidence
                for evidence in origin.evidence_refs
                if evidence.kind is HarnessEvidenceKind.SIDE_EFFECT_OUTCOME
                and evidence.evidence_ref == entry.effect_outcome_ref
                and evidence.event_sequence == entry.effect_commit_sequence
            )
            if len(matching_effects) != 1:
                raise HarnessValidationError(
                    "compensation entry requires exact durable side-effect evidence",
                    code="compensation_effect_evidence_mismatch",
                )
            compensation_node = (
                None
                if entry.compensation_node_instance_id is None
                else nodes_by_id.get(entry.compensation_node_instance_id)
            )
            if (
                entry.compensation_node_instance_id is not None
                and compensation_node is None
            ):
                raise HarnessValidationError(
                    "compensation execution belongs to an unknown node",
                    code="cross_node_compensation_rejected",
                )
            if compensation_node is not None:
                if compensation_node.node_kind is not HarnessGraphNodeKind.EXECUTABLE:
                    raise HarnessValidationError(
                        "compensation execution requires an executable node",
                        code="compensation_node_kind_mismatch",
                    )
                _validate_compensation_node_status(entry, compensation_node)
                _validate_compensation_node_binding(entry, compensation_node)
        assigned_node_ids = {
            item.compensation_node_instance_id
            for item in assigned_compensations
        }
        orphan_compensation_nodes = tuple(
            item.instance_id
            for item in nodes
            if item.status is HarnessNodeInstanceStatus.COMPENSATING
            and item.instance_id not in assigned_node_ids
        )
        if orphan_compensation_nodes:
            raise HarnessValidationError(
                "compensating node requires one durable compensation entry",
                code="compensation_node_entry_missing",
                details={"node_instance_ids": list(orphan_compensation_nodes)},
            )
        if not isinstance(self.budgets, HarnessGraphBudgetState):
            raise TypeError("budgets must be HarnessGraphBudgetState")
        parallelism = self.budgets.get("max_parallelism")
        if parallelism is not None and len(activities) > parallelism.limit:
            raise HarnessValidationError(
                "active activities exceed physical parallelism",
                code="graph_parallelism_exceeded",
            )
        active_nodes = self.budgets.get("max_active_nodes")
        if active_nodes is not None:
            active_count = sum(item.status in _ACTIVE_NODE_STATUSES for item in nodes)
            if active_count > active_nodes.limit:
                raise HarnessValidationError(
                    "active node instances exceed the pinned limit",
                    code="graph_active_node_limit_exceeded",
                )
        _nonnegative_int(self.last_event_sequence, "graph_state.last_event_sequence")
        component_sequences = [
            *(item.last_event_sequence for item in nodes),
            *(item.dispatched_sequence for item in activities),
            *(item.last_event_sequence for item in joins),
            *(item.last_event_sequence for item in loops),
            *(item.last_event_sequence for item in waits),
            *(item.signal.received_sequence for item in signals),
            *(
                item.match.matched_sequence
                for item in signals
                if item.match is not None
            ),
            *(item.last_event_sequence for item in compensations),
        ]
        if component_sequences and max(component_sequences) > self.last_event_sequence:
            raise HarnessValidationError(
                "graph state sequence trails one of its components",
                code="graph_state_sequence_regression",
            )
        if lifecycle is RunLifecycle.CREATED:
            if (
                nodes
                or activities
                or joins
                or loops
                or waits
                or signals
                or compensations
                or self.last_event_sequence not in {0, 1}
            ):
                raise HarnessValidationError(
                    "created run cannot contain activated graph state",
                    code="invalid_created_run_projection",
                )
        if lifecycle is RunLifecycle.WAITING:
            passive_join_ids = {
                item.join_instance_id
                for item in joins
                if item.status is HarnessJoinStatus.OPEN
            }
            if any(
                item.is_ready
                or (item.is_running and item.instance_id not in passive_join_ids)
                for item in nodes
            ):
                raise HarnessValidationError(
                    "WAITING lifecycle cannot have ready or running work",
                    code="invalid_waiting_run_projection",
                )
            if not any(item.unresolved for item in waits):
                raise HarnessValidationError(
                    "WAITING lifecycle requires an unresolved Wait node",
                    code="invalid_waiting_run_projection",
                )
        if lifecycle is RunLifecycle.COMPLETED:
            if (
                activities
                or any(not item.is_terminal for item in nodes)
                or any(item.unresolved for item in waits)
                or any(
                    item.status
                    not in {HarnessJoinStatus.SATISFIED, HarnessJoinStatus.FAILED}
                    for item in joins
                )
                or any(
                    item.status
                    not in {HarnessLoopStatus.EXITED, HarnessLoopStatus.EXHAUSTED}
                    for item in loops
                )
                or any(
                    item.status is HarnessCompensationStatus.RUNNING
                    or (
                        outcome
                        in {
                            RunOutcome.COMPENSATED,
                            RunOutcome.COMPENSATION_FAILED,
                            RunOutcome.INDETERMINATE,
                        }
                        and item.status is HarnessCompensationStatus.PENDING
                    )
                    for item in compensations
                )
            ):
                raise HarnessValidationError(
                    "completed run cannot retain active or non-terminal node state",
                    code="invalid_completed_run_projection",
                )
        metadata = freeze_json(self.metadata, "graph_state.metadata")
        if not isinstance(metadata, Mapping):
            raise HarnessValidationError(
                "graph state metadata must be an object",
                code="invalid_graph_state_projection",
            )
        raw_terminal_side_effect = metadata.get("pending_terminal_side_effect")
        if raw_terminal_side_effect is not None:
            if not isinstance(raw_terminal_side_effect, Mapping):
                raise HarnessValidationError(
                    "pending terminal side-effect metadata must be an object",
                    code="invalid_pending_side_effect_state",
                )
            pending_terminal = HarnessPendingSideEffectState.from_dict(
                raw_terminal_side_effect
            )
            pending_node = nodes_by_id.get(pending_terminal.node_instance_id)
            if (
                pending_terminal.scope
                is not HarnessPendingSideEffectScope.TERMINAL_RUN
                or pending_node is None
                or pending_node.identity.node_id != pending_terminal.node_id
                or pending_node.attempt != pending_terminal.attempt
                or pending_terminal.prepare_sequence > self.last_event_sequence
                or (
                    pending_terminal.observation_sequence is not None
                    and pending_terminal.observation_sequence
                    > self.last_event_sequence
                )
            ):
                raise HarnessValidationError(
                    "pending terminal side effect does not match its graph projection",
                    code="pending_side_effect_identity_mismatch",
                )
        if self.graph_ref.schema_version != GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA:
            raise HarnessValidationError(
                "Graph state requires an exact Graph v2 reference",
                code="graph_state_graph_schema_mismatch",
                details={"graph_schema_version": self.graph_ref.schema_version},
            )
        if self.schema_version != GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA:
            raise HarnessValidationError(
                "Graph state requires the exact v2 state schema",
                code="unsupported_graph_state_schema",
                details={"schema_version": str(self.schema_version)},
            )
        if self.runtime_version != HARNESS_GRAPH_RUNTIME_VERSION:
            raise HarnessValidationError(
                "unsupported graph runtime version",
                code="unsupported_graph_runtime_version",
                details={"runtime_version": str(self.runtime_version)},
            )
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "lifecycle", lifecycle)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "node_instances", nodes)
        object.__setattr__(self, "active_activities", activities)
        object.__setattr__(self, "join_states", joins)
        object.__setattr__(self, "loop_counters", loops)
        object.__setattr__(self, "wait_registrations", waits)
        object.__setattr__(self, "signal_inbox", signals)
        object.__setattr__(self, "compensation_stack", compensations)
        object.__setattr__(
            self,
            "terminal_reason_code",
            optional_text(
                self.terminal_reason_code, "graph_state.terminal_reason_code"
            ),
        )
        terminal_evidence_ref = optional_text(
            self.terminal_evidence_ref,
            "graph_state.terminal_evidence_ref",
        )
        if terminal_evidence_ref is not None:
            terminal_evidence_ref = _checksum(
                terminal_evidence_ref,
                "graph_state.terminal_evidence_ref",
            )
        if outcome is RunOutcome.INDETERMINATE and terminal_evidence_ref is None:
            raise HarnessValidationError(
                "indeterminate run outcome requires durable terminal evidence",
                code="terminal_evidence_missing",
            )
        if lifecycle is RunLifecycle.HALTED and self.terminal_reason_code is None:
            raise HarnessValidationError(
                "halted run requires a typed terminal reason",
                code="terminal_reason_missing",
            )
        object.__setattr__(self, "terminal_evidence_ref", terminal_evidence_ref)
        object.__setattr__(self, "metadata", metadata)
        calculated = canonical_checksum(self.checksum_projection())
        if (
            self.projection_checksum is not None
            and self.projection_checksum != calculated
        ):
            raise HarnessValidationError(
                "graph state projection checksum does not match canonical content",
                code="graph_state_checksum_mismatch",
                details={
                    "expected": calculated,
                    "actual": str(self.projection_checksum),
                },
            )
        object.__setattr__(self, "projection_checksum", calculated)

    @classmethod
    def initial(
        cls,
        *,
        run_id: str,
        graph_ref: _HarnessGraphReference,
        budgets: HarnessGraphBudgetState,
        metadata: Mapping[str, Any] | None = None,
    ) -> "HarnessGraphState":
        return cls(
            run_id=run_id,
            graph_ref=graph_ref,
            lifecycle=RunLifecycle.CREATED,
            outcome=RunOutcome.NONE,
            budgets=budgets,
            last_event_sequence=0,
            metadata={} if metadata is None else metadata,
            schema_version=GRAPH_ONLY_HARNESS_GRAPH_STATE_SCHEMA,
        )

    @property
    def ready_node_ids(self) -> tuple[str, ...]:
        return tuple(item.instance_id for item in self.node_instances if item.is_ready)

    @property
    def running_node_ids(self) -> tuple[str, ...]:
        return tuple(
            item.instance_id for item in self.node_instances if item.is_running
        )

    @property
    def waiting_node_ids(self) -> tuple[str, ...]:
        return tuple(
            item.instance_id for item in self.node_instances if item.is_waiting
        )

    @property
    def terminal_node_ids(self) -> tuple[str, ...]:
        return tuple(
            item.instance_id for item in self.node_instances if item.is_terminal
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "run_id": self.run_id,
            "graph_ref": self.graph_ref.to_dict(),
            "lifecycle": self.lifecycle.value,
            "outcome": self.outcome.value,
            "node_instances": [item.to_dict() for item in self.node_instances],
            "active_activities": [item.to_dict() for item in self.active_activities],
            "join_states": [item.to_dict() for item in self.join_states],
            "loop_counters": [item.to_dict() for item in self.loop_counters],
            "wait_registrations": [item.to_dict() for item in self.wait_registrations],
            "signal_inbox": [item.to_dict() for item in self.signal_inbox],
            "compensation_stack": [item.to_dict() for item in self.compensation_stack],
            "budgets": self.budgets.to_dict(),
            "last_event_sequence": self.last_event_sequence,
            "terminal_reason_code": self.terminal_reason_code,
            "terminal_evidence_ref": self.terminal_evidence_ref,
            "metadata": thaw_json(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "projection_checksum": self.projection_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HarnessGraphState":
        _exact_keys(
            value,
            {
                "schema_version",
                "runtime_version",
                "run_id",
                "graph_ref",
                "lifecycle",
                "outcome",
                "node_instances",
                "active_activities",
                "join_states",
                "loop_counters",
                "wait_registrations",
                "signal_inbox",
                "compensation_stack",
                "budgets",
                "last_event_sequence",
                "terminal_reason_code",
                "terminal_evidence_ref",
                "metadata",
                "projection_checksum",
            },
            "Harness graph state",
        )
        return cls(
            schema_version=value["schema_version"],
            runtime_version=value["runtime_version"],
            run_id=value["run_id"],
            graph_ref=_HarnessGraphReference.from_dict(value["graph_ref"]),
            lifecycle=value["lifecycle"],
            outcome=value["outcome"],
            node_instances=tuple(
                HarnessNodeInstanceState.from_dict(item)
                for item in _array(
                    value["node_instances"], "graph_state.node_instances"
                )
            ),
            active_activities=tuple(
                HarnessActiveActivityState.from_dict(item)
                for item in _array(
                    value["active_activities"],
                    "graph_state.active_activities",
                )
            ),
            join_states=tuple(
                HarnessJoinState.from_dict(item)
                for item in _array(value["join_states"], "graph_state.join_states")
            ),
            loop_counters=tuple(
                HarnessLoopCounterState.from_dict(item)
                for item in _array(
                    value["loop_counters"],
                    "graph_state.loop_counters",
                )
            ),
            wait_registrations=tuple(
                HarnessWaitRegistration.from_dict(item)
                for item in _array(
                    value["wait_registrations"],
                    "graph_state.wait_registrations",
                )
            ),
            signal_inbox=tuple(
                HarnessSignalInboxEntry.from_dict(item)
                for item in _array(
                    value["signal_inbox"],
                    "graph_state.signal_inbox",
                )
            ),
            compensation_stack=tuple(
                HarnessCompensationEntry.from_dict(item)
                for item in _array(
                    value["compensation_stack"],
                    "graph_state.compensation_stack",
                )
            ),
            budgets=HarnessGraphBudgetState.from_dict(value["budgets"]),
            last_event_sequence=value["last_event_sequence"],
            terminal_reason_code=value["terminal_reason_code"],
            terminal_evidence_ref=value["terminal_evidence_ref"],
            metadata=value["metadata"],
            projection_checksum=value["projection_checksum"],
        )


def project_public_graph_status(
    lifecycle: RunLifecycle | str,
    outcome: RunOutcome | str,
    *,
    waiting_for_approval: bool = False,
) -> HarnessRunStatus:
    resolved_lifecycle = RunLifecycle(lifecycle)
    resolved_outcome = RunOutcome(outcome)
    _validate_lifecycle_outcome(resolved_lifecycle, resolved_outcome)
    if not isinstance(waiting_for_approval, bool):
        raise HarnessValidationError(
            "waiting_for_approval must be boolean",
            code="invalid_graph_status_projection",
        )
    if waiting_for_approval and resolved_lifecycle is not RunLifecycle.WAITING:
        raise HarnessValidationError(
            "waiting_for_approval is valid only for WAITING lifecycle",
            code="invalid_graph_status_projection",
        )
    if resolved_lifecycle is RunLifecycle.CREATED:
        return HarnessRunStatus.CREATED
    if resolved_lifecycle is RunLifecycle.RUNNING:
        return HarnessRunStatus.RUNNING
    if resolved_lifecycle is RunLifecycle.WAITING:
        return (
            HarnessRunStatus.WAITING_APPROVAL
            if waiting_for_approval
            else HarnessRunStatus.BLOCKED
        )
    if resolved_lifecycle is RunLifecycle.HALTED:
        return HarnessRunStatus.HALTED
    if resolved_outcome in {RunOutcome.SUCCEEDED, RunOutcome.COMPENSATED}:
        return HarnessRunStatus.SUCCEEDED
    if resolved_outcome is RunOutcome.CANCELLED:
        return HarnessRunStatus.CANCELLED
    return HarnessRunStatus.FAILED


def _validate_node_step_status(
    node_status: HarnessNodeInstanceStatus,
    step_status: HarnessStepStatus,
) -> None:
    allowed = _NODE_STEP_STATUS_COMPATIBILITY[node_status]
    if step_status not in allowed:
        raise HarnessValidationError(
            "node instance status is incompatible with its Step lifecycle status",
            code="node_step_status_mismatch",
            details={
                "node_status": node_status.value,
                "step_status": step_status.value,
                "allowed_step_statuses": sorted(item.value for item in allowed),
            },
        )


def _validate_wait_node_status(
    wait: HarnessWaitRegistration,
    node: HarnessNodeInstanceState,
) -> None:
    allowed_by_status = {
        HarnessWaitStatus.REGISTERED: frozenset({HarnessNodeInstanceStatus.WAITING}),
        HarnessWaitStatus.RESUMED: frozenset(
            {
                HarnessNodeInstanceStatus.WAITING,
                HarnessNodeInstanceStatus.SUCCEEDED,
            }
        ),
        HarnessWaitStatus.TIMED_OUT: frozenset(
            {
                HarnessNodeInstanceStatus.WAITING,
                HarnessNodeInstanceStatus.SUCCEEDED,
                HarnessNodeInstanceStatus.FAILED,
                HarnessNodeInstanceStatus.HALTED,
            }
        ),
        HarnessWaitStatus.CANCELLED: frozenset(
            {
                HarnessNodeInstanceStatus.WAITING,
                HarnessNodeInstanceStatus.CANCEL_REQUESTED,
                HarnessNodeInstanceStatus.CANCELLED,
                HarnessNodeInstanceStatus.HALTED,
            }
        ),
    }
    allowed = allowed_by_status[wait.status]
    if (
        node.node_kind is HarnessGraphNodeKind.EXECUTABLE
        and wait.kind is WaitKind.APPROVAL
        and wait.status is HarnessWaitStatus.RESUMED
    ):
        allowed = allowed.union(
            {
                HarnessNodeInstanceStatus.RUNNING,
                HarnessNodeInstanceStatus.COMPENSATING,
                HarnessNodeInstanceStatus.FAILED,
                HarnessNodeInstanceStatus.HALTED,
            }
        )
    if node.status not in allowed:
        raise HarnessValidationError(
            "Wait registration status is incompatible with its node state",
            code="wait_node_state_mismatch",
        )


def _validate_join_node_status(
    join: HarnessJoinState,
    node: HarnessNodeInstanceState,
) -> None:
    allowed = {
        HarnessJoinStatus.PENDING: frozenset(
            {
                HarnessNodeInstanceStatus.PENDING,
                HarnessNodeInstanceStatus.READY,
            }
        ),
        HarnessJoinStatus.OPEN: frozenset(
            {
                HarnessNodeInstanceStatus.RUNNING,
                HarnessNodeInstanceStatus.WAITING,
            }
        ),
        HarnessJoinStatus.SATISFIED: frozenset({HarnessNodeInstanceStatus.SUCCEEDED}),
        HarnessJoinStatus.FAILED: frozenset(
            {
                HarnessNodeInstanceStatus.FAILED,
                HarnessNodeInstanceStatus.HALTED,
            }
        ),
        HarnessJoinStatus.CANCEL_REQUESTED: frozenset(
            {
                HarnessNodeInstanceStatus.CANCEL_REQUESTED,
                HarnessNodeInstanceStatus.CANCELLED,
            }
        ),
    }[join.status]
    if node.status not in allowed:
        raise HarnessValidationError(
            "join state is incompatible with its join node state",
            code="join_node_state_mismatch",
        )


def _validate_compensation_node_status(
    entry: HarnessCompensationEntry,
    node: HarnessNodeInstanceState,
) -> None:
    allowed = {
        HarnessCompensationStatus.PENDING: frozenset(),
        HarnessCompensationStatus.RUNNING: frozenset(
            {HarnessNodeInstanceStatus.COMPENSATING}
        ),
        HarnessCompensationStatus.SUCCEEDED: frozenset(
            {HarnessNodeInstanceStatus.COMPENSATED}
        ),
        HarnessCompensationStatus.FAILED: frozenset({HarnessNodeInstanceStatus.FAILED}),
        HarnessCompensationStatus.INDETERMINATE: frozenset(
            {HarnessNodeInstanceStatus.HALTED}
        ),
    }[entry.status]
    if node.status not in allowed:
        raise HarnessValidationError(
            "compensation entry status is incompatible with its node state",
            code="compensation_node_state_mismatch",
        )


def _validate_compensation_node_binding(
    entry: HarnessCompensationEntry,
    node: HarnessNodeInstanceState,
) -> None:
    expected = {
        "compensation_entry_id": entry.entry_id,
        "origin_node_instance_id": entry.origin_node_instance_id,
        "effect_outcome_ref": entry.effect_outcome_ref,
        "compensation_handler_ref": entry.handler_ref.exact_ref,
        "compensation_activity_ref": entry.activity_ref.exact_ref,
        "compensation_idempotency_key": entry.idempotency_key,
        "compensation_fencing_generation": entry.fencing_generation,
    }
    mismatches = [
        key for key, value in expected.items() if node.metadata.get(key) != value
    ]
    if mismatches:
        raise HarnessValidationError(
            "compensation node binding does not match its durable entry",
            code="graph_compensation_binding_mismatch",
            details={"mismatches": mismatches},
        )
    if entry.status in {
        HarnessCompensationStatus.SUCCEEDED,
        HarnessCompensationStatus.FAILED,
        HarnessCompensationStatus.INDETERMINATE,
    }:
        allowed_outcomes = {
            evidence.evidence_ref for evidence in node.evidence_refs
        }
        last_decision = node.metadata.get("last_decision_checksum")
        if isinstance(last_decision, str):
            allowed_outcomes.add(last_decision)
        if entry.outcome_ref not in allowed_outcomes:
            raise HarnessValidationError(
                "terminal compensation outcome is not bound to node evidence",
                code="compensation_outcome_evidence_mismatch",
            )


def _validate_lifecycle_outcome(
    lifecycle: RunLifecycle,
    outcome: RunOutcome,
) -> None:
    if lifecycle in {RunLifecycle.CREATED, RunLifecycle.RUNNING, RunLifecycle.WAITING}:
        if outcome is not RunOutcome.NONE:
            raise HarnessValidationError(
                "non-terminal run lifecycle cannot carry a terminal outcome",
                code="invalid_run_lifecycle_outcome",
            )
        return
    if lifecycle is RunLifecycle.COMPLETED:
        if outcome in {RunOutcome.NONE, RunOutcome.INDETERMINATE}:
            raise HarnessValidationError(
                "completed run requires a definite terminal outcome",
                code="invalid_run_lifecycle_outcome",
            )
        return
    if outcome not in {
        RunOutcome.NONE,
        RunOutcome.INDETERMINATE,
        RunOutcome.COMPENSATION_FAILED,
    }:
        raise HarnessValidationError(
            "halted run outcome is incompatible with a safety stop",
            code="invalid_run_lifecycle_outcome",
        )


def _checksum(value: Any, field_name: str) -> str:
    text = required_text(value, field_name)
    if _CHECKSUM_PATTERN.fullmatch(text) is None:
        raise HarnessValidationError(
            f"{field_name} must be a canonical sha256 reference",
            code="invalid_graph_checksum_reference",
            details={"field": field_name},
        )
    return text


def _require_contract_kind(
    value: Any,
    expected: HarnessContractKind,
    field_name: str,
) -> None:
    if not isinstance(value, HarnessContractReference):
        raise TypeError(f"{field_name} must be HarnessContractReference")
    if value.contract_kind is not expected:
        raise HarnessValidationError(
            f"{field_name} uses the wrong contract kind",
            code="graph_state_contract_kind_mismatch",
            details={
                "field": field_name,
                "expected": expected.value,
                "actual": value.contract_kind.value,
            },
        )


def _nonnegative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HarnessValidationError(
            f"{field_name} must be a non-negative integer",
            code="invalid_graph_state_counter",
            details={"field": field_name},
        )
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessValidationError(
            f"{field_name} must be a positive integer",
            code="invalid_graph_state_counter",
            details={"field": field_name},
        )
    return value


def _ordered_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    return tuple(required_text(item, field_name) for item in values)


def _stable_text_tuple(
    values: Sequence[Any],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    normalized = _ordered_text_tuple(values, field_name)
    if len(normalized) != len(set(normalized)):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="duplicate_graph_state_identity",
        )
    if not normalized and not allow_empty:
        raise HarnessValidationError(
            f"{field_name} must not be empty",
            code="graph_state_required_field",
        )
    return tuple(sorted(normalized))


def _freeze_text_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    frozen = freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_graph_state_projection",
        )
    if any(not isinstance(key, str) or not key.strip() for key in frozen):
        raise HarnessValidationError(
            f"{field_name} keys must be non-blank strings",
            code="invalid_graph_state_projection",
        )
    if any(not isinstance(item, str) or not item.strip() for item in frozen.values()):
        raise HarnessValidationError(
            f"{field_name} values must be non-blank references",
            code="invalid_graph_state_projection",
        )
    return frozen


T = TypeVar("T")


def _require_type_tuple(
    values: tuple[Any, ...], expected: type[T], field_name: str
) -> None:
    if not all(isinstance(item, expected) for item in values):
        raise TypeError(f"{field_name} must contain {expected.__name__} values")


def _require_unique(
    values: tuple[T, ...],
    key,
    field_name: str,
    *,
    code: str = "duplicate_graph_state_identity",
) -> None:
    identities = [key(item) for item in values]
    if len(identities) != len(set(identities)):
        raise HarnessValidationError(
            f"{field_name} identities must be unique",
            code=code,
            details={"field": field_name},
        )


def _array(value: Any, field_name: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, list | tuple):
        raise HarnessValidationError(
            f"{field_name} must be an array",
            code="invalid_graph_state_projection",
        )
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], field_name: str) -> None:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_graph_state_projection",
        )
    actual = set(value)
    if actual != expected:
        raise HarnessValidationError(
            f"{field_name} fields do not match its schema",
            code="invalid_graph_state_projection",
            details={
                "missing": sorted(expected.difference(actual)),
                "unknown": sorted(str(item) for item in actual.difference(expected)),
            },
        )


__all__ = [
    "HarnessActiveActivityState",
    "HarnessAttemptEvidenceReference",
    "HarnessBudgetCounterState",
    "HarnessBranchOutputReference",
    "HarnessCompensationEntry",
    "HarnessCompensationStatus",
    "HarnessEvidenceKind",
    "HarnessGraphBudgetState",
    "HarnessGraphState",
    "HarnessJoinKind",
    "HarnessJoinState",
    "HarnessJoinStatus",
    "HarnessLoopCounterState",
    "HarnessLoopIteration",
    "HarnessLoopStatus",
    "HarnessNodeInstanceIdentity",
    "HarnessNodeInstanceState",
    "HarnessNodeInstanceStatus",
    "HarnessPendingSideEffectScope",
    "HarnessPendingSideEffectState",
    "HarnessPendingSideEffectStatus",
    "HarnessWaitRegistration",
    "HarnessWaitStatus",
    "RunLifecycle",
    "RunOutcome",
    "project_public_graph_status",
]
