from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import TYPE_CHECKING, Any, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord
    from framework.harness.task_plan.capacity import CapacityScopeSnapshot, PoolReservation

from framework.events.schema.catalog import TASK_PLAN_EVENT_TYPES
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    exact_reference,
    frozen_mapping,
    identifier,
    non_negative_int,
    optional_text,
    positive_int,
    reference,
    required_text,
    stable_text_tuple,
    thaw_mapping,
)
from framework.harness.task_plan.models import (
    PlanCandidate,
    PlanPatch,
    TaskAdmissionOwner,
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskProjection,
    TaskResultReference,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.identity import TaskPlanStageIdentity
from framework.harness.task_plan.submission import (
    CandidateDedupIdentity,
    CandidateSubmission,
    CandidateSubmissionAdmission,
    require_submission_stage_available,
    validate_submission_event_append,
)
from framework.harness.task_plan.schema import (
    GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    TASK_PLAN_EVENT_SCHEMA_V2,
    TASK_PLAN_EVENT_SCHEMA_V3,
    TASK_PLAN_EVENT_SCHEMAS,
)


# Graph v3 is the sole live TaskPlan event contract.  V2 encoded allocated
# READY attempts and must not be silently reinterpreted as logical readiness.
TASK_PLAN_EVENT_SCHEMA = TASK_PLAN_EVENT_SCHEMA_V3
TASK_PLAN_RESULT_SCHEMA_V3 = "newsroom.harness-task-plan-result/v3"
LOGICAL_TASK_READINESS_SCHEMA = "newsroom.harness-task-readiness/v1"
TASK_QUEUE_ADMISSION_SCHEMA = "newsroom.harness-task-queue-admission/v1"


@dataclass(frozen=True, slots=True)
class LogicalTaskReadiness:
    """Versioned fact proving one unallocated logical READY transition."""

    task_id: str
    task_definition_checksum: str
    logical_ready_order: tuple[str, ...]
    schema_version: str = LOGICAL_TASK_READINESS_SCHEMA
    readiness_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        object.__setattr__(
            self,
            "task_definition_checksum",
            checksum(self.task_definition_checksum, "task_definition_checksum"),
        )
        values = self.logical_ready_order
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise HarnessValidationError(
                "logical ready order must be an ordered array",
                code="invalid_task_plan_collection",
            )
        ready_order = tuple(identifier(item, "logical_ready_order") for item in values)
        if (
            not ready_order
            or len(ready_order) != len(set(ready_order))
            or self.task_id not in ready_order
        ):
            raise HarnessValidationError(
                "logical readiness must include its task in the complete ordered READY set",
                code="task_plan_projection_ready_order_mismatch",
            )
        object.__setattr__(self, "logical_ready_order", ready_order)
        if self.schema_version != LOGICAL_TASK_READINESS_SCHEMA:
            raise HarnessValidationError(
                "unsupported logical readiness schema",
                code="unsupported_task_plan_event_schema",
            )
        object.__setattr__(
            self,
            "readiness_checksum",
            canonical_payload_checksum(self.to_dict(include_checksum=False)),
        )

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_definition_checksum": self.task_definition_checksum,
            "logical_ready_order": list(self.logical_ready_order),
        }
        if include_checksum:
            result["readiness_checksum"] = self.readiness_checksum
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LogicalTaskReadiness":
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "task_id",
                    "task_definition_checksum",
                    "logical_ready_order",
                    "readiness_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("readiness_checksum"), "readiness_checksum")
        readiness = cls(**payload)
        if supplied != readiness.readiness_checksum:
            raise HarnessValidationError(
                "logical readiness checksum does not match content",
                code="task_plan_checksum_mismatch",
            )
        return readiness


@dataclass(frozen=True, slots=True)
class TaskQueueAdmissionEvidence:
    """Canonical QUEUE-owned attempt and budget admission evidence."""

    task_instance: TaskInstance | Mapping[str, Any]
    budget_before_checksum: str
    budget_after_checksum: str
    admission_owner: TaskAdmissionOwner | str = TaskAdmissionOwner.QUEUE
    schema_version: str = TASK_QUEUE_ADMISSION_SCHEMA
    admission_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        instance = self.task_instance
        if isinstance(instance, Mapping):
            instance = TaskInstance.from_dict(instance)
        if not isinstance(instance, TaskInstance):
            raise TypeError("task_instance must be TaskInstance")
        object.__setattr__(self, "task_instance", instance)
        try:
            owner = TaskAdmissionOwner(self.admission_owner)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "queue admission owner is invalid",
                code="task_plan_queue_admission_invalid",
            ) from exc
        if owner is not TaskAdmissionOwner.QUEUE:
            raise HarnessValidationError(
                "queue admission must use QUEUE ownership",
                code="task_plan_queue_admission_invalid",
            )
        object.__setattr__(self, "admission_owner", owner)
        object.__setattr__(
            self,
            "budget_before_checksum",
            checksum(self.budget_before_checksum, "budget_before_checksum"),
        )
        object.__setattr__(
            self,
            "budget_after_checksum",
            checksum(self.budget_after_checksum, "budget_after_checksum"),
        )
        if self.schema_version != TASK_QUEUE_ADMISSION_SCHEMA:
            raise HarnessValidationError(
                "unsupported queue admission schema",
                code="task_plan_queue_admission_invalid",
            )
        object.__setattr__(
            self,
            "admission_checksum",
            canonical_payload_checksum(self.to_dict(include_checksum=False)),
        )

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "admission_owner": self.admission_owner.value,
            "task_instance": self.task_instance.to_dict(),
            "budget_before_checksum": self.budget_before_checksum,
            "budget_after_checksum": self.budget_after_checksum,
        }
        if include_checksum:
            result["admission_checksum"] = self.admission_checksum
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskQueueAdmissionEvidence":
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "admission_owner",
                    "task_instance",
                    "budget_before_checksum",
                    "budget_after_checksum",
                    "admission_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("admission_checksum"), "admission_checksum")
        evidence = cls(**payload)
        if supplied != evidence.admission_checksum:
            raise HarnessValidationError(
                "queue admission checksum does not match content",
                code="task_plan_queue_admission_invalid",
            )
        return evidence


@dataclass(frozen=True, slots=True)
class TaskResultRecord:
    """Durable task result envelope; payload itself remains outside the plan."""

    run_id: str
    stage_id: str
    plan_id: str
    plan_version: int
    task_id: str
    task_instance_id: str
    attempt: int
    worker_ref: str
    task_checksum: str
    binding_checksum: str
    status: TaskLifecycle | str
    graph_checksum: str | None = None
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    result_ref: str | None = None
    output_refs: tuple[str, ...] = ()
    output_roles: tuple[str, ...] = ()
    output_schema_ref: str = "schema://task-result@1"
    usage: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    verified_gate_refs: tuple[str, ...] = ()
    gate_evidence_refs: tuple[str, ...] = ()
    transcript_ref: str | None = None
    transcript_checksum: str | None = None
    subagent_output_ref: str | None = None
    subagent_output_checksum: str | None = None
    schema_version: str = TASK_PLAN_RESULT_SCHEMA_V3
    result_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TASK_PLAN_RESULT_SCHEMA_V3:
            raise HarnessValidationError(
                "TaskResultRecord schema is unsupported",
                code="task_plan_result_schema_unsupported",
            )
        for name in ("run_id", "stage_id", "plan_id", "task_id", "task_instance_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        _normalize_task_plan_contract_identity(
            self,
            graph_only_schema=TASK_PLAN_RESULT_SCHEMA_V3,
            graph_only_identity_fields=_GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS,
        )
        object.__setattr__(self, "plan_version", positive_int(self.plan_version, "plan_version"))
        object.__setattr__(self, "attempt", positive_int(self.attempt, "attempt"))
        object.__setattr__(self, "worker_ref", exact_reference(self.worker_ref, "worker_ref"))
        object.__setattr__(self, "task_checksum", checksum(self.task_checksum, "task_checksum"))
        object.__setattr__(self, "binding_checksum", checksum(self.binding_checksum, "binding_checksum"))
        try:
            status = TaskLifecycle(self.status)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "task result status must be succeeded or failed",
                code="task_plan_result_invalid",
            ) from exc
        if status not in {TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED}:
            raise HarnessValidationError(
                "task result status must be succeeded or failed",
                code="task_plan_result_invalid",
            )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "result_ref", reference(self.result_ref, "result_ref") if self.result_ref else None)
        object.__setattr__(self, "output_refs", stable_text_tuple(self.output_refs, "output_refs", item_kind="reference"))
        object.__setattr__(self, "output_roles", stable_text_tuple(self.output_roles, "output_roles"))
        object.__setattr__(self, "output_schema_ref", exact_reference(self.output_schema_ref, "output_schema_ref"))
        object.__setattr__(self, "usage", frozen_mapping(self.usage, "result.usage"))
        object.__setattr__(self, "error_code", optional_text(self.error_code, "error_code"))
        object.__setattr__(
            self,
            "verified_gate_refs",
            stable_text_tuple(
                self.verified_gate_refs,
                "verified_gate_refs",
                item_kind="exact_reference",
            ),
        )
        object.__setattr__(
            self,
            "gate_evidence_refs",
            stable_text_tuple(
                self.gate_evidence_refs,
                "gate_evidence_refs",
                item_kind="reference",
            ),
        )
        evidence_values = (
            self.transcript_ref,
            self.transcript_checksum,
            self.subagent_output_ref,
            self.subagent_output_checksum,
        )
        if any(item is not None for item in evidence_values) and not all(
            item is not None for item in evidence_values
        ):
            raise HarnessValidationError(
                "TaskPlan subagent evidence fields must be complete",
                code="task_plan_result_invalid",
            )
        object.__setattr__(
            self,
            "transcript_ref",
            reference(self.transcript_ref, "transcript_ref")
            if self.transcript_ref
            else None,
        )
        object.__setattr__(
            self,
            "transcript_checksum",
            checksum(self.transcript_checksum, "transcript_checksum")
            if self.transcript_checksum
            else None,
        )
        object.__setattr__(
            self,
            "subagent_output_ref",
            reference(self.subagent_output_ref, "subagent_output_ref")
            if self.subagent_output_ref
            else None,
        )
        object.__setattr__(
            self,
            "subagent_output_checksum",
            checksum(self.subagent_output_checksum, "subagent_output_checksum")
            if self.subagent_output_checksum
            else None,
        )
        if self.gate_evidence_refs and len(self.gate_evidence_refs) != len(
            self.verified_gate_refs
        ):
            raise HarnessValidationError(
                "TaskPlan gate evidence must correspond one-to-one with verified gates",
                code="task_plan_result_invalid",
            )
        if self.status is TaskLifecycle.SUCCEEDED and not self.result_ref:
            raise HarnessValidationError("successful task result requires result_ref", code="task_plan_result_invalid")
        if self.status is TaskLifecycle.SUCCEEDED:
            if not self.output_roles:
                raise HarnessValidationError(
                    "successful task result requires output_roles",
                    code="task_plan_result_invalid",
                )
            if self.error_code is not None:
                raise HarnessValidationError(
                    "successful task result must not carry error_code",
                    code="task_plan_result_invalid",
                )
        elif self.error_code is None:
            raise HarnessValidationError(
                "failed task result requires error_code",
                code="task_plan_result_invalid",
            )
        elif self.result_ref is not None or self.output_roles or self.output_refs:
            raise HarnessValidationError(
                "failed task result must not carry accepted output references",
                code="task_plan_result_invalid",
            )
        if (
            self.status is TaskLifecycle.SUCCEEDED
            and self.subagent_output_ref is not None
            and self.result_ref != self.subagent_output_ref
        ):
            raise HarnessValidationError(
                "successful subagent result_ref must use its durable output ref",
                code="task_plan_result_invalid",
            )
        object.__setattr__(self, "result_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            **_task_plan_identity_projection(self),
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "task_id": self.task_id,
            "task_instance_id": self.task_instance_id,
            "attempt": self.attempt,
            "worker_ref": self.worker_ref,
            "task_checksum": self.task_checksum,
            "binding_checksum": self.binding_checksum,
            "status": self.status.value,
            "result_ref": self.result_ref,
            "output_refs": list(self.output_refs),
            "output_roles": list(self.output_roles),
            "output_schema_ref": self.output_schema_ref,
            "usage": thaw_mapping(self.usage),
            "error_code": self.error_code,
            "verified_gate_refs": list(self.verified_gate_refs),
            "gate_evidence_refs": list(self.gate_evidence_refs),
        }
        return {
            "schema_version": self.schema_version,
            **payload,
            "transcript_ref": self.transcript_ref,
            "transcript_checksum": self.transcript_checksum,
            "subagent_output_ref": self.subagent_output_ref,
            "subagent_output_checksum": self.subagent_output_checksum,
        }

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload = self.checksum_projection()
        if include_checksum:
            payload["result_checksum"] = self.result_checksum
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskResultRecord":
        common = frozenset(
            {
                "run_id", "stage_id", "plan_id", "plan_version",
                "task_id", "task_instance_id", "attempt", "worker_ref",
                "task_checksum", "binding_checksum", "status", "result_ref",
                "output_refs", "output_roles", "output_schema_ref", "usage",
                "error_code", "verified_gate_refs", "gate_evidence_refs",
                "result_checksum",
            }
        )
        if value.get("schema_version") != TASK_PLAN_RESULT_SCHEMA_V3:
            raise HarnessValidationError(
                "TaskResultRecord schema is unsupported",
                code="task_plan_result_schema_unsupported",
            )
        payload = exact_keys(
            value,
            required=common
            | {
                "schema_version", "transcript_ref", "transcript_checksum",
                "subagent_output_ref", "subagent_output_checksum",
            }
            | _GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("result_checksum"), "result_checksum")
        result = cls(**payload)
        if supplied != result.result_checksum:
            raise HarnessValidationError("TaskResultRecord checksum does not match canonical content", code="task_plan_checksum_mismatch")
        return result

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == TASK_PLAN_RESULT_SCHEMA_V3

    def matches_plan_identity(self, plan: ValidatedTaskPlan) -> bool:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        return (
            self.plan_id == plan.plan_id
            and self.plan_version == plan.version
            and (self.run_id, self.stage_id) == (plan.run_id, plan.stage_id)
            and all(
                getattr(self, name) == getattr(plan, name)
                for name in _GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS
            )
        )

    @classmethod
    def for_plan(
        cls,
        plan: ValidatedTaskPlan,
        *,
        task_id: str,
        task_instance_id: str,
        attempt: int,
        status: TaskLifecycle | str,
        result_ref: str | None = None,
        output_refs: tuple[str, ...] = (),
        output_roles: tuple[str, ...] = (),
        output_schema_ref: str = "schema://task-result@1",
        usage: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        verified_gate_refs: tuple[str, ...] = (),
        gate_evidence_refs: tuple[str, ...] = (),
        transcript_ref: str | None = None,
        transcript_checksum: str | None = None,
        subagent_output_ref: str | None = None,
        subagent_output_checksum: str | None = None,
    ) -> "TaskResultRecord":
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        normalized_task_id = identifier(task_id, "task_id")
        definition = next(
            (item for item in plan.tasks if item.task_id == normalized_task_id),
            None,
        )
        if definition is None:
            raise HarnessValidationError(
                "TaskResultRecord task is outside the accepted plan",
                code="task_plan_unknown_task",
            )
        return cls(
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            task_id=normalized_task_id,
            task_instance_id=task_instance_id,
            attempt=attempt,
            worker_ref=definition.worker_ref,
            task_checksum=definition.task_definition_checksum,
            binding_checksum=definition.binding_checksum,
            status=status,
            result_ref=result_ref,
            output_refs=output_refs,
            output_roles=output_roles,
            output_schema_ref=output_schema_ref,
            usage=usage or {},
            error_code=error_code,
            verified_gate_refs=verified_gate_refs,
            gate_evidence_refs=gate_evidence_refs,
            transcript_ref=transcript_ref,
            transcript_checksum=transcript_checksum,
            subagent_output_ref=subagent_output_ref,
            subagent_output_checksum=subagent_output_checksum,
            schema_version=TASK_PLAN_RESULT_SCHEMA_V3,
            **_task_plan_graph_identity_kwargs(plan),
        )


_GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS = frozenset(
    {
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_schema_version",
        "compiler_version",
        "condition_policy_version",
        "stage_binding_checksum",
        "stage_identity_schema",
        "stage_identity_checksum",
    }
)
_GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS = (
    _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS | {"graph_checksum"}
)


def _normalize_task_plan_contract_identity(
    model: Any,
    *,
    graph_only_schema: str,
    graph_only_identity_fields: frozenset[str],
) -> None:
    if model.schema_version != graph_only_schema:
        raise HarnessValidationError(
            "TaskPlan contract schema does not select a supported identity",
            code="task_plan_identity_schema_mismatch",
        )
    normalized = {
        "graph_checksum": checksum(model.graph_checksum, "graph_checksum"),
        "graph_id": identifier(model.graph_id, "graph_id"),
        "graph_version": identifier(model.graph_version, "graph_version"),
        "graph_ref": exact_reference(model.graph_ref, "graph_ref"),
        "graph_schema_version": required_text(
            model.graph_schema_version,
            "graph_schema_version",
        ),
        "compiler_version": required_text(
            model.compiler_version,
            "compiler_version",
        ),
        "condition_policy_version": required_text(
            model.condition_policy_version,
            "condition_policy_version",
        ),
        "stage_binding_checksum": checksum(
            model.stage_binding_checksum,
            "stage_binding_checksum",
        ),
        "stage_identity_schema": required_text(
            model.stage_identity_schema,
            "stage_identity_schema",
        ),
        "stage_identity_checksum": checksum(
            model.stage_identity_checksum,
            "stage_identity_checksum",
        ),
    }
    expected = {
        "graph_schema_version": GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        "compiler_version": HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        "condition_policy_version": HARNESS_CONDITION_POLICY_VERSION,
        "stage_identity_schema": GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
        "graph_ref": f"{normalized['graph_id']}@{normalized['graph_version']}",
    }
    mismatched = {
        name: {"expected": expected_value, "actual": normalized[name]}
        for name, expected_value in expected.items()
        if normalized[name] != expected_value
    }
    if mismatched:
        raise HarnessValidationError(
            "Graph-only TaskPlan contract identity versions do not match",
            code="task_plan_graph_identity_mismatch",
            details={"mismatched": mismatched},
        )
    for name, normalized_value in normalized.items():
        object.__setattr__(model, name, normalized_value)
    identity_projection = {
        "schema_version": model.stage_identity_schema,
        "run_id": model.run_id,
        "graph_schema_version": model.graph_schema_version,
        "compiler_version": model.compiler_version,
        "condition_policy_version": model.condition_policy_version,
        "graph_id": model.graph_id,
        "graph_version": model.graph_version,
        "graph_checksum": model.graph_checksum,
        "stage_id": model.stage_id,
        "stage_binding_checksum": model.stage_binding_checksum,
        "graph_ref": model.graph_ref,
    }
    expected_identity_checksum = canonical_payload_checksum(identity_projection)
    if model.stage_identity_checksum != expected_identity_checksum:
        raise HarnessValidationError(
            "Graph-only TaskPlan contract stage identity checksum does not match",
            code="task_plan_stage_identity_checksum_invalid",
            details={
                "expected": expected_identity_checksum,
                "actual": model.stage_identity_checksum,
            },
        )


def _task_plan_identity_projection(model: Any) -> dict[str, Any]:
    return {
        name: getattr(model, name)
        for name in _GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS
    }


def _task_plan_graph_identity_kwargs(
    value: PlanCandidate | ValidatedTaskPlan,
) -> dict[str, Any]:
    return {
        name: getattr(value, name)
        for name in _GRAPH_ONLY_TASK_RESULT_IDENTITY_FIELDS
    }


@dataclass(frozen=True, slots=True)
class TaskPlanEvent:
    event_type: str
    run_id: str
    stage_id: str
    graph_checksum: str
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    task_id: str | None = None
    task_instance_id: str | None = None
    attempt: int | None = None
    schema_version: str = TASK_PLAN_EVENT_SCHEMA
    actor_type: str = "harness"
    causal_event_ref: str | None = None
    input_checksum: str | None = None
    output_refs: tuple[str, ...] = ()
    reason_code: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    event_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.event_type not in TASK_PLAN_EVENT_TYPES:
            raise HarnessValidationError("unknown TaskPlan event type", code="task_plan_unknown_event")
        if self.schema_version not in TASK_PLAN_EVENT_SCHEMAS:
            raise HarnessValidationError(
                "unsupported TaskPlan event schema",
                code="unsupported_task_plan_event_schema",
                details={"schema_version": str(self.schema_version)},
            )
        for name in ("run_id", "stage_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        object.__setattr__(self, "graph_checksum", checksum(self.graph_checksum, "graph_checksum"))
        _normalize_task_plan_contract_identity(
            self,
            graph_only_schema=TASK_PLAN_EVENT_SCHEMA_V3,
            graph_only_identity_fields=_GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS,
        )
        object.__setattr__(self, "plan_id", identifier(self.plan_id, "plan_id") if self.plan_id else None)
        if self.plan_version is not None:
            object.__setattr__(self, "plan_version", positive_int(self.plan_version, "plan_version"))
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id") if self.task_id else None)
        object.__setattr__(self, "task_instance_id", identifier(self.task_instance_id, "task_instance_id") if self.task_instance_id else None)
        if self.attempt is not None:
            object.__setattr__(self, "attempt", positive_int(self.attempt, "attempt"))
        object.__setattr__(self, "actor_type", required_text(self.actor_type, "actor_type"))
        object.__setattr__(self, "causal_event_ref", reference(self.causal_event_ref, "causal_event_ref") if self.causal_event_ref else None)
        object.__setattr__(self, "input_checksum", checksum(self.input_checksum, "input_checksum") if self.input_checksum else None)
        object.__setattr__(self, "output_refs", stable_text_tuple(self.output_refs, "output_refs", item_kind="reference"))
        object.__setattr__(self, "reason_code", optional_text(self.reason_code, "reason_code"))
        object.__setattr__(self, "payload", frozen_mapping(self.payload, "event.payload"))
        if self.event_type == "TASK_READY":
            readiness_payload = self.payload.get("logical_readiness")
            if not isinstance(readiness_payload, Mapping):
                raise HarnessValidationError(
                    "TASK_READY requires versioned logical readiness evidence",
                    code="task_plan_logical_readiness_missing",
                )
            readiness = LogicalTaskReadiness.from_dict(readiness_payload)
            if (
                self.task_instance_id is not None
                or self.attempt is not None
                or self.task_id != readiness.task_id
                or self.input_checksum != readiness.task_definition_checksum
            ):
                raise HarnessValidationError(
                    "TASK_READY must describe an unallocated logical task",
                    code="task_plan_logical_readiness_identity_mismatch",
                )
        elif self.event_type == "TASK_QUEUE_ADMITTED":
            admission_payload = self.payload.get("queue_admission")
            if not isinstance(admission_payload, Mapping):
                raise HarnessValidationError(
                    "TASK_QUEUE_ADMITTED requires versioned admission evidence",
                    code="task_plan_queue_admission_invalid",
                )
            admission = TaskQueueAdmissionEvidence.from_dict(admission_payload)
            instance = admission.task_instance
            if (
                self.task_id != instance.task_id
                or self.task_instance_id != instance.task_instance_id
                or self.attempt != instance.attempt
                or self.input_checksum != instance.task_definition_checksum
                or self.run_id != instance.run_id
                or self.stage_id != instance.stage_id
                or self.plan_id != instance.plan_id
                or self.plan_version != instance.plan_version
                or any(
                    getattr(self, name) != getattr(instance, name)
                    for name in _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
                    | {"graph_checksum"}
                )
            ):
                raise HarnessValidationError(
                    "TASK_QUEUE_ADMITTED differs from its allocated attempt",
                    code="task_plan_queue_admission_invalid",
                )
        sequence = non_negative_int(self.sequence, "sequence")
        if sequence == 0:
            raise HarnessValidationError(
                "TaskPlan event sequence must be positive",
                code="task_plan_invalid_event_sequence",
            )
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "event_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_type": self.event_type,
            "run_id": self.run_id,
        }
        payload.update(
            {
                "graph_id": self.graph_id,
                "graph_version": self.graph_version,
                "graph_ref": self.graph_ref,
                "graph_schema_version": self.graph_schema_version,
                "compiler_version": self.compiler_version,
                "condition_policy_version": self.condition_policy_version,
            }
        )
        payload.update(
            {
                "stage_id": self.stage_id,
                "graph_checksum": self.graph_checksum,
                "stage_binding_checksum": self.stage_binding_checksum,
                "stage_identity_schema": self.stage_identity_schema,
                "stage_identity_checksum": self.stage_identity_checksum,
                "plan_id": self.plan_id,
                "plan_version": self.plan_version,
                "task_id": self.task_id,
                "task_instance_id": self.task_instance_id,
                "attempt": self.attempt,
                "schema_version": self.schema_version,
                "actor_type": self.actor_type,
                "causal_event_ref": self.causal_event_ref,
                "input_checksum": self.input_checksum,
                "output_refs": list(self.output_refs),
                "reason_code": self.reason_code,
                "payload": thaw_mapping(self.payload),
                "sequence": self.sequence,
            }
        )
        if include_checksum:
            payload["event_checksum"] = self.event_checksum
        return payload

    @classmethod
    def for_plan(
        cls,
        event_type: str,
        plan: ValidatedTaskPlan,
        *,
        sequence: int,
        task_id: str | None = None,
        task_instance_id: str | None = None,
        attempt: int | None = None,
        actor_type: str = "harness",
        causal_event_ref: str | None = None,
        input_checksum: str | None = None,
        output_refs: tuple[str, ...] = (),
        reason_code: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> "TaskPlanEvent":
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        return cls(
            event_type,
            **_task_plan_event_identity_kwargs(plan),
            plan_id=plan.plan_id,
            plan_version=plan.version,
            task_id=task_id,
            task_instance_id=task_instance_id,
            attempt=attempt,
            actor_type=actor_type,
            causal_event_ref=causal_event_ref,
            input_checksum=input_checksum,
            output_refs=output_refs,
            reason_code=reason_code,
            payload=payload or {},
            sequence=sequence,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskPlanEvent":
        schema_version = value.get("schema_version")
        if schema_version not in TASK_PLAN_EVENT_SCHEMAS:
            raise HarnessValidationError(
                "unsupported TaskPlan event schema",
                code="unsupported_task_plan_event_schema",
                details={"schema_version": str(schema_version)},
            )
        common = frozenset(
            {
                "event_type",
                "run_id",
                "stage_id",
                "graph_checksum",
                "plan_id",
                "plan_version",
                "task_id",
                "task_instance_id",
                "attempt",
                "schema_version",
                "actor_type",
                "causal_event_ref",
                "input_checksum",
                "output_refs",
                "reason_code",
                "payload",
                "sequence",
                "event_checksum",
            }
        )
        identity = _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
        payload = exact_keys(
            value,
            required=common | identity,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("event_checksum"), "event_checksum")
        event = cls(**payload)
        if supplied != event.event_checksum:
            raise HarnessValidationError("TaskPlanEvent checksum does not match canonical content", code="task_plan_checksum_mismatch")
        return event

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == TASK_PLAN_EVENT_SCHEMA_V3

    def matches_contract_identity(
        self,
        value: PlanCandidate | ValidatedTaskPlan,
    ) -> bool:
        if not isinstance(value, (PlanCandidate, ValidatedTaskPlan)):
            raise TypeError("value must be PlanCandidate or ValidatedTaskPlan")
        if (
            self.run_id,
            self.stage_id,
            self.graph_checksum,
        ) != (
            value.run_id,
            value.stage_id,
            value.graph_checksum,
        ):
            return False
        return all(
            getattr(self, name) == getattr(value, name)
            for name in _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
        )


@runtime_checkable
class TaskPlanStorePort(Protocol):
    def append_candidate(self, candidate: PlanCandidate, *, event_type: str = "PLAN_CANDIDATE_BUILT") -> str: ...
    def admit_candidate_submission(
        self,
        candidate: PlanCandidate,
        identity: CandidateDedupIdentity,
        *,
        accepted_at: str,
        candidate_checksum: str | None = None,
    ) -> CandidateSubmission: ...
    def submit_candidate(
        self, candidate: PlanCandidate, identity: CandidateDedupIdentity, *,
        accepted_at: str, candidate_checksum: str | None = None,
        exclusive_stage: bool = False,
    ) -> CandidateSubmissionAdmission: ...
    def candidate_submission(self, identity: CandidateDedupIdentity) -> CandidateSubmission | None: ...
    def submissions_for(self, run_id: str, stage_id: str) -> tuple[CandidateSubmission, ...]: ...
    def candidate_for(self, run_id: str, stage_id: str, candidate_ref: str) -> PlanCandidate | None: ...
    def append_rejected_candidate(self, candidate: PlanCandidate, *, reason_code: str) -> str: ...
    def accept_plan(self, plan: ValidatedTaskPlan) -> str: ...
    def append_patch(self, patch: PlanPatch, *, accepted: bool = False) -> str: ...
    def accept_patched_plan(
        self,
        patch: PlanPatch,
        plan: ValidatedTaskPlan,
        *,
        skipped_task_ids: tuple[str, ...] = (),
    ) -> str: ...
    def append_result(self, result: TaskResultRecord) -> str: ...
    def load_projection(self, run_id: str, stage_id: str) -> TaskPlanProjection: ...
    def read_events(self, run_id: str, stage_id: str) -> tuple[TaskPlanEvent, ...]: ...
    def update_projection(self, projection: TaskPlanProjection) -> None: ...
    def results_for(self, run_id: str, stage_id: str, plan_id: str, plan_version: int) -> tuple[TaskResultRecord, ...]: ...
    def result_history_for(self, run_id: str, stage_id: str, plan_id: str, plan_version: int) -> tuple[TaskAttemptHistoryRecord, ...]: ...
    def append_event(self, event: TaskPlanEvent) -> str: ...
    def append_events(self, events: tuple[TaskPlanEvent, ...]) -> tuple[str, ...]: ...
    def commit_events(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
    ) -> tuple[str, ...]: ...
    def install_capacity_snapshot(self, snapshot: "CapacityScopeSnapshot") -> "CapacityScopeSnapshot": ...
    def load_capacity_snapshot(self, owner_scope: str) -> "CapacityScopeSnapshot": ...
    def commit_wave_admission(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]: ...
    def commit_wave_completion(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        settled_pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]: ...
    def commit_event(self, event: TaskPlanEvent, projection: TaskPlanProjection) -> str: ...
    def plan(self, run_id: str, stage_id: str, version: int | None = None) -> ValidatedTaskPlan | None: ...
    def patches_for(self, run_id: str, stage_id: str) -> tuple[PlanPatch, ...]: ...


class InMemoryTaskPlanStore:
    """Deterministic test store with immutable plan history and projections."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._candidates: dict[str, PlanCandidate] = {}
        self._submissions: dict[str, CandidateSubmission] = {}
        self._plans: dict[tuple[str, str, int], ValidatedTaskPlan] = {}
        self._patches: dict[str, PlanPatch] = {}
        self._results: dict[tuple[str, str, str, int, int], TaskResultRecord] = {}
        self._projections: dict[tuple[str, str], TaskPlanProjection] = {}
        self._events: dict[tuple[str, str], list[TaskPlanEvent]] = {}
        self._transition_projections: dict[tuple[str, str, str], TaskPlanProjection] = {}
        self._capacity_snapshots: dict[str, object] = {}
        self._capacity_admission_transitions: dict[
            str, tuple[object, object, tuple[object, ...]]
        ] = {}
        self._capacity_settlement_transitions: dict[
            str, tuple[object, object, tuple[object, ...]]
        ] = {}

    def append_candidate(self, candidate: PlanCandidate, *, event_type: str = "PLAN_CANDIDATE_BUILT") -> str:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        _require_live_graph_only(candidate, "candidate")
        with self._lock:
            existing = self._candidates.get(candidate.candidate_checksum)
            if existing is not None and existing != candidate:
                raise HarnessValidationError("candidate checksum identity conflict", code="task_plan_checksum_conflict")
            if existing is not None:
                return existing.candidate_checksum
            event = _candidate_event(
                candidate,
                event_type,
                self._next_sequence(candidate.run_id, candidate.stage_id),
            )
            current = self._current_plan(candidate.run_id, candidate.stage_id)
            if current is not None:
                _require_event_matches_plan(event, current)
            self._candidates[candidate.candidate_checksum] = candidate
            self._append_event(event)
        return candidate.candidate_checksum

    def admit_candidate_submission(
        self,
        candidate: PlanCandidate,
        identity: CandidateDedupIdentity,
        *,
        accepted_at: str,
        candidate_checksum: str | None = None,
    ) -> CandidateSubmission:
        return self.submit_candidate(
            candidate, identity, accepted_at=accepted_at, candidate_checksum=candidate_checksum,
        ).submission

    def submit_candidate(
        self, candidate: PlanCandidate, identity: CandidateDedupIdentity, *,
        accepted_at: str, candidate_checksum: str | None = None,
        exclusive_stage: bool = False,
    ) -> CandidateSubmissionAdmission:
        from uuid import uuid4

        if not isinstance(exclusive_stage, bool):
            raise TypeError("exclusive_stage must be boolean")
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        if not isinstance(identity, CandidateDedupIdentity):
            raise TypeError("identity must be CandidateDedupIdentity")
        _require_live_graph_only(candidate, "candidate")
        _require_submission_scope(candidate, identity)
        action_checksum = (
            candidate.candidate_checksum
            if candidate_checksum is None
            else candidate_checksum
        )
        submission = CandidateSubmission(
            identity=identity,
            candidate_checksum=action_checksum,
            candidate_ref=candidate.candidate_checksum,
            accepted_at=accepted_at,
            admission_id=uuid4().hex,
        )
        with self._lock:
            if exclusive_stage:
                require_submission_stage_available(self._events.get((identity.run_id, identity.stage_id), ()), identity)
            existing = self._submissions.get(identity.dedup_key)
            if existing is not None:
                _require_same_submission(existing, submission)
                return CandidateSubmissionAdmission(existing, created=False)
            existing_candidate = self._candidates.get(candidate.candidate_checksum)
            if existing_candidate is not None and existing_candidate != candidate:
                raise HarnessValidationError(
                    "candidate checksum identity conflict",
                    code="task_plan_checksum_conflict",
                )
            event = _candidate_event(
                candidate,
                "PLAN_CANDIDATE_BUILT",
                self._next_sequence(candidate.run_id, candidate.stage_id),
                submission=submission,
            )
            current = self._current_plan(candidate.run_id, candidate.stage_id)
            if current is not None:
                _require_event_matches_plan(event, current)
            self._candidates.setdefault(candidate.candidate_checksum, candidate)
            self._submissions[identity.dedup_key] = submission
            self._append_event(event)
            return CandidateSubmissionAdmission(submission, created=True)

    def candidate_submission(
        self,
        identity: CandidateDedupIdentity,
    ) -> CandidateSubmission | None:
        if not isinstance(identity, CandidateDedupIdentity):
            raise TypeError("identity must be CandidateDedupIdentity")
        with self._lock:
            return self._submissions.get(identity.dedup_key)

    def submissions_for(
        self,
        run_id: str,
        stage_id: str,
    ) -> tuple[CandidateSubmission, ...]:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        with self._lock:
            records = tuple(
                record
                for record in self._submissions.values()
                if record.identity.run_id == run and record.identity.stage_id == stage
            )
            return tuple(sorted(records, key=lambda item: item.submission_id))

    def candidate_for(
        self,
        run_id: str,
        stage_id: str,
        candidate_ref: str,
    ) -> PlanCandidate | None:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        ref = checksum(candidate_ref, "candidate_ref")
        with self._lock:
            candidate = self._candidates.get(ref)
            if candidate is None:
                return None
            if candidate.run_id != run or candidate.stage_id != stage:
                raise HarnessValidationError(
                    "candidate reference is outside the requested TaskPlan scope",
                    code="candidate_submission_event_invalid",
                )
            return candidate

    def append_rejected_candidate(self, candidate: PlanCandidate, *, reason_code: str) -> str:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        _require_live_graph_only(candidate, "candidate")
        with self._lock:
            existing = self._candidates.get(candidate.candidate_checksum)
            if existing is not None and existing != candidate:
                raise HarnessValidationError(
                    "candidate checksum identity conflict",
                    code="task_plan_checksum_conflict",
                )
            sequence = self._next_sequence(candidate.run_id, candidate.stage_id)
            rejected = _candidate_event(
                candidate,
                "PLAN_CANDIDATE_REJECTED",
                sequence,
                reason_code=reason_code,
            )
            validation_failed = _candidate_event(
                candidate,
                "PLAN_VALIDATION_FAILED",
                sequence + 1,
                reason_code=reason_code,
            )
            current = self._current_plan(candidate.run_id, candidate.stage_id)
            if current is not None:
                _require_event_matches_plan(rejected, current)
                _require_event_matches_plan(validation_failed, current)
            self._candidates.setdefault(candidate.candidate_checksum, candidate)
            self._append_event(rejected)
            self._append_event(validation_failed)
            projection = self._projections.get((candidate.run_id, candidate.stage_id))
            if projection is not None:
                self._projections[(candidate.run_id, candidate.stage_id)] = replace(
                    projection,
                    last_sequence=sequence + 1,
                )
            return candidate.candidate_checksum

    def accept_plan(self, plan: ValidatedTaskPlan) -> str:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        _require_live_graph_only(plan, "plan")
        key = (plan.run_id, plan.stage_id, plan.version)
        with self._lock:
            _require_initial_plan_submission_binding(
                plan,
                tuple(
                    submission
                    for submission in self._submissions.values()
                    if submission.identity.run_id == plan.run_id
                    and submission.identity.stage_id == plan.stage_id
                ),
            )
            existing = self._plans.get(key)
            if existing is not None:
                if existing.plan_id != plan.plan_id:
                    raise HarnessValidationError("TaskPlan version is already owned by another plan", code="task_plan_version_conflict")
                if existing.plan_checksum != plan.plan_checksum:
                    raise HarnessValidationError("plan version checksum conflict", code="task_plan_checksum_conflict")
                return existing.plan_checksum
            current = self._current_plan(plan.run_id, plan.stage_id)
            if current is not None:
                if plan.version != current.version + 1 or plan.parent_plan_id != current.plan_id:
                    raise HarnessValidationError("TaskPlan version is not monotonic", code="task_plan_version_conflict")
            elif plan.version != 1:
                raise HarnessValidationError("initial TaskPlan version must be 1", code="task_plan_version_conflict")
            if (
                plan.source_candidate_ref not in self._candidates
                and plan.source_candidate_ref not in self._patches
                and not any(plan.source_candidate_ref == item.candidate_checksum for item in self._candidates.values())
            ):
                raise HarnessValidationError("accepted plan candidate ref is missing", code="task_plan_candidate_missing")
            sequence = self._next_sequence(plan.run_id, plan.stage_id)
            previous_projection = self._projections.get((plan.run_id, plan.stage_id))
            projection = _projection_for_plan(
                plan,
                sequence=sequence,
                previous=previous_projection,
            )
            event = _plan_event(plan, "PLAN_ACCEPTED", sequence)
            validate_submission_event_append(self._events.get((plan.run_id, plan.stage_id), ()), (event,))
            self._plans[key] = plan
            self._projections[(plan.run_id, plan.stage_id)] = projection
            self._append_event(event)
            return plan.plan_checksum

    def append_patch(self, patch: PlanPatch, *, accepted: bool = False) -> str:
        if not isinstance(patch, PlanPatch):
            raise TypeError("patch must be PlanPatch")
        _require_live_graph_only(patch, "patch")
        with self._lock:
            plan = self._current_plan(patch.run_id, patch.stage_id)
            if plan is None:
                raise HarnessValidationError(
                    "cannot append a patch without an accepted base plan",
                    code="task_plan_projection_missing",
                )
            if not patch.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "patch identity does not match the accepted base plan",
                    code="task_plan_patch_scope_mismatch",
                )
            existing = self._patches.get(patch.patch_checksum)
            if existing is not None and existing != patch:
                raise HarnessValidationError("patch checksum identity conflict", code="task_plan_checksum_conflict")
            self._patches[patch.patch_checksum] = patch
            event_type = "PLAN_PATCH_ACCEPTED" if accepted else "PLAN_PATCH_PROPOSED"
            sequence = self._next_sequence(patch.run_id, patch.stage_id)
            event = TaskPlanEvent.for_plan(
                event_type,
                plan,
                input_checksum=patch.patch_checksum,
                reason_code=patch.reason_code,
                payload={"patch_ref": patch.patch_checksum},
                sequence=sequence,
            )
            self._append_event(event)
            projection = self._projections.get((patch.run_id, patch.stage_id))
            if projection is not None:
                self._projections[(patch.run_id, patch.stage_id)] = replace(
                    projection,
                    last_sequence=sequence,
                )
            return patch.patch_checksum

    def accept_patched_plan(
        self,
        patch: PlanPatch,
        plan: ValidatedTaskPlan,
        *,
        skipped_task_ids: tuple[str, ...] = (),
    ) -> str:
        """Atomically commit an accepted patch, its new plan, and skips."""

        if not isinstance(patch, PlanPatch) or not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("patch and plan must use TaskPlan contracts")
        _require_live_graph_only(patch, "patch")
        _require_live_graph_only(plan, "plan")
        skip_ids = tuple(sorted(set(identifier(item, "skipped_task_ids") for item in skipped_task_ids)))
        replacements = _replacement_mapping(patch)
        with self._lock:
            current = self._current_plan(patch.run_id, patch.stage_id)
            if current is None or not patch.matches_plan_identity(current):
                raise HarnessValidationError(
                    "patched plan base is stale",
                    code="task_plan_stale_patch",
                )
            if (
                not plan.shares_stage_identity(current)
                or plan.policy_ref != current.policy_ref
                or plan.policy_checksum != current.policy_checksum
            ):
                raise HarnessValidationError(
                    "patched plan identity does not match its accepted base",
                    code="task_plan_patch_scope_mismatch",
                )
            if plan.parent_plan_id != current.plan_id or plan.version != current.version + 1:
                raise HarnessValidationError(
                    "patched plan version is not monotonic",
                    code="task_plan_version_conflict",
                )
            if plan.source_candidate_ref != patch.patch_checksum:
                raise HarnessValidationError(
                    "patched plan source does not match patch checksum",
                    code="task_plan_patch_checksum_mismatch",
                )
            current_projection = self._projections.get((patch.run_id, patch.stage_id))
            if current_projection is None:
                raise HarnessValidationError(
                    "patched plan base projection is missing",
                    code="task_plan_projection_missing",
                )
            _validate_patch_transition_targets(
                current,
                plan,
                current_projection=current_projection,
                replacements=replacements,
                skipped_task_ids=skip_ids,
            )
            existing = self._plans.get((plan.run_id, plan.stage_id, plan.version))
            if existing is not None:
                if existing.plan_checksum != plan.plan_checksum:
                    raise HarnessValidationError(
                        "patched plan version checksum conflict",
                        code="task_plan_checksum_conflict",
                    )
                return existing.plan_checksum
            self._patches.setdefault(patch.patch_checksum, patch)
            sequence = self._next_sequence(plan.run_id, plan.stage_id)
            patch_event = TaskPlanEvent.for_plan(
                "PLAN_PATCH_ACCEPTED",
                current,
                input_checksum=patch.patch_checksum,
                reason_code=patch.reason_code,
                payload={"patch_ref": patch.patch_checksum},
                sequence=sequence,
            )
            plan_event = _plan_event(plan, "PLAN_ACCEPTED", sequence + 1)
            previous_projection = self._projections.get((plan.run_id, plan.stage_id))
            next_projection = _projection_for_plan(
                plan,
                sequence=plan_event.sequence,
                previous=previous_projection,
            )
            events = [patch_event, plan_event]
            for replaced_task_id, replacement_task_id in sorted(replacements.items()):
                old_state = next((item for item in next_projection.tasks if item.task_id == replaced_task_id), None)
                new_state = next((item for item in next_projection.tasks if item.task_id == replacement_task_id), None)
                if old_state is None or new_state is None:
                    raise HarnessValidationError(
                        "patched plan replacement references unknown task",
                        code="task_plan_unknown_task",
                        details={"replaced_task_id": replaced_task_id, "replacement_task_id": replacement_task_id},
                    )
                next_projection = replace(
                    next_projection,
                    tasks=tuple(
                        replace(
                            item,
                            status=TaskLifecycle.SKIPPED,
                            active_instance_id=None,
                            failure_reason_code="plan_patch_replaced",
                        )
                        if item.task_id == replaced_task_id else item
                        for item in next_projection.tasks
                    ),
                    last_sequence=next_projection.last_sequence + 1,
                )
                events.append(
                    TaskPlanEvent.for_plan(
                        "TASK_REPLACED",
                        plan,
                        task_id=replaced_task_id,
                        reason_code="plan_patch_replaced",
                        input_checksum=plan.plan_checksum,
                        payload={
                            "replaced_task_id": replaced_task_id,
                            "replacement_task_id": replacement_task_id,
                        },
                        sequence=next_projection.last_sequence,
                    )
                )
            for task_id in skip_ids:
                state = next((item for item in next_projection.tasks if item.task_id == task_id), None)
                if state is None:
                    raise HarnessValidationError(
                        "patched plan skip references unknown task",
                        code="task_plan_unknown_task",
                        details={"task_id": task_id},
                    )
                next_projection = replace(
                    next_projection,
                    tasks=tuple(
                        replace(item, status=TaskLifecycle.SKIPPED, active_instance_id=None, failure_reason_code="plan_patch_skip")
                        if item.task_id == task_id else item
                        for item in next_projection.tasks
                    ),
                    last_sequence=next_projection.last_sequence + 1,
                )
                events.append(
                    TaskPlanEvent.for_plan(
                        "TASK_SKIPPED",
                        plan,
                        task_id=task_id,
                        reason_code="plan_patch_skip",
                        input_checksum=plan.plan_checksum,
                        sequence=next_projection.last_sequence,
                    )
                )
            self._plans[(plan.run_id, plan.stage_id, plan.version)] = plan
            for event in events:
                self._append_event(event)
            self._projections[(plan.run_id, plan.stage_id)] = next_projection
            return plan.plan_checksum

    def append_result(self, result: TaskResultRecord) -> str:
        if not isinstance(result, TaskResultRecord):
            raise TypeError("result must be TaskResultRecord")
        _require_live_graph_only(result, "result")
        from framework.harness.runtime_contract import (
            validate_task_result_contract,
            validate_task_result_owner_contract,
        )

        validate_task_result_owner_contract(result)
        key = (result.run_id, result.stage_id, result.task_instance_id, result.attempt, result.plan_version)
        with self._lock:
            existing = self._results.get(key)
            if existing is not None:
                if existing.result_checksum != result.result_checksum:
                    raise HarnessValidationError("conflicting duplicate task result", code="task_plan_duplicate_result_conflict")
                return existing.result_checksum
            projection = self._projections.get((result.run_id, result.stage_id))
            if projection is None or projection.plan_id != result.plan_id or projection.plan_version != result.plan_version:
                raise HarnessValidationError("task result belongs to stale plan", code="task_plan_stale_result")
            plan = self._plans.get((result.run_id, result.stage_id, result.plan_version))
            if plan is None:
                raise HarnessValidationError("task result plan is unavailable", code="task_plan_stale_result")
            if not result.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "task result identity does not match accepted plan",
                    code="task_plan_result_identity_mismatch",
                )
            from framework.harness.task_plan.attempt_history_index import history_record_for_result

            if not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "TaskPlan projection does not match accepted plan identity",
                    code="task_plan_projection_identity_mismatch",
                )
            task = next((item for item in projection.tasks if item.task_id == result.task_id), None)
            if task is None:
                raise HarnessValidationError("task result references unknown task", code="task_plan_unknown_task")
            definition = next((item for item in plan.tasks if item.task_id == result.task_id), None)
            if definition is None or definition.task_definition_checksum != result.task_checksum:
                raise HarnessValidationError("task result definition checksum does not match accepted plan", code="task_plan_result_identity_mismatch")
            if definition.worker_ref != result.worker_ref:
                raise HarnessValidationError("task result worker binding does not match accepted plan", code="task_plan_wrong_binding")
            if result.binding_checksum != definition.binding_checksum:
                raise HarnessValidationError(
                    "task result binding checksum does not match accepted plan",
                    code="task_plan_wrong_binding",
                )
            if task.active_instance_id != result.task_instance_id or task.attempts != result.attempt:
                raise HarnessValidationError("task result belongs to a different attempt", code="task_plan_wrong_attempt")
            if task.status in {TaskLifecycle.SUCCEEDED, TaskLifecycle.SKIPPED}:
                raise HarnessValidationError("task already has a committed terminal result", code="task_plan_duplicate_result_conflict")
            validate_task_result_contract(plan, projection, result)
            history_record_for_result(plan, result, self.read_events(result.run_id, result.stage_id))
            _require_subagent_result_evidence(result, definition)
            _validate_result_usage(result, definition)
            if result.status is TaskLifecycle.SUCCEEDED:
                if result.output_schema_ref != definition.task.output_contract.schema_ref:
                    raise HarnessValidationError(
                        "task result output schema does not match accepted task",
                        code="task_plan_output_schema_mismatch",
                    )
                if result.output_roles != (definition.output_role,):
                    raise HarnessValidationError(
                        "task result output role does not match accepted task",
                        code="task_plan_output_role_mismatch",
                    )
                if not result.output_roles:
                    raise HarnessValidationError("successful task result requires an output role", code="task_plan_result_invalid")
                reference = TaskResultReference(result_ref=result.result_ref or "task-result:" + result.result_checksum, result_checksum=result.result_checksum, output_role=result.output_roles[0], output_schema_ref=result.output_schema_ref)
                updated = task.transitioned(
                    TaskLifecycle.SUCCEEDED,
                    attempts=result.attempt,
                    active_instance_id=None,
                    admission_owner=None,
                    result=reference,
                    failure_reason_code=None,
                )
                result_event_type = "TASK_RESULT_ACCEPTED"
                terminal_event_type = "TASK_COMPLETED"
            else:
                updated = task.transitioned(
                    TaskLifecycle.FAILED,
                    attempts=result.attempt,
                    active_instance_id=None,
                    admission_owner=None,
                    failure_reason_code=result.error_code or "task_failed",
                )
                result_event_type = "TASK_RESULT_REJECTED"
                terminal_event_type = "TASK_FAILED"
            tasks = tuple(updated if item.task_id == result.task_id else item for item in projection.tasks)
            result_sequence = self._next_sequence(result.run_id, result.stage_id)
            terminal_sequence = result_sequence + 1
            result_event = _result_event(
                result,
                result_event_type,
                result_sequence,
                plan=plan,
            )
            terminal_event = _terminal_result_event(
                result,
                terminal_event_type,
                terminal_sequence,
                plan=plan,
            )
            settled_budget = _settle_result_budget(
                projection.consumed_budget,
                definition,
                result,
            )
            next_projection = replace(
                projection,
                tasks=tasks,
                consumed_budget=settled_budget,
                last_sequence=terminal_sequence,
            )

            # The in-memory implementation models the same atomic boundary as
            # the durable adapter: result evidence, both causal events, and the
            # authoritative projection become visible together.
            scope = (result.run_id, result.stage_id)
            previous_events = list(self._events.get(scope, ()))
            try:
                self._append_event(result_event)
                self._append_event(terminal_event)
                self._results[key] = result
                self._projections[scope] = next_projection
            except BaseException:
                self._events[scope] = previous_events
                self._results.pop(key, None)
                self._projections[scope] = projection
                raise
            return result.result_checksum

    def load_projection(self, run_id: str, stage_id: str) -> TaskPlanProjection:
        key = (identifier(run_id, "run_id"), identifier(stage_id, "stage_id"))
        with self._lock:
            projection = self._projections.get(key)
            if projection is None:
                raise HarnessValidationError("TaskPlan projection is missing", code="task_plan_projection_missing", details={"run_id": key[0], "stage_id": key[1]})
            return projection

    def read_events(self, run_id: str, stage_id: str) -> tuple[TaskPlanEvent, ...]:
        with self._lock:
            return tuple(self._events.get((run_id, stage_id), ()))

    def update_projection(self, projection: TaskPlanProjection) -> None:
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        _require_live_graph_only(projection, "projection")
        with self._lock:
            current = self._projections.get((projection.run_id, projection.stage_id))
            if current is None:
                raise HarnessValidationError(
                    "TaskPlan projection update requires a durable accepted baseline",
                    code="task_plan_projection_missing",
                )
            _require_projection_transition_identity(current, projection)
            plan = self._current_plan(projection.run_id, projection.stage_id)
            if plan is None or not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "projection does not match the accepted plan",
                    code="task_plan_projection_mismatch",
                )
            if projection.last_sequence < current.last_sequence:
                raise HarnessValidationError("projection sequence moved backwards", code="task_plan_sequence_conflict")
            self._projections[(projection.run_id, projection.stage_id)] = projection

    def results_for(self, run_id: str, stage_id: str, plan_id: str, plan_version: int) -> tuple[TaskResultRecord, ...]:
        from framework.harness.task_plan.attempt_history_index import accepted_results_from_history

        with self._lock:
            history = self.result_history_for(run_id, stage_id, plan_id, plan_version)
            projection = self._projections.get((run_id, stage_id))
            return accepted_results_from_history(history, projection)

    def result_history_for(
        self, run_id: str, stage_id: str, plan_id: str, plan_version: int,
    ) -> tuple[TaskAttemptHistoryRecord, ...]:
        from framework.harness.task_plan.attempt_history_index import result_history_from_events

        with self._lock:
            requested = self._plans.get((run_id, stage_id, plan_version))
            if requested is None or requested.plan_id != plan_id:
                return ()
            return result_history_from_events(
                (self._plans[(run_id, stage_id, version)] for version in range(1, plan_version + 1)),
                self.read_events(run_id, stage_id),
                self._result_records_for(run_id, stage_id, plan_id, plan_version),
            )

    def _result_records_for(
        self,
        run_id: str,
        stage_id: str,
        plan_id: str,
        plan_version: int,
    ) -> tuple[TaskResultRecord, ...]:
        """Return every recorded attempt needed to replay the plan lifecycle.

        ``results_for`` intentionally exposes only the latest successful output
        for aggregation. Checkpoints and replay also need rejected attempts so
        retry and halt transitions retain their causal result evidence.
        """

        with self._lock:
            current_plan = self._plans.get((run_id, stage_id, plan_version))
            if current_plan is None or current_plan.plan_id != plan_id:
                return ()
            matching = [
                item
                for item in self._results.values()
                if item.run_id == run_id
                and item.stage_id == stage_id
                and (
                    (item.plan_id == plan_id and item.plan_version == plan_version)
                    or _plan_contains_task_version(self._plans, current_plan, item)
                )
            ]
            return tuple(
                sorted(
                    matching,
                    key=lambda item: (
                        item.task_id,
                        item.attempt,
                        item.plan_version,
                        item.task_instance_id,
                        item.result_checksum,
                    ),
                )
            )

    def append_event(self, event: TaskPlanEvent) -> str:
        if not isinstance(event, TaskPlanEvent):
            raise TypeError("event must be TaskPlanEvent")
        _require_live_graph_only(event, "event")
        with self._lock:
            current = self._next_sequence(event.run_id, event.stage_id)
            if event.sequence != current:
                raise HarnessValidationError("event sequence is not monotonic", code="task_plan_sequence_conflict", details={"expected": current, "actual": event.sequence})
            plan = self._current_plan(event.run_id, event.stage_id)
            if plan is not None:
                _require_event_matches_plan(event, plan)
            history = tuple(self._events.get((event.run_id, event.stage_id), ()))
            _reject_uncoordinated_capacity_transition((event,), history)
            from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

            validate_submission_event_append(history, (event,))
            validate_parallel_admission_append(history, (event,),
                                               plan_lookup=lambda version: self.plan(event.run_id, event.stage_id, version))
            self._append_event(event)
            key = (event.run_id, event.stage_id)
            projection = self._projections.get(key)
            if projection is not None:
                self._projections[key] = replace(
                    projection,
                    last_sequence=event.sequence,
                )
            return event.event_checksum

    def append_events(self, events: tuple[TaskPlanEvent, ...]) -> tuple[str, ...]:
        """Atomically append one contiguous, single-plan event batch."""

        batch = _validate_atomic_event_batch(events)
        run_id = batch[0].run_id
        stage_id = batch[0].stage_id
        key = (run_id, stage_id)
        with self._lock:
            history = tuple(self._events.get(key, ()))
            replayed = _classify_atomic_event_batch_history(batch, history)
            if replayed:
                return tuple(event.event_checksum for event in batch)
            _reject_uncoordinated_capacity_transition(batch, history)

            plan = self._current_plan(run_id, stage_id)
            if plan is not None:
                for event in batch:
                    _require_event_matches_plan(event, plan)

            # Every validation above runs before the visible event list or its
            # causal projection is changed.
            from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

            validate_submission_event_append(history, batch)
            validate_parallel_admission_append(history, batch, plan_lookup=lambda version: self.plan(run_id, stage_id, version))
            self._events.setdefault(key, []).extend(batch)
            projection = self._projections.get(key)
            if projection is not None:
                self._projections[key] = replace(
                    projection,
                    last_sequence=batch[-1].sequence,
                )
            return tuple(event.event_checksum for event in batch)

    def install_capacity_snapshot(
        self,
        snapshot: "CapacityScopeSnapshot",
    ) -> "CapacityScopeSnapshot":
        """Install one trusted shared-capacity baseline exactly once."""

        from framework.harness.task_plan.capacity import CapacityScopeSnapshot

        if not isinstance(snapshot, CapacityScopeSnapshot):
            raise TypeError("snapshot must be CapacityScopeSnapshot")
        if snapshot.revision != 1:
            raise HarnessValidationError(
                "initial capacity scope revision must be 1",
                code="CAPACITY_RESERVATION_CONFLICT",
            )
        with self._lock:
            existing = self._capacity_snapshots.get(snapshot.owner_scope)
            if existing is None:
                self._capacity_snapshots[snapshot.owner_scope] = snapshot
                return snapshot
            if existing != snapshot:
                raise HarnessValidationError(
                    "capacity scope is already installed with different content",
                    code="CAPACITY_RESERVATION_CONFLICT",
                    details={"owner_scope": snapshot.owner_scope},
                )
            return snapshot

    def load_capacity_snapshot(self, owner_scope: str) -> "CapacityScopeSnapshot":
        """Load the authoritative snapshot for one shared owner scope."""

        from framework.harness.task_plan.capacity import CapacityScopeSnapshot

        scope = identifier(owner_scope, "capacity_scope")
        with self._lock:
            snapshot = self._capacity_snapshots.get(scope)
            if not isinstance(snapshot, CapacityScopeSnapshot):
                raise HarnessValidationError(
                    "required shared capacity scope is missing",
                    code="CAPACITY_POLICY_MISSING",
                    details={"owner_scope": scope},
                )
            return snapshot

    def commit_wave_admission(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]:
        """Commit wave admission and shared-capacity mutation as one unit."""

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        before, reservations = _validate_capacity_admission_contract(
            batch,
            expected_capacity_revision=expected_capacity_revision,
            capacity_scope=capacity_scope,
            capacity_before_checksum=capacity_before_checksum,
            capacity_after=capacity_after,
            pool_reservations=pool_reservations,
        )
        transition_key = _capacity_admission_key(batch)
        scope_key = (batch[0].run_id, batch[0].stage_id)
        with self._lock:
            history = tuple(self._events.get(scope_key, ()))
            if _classify_atomic_event_batch_history(batch, history):
                for event, projection in zip(batch, projections, strict=True):
                    committed = self._transition_projections.get(
                        (event.run_id, event.stage_id, event.event_checksum)
                    )
                    if committed is None or committed.projection_checksum != projection.projection_checksum:
                        raise HarnessValidationError(
                            "committed event projection differs from retry",
                            code="task_plan_projection_mismatch",
                        )
                historical = self._capacity_admission_transitions.get(transition_key)
                if historical != (before, capacity_after, reservations):
                    raise HarnessValidationError(
                        "committed wave capacity transition differs from retry",
                        code="CAPACITY_RESERVATION_CONFLICT",
                    )
                return tuple(event.event_checksum for event in batch)

            current_capacity = self._capacity_snapshots.get(before.owner_scope)
            if current_capacity != before:
                raise HarnessValidationError(
                    "capacity CAS precondition differs from current shared scope",
                    code="CAPACITY_RESERVATION_CONFLICT",
                    details={"owner_scope": before.owner_scope},
                )
            event_checksums = self._commit_events(
                batch,
                projections,
                expected_projection_checksum=expected_projection_checksum,
                allow_capacity_transition=True,
            )
            self._capacity_snapshots[before.owner_scope] = capacity_after
            self._capacity_admission_transitions[transition_key] = (
                before,
                capacity_after,
                reservations,
            )
            return event_checksums

    def commit_wave_completion(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        settled_pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]:
        """Atomically release confirmed wave capacity beside completion facts."""

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        scope_key = (batch[0].run_id, batch[0].stage_id)
        with self._lock:
            history = tuple(self._events.get(scope_key, ()))
            before, settlements = _validate_capacity_completion_contract(
                batch,
                history,
                expected_capacity_revision=expected_capacity_revision,
                capacity_scope=capacity_scope,
                capacity_before_checksum=capacity_before_checksum,
                capacity_after=capacity_after,
                settled_pool_reservations=settled_pool_reservations,
            )
            transition_key = _capacity_admission_key(batch)
            if _classify_atomic_event_batch_history(batch, history):
                for event, projection in zip(batch, projections, strict=True):
                    committed = self._transition_projections.get(
                        (event.run_id, event.stage_id, event.event_checksum)
                    )
                    if committed is None or committed.projection_checksum != projection.projection_checksum:
                        raise HarnessValidationError(
                            "committed event projection differs from retry",
                            code="task_plan_projection_mismatch",
                        )
                historical = self._capacity_settlement_transitions.get(transition_key)
                if historical != (before, capacity_after, settlements):
                    raise HarnessValidationError(
                        "committed capacity settlement differs from retry",
                        code="CAPACITY_RESERVATION_CONFLICT",
                    )
                return tuple(event.event_checksum for event in batch)
            if self._capacity_snapshots.get(before.owner_scope) != before:
                raise HarnessValidationError(
                    "capacity settlement CAS differs from current shared scope",
                    code="CAPACITY_RESERVATION_CONFLICT",
                )
            event_checksums = self._commit_events(
                batch,
                projections,
                expected_projection_checksum=expected_projection_checksum,
                allow_capacity_transition=True,
            )
            self._capacity_snapshots[before.owner_scope] = capacity_after
            self._capacity_settlement_transitions[transition_key] = (
                before,
                capacity_after,
                settlements,
            )
            return event_checksums

    def commit_events(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
    ) -> tuple[str, ...]:
        """Commit a projection transition that needs no shared-capacity CAS."""

        return self._commit_events(
            events,
            projections,
            expected_projection_checksum=expected_projection_checksum,
            allow_capacity_transition=False,
        )

    def _commit_events(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        allow_capacity_transition: bool,
    ) -> tuple[str, ...]:
        """Atomically append a transition batch and all of its projections."""

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        expected_checksum = checksum(
            expected_projection_checksum,
            "expected_projection_checksum",
        )
        first = batch[0]
        key = (first.run_id, first.stage_id)
        with self._lock:
            current = self._projections.get(key)
            if current is None:
                raise HarnessValidationError(
                    "TaskPlan transition requires an accepted projection",
                    code="task_plan_projection_missing",
                )
            history = tuple(self._events.get(key, ()))
            if _classify_atomic_event_batch_history(batch, history):
                for event, projection in zip(batch, projections, strict=True):
                    committed = self._transition_projections.get(
                        (event.run_id, event.stage_id, event.event_checksum)
                    )
                    if committed is None or committed.projection_checksum != projection.projection_checksum:
                        raise HarnessValidationError(
                            "committed event projection differs from retry",
                            code="task_plan_projection_mismatch",
                        )
                return tuple(event.event_checksum for event in batch)
            if not allow_capacity_transition:
                _reject_uncoordinated_capacity_transition(batch, history)
            if current.projection_checksum != expected_checksum:
                raise HarnessValidationError(
                    "projection CAS precondition differs from current state",
                    code="task_plan_projection_mismatch",
                )
            expected_sequence = self._next_sequence(first.run_id, first.stage_id)
            if batch[0].sequence != expected_sequence:
                raise HarnessValidationError(
                    "event sequence is not monotonic",
                    code="task_plan_sequence_conflict",
                    details={"expected": expected_sequence, "actual": batch[0].sequence},
                )
            plan = self._current_plan(first.run_id, first.stage_id)
            if plan is None:
                raise HarnessValidationError(
                    "TaskPlan transition requires an accepted plan",
                    code="task_plan_projection_missing",
                )
            _validate_wave_admission_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_logical_readiness_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_queue_admission_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_wave_completion_projection_contract(
                current,
                batch,
                projections,
            )
            for event, projection in zip(batch, projections, strict=True):
                _require_event_matches_plan(event, plan)
                if not projection.matches_plan_identity(plan):
                    raise HarnessValidationError(
                        "projection does not match the accepted plan",
                        code="task_plan_projection_mismatch",
                    )
                _require_projection_transition_identity(current, projection)

            from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

            validate_submission_event_append(history, batch)
            validate_parallel_admission_append(history, batch, plan_lookup=lambda version: self.plan(first.run_id, first.stage_id, version))
            prior_events = (
                None
                if key not in self._events
                else list(self._events[key])
            )
            prior_projection = current
            committed_keys = [
                (event.run_id, event.stage_id, event.event_checksum)
                for event in batch
            ]
            prior_committed = {
                item: self._transition_projections.get(item)
                for item in committed_keys
            }
            try:
                self._events.setdefault(key, []).extend(batch)
                for event, projection in zip(batch, projections, strict=True):
                    self._transition_projections[
                        (event.run_id, event.stage_id, event.event_checksum)
                    ] = projection
                self._projections[key] = projections[-1]
            except BaseException:
                if prior_events is None:
                    self._events.pop(key, None)
                else:
                    self._events[key] = prior_events
                self._projections[key] = prior_projection
                for item, previous in prior_committed.items():
                    if previous is None:
                        self._transition_projections.pop(item, None)
                    else:
                        self._transition_projections[item] = previous
                raise
            return tuple(event.event_checksum for event in batch)

    def commit_event(
        self,
        event: TaskPlanEvent,
        projection: TaskPlanProjection,
    ) -> str:
        """Commit one event through the atomic transition boundary."""

        if not isinstance(event, TaskPlanEvent):
            raise TypeError("event must be TaskPlanEvent")
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        key = (event.run_id, event.stage_id)
        with self._lock:
            history = tuple(self._events.get(key, ()))
            expected = (
                projection.projection_checksum
                if event.sequence <= len(history)
                else self._projections.get(key, projection).projection_checksum
            )
        return self.commit_events(
            (event,),
            (projection,),
            expected_projection_checksum=expected,
        )[0]

    def candidate(self, candidate_ref: str) -> PlanCandidate | None:
        with self._lock:
            return self._candidates.get(candidate_ref)

    def patches_for(
        self,
        run_id: str,
        stage_id: str,
    ) -> tuple[PlanPatch, ...]:
        """Return immutable patch evidence needed for offline plan replay."""

        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        with self._lock:
            return tuple(
                sorted(
                    (
                        patch
                        for patch in self._patches.values()
                        if patch.run_id == run and patch.stage_id == stage
                    ),
                    key=lambda item: (item.base_plan_version, item.patch_checksum),
                )
            )

    def plan(self, run_id: str, stage_id: str, version: int | None = None) -> ValidatedTaskPlan | None:
        with self._lock:
            if version is not None:
                return self._plans.get((run_id, stage_id, version))
            return self._current_plan(run_id, stage_id)

    def _current_plan(self, run_id: str, stage_id: str) -> ValidatedTaskPlan | None:
        plans = [plan for (candidate_run, candidate_stage, _), plan in self._plans.items() if candidate_run == run_id and candidate_stage == stage_id]
        return max(plans, key=lambda item: item.version) if plans else None

    def _graph_checksum(self, run_id: str, stage_id: str) -> str:
        plan = self._current_plan(run_id, stage_id)
        return plan.graph_checksum if plan else "sha256:" + "0" * 64

    def _next_sequence(self, run_id: str, stage_id: str) -> int:
        return len(self._events.get((run_id, stage_id), ())) + 1

    def _append_event(self, event: TaskPlanEvent) -> None:
        self._events.setdefault((event.run_id, event.stage_id), []).append(event)


def _capacity_admission_key(events: tuple[TaskPlanEvent, ...]) -> str:
    return canonical_payload_checksum(
        {"event_checksums": [event.event_checksum for event in events]}
    )


def _reject_uncoordinated_capacity_transition(
    events: tuple[TaskPlanEvent, ...],
    history: tuple[TaskPlanEvent, ...],
) -> None:
    """Keep fresh shared-capacity mutations behind their CAS entry points.

    A wave with no pool allocation and a completion that leaves every pool
    reservation outstanding do not mutate the shared scope.  They remain valid
    ordinary projection transitions.  Any actual reserve or release must be
    committed through ``commit_wave_admission`` or ``commit_wave_completion``.
    """

    from framework.harness.task_plan.capacity import CapacityScopeSnapshot
    from framework.harness.task_plan.parallel import DispatchWave
    from framework.harness.task_plan.parallel_lifecycle import ReservationState

    for event in events:
        if event.event_type == "TASK_WAVE_ADMITTED":
            raw_wave = event.payload.get("wave")
            if not isinstance(raw_wave, Mapping):
                continue
            wave = DispatchWave.from_dict(thaw_mapping(raw_wave))
            before = wave.packing.capacity_before
            after = wave.packing.capacity_after
            if wave.packing.reservations or (
                before is not None
                and after is not None
                and before != after
            ):
                raise HarnessValidationError(
                    "shared-capacity wave admission requires its atomic CAS entry point",
                    code="CAPACITY_RESERVATION_CONFLICT",
                )
            continue

        if event.event_type != "TASK_WAVE_COMPLETED":
            continue
        payload = thaw_mapping(event.payload)
        wave_id = payload.get("wave_id")
        admissions = tuple(
            candidate
            for candidate in history
            if candidate.event_type == "TASK_WAVE_ADMITTED"
            and isinstance(candidate.payload.get("wave"), Mapping)
            and candidate.payload["wave"].get("wave_id") == wave_id
        )
        if len(admissions) != 1:
            # The canonical parallel-history validator reports the missing or
            # ambiguous admission.  It cannot authorize a capacity mutation.
            continue
        admitted_wave = DispatchWave.from_dict(
            thaw_mapping(admissions[0].payload["wave"])
        )
        capacity_task_ids = {
            reservation.task_id
            for reservation in admitted_wave.packing.reservations
        }
        if not capacity_task_ids:
            continue
        reservation_states = payload.get("reservation_states")
        if (
            not isinstance(reservation_states, Mapping)
            or not capacity_task_ids.issubset(reservation_states)
        ):
            raise HarnessValidationError(
                "capacity-backed completion is missing reservation state evidence",
                code="CAPACITY_RESERVATION_CONFLICT",
            )
        try:
            released_task_ids = {
                task_id
                for task_id, state in reservation_states.items()
                if ReservationState(state)
                in {ReservationState.CONSUMED, ReservationState.RELEASED}
            }
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "capacity-backed completion has invalid reservation state evidence",
                code="CAPACITY_RESERVATION_CONFLICT",
            ) from exc
        raw_before = payload.get("capacity_before")
        raw_after = payload.get("capacity_after")
        snapshots_change = False
        if raw_before is not None or raw_after is not None:
            if not isinstance(raw_before, Mapping) or not isinstance(raw_after, Mapping):
                snapshots_change = True
            else:
                snapshots_change = (
                    CapacityScopeSnapshot.from_dict(raw_before)
                    != CapacityScopeSnapshot.from_dict(raw_after)
                )
        if capacity_task_ids.intersection(released_task_ids) or snapshots_change:
            raise HarnessValidationError(
                "shared-capacity wave completion requires its atomic CAS entry point",
                code="CAPACITY_RESERVATION_CONFLICT",
            )


def _validate_wave_admission_projection_contract(
    current: TaskPlanProjection,
    plan: ValidatedTaskPlan,
    events: tuple[TaskPlanEvent, ...],
    projections: tuple[TaskPlanProjection, ...],
) -> None:
    """Bind a wave proposal to current READY order, attempts and budget."""

    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
    from framework.harness.task_plan.parallel import DispatchWave
    from framework.harness.task_plan.scheduler import (
        TaskPlanScheduler,
        task_instance_for_attempt,
    )

    admissions = tuple(
        (index, event)
        for index, event in enumerate(events)
        if event.event_type == "TASK_WAVE_ADMITTED"
    )
    if not admissions:
        return
    if len(admissions) != 1 or admissions[0][0] != 0:
        raise HarnessValidationError(
            "wave admission must lead its atomic admission batch",
            code="task_plan_parallel_event_invalid",
        )
    if any(
        event.event_type != "TASK_ATTEMPT_SPAWN_INTENT"
        for event in events[1:]
    ):
        raise HarnessValidationError(
            "wave admission batch contains a non-spawn transition",
            code="task_plan_parallel_event_invalid",
        )
    admission = admissions[0][1]
    raw_wave = admission.payload.get("wave")
    if not isinstance(raw_wave, Mapping):
        raise HarnessValidationError(
            "wave admission is missing its wave evidence",
            code="TASK_WAVE_PACKING_EVIDENCE_INVALID",
        )
    wave = DispatchWave.from_dict(thaw_mapping(raw_wave))
    packing = wave.packing
    if (
        packing.ready_order != current.logical_ready_order
        or tuple(
            task_id
            for task_id in current.logical_ready_order
            if task_id in set(wave.task_ids)
        )
        != wave.task_ids
    ):
        raise HarnessValidationError(
            "wave admission does not preserve the current logical READY order",
            code="task_plan_ready_order_mismatch",
        )
    states = {state.task_id: state for state in current.tasks}
    if any(
        task_id not in states or states[task_id].status is not TaskLifecycle.READY
        for task_id in wave.task_ids
    ):
        raise HarnessValidationError(
            "wave admission selected a task that is not logically READY",
            code="task_plan_task_not_pending",
        )
    instances = tuple(
        task_instance_for_attempt(
            plan,
            task_id,
            states[task_id].attempts + 1,
        )
        for task_id in wave.task_ids
    )
    for instance, reservation in zip(
        instances,
        wave.reservations,
        strict=True,
    ):
        if (
            reservation.task_id != instance.task_id
            or reservation.idempotency_key != instance.idempotency_key
            or thaw_mapping(reservation.budget) != instance.budget_snapshot.to_dict()
        ):
            raise HarnessValidationError(
                "wave reservation differs from its selected attempt",
                code="task_plan_budget_identity_conflict",
            )
    budget_before = TaskPlanBudgetLedger.from_snapshot(
        current.consumed_budget
    ).to_dict()["ledger_checksum"]
    if (
        admission.payload.get("budget_before_checksum") != budget_before
        or packing.budget_before_checksum != budget_before
    ):
        raise HarnessValidationError(
            "wave admission uses a stale budget ledger",
            code="task_plan_budget_checksum_mismatch",
        )
    projected = TaskPlanScheduler.admit_ready_tasks(
        current,
        instances,
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    budget_after = TaskPlanBudgetLedger.from_snapshot(
        projected.consumed_budget
    ).to_dict()["ledger_checksum"]
    if (
        admission.payload.get("budget_after_checksum") != budget_after
        or packing.budget_after_checksum != budget_after
    ):
        raise HarnessValidationError(
            "wave admission budget result differs from selected attempts",
            code="task_plan_budget_checksum_mismatch",
        )
    expected_projection = replace(projected, last_sequence=admission.sequence)
    for event, supplied in zip(events, projections, strict=True):
        if event is not admission:
            expected_projection = replace(
                expected_projection,
                last_sequence=event.sequence,
            )
        if supplied.projection_checksum != expected_projection.projection_checksum:
            raise HarnessValidationError(
                "wave event projection is not the authoritative admission transition",
                code="task_plan_projection_mismatch",
            )


def _validate_queue_admission_projection_contract(
    current: TaskPlanProjection,
    plan: ValidatedTaskPlan,
    events: tuple[TaskPlanEvent, ...],
    projections: tuple[TaskPlanProjection, ...],
) -> None:
    """Rebuild an atomic static-queue transition from canonical evidence.

    Queue publication is allowed to share one store transaction with the
    logical READY fact and the subsequent DISPATCHED transition.  Rebuilding
    every prefix here prevents a caller from using a valid queue admission as
    cover for an unrelated projection mutation in the same batch.
    """

    queue_events = tuple(
        event for event in events if event.event_type == "TASK_QUEUE_ADMITTED"
    )
    if not queue_events:
        return
    if len(queue_events) != 1:
        raise HarnessValidationError(
            "queue admission batch must contain exactly one admission",
            code="task_plan_queue_admission_invalid",
        )
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
    from framework.harness.task_plan.scheduler import (
        TaskPlanReadyDecision,
        TaskPlanScheduler,
    )

    allowed = {
        "TASK_READY",
        "TASK_QUEUE_ADMITTED",
        "TASK_DISPATCHED",
        "TASK_STARTED",
    }
    if any(event.event_type not in allowed for event in events):
        raise HarnessValidationError(
            "queue admission batch contains an unrelated transition",
            code="task_plan_queue_admission_invalid",
        )

    projected = current
    admitted_instance: TaskInstance | None = None
    admission_seen = False
    for event, supplied in zip(events, projections, strict=True):
        if event.event_type == "TASK_READY":
            if admission_seen:
                raise HarnessValidationError(
                    "logical READY must precede queue admission in one batch",
                    code="task_plan_queue_admission_invalid",
                )
            raw_readiness = event.payload.get("logical_readiness")
            if not isinstance(raw_readiness, Mapping):
                raise HarnessValidationError(
                    "queue admission batch is missing logical readiness evidence",
                    code="task_plan_logical_readiness_missing",
                )
            readiness = LogicalTaskReadiness.from_dict(raw_readiness)
            _validate_logical_readiness_order(readiness, plan)
            projected = TaskPlanScheduler().reserve_ready_tasks(
                projected,
                TaskPlanReadyDecision(
                    logical_ready_task_ids=readiness.logical_ready_order,
                ),
            )
        elif event.event_type == "TASK_QUEUE_ADMITTED":
            raw = event.payload.get("queue_admission")
            if not isinstance(raw, Mapping):
                raise HarnessValidationError(
                    "queue admission is missing canonical evidence",
                    code="task_plan_queue_admission_invalid",
                )
            evidence = TaskQueueAdmissionEvidence.from_dict(raw)
            instance = evidence.task_instance
            state = next(
                (item for item in projected.tasks if item.task_id == instance.task_id),
                None,
            )
            if (
                admission_seen
                or state is None
                or state.status is not TaskLifecycle.READY
                or not projected.logical_ready_order
                or projected.logical_ready_order[0] != instance.task_id
                or instance.attempt != state.attempts + 1
                or not instance.matches_plan_identity(plan)
            ):
                raise HarnessValidationError(
                    "queue admission does not select the next logical READY attempt",
                    code="task_plan_queue_admission_invalid",
                )
            before = TaskPlanBudgetLedger.from_snapshot(
                projected.consumed_budget
            ).to_dict()["ledger_checksum"]
            projected = TaskPlanScheduler.admit_ready_tasks(
                projected,
                (instance,),
                admission_owner=TaskAdmissionOwner.QUEUE,
            )
            after = TaskPlanBudgetLedger.from_snapshot(
                projected.consumed_budget
            ).to_dict()["ledger_checksum"]
            if (
                evidence.budget_before_checksum != before
                or evidence.budget_after_checksum != after
            ):
                raise HarnessValidationError(
                    "queue admission budget differs from authoritative transition",
                    code="task_plan_queue_admission_invalid",
                )
            admitted_instance = instance
            admission_seen = True
        else:
            if (
                not admission_seen
                or admitted_instance is None
                or event.task_id != admitted_instance.task_id
                or event.task_instance_id != admitted_instance.task_instance_id
                or event.attempt != admitted_instance.attempt
                or event.input_checksum
                != admitted_instance.task_definition_checksum
            ):
                raise HarnessValidationError(
                    "queue dispatch transition differs from admitted attempt",
                    code="task_plan_queue_admission_invalid",
                )
            if event.event_type == "TASK_DISPATCHED":
                projected = TaskPlanScheduler.mark_dispatched(
                    projected,
                    admitted_instance,
                )
            elif event.event_type == "TASK_STARTED":
                projected = TaskPlanScheduler.mark_started(
                    projected,
                    admitted_instance,
                )

        expected = replace(projected, last_sequence=event.sequence)
        if supplied.projection_checksum != expected.projection_checksum:
            raise HarnessValidationError(
                "queue batch projection differs from authoritative transition",
                code="task_plan_queue_admission_invalid",
            )
        projected = expected


def _validate_logical_readiness_projection_contract(
    current: TaskPlanProjection,
    plan: ValidatedTaskPlan,
    events: tuple[TaskPlanEvent, ...],
    projections: tuple[TaskPlanProjection, ...],
) -> None:
    """Bind standalone logical READY facts to their complete ordered set."""

    if any(event.event_type == "TASK_QUEUE_ADMITTED" for event in events):
        # The queue validator rebuilds every prefix of this richer transition.
        return
    ready_events = tuple(event for event in events if event.event_type == "TASK_READY")
    if not ready_events:
        return
    if len(ready_events) != len(events):
        raise HarnessValidationError(
            "logical READY batch contains an unrelated transition",
            code="task_plan_logical_readiness_identity_mismatch",
        )

    from framework.harness.task_plan.scheduler import (
        TaskPlanReadyDecision,
        TaskPlanScheduler,
    )

    projected = current
    for event, supplied in zip(events, projections, strict=True):
        raw = event.payload.get("logical_readiness")
        if not isinstance(raw, Mapping):
            raise HarnessValidationError(
                "logical TASK_READY event is missing readiness evidence",
                code="task_plan_logical_readiness_missing",
            )
        readiness = LogicalTaskReadiness.from_dict(raw)
        _validate_logical_readiness_order(readiness, plan)
        projected = TaskPlanScheduler().reserve_ready_tasks(
            projected,
            TaskPlanReadyDecision(
                logical_ready_task_ids=readiness.logical_ready_order,
            ),
        )
        expected = replace(projected, last_sequence=event.sequence)
        if supplied.projection_checksum != expected.projection_checksum:
            raise HarnessValidationError(
                "logical READY projection differs from authoritative ordering",
                code="task_plan_projection_ready_order_mismatch",
            )
        projected = expected


def _validate_wave_completion_projection_contract(
    current: TaskPlanProjection,
    events: tuple[TaskPlanEvent, ...],
    projections: tuple[TaskPlanProjection, ...],
) -> None:
    """Keep completion facts from smuggling an unrelated projection change."""

    completions = tuple(
        event for event in events if event.event_type == "TASK_WAVE_COMPLETED"
    )
    if not completions:
        return
    if len(completions) != 1 or len(events) != 1:
        raise HarnessValidationError(
            "wave completion must be one isolated projection transition",
            code="task_plan_parallel_event_invalid",
        )
    expected = replace(current, last_sequence=completions[0].sequence)
    if projections[0].projection_checksum != expected.projection_checksum:
        raise HarnessValidationError(
            "wave completion projection differs from current authoritative state",
            code="task_plan_projection_mismatch",
        )


def _validate_capacity_admission_contract(
    events: tuple[TaskPlanEvent, ...],
    *,
    expected_capacity_revision: int,
    capacity_scope: str,
    capacity_before_checksum: str,
    capacity_after: "CapacityScopeSnapshot",
    pool_reservations: tuple["PoolReservation", ...],
) -> tuple["CapacityScopeSnapshot", tuple["PoolReservation", ...]]:
    """Validate policy evidence without granting a proposal authority."""

    from framework.harness.task_plan.capacity import (
        CapacityScopeSnapshot,
        PoolReservation,
    )
    from framework.harness.task_plan.parallel import DispatchWave
    from framework.harness.task_plan.parallel_lifecycle import ReservationState

    if (
        isinstance(expected_capacity_revision, bool)
        or not isinstance(expected_capacity_revision, int)
        or expected_capacity_revision < 1
    ):
        raise HarnessValidationError(
            "expected capacity revision must be a positive integer",
            code="CAPACITY_RESERVATION_CONFLICT",
        )
    scope = identifier(capacity_scope, "capacity_scope")
    before_checksum = checksum(
        capacity_before_checksum,
        "capacity_before_checksum",
    )
    if not isinstance(capacity_after, CapacityScopeSnapshot):
        raise TypeError("capacity_after must be CapacityScopeSnapshot")
    if not isinstance(pool_reservations, tuple) or any(
        not isinstance(item, PoolReservation) for item in pool_reservations
    ):
        raise TypeError("pool_reservations must be a tuple of PoolReservation values")
    reservations = tuple(pool_reservations)
    admissions = tuple(
        event for event in events if event.event_type == "TASK_WAVE_ADMITTED"
    )
    if len(admissions) != 1:
        raise HarnessValidationError(
            "capacity transaction requires exactly one wave admission",
            code="TASK_WAVE_PACKING_EVIDENCE_INVALID",
        )
    payload = thaw_mapping(admissions[0].payload)
    raw_wave = payload.get("wave")
    if not isinstance(raw_wave, Mapping):
        raise HarnessValidationError(
            "wave admission is missing packing evidence",
            code="TASK_WAVE_PACKING_EVIDENCE_INVALID",
        )
    wave = DispatchWave.from_dict(raw_wave)
    packing = wave.packing
    before = packing.capacity_before
    after = packing.capacity_after
    if (
        before is None
        or after is None
        or before.owner_scope != scope
        or before.revision != expected_capacity_revision
        or before.snapshot_checksum != before_checksum
        or after != capacity_after
        or capacity_after.owner_scope != scope
        or capacity_after.revision != before.revision + 1
        or packing.reservations != reservations
        or tuple(item.task_id for item in reservations) != wave.task_ids
        or payload.get("packing_checksum") != packing.packing_checksum
        or payload.get("budget_before_checksum") != packing.budget_before_checksum
        or payload.get("budget_after_checksum") != packing.budget_after_checksum
    ):
        raise HarnessValidationError(
            "wave capacity proposal differs from its committed packing evidence",
            code="TASK_WAVE_PACKING_EVIDENCE_INVALID",
        )

    before_pools = {pool.pool_id: pool for pool in before.pools}
    after_pools = {pool.pool_id: pool for pool in after.pools}
    if tuple(before_pools) != tuple(after_pools):
        raise HarnessValidationError(
            "capacity transition changes the pinned pool set",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        )
    allocated = {pool_id: 0 for pool_id in before_pools}
    for reservation in reservations:
        if (
            reservation.owner_scope != scope
            or reservation.state is not ReservationState.RESERVED
        ):
            raise HarnessValidationError(
                "capacity reservation owner or state is invalid",
                code="CAPACITY_RESERVATION_INVALID",
            )
        for pool_id, quantity in reservation.allocations.items():
            pool = before_pools.get(pool_id)
            if (
                pool is None
                or reservation.policy_checksums.get(pool_id) != pool.policy_checksum
                or reservation.pool_versions.get(pool_id) != pool.reservation_version
                or reservation.pool_reservation_keys.get(pool_id) != pool.reservation_key
                or reservation.expires_at_ms > pool.expires_at_ms
            ):
                raise HarnessValidationError(
                    "capacity reservation differs from the pinned pool snapshot",
                    code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
                )
            allocated[pool_id] += quantity

    for pool_id, before_pool in before_pools.items():
        after_pool = after_pools[pool_id]
        delta = allocated[pool_id]
        if (
            after_pool.capacity != before_pool.capacity
            or after_pool.policy_version != before_pool.policy_version
            or after_pool.policy_checksum != before_pool.policy_checksum
            or after_pool.owner_scope != before_pool.owner_scope
            or after_pool.reservation_key != before_pool.reservation_key
            or after_pool.expires_at_ms != before_pool.expires_at_ms
            or after_pool.reserved != before_pool.reserved + delta
            or after_pool.reservation_version
            != before_pool.reservation_version + int(delta > 0)
        ):
            raise HarnessValidationError(
                "capacity after snapshot is not explained by pool reservations",
                code="CAPACITY_RESERVATION_INVALID",
                details={"pool_id": pool_id},
            )
    return before, reservations


def _validate_capacity_completion_contract(
    events: tuple[TaskPlanEvent, ...],
    history: tuple[TaskPlanEvent, ...],
    *,
    expected_capacity_revision: int,
    capacity_scope: str,
    capacity_before_checksum: str,
    capacity_after: "CapacityScopeSnapshot",
    settled_pool_reservations: tuple["PoolReservation", ...],
) -> tuple["CapacityScopeSnapshot", tuple["PoolReservation", ...]]:
    """Verify that confirmed terminal attempts explain an exact pool release."""

    from framework.harness.task_plan.attempt_history import (
        TaskAttemptHistoryRecord,
        TaskAttemptOutcome,
    )
    from framework.harness.task_plan.capacity import (
        CapacityScopeSnapshot,
        PoolReservation,
    )
    from framework.harness.task_plan.parallel import DispatchWave
    from framework.harness.task_plan.parallel_lifecycle import ReservationState

    if (
        isinstance(expected_capacity_revision, bool)
        or not isinstance(expected_capacity_revision, int)
        or expected_capacity_revision < 1
    ):
        raise HarnessValidationError(
            "expected capacity revision must be a positive integer",
            code="CAPACITY_RESERVATION_CONFLICT",
        )
    scope = identifier(capacity_scope, "capacity_scope")
    before_checksum = checksum(
        capacity_before_checksum,
        "capacity_before_checksum",
    )
    if not isinstance(capacity_after, CapacityScopeSnapshot):
        raise TypeError("capacity_after must be CapacityScopeSnapshot")
    if not isinstance(settled_pool_reservations, tuple) or not settled_pool_reservations:
        raise HarnessValidationError(
            "capacity completion requires at least one confirmed settlement",
            code="CAPACITY_RESERVATION_INVALID",
        )
    if any(not isinstance(item, PoolReservation) for item in settled_pool_reservations):
        raise TypeError(
            "settled_pool_reservations must contain PoolReservation values"
        )
    settlements = tuple(settled_pool_reservations)
    if len({item.task_id for item in settlements}) != len(settlements):
        raise HarnessValidationError(
            "capacity completion contains duplicate task settlements",
            code="CAPACITY_RESERVATION_INVALID",
        )
    completions = tuple(
        event for event in events if event.event_type == "TASK_WAVE_COMPLETED"
    )
    if len(completions) != 1 or len(events) != 1:
        raise HarnessValidationError(
            "capacity settlement requires one isolated wave completion event",
            code="CAPACITY_RESERVATION_INVALID",
        )
    completion = completions[0]
    payload = thaw_mapping(completion.payload)
    raw_before = payload.get("capacity_before")
    raw_after = payload.get("capacity_after")
    if not isinstance(raw_before, Mapping) or not isinstance(raw_after, Mapping):
        raise HarnessValidationError(
            "wave completion is missing capacity settlement snapshots",
            code="CAPACITY_RESERVATION_INVALID",
        )
    before = CapacityScopeSnapshot.from_dict(raw_before)
    recorded_after = CapacityScopeSnapshot.from_dict(raw_after)
    if (
        before.owner_scope != scope
        or before.revision != expected_capacity_revision
        or before.snapshot_checksum != before_checksum
        or recorded_after != capacity_after
        or capacity_after.owner_scope != scope
        or capacity_after.revision != before.revision + 1
    ):
        raise HarnessValidationError(
            "wave completion capacity snapshots differ from CAS evidence",
            code="CAPACITY_RESERVATION_CONFLICT",
        )
    wave_id = payload.get("wave_id")
    admissions = tuple(
        event
        for event in history
        if event.event_type == "TASK_WAVE_ADMITTED"
        and isinstance(event.payload.get("wave"), Mapping)
        and event.payload["wave"].get("wave_id") == wave_id
    )
    if len(admissions) != 1:
        raise HarnessValidationError(
            "wave completion has no unique canonical admission",
            code="CAPACITY_RESERVATION_INVALID",
        )
    admitted_wave = DispatchWave.from_dict(
        thaw_mapping(admissions[0].payload["wave"])
    )
    admitted = {
        reservation.task_id: reservation
        for reservation in admitted_wave.packing.reservations
    }
    reservation_states = payload.get("reservation_states")
    child_states = payload.get("child_states")
    if not isinstance(reservation_states, Mapping) or not isinstance(child_states, Mapping):
        raise HarnessValidationError(
            "wave completion settlement maps are invalid",
            code="CAPACITY_RESERVATION_INVALID",
        )
    if set(reservation_states) != set(admitted_wave.task_ids):
        raise HarnessValidationError(
            "wave completion must report every admitted reservation state",
            code="CAPACITY_RESERVATION_INVALID",
        )
    try:
        normalized_reservation_states = {
            task_id: ReservationState(state)
            for task_id, state in reservation_states.items()
        }
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "wave completion contains an unknown reservation state",
            code="CAPACITY_RESERVATION_INVALID",
        ) from exc
    expected_settlement_task_ids = {
        task_id
        for task_id, state in normalized_reservation_states.items()
        if state
        in {
            ReservationState.CONSUMED,
            ReservationState.RELEASED,
        }
    }
    supplied_settlement_task_ids = {item.task_id for item in settlements}
    if supplied_settlement_task_ids != expected_settlement_task_ids:
        raise HarnessValidationError(
            "capacity settlements must exactly cover every released reservation",
            code="CAPACITY_RESERVATION_INVALID",
            details={
                "expected_task_ids": sorted(expected_settlement_task_ids),
                "actual_task_ids": sorted(supplied_settlement_task_ids),
            },
        )
    history_records = []
    for event in history:
        raw_record = event.payload.get("history_record")
        if event.event_type != "TASK_ATTEMPT_RECORDED" or not isinstance(raw_record, Mapping):
            continue
        history_records.append(TaskAttemptHistoryRecord.from_dict(raw_record))
    terminal_outcomes = {
        TaskAttemptOutcome.ACCEPTED,
        TaskAttemptOutcome.REJECTED,
        TaskAttemptOutcome.FAILED,
        TaskAttemptOutcome.CANCELLED,
        TaskAttemptOutcome.RECLAIMED,
    }
    released = {pool.pool_id: 0 for pool in before.pools}
    for settlement in settlements:
        original = admitted.get(settlement.task_id)
        if (
            original is None
            or settlement.state
            not in {ReservationState.CONSUMED, ReservationState.RELEASED}
            or settlement.reservation_version != 2
            or replace(
                settlement,
                state=ReservationState.RESERVED,
                reservation_version=1,
            )
            != original
            or reservation_states.get(settlement.task_id) != settlement.state.value
            or child_states.get(settlement.task_id)
            not in {TaskLifecycle.SUCCEEDED.value, TaskLifecycle.FAILED.value}
        ):
            raise HarnessValidationError(
                "wave completion settlement differs from admitted reservation",
                code="CAPACITY_RESERVATION_INVALID",
                details={"task_id": settlement.task_id},
            )
        matching_records = [
            record
            for record in history_records
            if record.task_id == settlement.task_id
            and record.wave is not None
            and record.wave.get("wave_id") == wave_id
            and record.outcome in terminal_outcomes
        ]
        if not matching_records or (
            admitted_wave.execution_mode == "SUPERVISED"
            and not any(
                record.terminal_receipt is not None
                and record.terminal_receipt.get("termination_confirmed") is True
                for record in matching_records
            )
        ):
            raise HarnessValidationError(
                "capacity release lacks confirmed terminal attempt evidence",
                code="CAPACITY_RESERVATION_INVALID",
                details={"task_id": settlement.task_id},
            )
        for pool_id, quantity in original.allocations.items():
            if pool_id not in released:
                raise HarnessValidationError(
                    "settlement references a pool outside the current scope",
                    code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
                )
            released[pool_id] += quantity

    before_pools = {pool.pool_id: pool for pool in before.pools}
    after_pools = {pool.pool_id: pool for pool in capacity_after.pools}
    if tuple(before_pools) != tuple(after_pools):
        raise HarnessValidationError(
            "capacity settlement changes the pinned pool set",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        )
    for pool_id, before_pool in before_pools.items():
        after_pool = after_pools[pool_id]
        quantity = released[pool_id]
        if quantity > before_pool.reserved or (
            after_pool.capacity != before_pool.capacity
            or after_pool.policy_version != before_pool.policy_version
            or after_pool.policy_checksum != before_pool.policy_checksum
            or after_pool.owner_scope != before_pool.owner_scope
            or after_pool.reservation_key != before_pool.reservation_key
            or after_pool.expires_at_ms != before_pool.expires_at_ms
            or after_pool.reserved != before_pool.reserved - quantity
            or after_pool.reservation_version
            != before_pool.reservation_version + int(quantity > 0)
        ):
            raise HarnessValidationError(
                "capacity after snapshot over-releases or rewrites a pool",
                code="CAPACITY_RESERVATION_INVALID",
                details={"pool_id": pool_id},
            )
    return before, settlements


def _validate_atomic_event_batch(
    events: tuple[TaskPlanEvent, ...],
) -> tuple[TaskPlanEvent, ...]:
    """Validate the immutable identity and sequence shape of one batch."""

    if not isinstance(events, tuple):
        raise TypeError("events must be a tuple of TaskPlanEvent values")
    if not events:
        raise HarnessValidationError(
            "TaskPlan atomic event batch must not be empty",
            code="task_plan_event_batch_invalid",
        )
    if any(not isinstance(event, TaskPlanEvent) for event in events):
        raise TypeError("events must contain only TaskPlanEvent values")
    for event in events:
        _require_live_graph_only(event, "event")

    first = events[0]
    scope = (
        first.run_id,
        first.stage_id,
        first.graph_checksum,
        first.graph_id,
        first.graph_version,
        first.graph_ref,
        first.graph_schema_version,
        first.compiler_version,
        first.condition_policy_version,
        first.stage_binding_checksum,
        first.stage_identity_schema,
        first.stage_identity_checksum,
        first.plan_id,
        first.plan_version,
    )
    for offset, event in enumerate(events):
        event_scope = (
            event.run_id,
            event.stage_id,
            event.graph_checksum,
            event.graph_id,
            event.graph_version,
            event.graph_ref,
            event.graph_schema_version,
            event.compiler_version,
            event.condition_policy_version,
            event.stage_binding_checksum,
            event.stage_identity_schema,
            event.stage_identity_checksum,
            event.plan_id,
            event.plan_version,
        )
        if event_scope != scope:
            raise HarnessValidationError(
                "TaskPlan atomic event batch cannot cross a run, stage, graph, or plan",
                code="task_plan_event_scope_mismatch",
            )
        expected_sequence = first.sequence + offset
        if event.sequence != expected_sequence:
            raise HarnessValidationError(
                "TaskPlan atomic event batch sequence is not contiguous",
                code="task_plan_sequence_conflict",
                details={"expected": expected_sequence, "actual": event.sequence},
            )
    return events


def _validate_transition_projections(
    events: tuple[TaskPlanEvent, ...],
    projections: tuple[TaskPlanProjection, ...],
) -> None:
    """Validate the immutable event-to-projection correspondence of a batch."""

    if not isinstance(projections, tuple) or len(projections) != len(events):
        raise HarnessValidationError(
            "TaskPlan transition batch requires one projection per event",
            code="task_plan_projection_mismatch",
        )
    for event, projection in zip(events, projections, strict=True):
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projections must contain only TaskPlanProjection values")
        _require_live_graph_only(projection, "projection")
        if projection.last_sequence != event.sequence:
            raise HarnessValidationError(
                "projection sequence must match its causal event",
                code="task_plan_sequence_conflict",
                details={
                    "event_sequence": event.sequence,
                    "projection_sequence": projection.last_sequence,
                },
            )


def _classify_atomic_event_batch_history(
    events: tuple[TaskPlanEvent, ...],
    history: Sequence[TaskPlanEvent],
) -> bool:
    """Return whether a complete matching batch is already durable.

    A mixed batch, where only a prefix is present, is never safe to extend:
    it is evidence of an interrupted or conflicting prior transaction.
    """

    existing_count = len(history)
    present = False
    missing = False
    for event in events:
        if event.sequence <= existing_count:
            current = history[event.sequence - 1]
            if current.event_checksum != event.event_checksum:
                raise HarnessValidationError(
                    "TaskPlan sequence already contains different content",
                    code="task_plan_sequence_conflict",
                )
            present = True
        else:
            missing = True
    if present and missing:
        raise HarnessValidationError(
            "TaskPlan atomic event batch is only partially present",
            code="task_plan_event_history_conflict",
        )
    if present:
        return True

    expected_sequence = existing_count + 1
    if events[0].sequence != expected_sequence:
        raise HarnessValidationError(
            "TaskPlan event sequence is not monotonic",
            code="task_plan_sequence_conflict",
            details={"expected": expected_sequence, "actual": events[0].sequence},
        )
    return False


def _require_live_graph_only(value: Any, model: str) -> None:
    """Reject legacy contracts before they can mutate the live TaskPlan log."""

    if getattr(value, "is_graph_only", False):
        return
    raise HarnessValidationError(
        "live TaskPlan operations require the Graph v2 identity",
        code="legacy_task_plan_live_write_forbidden",
        details={"model": model},
    )


def _projection_for_plan(plan: ValidatedTaskPlan, *, sequence: int, previous: TaskPlanProjection | None = None) -> TaskPlanProjection:
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

    previous_by_id = {item.task_id: item for item in previous.tasks} if previous is not None else {}
    states = []
    for item in plan.tasks:
        old = previous_by_id.get(item.task_id)
        if old is not None and old.task_definition_checksum == item.task_definition_checksum and old.status in {TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED, TaskLifecycle.SKIPPED, TaskLifecycle.BLOCKED_DEPENDENCY}:
            states.append(old)
        else:
            states.append(
                TaskProjection(
                    task_id=item.task_id,
                    task_definition_checksum=item.task_definition_checksum,
                    status=TaskLifecycle.PENDING,
                    schema_version=GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
                )
            )
    graph_identity = {
        name: getattr(plan, name)
        for name in _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
    }
    return TaskPlanProjection(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        graph_checksum=plan.graph_checksum,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_checksum=plan.plan_checksum,
        policy_ref=plan.policy_ref,
        tasks=tuple(states),
        consumed_budget=(previous.consumed_budget if previous is not None else TaskPlanBudgetLedger.for_plan(plan).snapshot()),
        last_sequence=sequence,
        schema_version=GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
        **graph_identity,
    )


def _replacement_mapping(patch: PlanPatch) -> dict[str, str]:
    """Return the explicit old-to-new task mapping carried by a patch."""

    mapping: dict[str, str] = {}
    for operation in patch.operations:
        if operation.operation.value != "ADD_REPLACEMENT_TASK":
            continue
        if operation.target_task_id is None or operation.replacement_task is None:
            raise HarnessValidationError(
                "replacement operation is incomplete",
                code="task_plan_patch_operation_not_allowed",
            )
        if operation.target_task_id in mapping:
            raise HarnessValidationError(
                "a patch may replace each task only once",
                code="task_plan_patch_duplicate_target",
                details={"task_id": operation.target_task_id},
            )
        mapping[operation.target_task_id] = operation.replacement_task.task_id
    return mapping


def _validate_patch_transition_targets(
    current: ValidatedTaskPlan,
    next_plan: ValidatedTaskPlan,
    *,
    current_projection: TaskPlanProjection,
    replacements: Mapping[str, str],
    skipped_task_ids: tuple[str, ...],
) -> None:
    """Keep direct store callers inside the same patch state boundary.

    The runner performs full patch graph validation.  The store is still an
    independent mutation boundary, so it must not allow callers to skip a
    terminal/active task or replace a required output through a forged API
    call.
    """

    if not current_projection.matches_plan_identity(current):
        raise HarnessValidationError(
            "patched plan base projection does not match the accepted plan",
            code="task_plan_projection_identity_mismatch",
        )
    current_states = {item.task_id: item for item in current_projection.tasks}
    current_definitions = {item.task_id: item for item in current.tasks}
    next_definitions = {item.task_id: item for item in next_plan.tasks}
    allowed_states = {
        TaskLifecycle.PENDING,
        TaskLifecycle.READY,
        TaskLifecycle.FAILED,
    }
    if set(replacements).intersection(skipped_task_ids):
        raise HarnessValidationError(
            "a patch cannot replace and skip the same task",
            code="task_plan_patch_duplicate_target",
        )
    for task_id in skipped_task_ids:
        definition = current_definitions.get(task_id)
        state = current_states.get(task_id)
        if definition is None or state is None:
            raise HarnessValidationError(
                "patched plan skip references unknown task",
                code="task_plan_unknown_task",
                details={"task_id": task_id},
            )
        if state.status not in allowed_states:
            raise HarnessValidationError(
                "only pending tasks may be skipped",
                code="task_plan_patch_task_not_pending",
                details={"task_id": task_id, "status": state.status.value},
            )
        if definition.output_role in current.required_output_roles:
            raise HarnessValidationError(
                "required output task cannot be skipped",
                code="task_plan_patch_required_role",
                details={"task_id": task_id, "output_role": definition.output_role},
            )
    for replaced_task_id, replacement_task_id in replacements.items():
        old_definition = current_definitions.get(replaced_task_id)
        old_state = current_states.get(replaced_task_id)
        replacement = next_definitions.get(replacement_task_id)
        if old_definition is None or old_state is None or replacement is None:
            raise HarnessValidationError(
                "patched plan replacement references unknown task",
                code="task_plan_unknown_task",
                details={
                    "replaced_task_id": replaced_task_id,
                    "replacement_task_id": replacement_task_id,
                },
            )
        if old_state.status not in allowed_states:
            raise HarnessValidationError(
                "replacement may only target an unstarted or failed task",
                code="task_plan_patch_task_not_pending",
                details={
                    "task_id": replaced_task_id,
                    "status": old_state.status.value,
                },
            )
        if replacement.output_role != old_definition.output_role:
            raise HarnessValidationError(
                "replacement task must preserve the target output role",
                code="task_plan_replacement_output_role_mismatch",
                details={
                    "target_task_id": replaced_task_id,
                    "target_role": old_definition.output_role,
                    "replacement_task_id": replacement_task_id,
                    "replacement_role": replacement.output_role,
                },
            )


def _plan_contains_task_version(plans: Mapping[tuple[str, str, int], ValidatedTaskPlan], current: ValidatedTaskPlan, result: TaskResultRecord) -> bool:
    if result.plan_id == current.plan_id and result.plan_version == current.version:
        return True
    if result.run_id != current.run_id or result.stage_id != current.stage_id or result.plan_version >= current.version:
        return False
    ancestor = plans.get((current.run_id, current.stage_id, result.plan_version))
    if ancestor is None:
        return False
    return any(item.task_id == result.task_id and item.task_definition_checksum == result.task_checksum for item in ancestor.tasks)


def _candidate_event(
    candidate: PlanCandidate,
    event_type: str,
    sequence: int,
    *,
    reason_code: str | None = None,
    submission: CandidateSubmission | None = None,
) -> TaskPlanEvent:
    payload: dict[str, Any] = {"candidate_ref": candidate.candidate_checksum}
    if submission is not None:
        _require_submission_scope(candidate, submission.identity)
        if submission.candidate_ref != candidate.candidate_checksum:
            raise HarnessValidationError(
                "candidate submission reference does not match candidate",
                code="CANDIDATE_IDEMPOTENCY_CONFLICT",
            )
        payload["submission"] = submission.to_dict()
    return TaskPlanEvent(
        event_type,
        **_task_plan_event_identity_kwargs(candidate),
        input_checksum=candidate.candidate_checksum,
        reason_code=reason_code,
        payload=payload,
        sequence=sequence,
    )


def _plan_event(plan: ValidatedTaskPlan, event_type: str, sequence: int) -> TaskPlanEvent:
    return TaskPlanEvent(
        event_type,
        **_task_plan_event_identity_kwargs(plan),
        plan_id=plan.plan_id,
        plan_version=plan.version,
        input_checksum=plan.plan_checksum,
        payload={"plan_ref": plan.plan_checksum, "policy_ref": plan.policy_ref},
        sequence=sequence,
    )


def _task_plan_event_identity_kwargs(
    value: PlanCandidate | ValidatedTaskPlan | TaskPlanStageIdentity,
) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "run_id": value.run_id,
        "stage_id": value.stage_id,
        "graph_checksum": value.graph_checksum,
        "schema_version": TASK_PLAN_EVENT_SCHEMA_V3,
    }
    graph_identity = {
        name: getattr(value, name)
        for name in _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
        if name not in {"stage_identity_schema", "stage_identity_checksum"}
    }
    graph_identity.update(
        {
            "stage_identity_schema": (
                value.schema_version
                if isinstance(value, TaskPlanStageIdentity)
                else value.stage_identity_schema
            ),
            "stage_identity_checksum": (
                value.identity_checksum
                if isinstance(value, TaskPlanStageIdentity)
                else value.stage_identity_checksum
            ),
        }
    )
    identity.update(graph_identity)
    return identity


def _result_event(
    result: TaskResultRecord,
    event_type: str,
    sequence: int,
    *,
    plan: ValidatedTaskPlan,
) -> TaskPlanEvent:
    if not result.matches_plan_identity(plan):
        raise HarnessValidationError(
            "task result identity does not match accepted plan",
            code="task_plan_result_identity_mismatch",
        )
    return TaskPlanEvent.for_plan(
        event_type,
        plan,
        task_id=result.task_id,
        task_instance_id=result.task_instance_id,
        attempt=result.attempt,
        input_checksum=result.task_checksum,
        output_refs=result.output_refs,
        reason_code=result.error_code,
        payload={
            "result_ref": result.result_ref,
            "result_checksum": result.result_checksum,
            "gate_refs": list(result.verified_gate_refs),
            "gate_evidence_refs": list(result.gate_evidence_refs),
            "transcript_ref": result.transcript_ref,
            "transcript_checksum": result.transcript_checksum,
            "subagent_output_ref": result.subagent_output_ref,
            "subagent_output_checksum": result.subagent_output_checksum,
        },
        sequence=sequence,
    )


def _terminal_result_event(
    result: TaskResultRecord,
    event_type: str,
    sequence: int,
    *,
    plan: ValidatedTaskPlan,
) -> TaskPlanEvent:
    if not result.matches_plan_identity(plan):
        raise HarnessValidationError(
            "task result identity does not match accepted plan",
            code="task_plan_result_identity_mismatch",
        )
    return TaskPlanEvent.for_plan(
        event_type,
        plan,
        task_id=result.task_id,
        task_instance_id=result.task_instance_id,
        attempt=result.attempt,
        input_checksum=result.result_checksum,
        output_refs=result.output_refs,
        reason_code=result.error_code,
        payload={
            "result_ref": result.result_ref,
            "result_checksum": result.result_checksum,
            "gate_refs": list(result.verified_gate_refs),
            "gate_evidence_refs": list(result.gate_evidence_refs),
            "transcript_ref": result.transcript_ref,
            "transcript_checksum": result.transcript_checksum,
            "subagent_output_ref": result.subagent_output_ref,
            "subagent_output_checksum": result.subagent_output_checksum,
        },
        sequence=sequence,
    )


def _validate_result_usage(result: TaskResultRecord, definition: Any) -> None:
    from framework.harness.task_plan.budget_ledger import result_budget_usage

    result_budget_usage(result.usage, definition.normalized_budget.to_dict())


def _require_subagent_result_evidence(
    result: TaskResultRecord,
    definition: Any,
) -> None:
    evidence = (
        result.transcript_ref,
        result.transcript_checksum,
        result.subagent_output_ref,
        result.subagent_output_checksum,
    )
    if definition.subagent_id is not None:
        if not all(item is not None for item in evidence):
            raise HarnessValidationError(
                "subagent task result requires durable transcript evidence",
                code="task_plan_subagent_evidence_required",
            )
    elif any(item is not None for item in evidence):
        raise HarnessValidationError(
            "non-subagent task result must not carry subagent evidence",
            code="task_plan_unexpected_subagent_evidence",
        )


def _settle_result_budget(
    snapshot: Mapping[str, Any],
    definition: Any,
    result: TaskResultRecord,
) -> dict[str, Any]:
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

    _validate_result_usage(result, definition)
    return TaskPlanBudgetLedger.from_snapshot(snapshot).settle(
        result, expected_allocation=definition.normalized_budget.to_dict(),
    ).snapshot()


def _require_projection_transition_identity(
    current: TaskPlanProjection,
    proposed: TaskPlanProjection,
) -> None:
    identity_fields = (
        "schema_version",
        "run_id",
        "stage_id",
        "graph_checksum",
        "plan_id",
        "plan_version",
        "plan_checksum",
        "policy_ref",
    )
    graph_identity_fields = _GRAPH_ONLY_TASK_PLAN_IDENTITY_FIELDS
    if any(
        getattr(current, name) != getattr(proposed, name)
        for name in (*identity_fields, *graph_identity_fields)
    ):
        raise HarnessValidationError(
            "projection transition changed accepted plan identity",
            code="task_plan_projection_mismatch",
        )


def _require_event_matches_plan(
    event: TaskPlanEvent,
    plan: ValidatedTaskPlan,
) -> None:
    if not event.matches_contract_identity(plan):
        raise HarnessValidationError(
            "TaskPlan event identity does not match the accepted plan",
            code="task_plan_event_identity_mismatch",
        )
    if event.plan_id is not None and (
        event.plan_id != plan.plan_id or event.plan_version != plan.version
    ):
        raise HarnessValidationError(
            "TaskPlan event plan version does not match the accepted plan",
            code="task_plan_event_identity_mismatch",
        )
    if event.event_type in {"TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED"}:
        from framework.harness.task_plan.parallel_admission import validate_group_plan_binding

        group = event.payload.get("group")
        if not isinstance(group, Mapping):
            raise HarnessValidationError("group admission snapshot is missing", code="TASK_GROUP_SCOPE_MISMATCH")
        validate_group_plan_binding(group, plan)
    if event.event_type == "TASK_READY":
        raw = event.payload.get("logical_readiness")
        if not isinstance(raw, Mapping):
            raise HarnessValidationError(
                "logical TASK_READY event is missing readiness evidence",
                code="task_plan_logical_readiness_missing",
            )
        _validate_logical_readiness_order(
            LogicalTaskReadiness.from_dict(raw),
            plan,
        )


def _validate_logical_readiness_order(
    readiness: LogicalTaskReadiness,
    plan: ValidatedTaskPlan,
) -> None:
    """Apply the single pinned scheduler ordering rule to wire evidence."""

    from framework.harness.task_plan.scheduler import _task_depths

    definitions = {item.task_id: item for item in plan.tasks}
    readiness_definition = definitions.get(readiness.task_id)
    if (
        readiness_definition is None
        or readiness_definition.task_definition_checksum
        != readiness.task_definition_checksum
    ):
        raise HarnessValidationError(
            "logical TASK_READY identity differs from the accepted definition",
            code="task_plan_logical_readiness_identity_mismatch",
        )
    if any(task_id not in definitions for task_id in readiness.logical_ready_order):
        raise HarnessValidationError(
            "logical TASK_READY order references an unknown task",
            code="task_plan_replay_unknown_task",
        )
    depths = _task_depths(definitions)
    expected = tuple(
        sorted(
            readiness.logical_ready_order,
            key=lambda task_id: (
                definitions[task_id].priority,
                depths[task_id],
                task_id,
                definitions[task_id].task_definition_checksum,
            ),
        )
    )
    if readiness.logical_ready_order != expected:
        raise HarnessValidationError(
            "logical TASK_READY order violates the pinned scheduler key",
            code="task_plan_replay_ready_order_mismatch",
        )


def _require_submission_scope(
    candidate: PlanCandidate,
    identity: CandidateDedupIdentity,
) -> None:
    if (candidate.run_id, candidate.stage_id) != (
        identity.run_id,
        identity.stage_id,
    ):
        raise HarnessValidationError(
            "candidate submission identity is outside the candidate scope",
            code="candidate_submission_event_invalid",
        )


def _require_same_submission(
    existing: CandidateSubmission,
    submitted: CandidateSubmission,
) -> None:
    if (
        existing.candidate_checksum != submitted.candidate_checksum
        or existing.candidate_ref != submitted.candidate_ref
    ):
        raise HarnessValidationError(
            "candidate submission dedup identity was reused with a different payload",
            code="CANDIDATE_IDEMPOTENCY_CONFLICT",
            details={"dedup_key": existing.identity.dedup_key},
        )


def _require_initial_plan_submission_binding(
    plan: ValidatedTaskPlan,
    submissions: tuple[CandidateSubmission, ...],
) -> None:
    """Bind a submitted initial plan to its exact durable parent action."""

    if plan.version != 1 or not submissions:
        return
    source_matches = tuple(
        item for item in submissions if item.candidate_ref == plan.source_candidate_ref
    )
    exact_matches = tuple(
        item
        for item in source_matches
        if item.plan_id == plan.plan_id and item.accepted_at == plan.accepted_at
    )
    if len(exact_matches) != 1:
        raise HarnessValidationError(
            "initial TaskPlan does not match its durable candidate submission",
            code="task_plan_submission_binding_conflict",
            details={
                "source_candidate_ref": plan.source_candidate_ref,
                "plan_id": plan.plan_id,
            },
        )


__all__ = [
    "InMemoryTaskPlanStore",
    "TASK_PLAN_EVENT_SCHEMA",
    "TASK_PLAN_EVENT_SCHEMA_V2",
    "TASK_PLAN_EVENT_SCHEMA_V3",
    "TASK_PLAN_EVENT_SCHEMAS",
    "TASK_PLAN_EVENT_TYPES",
    "LOGICAL_TASK_READINESS_SCHEMA",
    "LogicalTaskReadiness",
    "TASK_QUEUE_ADMISSION_SCHEMA",
    "TaskQueueAdmissionEvidence",
    "TASK_PLAN_RESULT_SCHEMA_V3",
    "TaskPlanEvent",
    "TaskPlanStorePort",
    "TaskResultRecord",
]
