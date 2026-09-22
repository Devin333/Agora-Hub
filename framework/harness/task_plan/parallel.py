"""Bounded Harness-owned fan-out/fan-in orchestration.

The coordinator owns admission, capacity, wave ordering and joining. Worker
callbacks only produce already verified ``TaskResultRecord`` values; they do
not receive routing, policy or sibling context.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
from threading import Event, Lock, RLock
from types import MappingProxyType
from time import monotonic, sleep
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from framework.agent.models.orchestration import ParentObservationLimits, truncate_observation_text
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import (
    ChildAgentHandle,
    ChildAgentOperationResult,
    ChildAgentSpawnRequest,
    ChildAgentState,
    ChildAgentSupervisorError,
    ChildAgentTerminalReceipt,
    ChildAgentOperationConflict,
    ChildAgentSupervisor,
)
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_reference,
    exact_keys,
    frozen_mapping,
    identifier,
    reference,
    stable_text_tuple,
    thaw_mapping,
)
from framework.harness.task_plan.models import (
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.parallel_lifecycle import (
    DispatchGroupState,
    DispatchWaveState,
    DispatchWaveTerminalOutcome,
    ReservationState,
    SideEffectClass,
    _GROUP_TRANSITIONS,
)
from framework.harness.task_plan.parallel_state import validate_group_transition, validate_wave_transition
from framework.harness.task_plan.capacity import (
    CapacityPool,
    CapacityScopeSnapshot,
    FirstFitPacking,
    PoolReservation,
    TaskCapacityDemand,
    capacity_now_ms,
    pack_first_fit,
)
from framework.harness.task_plan.capacity_policy import TaskCapacityPolicy
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.subagents.execution_control import (
    ChildExecutionControl,
    bind_child_execution_control,
)
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.store import TaskResultRecord
from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord, TaskAttemptOutcome
from framework.shared.graph_identity import GraphExecutionIdentity


PARALLEL_DISPATCH_REQUEST_SCHEMA = "agora.harness-parallel-dispatch-request/v3"
PARALLEL_DISPATCH_RESULT_SCHEMA = "agora.harness-parallel-dispatch-result/v1"
DISPATCH_GROUP_SCHEMA = "agora.harness-dispatch-group/v3"
DISPATCH_WAVE_SCHEMA = "agora.harness-dispatch-wave/v4"
TASK_RESERVATION_SCHEMA = "agora.harness-task-reservation/v1"
PARENT_OBSERVATION_SCHEMA = "agora.harness-parent-observation/v1"
CAPACITY_WAITING_EVIDENCE_SCHEMA = "agora.task-group-capacity-waiting/v1"


class JoinPolicy(StrEnum):
    WAIT_ALL = "wait_all"
    FAIL_FAST = "fail_fast"


def _integer_deadline(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HarnessValidationError(
            f"{name} must be a positive integer",
            code="TASK_GROUP_DEADLINE_INVALID",
        )
    return value


def _capacity_waiting_evidence(
    group: DispatchGroup,
    packing: FirstFitPacking,
    *,
    budget_checksum: str,
) -> dict[str, Any]:
    """Build one stable, non-terminal capacity-wait fact."""

    if packing.selected or not packing.overflow:
        raise HarnessValidationError(
            "capacity waiting requires a zero-selection packing pass",
            code="TASK_GROUP_CAPACITY_WAIT_INVALID",
        )
    payload = {
        "schema_version": CAPACITY_WAITING_EVIDENCE_SCHEMA,
        "group_id": group.group_id,
        "group_checksum": group.group_checksum,
        "logical_ready_order": list(packing.ready_order),
        "reasons": thaw_mapping(packing.reasons),
        "capacity_snapshot": (
            packing.capacity_before.to_dict()
            if packing.capacity_before is not None else None
        ),
        "budget_checksum": checksum(budget_checksum, "budget_checksum"),
        "packing_checksum": packing.packing_checksum,
        "absolute_deadline_ms": group.absolute_deadline_ms,
    }
    return {
        **payload,
        "waiting_key": "capacity-wait:" + canonical_payload_checksum(payload),
    }


@dataclass(frozen=True, slots=True)
class ParallelEventSink:
    """Canonical event writer with an explicit atomic admission boundary."""

    append: Callable[[Mapping[str, Any]], Any]
    append_batch: Callable[[tuple[Mapping[str, Any], ...]], Any]

    def __call__(self, event: Mapping[str, Any]) -> Any:
        return self.append(event)


def spawn_operation_key(group_id: str, wave_id: str, task_instance_id: str, attempt: int) -> str:
    return f"parallel:{group_id}:{wave_id}:{task_instance_id}:{attempt}"


@dataclass(frozen=True, slots=True)
class TaskReservation:
    task_id: str
    idempotency_key: str
    budget: Mapping[str, int]
    state: ReservationState | str = ReservationState.RESERVED
    capacity_allocations: Mapping[str, int] = field(default_factory=dict)
    capacity_policy_checksums: Mapping[str, str] = field(default_factory=dict)
    schema_version: str = TASK_RESERVATION_SCHEMA
    capacity_reservation: PoolReservation | Mapping[str, Any] | None = None
    reservation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        object.__setattr__(self, "idempotency_key", identifier(self.idempotency_key, "idempotency_key"))
        if not isinstance(self.budget, Mapping):
            raise HarnessValidationError("reservation budget must be an object", code="PLAN_SCHEMA_INVALID")
        normalized: dict[str, int] = {}
        for key, value in self.budget.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessValidationError("reservation budget must be non-negative", code="PLAN_SCHEMA_INVALID")
            normalized[str(key)] = value
        object.__setattr__(self, "budget", frozen_mapping(normalized, "reservation.budget"))
        if not isinstance(self.capacity_allocations, Mapping):
            raise HarnessValidationError(
                "reservation capacity allocations must be an object",
                code="CAPACITY_RESERVATION_INVALID",
            )
        capacity_allocations: dict[str, int] = {}
        for pool_id, quantity in self.capacity_allocations.items():
            normalized_pool_id = identifier(str(pool_id), "pool_id")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise HarnessValidationError(
                    "reservation capacity allocation must be positive",
                    code="CAPACITY_RESERVATION_INVALID",
                )
            capacity_allocations[normalized_pool_id] = quantity
        object.__setattr__(
            self,
            "capacity_allocations",
            frozen_mapping(capacity_allocations, "reservation.capacity_allocations"),
        )
        if not isinstance(self.capacity_policy_checksums, Mapping):
            raise HarnessValidationError(
                "reservation capacity policy checksums must be an object",
                code="CAPACITY_RESERVATION_INVALID",
            )
        capacity_policy_checksums: dict[str, str] = {}
        for pool_id, policy_checksum in self.capacity_policy_checksums.items():
            normalized_pool_id = identifier(str(pool_id), "pool_id")
            try:
                normalized_checksum = checksum(policy_checksum, "capacity_policy_checksum")
            except HarnessValidationError as exc:
                raise HarnessValidationError(
                    "reservation capacity policy checksum is invalid",
                    code="CAPACITY_RESERVATION_INVALID",
                ) from exc
            capacity_policy_checksums[normalized_pool_id] = normalized_checksum
        if set(capacity_policy_checksums) != set(capacity_allocations):
            raise HarnessValidationError(
                "reservation capacity policy checksums must match allocations",
                code="CAPACITY_RESERVATION_INVALID",
            )
        object.__setattr__(
            self,
            "capacity_policy_checksums",
            frozen_mapping(capacity_policy_checksums, "reservation.capacity_policy_checksums"),
        )
        object.__setattr__(self, "state", ReservationState(self.state))
        if self.schema_version != TASK_RESERVATION_SCHEMA:
            raise HarnessValidationError("unsupported reservation schema", code="PLAN_SCHEMA_INVALID")
        pool_reservation = self.capacity_reservation
        if isinstance(pool_reservation, Mapping):
            pool_reservation = PoolReservation.from_dict(pool_reservation)
        if pool_reservation is not None:
            if not isinstance(pool_reservation, PoolReservation) or (
                pool_reservation.task_id != self.task_id
                or pool_reservation.reservation_key != self.idempotency_key
                or pool_reservation.allocations != self.capacity_allocations
                or pool_reservation.policy_checksums != self.capacity_policy_checksums
                or pool_reservation.state is not self.state
            ):
                raise HarnessValidationError("capacity reservation differs from task reservation", code="CAPACITY_RESERVATION_CONFLICT")
        object.__setattr__(self, "capacity_reservation", pool_reservation)
        object.__setattr__(self, "reservation_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {"schema_version": self.schema_version, "task_id": self.task_id, "idempotency_key": self.idempotency_key, "budget": thaw_mapping(self.budget), "state": self.state.value}
        if self.capacity_allocations:
            value["capacity_allocations"] = thaw_mapping(self.capacity_allocations)
            value["capacity_policy_checksums"] = thaw_mapping(self.capacity_policy_checksums)
        if self.capacity_reservation is not None:
            value["capacity_reservation"] = self.capacity_reservation.to_dict()
        if include_checksum:
            value["reservation_checksum"] = self.reservation_checksum
        return value

    def settled(self, state: ReservationState | str) -> TaskReservation:
        target = ReservationState(state)
        return replace(self, state=target, capacity_reservation=(
            self.capacity_reservation.settled(target, reservation_key=self.idempotency_key, expected_version=1)
            if self.capacity_reservation is not None else None
        ))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskReservation":
        payload = exact_keys(
            value,
            required=frozenset({
                "schema_version", "task_id", "idempotency_key", "budget", "state",
                "reservation_checksum",
            }),
            optional=frozenset({"capacity_allocations", "capacity_policy_checksums", "capacity_reservation"}),
            model=cls.__name__,
        )
        payload.setdefault("capacity_allocations", {})
        payload.setdefault("capacity_policy_checksums", {})
        supplied_checksum = checksum(payload.pop("reservation_checksum"), "reservation_checksum")
        try:
            reservation = cls(**payload)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskReservation payload is invalid",
                code="TASK_RESERVATION_SCHEMA_INVALID",
            ) from exc
        if supplied_checksum != reservation.reservation_checksum:
            raise HarnessValidationError(
                "TaskReservation checksum does not match canonical content",
                code="TASK_RESERVATION_CHECKSUM_MISMATCH",
            )
        return reservation


@dataclass(frozen=True, slots=True)
class DispatchGroup:
    run_id: str
    stage_id: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    policy_ref: str
    policy_checksum: str
    parent_graph_identity: GraphExecutionIdentity
    admission_policy_checksum: str
    task_ids: tuple[str, ...]
    required_output_roles: tuple[str, ...]
    join_policy: JoinPolicy | str = JoinPolicy.WAIT_ALL
    max_waves: int = 16
    max_parallelism: int = 3
    budget_envelope: Mapping[str, int] = field(default_factory=dict)
    admitted_at_ms: int = 0
    absolute_deadline_ms: int = 0
    correlation_id: str = ""
    state: DispatchGroupState | str = DispatchGroupState.PLANNED
    schema_version: str = DISPATCH_GROUP_SCHEMA
    group_id: str = field(init=False)
    group_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        object.__setattr__(self, "stage_id", identifier(self.stage_id, "stage_id"))
        object.__setattr__(self, "plan_id", identifier(self.plan_id, "plan_id"))
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version < 1:
            raise HarnessValidationError("plan_version must be positive", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "plan_checksum", checksum(self.plan_checksum, "plan_checksum"))
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
        object.__setattr__(self, "policy_checksum", checksum(self.policy_checksum, "policy_checksum"))
        if not isinstance(self.parent_graph_identity, GraphExecutionIdentity):
            raise TypeError("parent_graph_identity must be GraphExecutionIdentity")
        if self.parent_graph_identity.run_id != self.run_id:
            raise HarnessValidationError(
                "parent Graph identity does not match dispatch group run",
                code="TASK_GROUP_SCOPE_MISMATCH",
            )
        object.__setattr__(self, "admission_policy_checksum", checksum(
            self.admission_policy_checksum,
            "admission_policy_checksum",
        ))
        ids = tuple(identifier(item, "task_id") for item in self.task_ids)
        if not ids or len(ids) != len(set(ids)):
            raise HarnessValidationError("group task ids must be unique and non-empty", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "task_ids", tuple(sorted(ids)))
        object.__setattr__(self, "required_output_roles", stable_text_tuple(self.required_output_roles, "required_output_roles", allow_empty=False))
        object.__setattr__(self, "join_policy", JoinPolicy(self.join_policy))
        if isinstance(self.max_waves, bool) or not isinstance(self.max_waves, int) or self.max_waves < 1:
            raise HarnessValidationError("max_waves must be positive", code="PLAN_SCHEMA_INVALID")
        if isinstance(self.max_parallelism, bool) or not isinstance(self.max_parallelism, int) or self.max_parallelism < 1:
            raise HarnessValidationError("max_parallelism must be positive", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "state", DispatchGroupState(self.state))
        if self.schema_version != DISPATCH_GROUP_SCHEMA:
            raise HarnessValidationError("unsupported dispatch group schema", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "correlation_id", identifier(self.correlation_id or f"group-{self.plan_id}", "correlation_id"))
        object.__setattr__(self, "budget_envelope", _non_negative_mapping(self.budget_envelope, "budget_envelope"))
        _integer_deadline(self.admitted_at_ms, "admitted_at_ms")
        _integer_deadline(self.absolute_deadline_ms, "absolute_deadline_ms")
        if self.absolute_deadline_ms <= self.admitted_at_ms:
            raise HarnessValidationError(
                "dispatch group deadline must follow admission",
                code="TASK_GROUP_DEADLINE_INVALID",
            )
        projection = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "policy_ref": self.policy_ref,
            "policy_checksum": self.policy_checksum,
            "parent_graph_identity": self.parent_graph_identity.to_dict(),
            "admission_policy_checksum": self.admission_policy_checksum,
            "task_ids": list(self.task_ids),
            "required_output_roles": list(self.required_output_roles),
            "join_policy": self.join_policy.value,
            "max_waves": self.max_waves,
            "max_parallelism": self.max_parallelism,
            "budget_envelope": thaw_mapping(self.budget_envelope),
            "admitted_at_ms": self.admitted_at_ms,
            "absolute_deadline_ms": self.absolute_deadline_ms,
            "correlation_id": self.correlation_id,
        }
        object.__setattr__(self, "group_checksum", canonical_payload_checksum(projection))
        group_identity = canonical_payload_checksum({
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "correlation_id": self.correlation_id,
        })
        object.__setattr__(self, "group_id", f"dg_{group_identity.removeprefix('sha256:')[:32]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "group_checksum": self.group_checksum,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "policy_ref": self.policy_ref,
            "policy_checksum": self.policy_checksum,
            "parent_graph_identity": self.parent_graph_identity.to_dict(),
            "admission_policy_checksum": self.admission_policy_checksum,
            "task_ids": list(self.task_ids),
            "required_output_roles": list(self.required_output_roles),
            "join_policy": self.join_policy.value,
            "max_waves": self.max_waves,
            "max_parallelism": self.max_parallelism,
            "budget_envelope": thaw_mapping(self.budget_envelope),
            "admitted_at_ms": self.admitted_at_ms,
            "absolute_deadline_ms": self.absolute_deadline_ms,
            "correlation_id": self.correlation_id,
            "state": self.state.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DispatchGroup":
        payload = exact_keys(
            value,
            required=frozenset({
                "schema_version", "group_id", "group_checksum", "run_id", "stage_id",
                "plan_id", "plan_version", "plan_checksum", "policy_ref", "policy_checksum",
                "parent_graph_identity", "admission_policy_checksum", "task_ids", "required_output_roles", "join_policy",
                "max_waves", "max_parallelism", "budget_envelope", "admitted_at_ms",
                "absolute_deadline_ms", "correlation_id", "state",
            }),
            model=cls.__name__,
        )
        supplied_group_id = identifier(payload.pop("group_id"), "group_id")
        supplied_checksum = checksum(payload.pop("group_checksum"), "group_checksum")
        try:
            payload["parent_graph_identity"] = GraphExecutionIdentity.from_dict(
                payload["parent_graph_identity"],
            )
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "DispatchGroup parent Graph identity is invalid",
                code="TASK_GROUP_SCHEMA_INVALID",
            ) from exc
        try:
            group = cls(**payload)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "DispatchGroup payload is invalid",
                code="TASK_GROUP_SCHEMA_INVALID",
            ) from exc
        if supplied_checksum != group.group_checksum:
            raise HarnessValidationError(
                "DispatchGroup checksum does not match canonical content",
                code="TASK_GROUP_CHECKSUM_MISMATCH",
            )
        if supplied_group_id != group.group_id:
            raise HarnessValidationError(
                "DispatchGroup id does not match canonical content",
                code="TASK_GROUP_IDENTITY_MISMATCH",
            )
        return group

    def transitioned(self, state: DispatchGroupState | str) -> "DispatchGroup":
        validate_group_transition(self.state, state)
        target = DispatchGroupState(state)
        if target is self.state:
            return self
        return replace(self, state=target)


@dataclass(frozen=True, slots=True)
class DispatchWave:
    group_id: str
    ordinal: int
    task_ids: tuple[str, ...]
    effective_parallelism: int
    reservations: tuple[TaskReservation, ...] = ()
    state: DispatchWaveState | str = DispatchWaveState.PLANNED
    terminal_outcome: DispatchWaveTerminalOutcome | str | None = None
    schema_version: str = DISPATCH_WAVE_SCHEMA
    execution_mode: str = "SUPERVISED"
    packing: FirstFitPacking | Mapping[str, Any] | None = None
    wave_id: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_id", identifier(self.group_id, "group_id"))
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise HarnessValidationError("wave ordinal must be positive", code="PLAN_SCHEMA_INVALID")
        ids = tuple(identifier(item, "task_id") for item in self.task_ids)
        if not ids or len(ids) != len(set(ids)):
            raise HarnessValidationError("wave task ids must be unique and non-empty", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "task_ids", ids)
        if isinstance(self.effective_parallelism, bool) or not isinstance(self.effective_parallelism, int) or self.effective_parallelism < 1:
            raise HarnessValidationError("wave parallelism must be positive", code="PLAN_SCHEMA_INVALID")
        reservations = tuple(self.reservations)
        if len(reservations) != len(self.task_ids) or any(not isinstance(item, TaskReservation) for item in reservations) or {item.task_id for item in reservations} != set(self.task_ids):
            raise HarnessValidationError("wave reservations must cover exactly its tasks", code="PLAN_SCHEMA_INVALID")
        if len(self.task_ids) > self.effective_parallelism:
            raise HarnessValidationError("wave exceeds its admitted parallelism", code="TASK_WAVE_CAPACITY_EXCEEDED")
        reservations_by_task = {item.task_id: item for item in reservations}
        object.__setattr__(
            self,
            "reservations",
            tuple(reservations_by_task[task_id] for task_id in ids),
        )
        packing = self.packing
        if isinstance(packing, Mapping):
            packing = FirstFitPacking.from_dict(packing)
        if (
            not isinstance(packing, FirstFitPacking)
            or packing.selected != ids
            or tuple(item.task_id for item in packing.reservations)
            != tuple(item.task_id for item in reservations if item.capacity_allocations)
        ):
            raise HarnessValidationError(
                "dispatch wave requires matching joint packing evidence",
                code="TASK_WAVE_PACKING_EVIDENCE_INVALID",
            )
        object.__setattr__(self, "packing", packing)
        object.__setattr__(self, "state", DispatchWaveState(self.state))
        if self.state is DispatchWaveState.TERMINAL:
            if self.terminal_outcome is None:
                raise HarnessValidationError(
                    "terminal wave requires a terminal outcome",
                    code="TASK_WAVE_TERMINAL_OUTCOME_REQUIRED",
                )
            object.__setattr__(self, "terminal_outcome", DispatchWaveTerminalOutcome(self.terminal_outcome))
        elif self.terminal_outcome is not None:
            raise HarnessValidationError(
                "non-terminal wave must not carry a terminal outcome",
                code="TASK_WAVE_TERMINAL_OUTCOME_INVALID",
            )
        if self.schema_version != DISPATCH_WAVE_SCHEMA:
            raise HarnessValidationError("unsupported dispatch wave schema", code="PLAN_SCHEMA_INVALID")
        if self.execution_mode not in {"SUPERVISED", "SERIAL", "INLINE_TEST"}:
            raise HarnessValidationError("unsupported wave execution mode", code="PLAN_SCHEMA_INVALID")
        if self.execution_mode == "SERIAL" and (self.effective_parallelism != 1 or len(ids) != 1):
            raise HarnessValidationError("serial wave must have one task and slot", code="TASK_GROUP_SERIAL_WAVE_INVALID")
        digest = canonical_payload_checksum(
            {
                "schema_version": self.schema_version,
                "group_id": self.group_id,
                "ordinal": self.ordinal,
                "task_ids": list(self.task_ids),
                "effective_parallelism": self.effective_parallelism,
                "execution_mode": self.execution_mode,
                "reservations": [
                    {
                        "task_id": item.task_id,
                        "idempotency_key": item.idempotency_key,
                        "budget": thaw_mapping(item.budget),
                        **({"capacity_allocations": thaw_mapping(item.capacity_allocations)} if item.capacity_allocations else {}),
                        **({"capacity_policy_checksums": thaw_mapping(item.capacity_policy_checksums)} if item.capacity_policy_checksums else {}),
                        **({"capacity_reservation": item.capacity_reservation.admission_snapshot()} if item.capacity_reservation is not None else {}),
                    }
                    for item in self.reservations
                ],
                "packing": self.packing.to_dict(),
            }
        )
        object.__setattr__(self, "wave_id", f"dw_{digest.removeprefix('sha256:')[:32]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "wave_id": self.wave_id,
            "group_id": self.group_id,
            "ordinal": self.ordinal,
            "task_ids": list(self.task_ids),
            "effective_parallelism": self.effective_parallelism,
            "execution_mode": self.execution_mode,
            "reservations": [item.to_dict() for item in self.reservations],
            "packing": self.packing.to_dict(),
            "state": self.state.value,
            "terminal_outcome": (
                self.terminal_outcome.value if self.terminal_outcome is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DispatchWave":
        payload = exact_keys(
            value,
            required=frozenset({
                "schema_version", "wave_id", "group_id", "ordinal", "task_ids",
                "effective_parallelism", "reservations", "state", "terminal_outcome",
                "execution_mode", "packing",
            }),
            model=cls.__name__,
        )
        supplied_wave_id = identifier(payload.pop("wave_id"), "wave_id")
        reservations = payload.get("reservations")
        if not isinstance(reservations, list):
            raise HarnessValidationError(
                "DispatchWave reservations must be an array",
                code="TASK_WAVE_SCHEMA_INVALID",
            )
        payload["reservations"] = tuple(TaskReservation.from_dict(item) for item in reservations)
        try:
            wave = cls(**payload)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "DispatchWave payload is invalid",
                code="TASK_WAVE_SCHEMA_INVALID",
            ) from exc
        if supplied_wave_id != wave.wave_id:
            raise HarnessValidationError(
                "DispatchWave id does not match canonical content",
                code="TASK_WAVE_IDENTITY_MISMATCH",
            )
        return wave

    def transitioned(
        self,
        state: DispatchWaveState | str,
        *,
        terminal_outcome: DispatchWaveTerminalOutcome | str | None = None,
    ) -> "DispatchWave":
        validate_wave_transition(self.state, state)
        target = DispatchWaveState(state)
        if target is not DispatchWaveState.TERMINAL and terminal_outcome is not None:
            raise HarnessValidationError(
                "non-terminal wave must not carry a terminal outcome",
                code="TASK_WAVE_TERMINAL_OUTCOME_INVALID",
            )
        if target is self.state:
            if target is DispatchWaveState.TERMINAL and terminal_outcome not in {None, self.terminal_outcome}:
                raise HarnessValidationError(
                    "terminal wave outcome cannot be rewritten",
                    code="TASK_WAVE_INVALID_TRANSITION",
                )
            return self
        return replace(self, state=target, terminal_outcome=terminal_outcome)


@dataclass(frozen=True, slots=True)
class ParallelDispatchRequest:
    plan: ValidatedTaskPlan
    task_instances: tuple[TaskInstance, ...]
    budget_snapshot: Mapping[str, Any]
    requested_parallelism: int | None = None
    capability_capacity: int | None = None
    supervisor_capacity: int | None = None
    available_concurrency_reservations: int | None = None
    serial_fallback: bool = False
    join_policy: JoinPolicy | str = JoinPolicy.WAIT_ALL
    correlation_id: str = ""
    group_task_ids: tuple[str, ...] | None = None
    side_effect_class: SideEffectClass | str = SideEffectClass.READ_ONLY
    resource_conflict_key: str | None = None
    max_waves: int = 16
    max_tasks_per_group: int | None = None
    max_group_runtime_seconds: float = 900.0
    max_join_wait_seconds: float = 300.0
    parent_graph_identity: GraphExecutionIdentity | None = None
    schema_version: str = PARALLEL_DISPATCH_REQUEST_SCHEMA
    capacity_pools: tuple[CapacityPool, ...] = ()
    task_capacity_demands: Mapping[str, TaskCapacityDemand] = field(default_factory=dict)
    capacity_policy: TaskCapacityPolicy | None = None
    capacity_snapshot: CapacityScopeSnapshot | None = None
    group_admitted_at_ms: int | None = None
    group_absolute_deadline_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        ledger = TaskPlanBudgetLedger.from_snapshot(self.budget_snapshot)
        if (
            (ledger.run_id, ledger.stage_id, ledger.policy_ref)
            != (self.plan.run_id, self.plan.stage_id, self.plan.policy_ref)
            or dict(ledger.parent_allocation) != self.plan.limits.aggregate_task_budget.to_dict()
        ):
            raise HarnessValidationError("dispatch budget owner differs from plan", code="task_plan_budget_identity_conflict")
        object.__setattr__(self, "budget_snapshot", frozen_mapping(ledger.snapshot(), "budget_snapshot"))
        instances = tuple(self.task_instances)
        if any(not isinstance(item, TaskInstance) for item in instances):
            raise HarnessValidationError("dispatch request must contain TaskInstance values", code="PLAN_SCHEMA_INVALID")
        if any(item != task_instance_for_attempt(self.plan, item.task_id, item.attempt) for item in instances):
            raise HarnessValidationError("dispatch attempt differs from accepted task", code="task_plan_task_instance_mismatch")
        ids = tuple(item.task_id for item in instances)
        if len(ids) != len(set(ids)):
            raise HarnessValidationError("duplicate task identities in dispatch request", code="PLAN_SCHEMA_INVALID")
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0) for value in (self.requested_parallelism, self.capability_capacity, self.supervisor_capacity, self.available_concurrency_reservations)):
            raise HarnessValidationError("parallelism capacity must be non-negative", code="PLAN_SCHEMA_INVALID")
        if not isinstance(self.serial_fallback, bool):
            raise HarnessValidationError("serial_fallback must be boolean", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "task_instances", instances)
        pools = tuple(self.capacity_pools)
        if any(not isinstance(pool, CapacityPool) for pool in pools) or len({pool.pool_id for pool in pools}) != len(pools):
            raise HarnessValidationError("capacity pools must be unique CapacityPool values", code="CAPACITY_POLICY_INVALID")
        object.__setattr__(self, "capacity_pools", pools)
        capacity_snapshot = self.capacity_snapshot
        if capacity_snapshot is not None and (
            not isinstance(capacity_snapshot, CapacityScopeSnapshot)
            or capacity_snapshot.pools != tuple(sorted(pools, key=lambda item: item.pool_id))
        ):
            raise HarnessValidationError(
                "dispatch capacity scope differs from its pool snapshot",
                code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
            )
        if pools and capacity_snapshot is None:
            raise HarnessValidationError(
                "multi-pool dispatch requires an authoritative capacity scope",
                code="CAPACITY_POLICY_MISSING",
            )
        demands = dict(self.task_capacity_demands)
        if any(not isinstance(value, TaskCapacityDemand) or key != value.task_id for key, value in demands.items()):
            raise HarnessValidationError("task capacity demands must be keyed by task id", code="CAPACITY_DEMAND_INVALID")
        task_id_set = {item.task_id for item in self.plan.tasks}
        demand_task_ids = set(demands)
        if demands and not pools:
            raise HarnessValidationError(
                "capacity policy is required when task demands are configured",
                code="CAPACITY_POLICY_MISSING",
            )
        if pools and demand_task_ids != task_id_set:
            raise HarnessValidationError(
                "capacity policy must define one demand for every accepted plan task",
                code="CAPACITY_DEMAND_INVALID",
                details={
                    "missing_task_ids": sorted(task_id_set - demand_task_ids),
                    "unknown_task_ids": sorted(demand_task_ids - task_id_set),
                },
            )
        pool_ids = {pool.pool_id for pool in pools}
        unknown_pools = sorted({pool_id for demand in demands.values() for pool_id in demand.quantities if pool_id not in pool_ids})
        if unknown_pools:
            raise HarnessValidationError(
                "capacity demand references an unavailable pool policy",
                code="CAPACITY_POLICY_MISSING",
                details={"pool_ids": unknown_pools},
            )
        object.__setattr__(self, "task_capacity_demands", MappingProxyType(demands))
        if self.capacity_policy is not None:
            if not isinstance(self.capacity_policy, TaskCapacityPolicy):
                raise HarnessValidationError("dispatch capacity policy must be typed", code="CAPACITY_POLICY_INVALID")
            expected_demands = self.capacity_policy.demands_for(self.plan)
            if expected_demands != demands or not pools:
                raise HarnessValidationError("dispatch demands differ from trusted capacity policy", code="CAPACITY_DEMAND_INVALID")
        object.__setattr__(self, "join_policy", JoinPolicy(self.join_policy))
        object.__setattr__(self, "side_effect_class", SideEffectClass(self.side_effect_class))
        if self.group_task_ids is not None:
            group_ids = tuple(identifier(item, "group_task_id") for item in self.group_task_ids)
            if not group_ids or len(group_ids) != len(set(group_ids)) or not set(ids).issubset(group_ids):
                raise HarnessValidationError("group task ids must contain ready task ids", code="PLAN_SCHEMA_INVALID")
            object.__setattr__(self, "group_task_ids", tuple(sorted(group_ids)))
        if self.resource_conflict_key is not None and (not isinstance(self.resource_conflict_key, str) or not self.resource_conflict_key.strip()):
            raise HarnessValidationError("resource_conflict_key must be non-empty", code="PLAN_SCHEMA_INVALID")
        if isinstance(self.max_waves, bool) or not isinstance(self.max_waves, int) or self.max_waves < 1:
            raise HarnessValidationError("max_waves must be positive", code="PLAN_SCHEMA_INVALID")
        if self.max_tasks_per_group is not None and (
            isinstance(self.max_tasks_per_group, bool)
            or not isinstance(self.max_tasks_per_group, int)
            or self.max_tasks_per_group < 1
        ):
            raise HarnessValidationError("max_tasks_per_group must be positive", code="PLAN_SCHEMA_INVALID")
        for name in ("max_group_runtime_seconds", "max_join_wait_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > 3600:
                raise HarnessValidationError(f"{name} must be in (0, 3600]", code="PLAN_SCHEMA_INVALID")
            object.__setattr__(self, name, float(value))
        admitted_at_ms = self.group_admitted_at_ms
        absolute_deadline_ms = self.group_absolute_deadline_ms
        if admitted_at_ms is None:
            admitted_at_ms = capacity_now_ms()
        _integer_deadline(admitted_at_ms, "group_admitted_at_ms")
        if absolute_deadline_ms is None:
            absolute_deadline_ms = admitted_at_ms + int(self.max_group_runtime_seconds * 1000)
        _integer_deadline(absolute_deadline_ms, "group_absolute_deadline_ms")
        if absolute_deadline_ms <= admitted_at_ms:
            raise HarnessValidationError(
                "dispatch request deadline must follow admission",
                code="TASK_GROUP_DEADLINE_INVALID",
            )
        object.__setattr__(self, "group_admitted_at_ms", admitted_at_ms)
        object.__setattr__(self, "group_absolute_deadline_ms", absolute_deadline_ms)
        if self.parent_graph_identity is not None:
            identity = self.parent_graph_identity
            if not isinstance(identity, GraphExecutionIdentity):
                raise TypeError("parent_graph_identity must be GraphExecutionIdentity")
            if (
                identity.run_id != self.plan.run_id
                or identity.graph_id != self.plan.graph_id
                or identity.graph_version != self.plan.graph_version
                or identity.graph_ref != self.plan.graph_ref
                or identity.graph_checksum != self.plan.graph_checksum
            ):
                raise HarnessValidationError(
                    "parent Graph identity does not match dispatch plan",
                    code="TASK_GROUP_SCOPE_MISMATCH",
                )
        if self.schema_version != PARALLEL_DISPATCH_REQUEST_SCHEMA:
            raise HarnessValidationError("unsupported parallel dispatch request schema", code="PLAN_SCHEMA_INVALID")


@dataclass(frozen=True, slots=True)
class ParentObservation:
    run_id: str
    stage_id: str
    plan_version: int
    group_id: str
    group_state: str
    task_summaries: tuple[Mapping[str, Any], ...]
    aggregate_ref: str | None = None
    aggregate_checksum: str | None = None
    diagnostics: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    requested_parallelism: int = 0
    effective_parallelism: int = 0
    wave_summaries: tuple[Mapping[str, Any], ...] = ()
    truncated: bool = False
    schema_version: str = PARENT_OBSERVATION_SCHEMA
    observation_checksum: str = field(init=False)

    @property
    def group_status(self) -> str:
        return self.group_state

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        object.__setattr__(self, "stage_id", identifier(self.stage_id, "stage_id"))
        object.__setattr__(self, "group_id", identifier(self.group_id, "group_id"))
        object.__setattr__(self, "task_summaries", tuple(frozen_mapping(item, "task_summary") for item in self.task_summaries))
        object.__setattr__(self, "wave_summaries", tuple(frozen_mapping(item, "wave_summary") for item in self.wave_summaries))
        object.__setattr__(self, "diagnostics", tuple(str(item) for item in self.diagnostics))
        object.__setattr__(self, "refs", tuple(reference(item, "ref") for item in self.refs))
        if self.schema_version != PARENT_OBSERVATION_SCHEMA:
            raise HarnessValidationError("unsupported parent observation schema", code="PLAN_SCHEMA_INVALID")
        object.__setattr__(self, "observation_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_version": self.plan_version,
            "group_id": self.group_id,
            "group_state": self.group_state,
            "task_summaries": [thaw_mapping(item) for item in self.task_summaries],
            "wave_summaries": [thaw_mapping(item) for item in self.wave_summaries],
            "aggregate_ref": self.aggregate_ref,
            "aggregate_checksum": self.aggregate_checksum,
            "diagnostics": list(self.diagnostics),
            "refs": list(self.refs),
            "requested_parallelism": self.requested_parallelism,
            "effective_parallelism": self.effective_parallelism,
            "truncated": self.truncated,
        }
        if include_checksum:
            value["observation_checksum"] = self.observation_checksum
        return value

    def project(self, limits: ParentObservationLimits) -> dict[str, Any]:
        summaries = [thaw_mapping(item) for item in self.task_summaries[: limits.max_task_summaries]]
        summaries = [_bounded_summary(item, limits.max_summary_bytes) for item in summaries]
        refs = list(self.refs[: limits.max_refs])
        diagnostics = [
            truncate_observation_text(str(item), limits.max_summary_bytes)
            for item in self.diagnostics[: limits.max_diagnostics]
        ]
        projected: dict[str, Any] = {
            "group_id": self.group_id,
            "group_status": self.group_state,
            "plan_version": self.plan_version,
            "waves": [thaw_mapping(item) for item in self.wave_summaries],
            "tasks": summaries,
            "aggregate_ref": self.aggregate_ref,
            "aggregate_checksum": self.aggregate_checksum,
            "diagnostics": diagnostics,
            "result_refs": refs,
            "truncated": (
                len(summaries) != len(self.task_summaries)
                or len(refs) != len(self.refs)
                or len(diagnostics) != len(self.diagnostics)
                or any(item.get("summary_truncated", False) for item in summaries)
                or tuple(diagnostics) != self.diagnostics[: limits.max_diagnostics]
            ),
        }
        # Reserve the fixed checksum envelope while enforcing the byte limit;
        # the final digest is filled after truncation is complete.
        projected["observation_checksum"] = "sha256:" + "0" * 64
        removable = ("diagnostics", "tasks", "result_refs", "waves")
        while _encoded_json_size(projected) > limits.max_observation_bytes:
            for field_name in removable:
                values = projected[field_name]
                if values:
                    values.pop()
                    projected["truncated"] = True
                    break
            else:
                raise HarnessValidationError(
                    "parent observation byte limit cannot hold its identity envelope",
                    code="PARENT_OBSERVATION_LIMIT_TOO_SMALL",
                )
        projected["observation_checksum"] = canonical_payload_checksum(
            {
                key: value
                for key, value in projected.items()
                if key != "observation_checksum"
            }
        )
        return projected


@dataclass(frozen=True, slots=True)
class ParallelDispatchResult:
    group: DispatchGroup
    waves: tuple[DispatchWave, ...]
    results: tuple[TaskResultRecord, ...]
    observation: ParentObservation
    aggregate_ref: str | None = None
    aggregate_checksum: str | None = None
    projected_observation: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PARALLEL_DISPATCH_RESULT_SCHEMA
    attempt_history: tuple[TaskAttemptHistoryRecord, ...] = ()
    dispatch_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PARALLEL_DISPATCH_RESULT_SCHEMA:
            raise HarnessValidationError("unsupported parallel dispatch result schema", code="RESULT_SCHEMA_INVALID")
        waves = tuple(self.waves)
        results = tuple(self.results)
        task_order = {task_id: index for index, task_id in enumerate(self.group.task_ids)}
        result_task_ids = [item.task_id for item in results]
        if len(result_task_ids) != len(set(result_task_ids)):
            raise HarnessValidationError(
                "joined result contains duplicate task identities",
                code="RESULT_IDENTITY_CONFLICT",
            )
        if any(item.task_id not in task_order for item in results):
            raise HarnessValidationError(
                "joined result contains a task outside group scope",
                code="RESULT_IDENTITY_MISMATCH",
            )
        results = tuple(sorted(results, key=lambda item: task_order[item.task_id]))
        if any(item.group_id != self.group.group_id for item in waves):
            raise HarnessValidationError("wave belongs to another dispatch group", code="RESULT_IDENTITY_MISMATCH")
        if any(
            item.run_id != self.group.run_id
            or item.stage_id != self.group.stage_id
            or item.plan_id != self.group.plan_id
            or item.plan_version != self.group.plan_version
            or item.task_id not in self.group.task_ids
            for item in results
        ):
            raise HarnessValidationError("result belongs to another dispatch group", code="RESULT_IDENTITY_MISMATCH")
        if self.observation.group_id != self.group.group_id:
            raise HarnessValidationError("observation belongs to another dispatch group", code="RESULT_IDENTITY_MISMATCH")
        projected_observation = frozen_mapping(
            self.projected_observation or self.observation.to_dict(),
            "projected_observation",
        )
        if projected_observation.get("group_id") != self.group.group_id:
            raise HarnessValidationError(
                "projected observation belongs to another dispatch group",
                code="RESULT_IDENTITY_MISMATCH",
            )
        projected_checksum = projected_observation.get("observation_checksum")
        expected_projected_checksum = canonical_payload_checksum({
            key: value for key, value in thaw_mapping(projected_observation).items()
            if key != "observation_checksum"
        })
        if projected_checksum != expected_projected_checksum:
            raise HarnessValidationError(
                "projected observation checksum does not match content",
                code="RESULT_OBSERVATION_CHECKSUM_MISMATCH",
            )
        if (
            projected_observation.get("aggregate_ref") != self.aggregate_ref
            or projected_observation.get("aggregate_checksum") != self.aggregate_checksum
            or self.observation.aggregate_ref != self.aggregate_ref
            or self.observation.aggregate_checksum != self.aggregate_checksum
        ):
            raise HarnessValidationError(
                "aggregate evidence differs from the joined observation",
                code="RESULT_AGGREGATE_CHECKSUM_MISMATCH",
            )
        if self.group.state is DispatchGroupState.SUCCEEDED:
            expected_aggregate = canonical_payload_checksum({
                "group_id": self.group.group_id,
                "results": [
                    {"task_id": item.task_id, "result_checksum": item.result_checksum}
                    for item in results
                ],
            })
            if self.aggregate_checksum != expected_aggregate:
                raise HarnessValidationError(
                    "joined aggregate checksum does not match task results",
                    code="RESULT_AGGREGATE_CHECKSUM_MISMATCH",
                )
        object.__setattr__(self, "waves", waves)
        object.__setattr__(self, "results", results)
        history = tuple(self.attempt_history)
        if any(not isinstance(item, TaskAttemptHistoryRecord) or item.group is None
               or item.group["group_id"] != self.group.group_id for item in history):
            raise HarnessValidationError("attempt history belongs to another group", code="RESULT_IDENTITY_MISMATCH")
        object.__setattr__(self, "attempt_history", history)
        object.__setattr__(self, "projected_observation", projected_observation)
        object.__setattr__(
            self,
            "dispatch_checksum",
            canonical_payload_checksum(
                {
                    "schema_version": self.schema_version,
                    "group": self.group.to_dict(),
                    "waves": [item.to_dict() for item in waves],
                    "results": [item.to_dict() for item in results],
                    "attempt_history": [item.to_dict() for item in history],
                    "observation": self.observation.to_dict(),
                    "projected_observation": thaw_mapping(projected_observation),
                    "aggregate_ref": self.aggregate_ref,
                    "aggregate_checksum": self.aggregate_checksum,
                }
            ),
        )

    @property
    def succeeded(self) -> bool:
        return self.group.state is DispatchGroupState.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "group": self.group.to_dict(),
            "waves": [item.to_dict() for item in self.waves],
            "results": [item.to_dict() for item in self.results],
            "attempt_history": [item.to_dict() for item in self.attempt_history],
            "observation": self.observation.to_dict(),
            "projected_observation": thaw_mapping(self.projected_observation),
            "aggregate_ref": self.aggregate_ref,
            "aggregate_checksum": self.aggregate_checksum,
            "dispatch_checksum": self.dispatch_checksum,
        }


@dataclass
class _GroupSession:
    group: DispatchGroup
    request: ParallelDispatchRequest
    waves: list[DispatchWave] = field(default_factory=list)
    results: dict[str, TaskResultRecord] = field(default_factory=dict)
    attempt_history: dict[str, TaskAttemptHistoryRecord] = field(default_factory=dict)
    attempt_receipts: dict[str, Any] = field(default_factory=dict)
    wave_instances: dict[str, tuple[TaskInstance, ...]] = field(default_factory=dict)
    blocked_task_ids: set[str] = field(default_factory=set)
    blocked_task_checksums: dict[str, str] = field(default_factory=dict)
    failed_task_ids: set[str] = field(default_factory=set)
    reserved: set[str] = field(default_factory=set)
    degraded_reason: str | None = None
    active_children: dict[str, tuple[ChildAgentHandle, "_SupervisorTaskWorker"]] = field(default_factory=dict)
    spawn_receipts: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    recovery_status_reads: dict[str, tuple[str, ChildAgentHandle | None]] = field(default_factory=dict)
    reconciled_spawns: set[str] = field(default_factory=set)
    quarantined_task_ids: set[str] = field(default_factory=set)
    next_wave_ordinal: int = 1
    started_at: float = field(default_factory=monotonic)
    join_started_at: float | None = None
    wave_admitted_at: dict[str, float] = field(default_factory=dict)
    wave_dispatched_at: dict[str, float] = field(default_factory=dict)
    dispatch_lock: Any = field(default_factory=Lock)
    terminal_diagnostics: tuple[str, ...] = ()


@dataclass
class _PendingGroupAdmission:
    """Coordinates concurrent attempts before an admission becomes visible."""

    group: DispatchGroup
    completed: Event = field(default_factory=Event)
    failure: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _WaveRunOutcome:
    results: tuple[TaskResultRecord, ...]
    released_task_ids: frozenset[str] = frozenset()
    consumed_task_ids: frozenset[str] = frozenset()
    quarantined_task_ids: frozenset[str] = frozenset()
    reclaimed_task_ids: frozenset[str] = frozenset()


class _SupervisorTaskWorker:
    """Adapter that keeps task-result semantics outside supervisor policy."""

    def __init__(
        self,
        invoke: Callable[[TaskInstance], TaskResultRecord],
        item: TaskInstance,
        *,
        spawn_request: ChildAgentSpawnRequest | None = None,
        group_absolute_deadline_ms: int | None = None,
    ) -> None:
        self._invoke = invoke
        self._item = item
        self._spawn_request = spawn_request
        self._reservation = (
            BudgetReservation.from_dict(spawn_request.budget)
            if spawn_request is not None
            else None
        )
        self._group_absolute_deadline_ms = group_absolute_deadline_ms
        self._cancel_requested = Event()
        self._started = Event()
        self._live_control: ChildExecutionControl | None = None
        self.result: TaskResultRecord | None = None
        self._lock = Lock()

    def run(self, handle: ChildAgentHandle) -> Mapping[str, Any]:
        if self._cancel_requested.is_set():
            raise RuntimeError("parallel task cancellation requested before execution")
        if (
            self._spawn_request is None
            or self._reservation is None
            or self._group_absolute_deadline_ms is None
        ):
            # Recovered placeholders carry no newly issued live authority.
            raise HarnessValidationError(
                "recovered child worker has no live execution control",
                code="CHILD_EXECUTION_CONTROL_REQUIRED",
            )
        control = ChildExecutionControl.create(
            handle=handle,
            spawn_request=self._spawn_request,
            task_instance=self._item,
            reservation=self._reservation,
            group_absolute_deadline_ms=self._group_absolute_deadline_ms,
        )
        with self._lock:
            self._live_control = control
        try:
            if self._cancel_requested.is_set():
                control.request_cancel()
            control.raise_if_active()
            self._started.set()
            with bind_child_execution_control(control):
                control.raise_if_active()
                result = self._invoke(self._item)
        finally:
            with self._lock:
                if self._live_control is control:
                    self._live_control = None
        if not isinstance(result, TaskResultRecord):
            raise HarnessValidationError("parallel worker returned invalid result", code="RESULT_SCHEMA_INVALID")
        if result.task_id != self._item.task_id:
            raise HarnessValidationError("worker result task identity mismatch", code="RESULT_IDENTITY_MISMATCH")
        with self._lock:
            self.result = result
        # Persist the complete typed result envelope in the supervisor's
        # terminal receipt metadata as well as keeping the typed value on the
        # adapter.  A fresh coordinator can therefore recover the result
        # without invoking the worker again after a parent-side crash.
        return {
            "task_result": result.to_dict(),
            "task_result_checksum": result.result_checksum,
        }

    def cancel(self, _handle: ChildAgentHandle) -> bool:
        self._cancel_requested.set()
        # The lexical control carries the same cooperative cancellation event
        # into child descendants.  The return value still only speaks to
        # whether this adapter had entered its callback.
        with self._lock:
            control = self._live_control
        if control is not None:
            control.request_cancel()
        # A not-yet-started task is safely cancelled.  Once the real worker
        # entered its body the supervisor must retain an indeterminate receipt
        # instead of pretending an external side effect stopped.
        return not self._started.is_set()


@runtime_checkable
class SerialTaskExecutorPort(Protocol):
    """Explicit production fallback when no parallel wave transport exists."""

    def execute(
        self,
        task_instance: TaskInstance,
        invoke: Callable[[TaskInstance], TaskResultRecord],
    ) -> TaskResultRecord:
        """Execute exactly one trusted task instance."""


class SerialTaskExecutorAdapter:
    """Run one Harness-materialized task without creating an implicit pool."""

    def execute(
        self,
        task_instance: TaskInstance,
        invoke: Callable[[TaskInstance], TaskResultRecord],
    ) -> TaskResultRecord:
        if not isinstance(task_instance, TaskInstance):
            raise TypeError("task_instance must be TaskInstance")
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        return _validated_task_result(invoke(task_instance), task_instance)


def child_budget_reservation(
    ledger: TaskPlanBudgetLedger,
    instance: TaskInstance,
    *,
    group_id: str,
    wave_id: str,
) -> dict[str, Any]:
    """Bind the child envelope to the immutable accepted attempt charge."""

    record = ledger.records.get(instance.idempotency_key)
    if record is None or thaw_mapping(record["instance"]) != instance.to_dict():
        raise HarnessValidationError("child has no matching attempt reservation", code="task_plan_budget_identity_conflict")
    task_budget = instance.budget_snapshot.to_dict()
    aggregate_budget = ledger.parent_allocation
    # Supervised children must carry the versioned execution reservation.
    # Legacy receipts require offline migration, never implicit live fallback.
    if "token_limit" not in task_budget or "time_limit_ms" not in task_budget:
        raise HarnessValidationError(
            "supervised child requires explicit token and time budget limits",
            code="task_plan_budget_policy_missing",
            details={"task_id": instance.task_id, "task_instance_id": instance.task_instance_id},
        )
    if "token_limit" not in aggregate_budget or "time_limit_ms" not in aggregate_budget:
        raise HarnessValidationError(
            "supervised child requires an explicit parent execution budget envelope",
            code="task_plan_budget_policy_missing",
            details={"task_id": instance.task_id, "task_instance_id": instance.task_instance_id},
        )
    return BudgetReservation(
        owner_scope=f"{ledger.run_id}:{ledger.stage_id}:{group_id}",
        reservation_key=spawn_operation_key(group_id, wave_id, instance.task_instance_id, instance.attempt),
        parent_allocation=aggregate_budget,
        attempt_allocation=task_budget,
        ledger_version=record["reserved_revision"],
    ).to_dict()


def _require_live_execution_budget(plan: ValidatedTaskPlan) -> None:
    """Require versioned execution dimensions before live group admission.

    A static four-field ``TaskBudget`` remains valid for non-live planning and
    historical read paths.  Once a group is about to become durable, however,
    every accepted task and both policy envelopes must carry the token/time
    dimensions consumed by the v2 child reservation contract.  This check is
    deliberately independent of transport selection, so serial fallback and
    supervised dispatch share the same fail-closed admission boundary.
    """

    budgets = [
        ("per_task_budget", plan.limits.per_task_budget),
        ("aggregate_task_budget", plan.limits.aggregate_task_budget),
        *(
            (f"task:{item.task_id}", item.normalized_budget)
            for item in plan.tasks
        ),
    ]
    missing = sorted(
        {
            f"{owner}.{dimension}"
            for owner, budget in budgets
            for dimension in ("token_limit", "time_limit_ms")
            if dimension not in budget.to_dict()
        }
    )
    if missing:
        raise HarnessValidationError(
            "live parallel group requires explicit token and time budget limits",
            code="task_plan_budget_policy_missing",
            details={"missing_dimensions": missing},
        )


class ParallelAgentCoordinator:
    """Execute bounded waves and join all waves in deterministic plan order."""

    def __init__(
        self,
        *,
        max_workers: int = 3,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        child_supervisor: ChildAgentSupervisor | None = None,
        serial_executor: SerialTaskExecutorPort | None = None,
        allow_test_executor: bool = False,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers
        self.event_sink = event_sink
        if child_supervisor is not None and not isinstance(child_supervisor, ChildAgentSupervisor):
            raise TypeError("child_supervisor must be ChildAgentSupervisor")
        if child_supervisor is not None and max_workers > child_supervisor.capacity:
            raise ValueError("max_workers cannot exceed child supervisor capacity")
        if not isinstance(allow_test_executor, bool):
            raise TypeError("allow_test_executor must be boolean")
        if serial_executor is not None and not isinstance(
            serial_executor,
            SerialTaskExecutorPort,
        ):
            raise TypeError("serial_executor must implement SerialTaskExecutorPort")
        if child_supervisor is None and serial_executor is None and not allow_test_executor:
            raise ValueError(
                "production parallel coordination requires ChildAgentSupervisor "
                "or SerialTaskExecutorPort"
            )
        self.child_supervisor = child_supervisor
        self.serial_executor = serial_executor
        self._allow_test_executor = allow_test_executor
        self._lock = RLock()
        self._sessions: dict[str, _GroupSession] = {}
        self._pending_admissions: dict[str, _PendingGroupAdmission] = {}

    def _group_parallelism_limit(self, request: ParallelDispatchRequest) -> int:
        """Return the immutable admission ceiling used in group identity."""

        values = [request.plan.limits.max_parallelism, self.max_workers]
        for value in (
            request.requested_parallelism,
            request.capability_capacity,
            request.supervisor_capacity,
        ):
            if value is not None:
                values.append(value)
        limit = min(values)
        if not request.task_capacity_demands and request.side_effect_class in {
            SideEffectClass.MUTATING_SERIAL,
            SideEffectClass.FENCED_MUTATION,
        }:
            limit = min(limit, 1)
        if request.side_effect_class is SideEffectClass.FENCED_MUTATION and not request.resource_conflict_key:
            raise HarnessValidationError("fenced mutation requires a resource conflict key", code="SIDE_EFFECT_FENCE_REQUIRED")
        return limit

    @staticmethod
    def _validate_capacity_policy(request: ParallelDispatchRequest) -> None:
        observed_at = capacity_now_ms()
        if request.capacity_pools and request.capacity_policy is None:
            raise HarnessValidationError("multi-pool dispatch requires trusted capacity rules", code="CAPACITY_POLICY_MISSING")
        if request.capacity_policy is not None:
            request.capacity_policy.resolve(request.plan, request.capacity_pools, now_ms=observed_at)
        if request.capacity_snapshot is not None:
            request.capacity_snapshot.require_current(now_ms=observed_at)
        for pool in request.capacity_pools:
            pool.require_current(now_ms=observed_at)

    def effective_parallelism(self, request: ParallelDispatchRequest) -> int:
        values = [self._group_parallelism_limit(request)]
        if request.available_concurrency_reservations is not None:
            values.append(request.available_concurrency_reservations)
        if self.child_supervisor is not None:
            values.append(self.child_supervisor.available_capacity)
        effective = min(values)
        return effective

    def dispatch_parallelism(self, request: ParallelDispatchRequest) -> int:
        """Return the admitted dispatch capacity, enforcing transport policy.

        Stage schedulers must ask the coordinator for capacity instead of
        calling ``effective_parallelism`` directly.  This keeps the explicit
        serial adapter and fail-closed capacity checks on the same path as
        group admission and wave dispatch.
        """

        effective, _reason_code = self._dispatch_parallelism(request)
        return effective

    def _requires_serial_fallback_transport(self) -> bool:
        return self.child_supervisor is None and not self._allow_test_executor

    def _dispatch_parallelism(
        self,
        request: ParallelDispatchRequest,
    ) -> tuple[int, str | None]:
        self._validate_capacity_policy(request)
        if request.side_effect_class is SideEffectClass.FENCED_MUTATION or any(
            demand.side_effect_class is SideEffectClass.FENCED_MUTATION
            for demand in request.task_capacity_demands.values()
        ):
            raise HarnessValidationError(
                "fenced mutation requires a resource authority and execution lease adapter",
                code="SIDE_EFFECT_FENCE_REQUIRED",
            )
        effective = self.effective_parallelism(request)
        if effective < 1:
            # Zero live slots is a recoverable scheduling condition.  The
            # full logical READY set still flows into first-fit so the
            # coordinator can record one fixed-deadline capacity-wait fact.
            return 0, "capacity_limited"
        if self._requires_serial_fallback_transport():
            if not request.serial_fallback:
                raise HarnessValidationError(
                    "parallel wave transport is unavailable",
                    code="TASK_GROUP_WAVE_ADAPTER_REQUIRED",
                )
            if self.serial_executor is None:
                raise HarnessValidationError(
                    "serial fallback requires an explicit executor adapter",
                    code="TASK_GROUP_WAVE_ADAPTER_REQUIRED",
                )
            return 1, "wave_adapter_unavailable"
        if not request.task_capacity_demands and request.side_effect_class is SideEffectClass.MUTATING_SERIAL:
            return effective, "side_effect_fence"
        requested = request.requested_parallelism or self._group_parallelism_limit(request)
        if effective == 1 and requested > 1:
            return effective, "capacity_limited"
        return effective, None

    def _group_definition(
        self,
        request: ParallelDispatchRequest,
    ) -> DispatchGroup:
        group_parallelism = self._group_parallelism_limit(request)
        if group_parallelism < 1:
            raise HarnessValidationError("parallel capacity limit is unavailable", code="CAPACITY_EXHAUSTED")
        task_ids = tuple(item.task_id for item in request.plan.tasks)
        if request.group_task_ids is not None and request.group_task_ids != task_ids:
            raise HarnessValidationError(
                "dispatch group membership must equal the complete accepted plan",
                code="TASK_GROUP_SCOPE_MISMATCH",
            )
        if len(task_ids) > request.plan.limits.max_tasks:
            raise HarnessValidationError("group exceeds TaskPlan max_tasks", code="TASK_GROUP_LIMIT_EXCEEDED")
        if request.max_tasks_per_group is not None and len(task_ids) > request.max_tasks_per_group:
            raise HarnessValidationError("group exceeds max_tasks_per_group", code="TASK_GROUP_LIMIT_EXCEEDED")
        if request.parent_graph_identity is None:
            raise HarnessValidationError(
                "dispatch group requires the parent Graph identity",
                code="TASK_GROUP_PARENT_IDENTITY_REQUIRED",
            )
        group = DispatchGroup(
            run_id=request.plan.run_id,
            stage_id=request.plan.stage_id,
            plan_id=request.plan.plan_id,
            plan_version=request.plan.version,
            plan_checksum=request.plan.plan_checksum,
            policy_ref=request.plan.policy_ref,
            policy_checksum=request.plan.policy_checksum,
            parent_graph_identity=request.parent_graph_identity,
            admission_policy_checksum=_admission_policy_checksum(request),
            task_ids=tuple(task_ids),
            required_output_roles=request.plan.required_output_roles,
            join_policy=request.join_policy,
            max_waves=request.max_waves,
            max_parallelism=group_parallelism,
            budget_envelope=request.plan.limits.aggregate_task_budget.to_dict(),
            admitted_at_ms=request.group_admitted_at_ms,
            absolute_deadline_ms=request.group_absolute_deadline_ms,
            correlation_id=request.correlation_id,
            state=DispatchGroupState.PLANNED,
        ).transitioned(DispatchGroupState.ADMITTED)
        return group

    def create_group(
        self,
        request: ParallelDispatchRequest,
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        check_capacity: bool = True,
    ) -> DispatchGroup:
        # Validate the complete execution budget before any group admission
        # event can be emitted or persisted, including serial fallback.
        _require_live_execution_budget(request.plan)
        group = self._group_definition(request)
        with self._lock:
            session = self._sessions.get(group.group_id)
            if session is not None:
                group = _reuse_admission_clock(group, session.group)
                if session.group.group_checksum != group.group_checksum:
                    raise HarnessValidationError(
                        "dispatch group identity conflicts with immutable admission",
                        code="TASK_GROUP_ADMISSION_CONFLICT",
                    )
                return session.group
            pending = self._pending_admissions.get(group.group_id)
            if pending is None:
                pending = _PendingGroupAdmission(group=group)
                self._pending_admissions[group.group_id] = pending
                admission_owner = True
            else:
                group = _reuse_admission_clock(group, pending.group)
                if pending.group.group_checksum != group.group_checksum:
                    raise HarnessValidationError(
                        "dispatch group identity conflicts with immutable admission",
                        code="TASK_GROUP_ADMISSION_CONFLICT",
                    )
                group = pending.group
                admission_owner = False

        if not admission_owner:
            if not pending.completed.wait(timeout=request.max_group_runtime_seconds):
                raise HarnessValidationError("group admission wait exceeded its bound", code="TASK_GROUP_ADMISSION_TIMEOUT")
            with self._lock:
                if pending.failure is not None:
                    raise pending.failure
                session = self._sessions.get(group.group_id)
                if session is None or session.group.group_checksum != group.group_checksum:
                    raise HarnessValidationError(
                        "dispatch group admission completed without its immutable session",
                        code="TASK_GROUP_ADMISSION_CONFLICT",
                    )
                return session.group

        try:
            admitted_parallelism = (
                self._dispatch_parallelism(request)[0]
                if check_capacity
                else group.max_parallelism
            )
            self._emit(
                "TASK_GROUP_ADMITTED",
                event_sink=event_sink,
                group=group.to_dict(),
                requested_parallelism=request.requested_parallelism
                or group.max_parallelism,
                effective_parallelism=admitted_parallelism,
                idempotency_key=group.group_id,
            )
        except BaseException as exc:
            with self._lock:
                pending.failure = exc
                self._pending_admissions.pop(group.group_id, None)
                pending.completed.set()
            raise

        with self._lock:
            self._sessions[group.group_id] = _GroupSession(group=group, request=request)
            self._pending_admissions.pop(group.group_id, None)
            pending.completed.set()
        return group

    def restore_group(
        self,
        request: ParallelDispatchRequest,
        group: DispatchGroup,
        waves: tuple[DispatchWave, ...],
    ) -> DispatchGroup:
        """Restore canonical replay snapshots without repeating admission or spawn."""
        definition = self._group_definition(request)
        if not isinstance(group, DispatchGroup):
            raise HarnessValidationError(
                "durable group differs from immutable admission",
                code="TASK_GROUP_ADMISSION_CONFLICT",
            )
        # ``ParallelDispatchRequest`` materializes a local admission clock
        # when durable timestamps are not supplied. During reopen that clock
        # can differ from the original event even though every immutable
        # admission input is identical. Compare against the original fixed
        # clock, while retaining strict checksum validation for all other
        # group fields.
        definition = _reuse_admission_clock(definition, group)
        if group.group_checksum != definition.group_checksum:
            raise HarnessValidationError("durable group differs from immutable admission", code="TASK_GROUP_ADMISSION_CONFLICT")
        if any(not isinstance(wave, DispatchWave) for wave in waves):
            raise TypeError("waves must contain DispatchWave values")
        ordered = tuple(sorted(waves, key=lambda wave: wave.ordinal))
        if (
            tuple(wave.ordinal for wave in ordered) != tuple(range(1, len(ordered) + 1))
            or len(ordered) > group.max_waves
            or any(wave.group_id != group.group_id or not set(wave.task_ids).issubset(group.task_ids) for wave in ordered)
            or any(wave.state is not DispatchWaveState.TERMINAL for wave in ordered[:-1])
        ):
            raise HarnessValidationError("durable wave history differs from group sequence", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
        with self._lock:
            existing = self._sessions.get(group.group_id)
            if existing is not None:
                if existing.group.group_checksum != group.group_checksum:
                    raise HarnessValidationError("restored group conflicts with local admission", code="TASK_GROUP_ADMISSION_CONFLICT")
                if tuple(existing.waves) != ordered or existing.next_wave_ordinal != len(ordered) + 1:
                    raise HarnessValidationError("restored waves conflict with local admission history", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
                return existing.group
            if group.group_id in self._pending_admissions:
                raise HarnessValidationError("group admission is still being committed", code="TASK_GROUP_DISPATCH_BUSY")
            session = _GroupSession(group=group, request=request)
            session.waves = list(ordered)
            for wave in ordered:
                instances = []
                for reservation in wave.reservations:
                    allocated = [task_instance_for_attempt(request.plan, reservation.task_id, attempt)
                                 for attempt in range(1, request.plan.limits.max_task_attempts + 1)]
                    instance = next((item for item in allocated if item.idempotency_key == reservation.idempotency_key), None)
                    if instance is None:
                        raise HarnessValidationError("wave reservation has no accepted attempt", code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH")
                    instances.append(instance)
                session.wave_instances[wave.wave_id] = tuple(instances)
            session.next_wave_ordinal = len(ordered) + 1
            session.reserved.update(
                reservation.task_id for wave in ordered for reservation in wave.reservations
                if reservation.state is ReservationState.RESERVED
            )
            self._sessions[group.group_id] = session
        return group

    def recover(
        self,
        request: ParallelDispatchRequest,
        recovered_results: tuple[TaskResultRecord, ...],
        *,
        historical_wave_ordinals: tuple[int, ...] = (),
        attempt_history: tuple[TaskAttemptHistoryRecord, ...] = (),
        recover_result: Callable[[TaskInstance], tuple[TaskResultRecord, Any] | None] | None = None,
        limits: ParentObservationLimits | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        """Hydrate one admitted group from verified durable task outcomes.

        Recovery is deliberately explicit: a terminal outer child must have a
        confirmed receipt before the old handle is closed, and every durable
        result must match the immutable group identity.
        """

        results = tuple(recovered_results)
        if any(not isinstance(item, TaskResultRecord) for item in results):
            raise HarnessValidationError(
                "parallel recovery requires TaskResultRecord values",
                code="TASK_GROUP_RECOVERY_RESULT_INVALID",
            )
        historical_ordinals = tuple(historical_wave_ordinals)
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                for value in historical_ordinals
            )
            or len(historical_ordinals) != len(set(historical_ordinals))
        ):
            raise HarnessValidationError(
                "parallel recovery has invalid historical wave ordinals",
                code="TASK_GROUP_RECOVERY_WAVE_INVALID",
            )
        # Recovery must not re-run admission against current live capacity:
        # a confirmed child may itself occupy the last supervisor slot.
        group = self.create_group(request, event_sink=event_sink, check_capacity=False)
        with self._lock:
            session = self._sessions[group.group_id]
            admitted_attempts = {
                instance.task_id: instance
                for wave in session.waves
                for instance in session.wave_instances.get(wave.wave_id, ())
            }
            if not session.waves:
                admitted_attempts = {instance.task_id: instance for instance in request.task_instances}
            for record in attempt_history:
                if record.group is None or record.group["group_id"] != group.group_id:
                    continue
                if not record.instance.matches_plan_identity(request.plan):
                    raise HarnessValidationError("recovered attempt history scope mismatch", code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH")
                self._sessions[group.group_id].attempt_history.setdefault(record.record_checksum, record)
        by_task: dict[str, TaskResultRecord] = {}
        for result in results:
            if (
                result.run_id != group.run_id
                or result.stage_id != group.stage_id
                or result.plan_id != group.plan_id
                or result.plan_version != group.plan_version
                or result.task_id not in group.task_ids
                or result.status not in {TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED}
                or (result.status is TaskLifecycle.FAILED and not any(
                    record.result is not None and record.result.result_checksum == result.result_checksum
                    and record.outcome in {TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.FAILED}
                    for record in attempt_history
                ))
                or not result.matches_plan_identity(request.plan)
                or result.task_id not in admitted_attempts
                or result.task_instance_id != admitted_attempts[result.task_id].task_instance_id
                or result.attempt != admitted_attempts[result.task_id].attempt
            ):
                raise HarnessValidationError(
                    "recovered result does not match dispatch group",
                    code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH",
                    details={"task_id": result.task_id},
                )
            existing = by_task.get(result.task_id)
            if existing is not None and existing.result_checksum != result.result_checksum:
                raise HarnessValidationError(
                    "parallel recovery contains conflicting task outcomes",
                    code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                    details={"task_id": result.task_id},
                )
            by_task[result.task_id] = result

        with self._lock:
            session = self._sessions[group.group_id]
            if session.group.state in {
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
                DispatchGroupState.HALTED,
                DispatchGroupState.SUPERSEDED,
            }:
                raise HarnessValidationError(
                    "terminal dispatch group cannot be recovered",
                    code="TASK_GROUP_RECOVERY_STATE_INVALID",
                    details={"group_state": session.group.state.value},
                )
            for task_id, result in by_task.items():
                existing = session.results.get(task_id)
                if existing is not None and existing.result_checksum != result.result_checksum:
                    raise HarnessValidationError(
                        "durable result conflicts with coordinator state",
                        code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                        details={"task_id": task_id},
                    )
            missing_results = {
                task_id: result
                for task_id, result in by_task.items()
                if task_id not in session.results
            }
            if historical_ordinals:
                session.next_wave_ordinal = max(
                    session.next_wave_ordinal,
                    max(historical_ordinals) + 1,
                )
            active = tuple(session.active_children.items())
            # An indeterminate group can only be reopened by an explicit
            # online reconciliation while a supervisor handle is still held.
            # This is a recovery transition, not ordinary dispatch or retry.
            if session.group.state is DispatchGroupState.INDETERMINATE and not active:
                raise HarnessValidationError(
                    "indeterminate group has no active receipt to reconcile",
                    code="TASK_GROUP_RECOVERY_STATE_INVALID",
                )
            needs_recovery = bool(missing_results or active or any(
                wave.state is not DispatchWaveState.TERMINAL
                and set(wave.task_ids).issubset(set(by_task) | set(session.results))
                for wave in session.waves
            ))
            if not needs_recovery:
                return self._result_for_session(session, request, limits=limits)

        if self.child_supervisor is None and active:
            raise HarnessValidationError(
                "active child handles cannot be reconciled without a supervisor",
                code="TASK_GROUP_RECOVERY_SUPERVISOR_REQUIRED",
            )
        recovered_worker_results: dict[str, TaskResultRecord] = {}
        recovered_task_ids = frozenset(missing_results)
        for task_id, (handle, worker) in active:
            assert self.child_supervisor is not None
            admitted_wave = _wave_for_child(session, handle)
            operation = self._recovery_supervisor_call(
                session,
                admitted_wave,
                handle,
                recovery_operation="wait",
                event_sink=event_sink,
                invoke=lambda: self.child_supervisor.wait(
                    handle.child_id,
                    operation_id=handle.operation_id,
                    timeout_seconds=request.max_join_wait_seconds,
                ),
            )
            receipt = operation.receipt
            if receipt is None or not receipt.termination_confirmed:
                raise HarnessValidationError(
                    "child termination could not be confirmed during recovery",
                    code="TASK_GROUP_RECOVERY_UNCONFIRMED",
                    details={"task_id": task_id, "child_id": handle.child_id},
                )
            if task_id in recovered_task_ids:
                # The parent result was independently verified and durably
                # recovered before this coordinator pass. The child receipt
                # still must prove terminal termination, but its FAILED or
                # CANCELLED state must not discard the verified parent result.
                self._recovery_supervisor_call(
                    session,
                    admitted_wave,
                    handle,
                    recovery_operation="close",
                    event_sink=event_sink,
                    invoke=lambda: self.child_supervisor.close(
                        handle.child_id,
                        operation_id=handle.operation_id,
                    ),
                )
                with self._lock:
                    self._sessions[group.group_id].active_children.pop(task_id, None)
                continue
            worker_result = worker.result if worker is not None else None
            if worker_result is None and isinstance(operation.result, Mapping):
                raw_task_result = operation.result.get("task_result")
                raw_checksum = operation.result.get("task_result_checksum")
                if raw_task_result is not None and not isinstance(raw_task_result, Mapping):
                    raise HarnessValidationError(
                        "terminal child task result must be an object",
                        code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                        details={"task_id": task_id, "child_id": handle.child_id},
                    )
                if isinstance(raw_task_result, Mapping):
                    try:
                        recovered_result = TaskResultRecord.from_dict(raw_task_result)
                    except (TypeError, ValueError, HarnessValidationError) as exc:
                        raise HarnessValidationError(
                            "terminal child task result is not verifiable",
                            code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                            details={"task_id": task_id, "child_id": handle.child_id},
                        ) from exc
                    if raw_checksum != recovered_result.result_checksum:
                        raise HarnessValidationError(
                            "terminal child task result checksum conflicts with envelope",
                            code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                            details={"task_id": task_id, "child_id": handle.child_id},
                        )
                    worker_result = recovered_result
            admitted_instance = task_instance_for_attempt(
                request.plan, handle.task_id, handle.attempt,
                task_instance_id=handle.task_instance_id,
            )
            recovered_from = None
            recovery_receipt = None
            if worker_result is None and receipt.status is ChildAgentState.FAILED and recover_result is not None:
                original_failure = self._record_attempt(
                    session, admitted_wave, admitted_instance,
                    outcome=TaskAttemptOutcome.FAILED, receipt=receipt,
                    reason_code=receipt.reason_code, event_sink=event_sink,
                )
                previous = next((item for item in session.attempt_history.values()
                                 if item.recovered_from == original_failure.record_checksum), None)
                if previous is not None:
                    worker_result, recovery_receipt = previous.result, previous.recovery_receipt
                else:
                    recovered = recover_result(admitted_instance)
                    if recovered is None:
                        raise HarnessValidationError(
                            "failed wrapper has no verifiable committed subagent result",
                            code="task_plan_subagent_attempt_indeterminate",
                            details={"task_id": task_id, "child_id": handle.child_id},
                        )
                    worker_result, recovery_receipt = recovered
                recovered_from = original_failure.record_checksum
            if worker_result is not None:
                try:
                    recovered_worker_results[task_id] = _validated_supervised_task_result(
                        worker_result,
                        admitted_instance,
                        request.plan,
                    )
                    session.attempt_receipts[admitted_instance.task_instance_id] = receipt
                    self._record_attempt(session, admitted_wave, admitted_instance, result=worker_result,
                                         receipt=receipt, event_sink=event_sink,
                                         recovered_from=recovered_from, recovery_receipt=recovery_receipt)
                except HarnessValidationError as exc:
                    raise HarnessValidationError(
                        "terminal child result does not match its admitted attempt",
                        code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                        details={"task_id": task_id, "child_id": handle.child_id},
                    ) from exc
            self._recovery_supervisor_call(
                session,
                admitted_wave,
                handle,
                recovery_operation="close",
                event_sink=event_sink,
                invoke=lambda: self.child_supervisor.close(
                    handle.child_id,
                    operation_id=handle.operation_id,
                ),
            )
            with self._lock:
                self._sessions[group.group_id].active_children.pop(task_id, None)

        with self._lock:
            session = self._sessions[group.group_id]
            for task_id, result in recovered_worker_results.items():
                existing = session.results.get(task_id)
                if existing is not None and existing.result_checksum != result.result_checksum:
                    raise HarnessValidationError(
                        "recovered worker result conflicts with coordinator state",
                        code="TASK_GROUP_RECOVERY_RESULT_CONFLICT",
                        details={"task_id": task_id},
                    )
                if task_id not in session.results:
                    missing_results[task_id] = result
            session.results.update(missing_results)
            session.reserved.difference_update(missing_results)
            if session.group.state is DispatchGroupState.INDETERMINATE:
                session.group = replace(session.group, state=DispatchGroupState.RUNNING)
            elif session.group.state is not DispatchGroupState.SUCCEEDED:
                session.group = session.group.transitioned(DispatchGroupState.RUNNING)
            recovery_capacity_snapshot = request.capacity_snapshot
            for wave in tuple(session.waves):
                if wave.state is DispatchWaveState.TERMINAL or not set(wave.task_ids).issubset(session.results):
                    continue
                wave_results = tuple(session.results[task_id] for task_id in wave.task_ids)
                outcome = _WaveRunOutcome(wave_results)
                reservation_states = {
                    item.task_id: ReservationState.CONSUMED
                    for item in wave.reservations
                }
                terminal = replace(
                    wave.transitioned(DispatchWaveState.TERMINAL, terminal_outcome=_terminal_wave_outcome(outcome)),
                    reservations=tuple(item.settled(ReservationState.CONSUMED) for item in wave.reservations),
                )
                capacity_before_release = recovery_capacity_snapshot
                capacity_after_release = _capacity_release_transition(
                    capacity_before_release,
                    wave,
                    reservation_states,
                )
                self._emit(
                    "TASK_WAVE_COMPLETED", event_sink=event_sink,
                    group_id=group.group_id, wave_id=wave.wave_id, task_ids=list(wave.task_ids),
                    terminal_outcome=terminal.terminal_outcome.value,
                    reservation_states={item.task_id: item.state.value for item in terminal.reservations},
                    child_states={item.task_id: item.status.value for item in wave_results},
                    capacity_before=(
                        capacity_before_release.to_dict()
                        if capacity_before_release is not None else None
                    ),
                    capacity_after=(
                        capacity_after_release.to_dict()
                        if capacity_after_release is not None else None
                    ),
                )
                recovery_capacity_snapshot = capacity_after_release
                session.waves = [terminal if item.wave_id == wave.wave_id else item for item in session.waves]
                session.reserved.difference_update(wave.task_ids)
            recovered_projection = tuple(
                {
                    "task_id": item.task_id,
                    "task_instance_id": item.task_instance_id,
                    "attempt": item.attempt,
                    "status": item.status.value,
                    "result_checksum": item.result_checksum,
                }
                for item in sorted(
                    {**by_task, **recovered_worker_results}.values(),
                    key=lambda value: value.task_id,
                )
            )
            recovery_checksum = canonical_payload_checksum(
                {
                    "group_id": group.group_id,
                    "results": list(recovered_projection),
                    "outcome": "receipts_reconciled",
                }
            )
            self._emit(
                "TASK_GROUP_RECOVERY",
                event_sink=event_sink,
                group=session.group.to_dict(),
                group_id=group.group_id,
                recovered_results=list(recovered_projection),
                recovery_outcome="receipts_reconciled",
                idempotency_key=recovery_checksum,
            )
            return self._result_for_session(session, request, limits=limits)

    def _recovery_supervisor_call(
        self,
        session: _GroupSession,
        wave: DispatchWave,
        handle: ChildAgentHandle,
        *,
        recovery_operation: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        invoke: Callable[[], Any],
    ) -> Any:
        """Audit one online supervisor call before and after it is allowed.

        ``recover()`` is an online path even when the outer stage is resuming a
        durable group.  A status reconciliation only proves that a handle is
        trackable; each subsequent ``wait`` and ``close`` remains a separate
        live effect.  The intent is therefore durable before the call, and a
        failed audit write cannot silently advance to the supervisor.
        """

        if recovery_operation not in {"wait", "close"}:
            raise ValueError("recovery_operation must be wait or close")
        # Recovery is an online side-effect path.  Unlike ordinary in-memory
        # coordination, it must never call a supervisor unless the pre-call
        # fact can be durably recorded.  ``_emit`` deliberately supports
        # sink-less local use elsewhere, so enforce the stricter contract at
        # this recovery boundary instead of changing global emission rules.
        if event_sink is None and self.event_sink is None:
            raise HarnessValidationError(
                "online recovery supervisor call requires a durable audit sink",
                code="TASK_GROUP_RECOVERY_AUDIT_REQUIRED",
            )
        instance = task_instance_for_attempt(
            session.request.plan,
            handle.task_id,
            handle.attempt,
            task_instance_id=handle.task_instance_id,
        )
        expected_operation = spawn_operation_key(
            session.group.group_id,
            wave.wave_id,
            instance.task_instance_id,
            instance.attempt,
        )
        if handle.operation_id != expected_operation:
            raise HarnessValidationError(
                "recovery supervisor handle differs from admitted operation",
                code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH",
            )
        recovery_id = f"recovery-{uuid4().hex}"
        identity = {
            "group_id": session.group.group_id,
            "wave_id": wave.wave_id,
            "task_id": instance.task_id,
            "task_instance_id": instance.task_instance_id,
            "attempt": instance.attempt,
            "operation_key": expected_operation,
            "child_id": handle.child_id,
            "recovery_id": recovery_id,
            "recovery_operation": recovery_operation,
        }
        self._emit(
            "RECOVERY_OPERATION_INTENT",
            event_sink=event_sink,
            **identity,
            idempotency_key=f"{recovery_id}:{recovery_operation}:intent",
        )
        try:
            outcome = invoke()
            receipt = self._validated_recovery_operation_result(
                session,
                handle,
                expected_operation,
                recovery_operation=recovery_operation,
                outcome=outcome,
            )
        except BaseException:
            self._emit(
                "RECOVERY_OPERATION_HALTED",
                event_sink=event_sink,
                **identity,
                reason_code=(
                    "RECOVERY_WAIT_FAILED"
                    if recovery_operation == "wait"
                    else "RECOVERY_CLOSE_FAILED"
                ),
                idempotency_key=f"{recovery_id}:{recovery_operation}:halted",
            )
            raise
        self._emit(
            "RECOVERY_OPERATION_RECONCILED",
            event_sink=event_sink,
            **identity,
            recovery_outcome=(
                "wait_terminal"
                if recovery_operation == "wait"
                else "close_confirmed"
            ),
            child_state=outcome.handle.state.value,
            terminal_receipt=receipt.to_dict(),
            idempotency_key=f"{recovery_id}:{recovery_operation}:reconciled",
        )
        return outcome

    @staticmethod
    def _validated_recovery_operation_result(
        session: _GroupSession,
        admitted_handle: ChildAgentHandle,
        expected_operation: str,
        *,
        recovery_operation: str,
        outcome: Any,
    ) -> ChildAgentTerminalReceipt:
        """Require a typed, admitted and terminal fact before recovery succeeds."""

        if not isinstance(outcome, ChildAgentOperationResult):
            raise HarnessValidationError(
                "recovery supervisor returned an invalid operation result",
                code="TASK_GROUP_RECOVERY_OPERATION_INVALID",
            )
        handle = outcome.handle
        if (
            outcome.operation_id != expected_operation
            or outcome.child_id != admitted_handle.child_id
            or handle.child_id != admitted_handle.child_id
            or handle.operation_id != expected_operation
            or handle.task_id != admitted_handle.task_id
            or handle.task_instance_id != admitted_handle.task_instance_id
            or handle.attempt != admitted_handle.attempt
            or handle.parent_graph_identity != session.group.parent_graph_identity
        ):
            raise HarnessValidationError(
                "recovery supervisor result differs from admitted child identity",
                code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH",
            )
        receipt = outcome.receipt
        if (
            not isinstance(receipt, ChildAgentTerminalReceipt)
            or receipt.child_id != admitted_handle.child_id
            or receipt.operation_id != expected_operation
            or receipt.parent_graph_identity != session.group.parent_graph_identity
            or not receipt.termination_confirmed
        ):
            raise HarnessValidationError(
                "child termination could not be confirmed during recovery",
                code="TASK_GROUP_RECOVERY_UNCONFIRMED",
                details={
                    "task_id": admitted_handle.task_id,
                    "child_id": admitted_handle.child_id,
                },
            )
        if recovery_operation == "wait":
            # A prior close may have succeeded just before its outcome audit
            # failed.  Repeating recovery must be able to re-read that same
            # immutable terminal receipt from a CLOSED handle; this is not a
            # relaxation for non-terminal states.
            allowed_states = {receipt.status, ChildAgentState.CLOSED}
        else:
            allowed_states = {ChildAgentState.CLOSED}
        if handle.state not in allowed_states:
            raise HarnessValidationError(
                "recovery supervisor operation state is not terminally verified",
                code="TASK_GROUP_RECOVERY_OPERATION_INVALID",
                details={
                    "task_id": admitted_handle.task_id,
                    "child_id": admitted_handle.child_id,
                    "recovery_operation": recovery_operation,
                },
            )
        return receipt

    def reconcile_spawn_intents(
        self,
        request: ParallelDispatchRequest,
        intents: tuple[Mapping[str, Any], ...],
        invoke: Callable[[TaskInstance], TaskResultRecord],
        *,
        admitted_waves: tuple[DispatchWave, ...] = (),
        admitted_group: DispatchGroup | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        """Restore verified admission evidence, then reconcile without spawning.

        The caller supplies canonical replay snapshots on restart. A complete
        wave is validated before any projection mutation or supervisor call.
        Unknown outcomes retain their reservations and close admission.
        """
        if self.child_supervisor is None:
            raise HarnessValidationError(
                "spawn reconciliation requires a supervisor",
                code="TASK_GROUP_RECOVERY_SUPERVISOR_REQUIRED",
            )
        if event_sink is None and self.event_sink is None:
            raise HarnessValidationError(
                "spawn reconciliation requires an audit sink",
                code="TASK_GROUP_RECOVERY_EVENT_SINK_REQUIRED",
            )
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        definition = self._group_definition(request)
        if admitted_group is not None and (
            not isinstance(admitted_group, DispatchGroup)
            or admitted_group.group_checksum != definition.group_checksum
        ):
            raise HarnessValidationError(
                "recovery group differs from immutable admission",
                code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH",
            )
        with self._lock:
            session = self._sessions.get(definition.group_id)
            group = session.group if session is not None else admitted_group
            if group is None:
                raise HarnessValidationError(
                    "restart recovery requires the durable group projection",
                    code="TASK_GROUP_RECOVERY_STATE_INVALID",
                )
            if (admitted_group is not None and admitted_group.state not in {
                DispatchGroupState.ADMITTED, DispatchGroupState.DISPATCHING,
                DispatchGroupState.RUNNING, DispatchGroupState.INDETERMINATE,
            }) or group.state not in {
                DispatchGroupState.ADMITTED, DispatchGroupState.DISPATCHING,
                DispatchGroupState.RUNNING, DispatchGroupState.INDETERMINATE,
            }:
                raise HarnessValidationError(
                    "terminal group cannot reconcile spawn admission",
                    code="TASK_GROUP_RECOVERY_STATE_INVALID",
                )
            wave, tasks = self._validate_spawn_recovery(
                request, group, session, intents, admitted_waves,
            )
            if session is None:
                session = _GroupSession(group=group, request=request)
                self._sessions[group.group_id] = session
            if not session.dispatch_lock.acquire(blocking=False):
                raise HarnessValidationError(
                    "dispatch group already has an active operation",
                    code="TASK_GROUP_DISPATCH_BUSY",
                )
            if not any(item.wave_id == wave.wave_id for item in session.waves):
                session.waves.append(wave)
                session.wave_instances[wave.wave_id] = tuple(instance for instance, _spawn in tasks)
                session.waves.sort(key=lambda item: item.ordinal)
                session.reserved.update(wave.task_ids)
                session.next_wave_ordinal = max(session.next_wave_ordinal, wave.ordinal + 1)
            elif wave.state is DispatchWaveState.RUNNING:
                session.waves = [wave if item.wave_id == wave.wave_id else item for item in session.waves]
        try:
            if wave.state is DispatchWaveState.RUNNING and all(
                spawn.operation_id in session.reconciled_spawns for _, spawn in tasks
            ):
                return self._result_for_session(session, request, limits=None)
            for item, spawn in tasks:
                self._reconcile_spawn_status(session, wave, item, spawn, invoke, event_sink)
            with self._lock:
                confirmed = all(
                    session.spawn_receipts.get(spawn.operation_id) == (
                        "SPAWN_CONFIRMED", spawn.child_id,
                    )
                    and item.task_id in session.active_children
                    for item, spawn in tasks
                )
                if confirmed:
                    # This transition is justified by the committed per-attempt
                    # RECOVERY_RECONCILED facts, never by an unlogged status read.
                    if session.group.state is DispatchGroupState.INDETERMINATE:
                        session.group = replace(session.group, state=(
                            DispatchGroupState.RUNNING if wave.state is DispatchWaveState.RUNNING
                            else DispatchGroupState.DISPATCHING
                        ))
                    self._mark_wave_dispatched(session, wave, event_sink=event_sink)
                elif session.group.state is not DispatchGroupState.INDETERMINATE:
                    self._mark_indeterminate(
                        group.group_id, reason_code="child_runtime_indeterminate",
                        event_sink=event_sink, diagnostics=("SPAWN_UNKNOWN",),
                    )
                return self._result_for_session(session, request, limits=None)
        finally:
            session.dispatch_lock.release()

    def _validate_spawn_recovery(
        self,
        request: ParallelDispatchRequest,
        group: DispatchGroup,
        session: _GroupSession | None,
        intents: tuple[Mapping[str, Any], ...],
        admitted_waves: tuple[DispatchWave, ...],
    ) -> tuple[DispatchWave, tuple[tuple[TaskInstance, ChildAgentSpawnRequest], ...]]:
        if not intents or any(not isinstance(raw, Mapping) for raw in intents):
            raise HarnessValidationError("spawn intents are invalid", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
        wave_ids = {identifier(raw.get("wave_id"), "wave_id") for raw in intents}
        task_ids = tuple(identifier(raw.get("task_id"), "task_id") for raw in intents)
        if len(wave_ids) != 1 or len(task_ids) != len(set(task_ids)):
            raise HarnessValidationError("spawn intents are not one unique wave", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
        wave_id = next(iter(wave_ids))
        if len(admitted_waves) > 1 or any(
            not isinstance(wave, DispatchWave) or wave.wave_id != wave_id
            or wave.group_id != group.group_id
            or wave.state not in {DispatchWaveState.ADMITTED, DispatchWaveState.RUNNING}
            for wave in admitted_waves
        ):
            raise HarnessValidationError("recovery admission wave is invalid", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
        local = next((wave for wave in session.waves if wave.wave_id == wave_id), None) if session else None
        wave = local or (admitted_waves[0] if admitted_waves else None)
        if local is not None and local.state is DispatchWaveState.ADMITTED and admitted_waves:
            wave = admitted_waves[0]
        if wave is None or wave.state not in {DispatchWaveState.ADMITTED, DispatchWaveState.RUNNING}:
            raise HarnessValidationError("recovery wave is not dispatchable", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
        if (
            wave.execution_mode != "SUPERVISED"
            or wave.group_id != group.group_id
            or set(wave.task_ids) != set(task_ids)
            or not set(task_ids).issubset(group.task_ids)
            or wave.effective_parallelism > group.max_parallelism
            or wave.ordinal > group.max_waves
        ):
            raise HarnessValidationError("recovery intents must cover the admitted wave", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
        if session and any(
            other.wave_id != wave_id and (
                other.ordinal == wave.ordinal or other.state is not DispatchWaveState.TERMINAL
            )
            for other in session.waves
        ):
            raise HarnessValidationError("recovery conflicts with another wave", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
        tasks = {item.task_id: item for item in request.task_instances}
        reservations = {item.task_id: item for item in wave.reservations}
        validated = []
        for raw in sorted(intents, key=lambda value: value["task_id"]):
            item = tasks.get(raw["task_id"])
            if (
                item is None or isinstance(raw.get("attempt"), bool)
                or raw.get("attempt") != item.attempt
                or raw.get("task_instance_id") != item.task_instance_id
                or raw.get("group_id") != group.group_id
                or raw.get("event_type") != "TASK_ATTEMPT_SPAWN_INTENT"
            ):
                raise HarnessValidationError("spawn intent does not match task instance", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
            reservation = reservations[item.task_id]
            if (
                reservation.idempotency_key != item.idempotency_key
                or dict(reservation.budget) != item.budget_snapshot.to_dict()
                or reservation.state is not ReservationState.RESERVED
            ):
                raise HarnessValidationError("spawn reservation differs from task", code="TASK_GROUP_RECOVERY_WAVE_INVALID")
            spawn = self._spawn_request(request, wave, item)
            if raw.get("operation_key") != spawn.operation_id or raw.get("idempotency_key") != spawn.operation_id:
                raise HarnessValidationError("spawn intent operation key is invalid", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
            if raw.get("budget_reservation") != dict(spawn.budget):
                raise HarnessValidationError("spawn intent differs from attempt ledger", code="TASK_GROUP_RECOVERY_INTENT_INVALID")
            validated.append((item, spawn))
        return wave, tuple(validated)

    def _reconcile_spawn_status(
        self,
        session: _GroupSession,
        wave: DispatchWave,
        item: TaskInstance,
        spawn: ChildAgentSpawnRequest,
        invoke: Callable[[TaskInstance], TaskResultRecord],
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
    ) -> None:
        operation_key = spawn.operation_id
        with self._lock:
            if operation_key in session.reconciled_spawns:
                return
            pending = session.recovery_status_reads.get(operation_key)
        identity = {
            "group_id": wave.group_id, "wave_id": wave.wave_id,
            "task_id": item.task_id, "task_instance_id": item.task_instance_id,
            "attempt": item.attempt, "operation_key": operation_key,
        }
        if pending is None:
            recovery_id = f"recovery-{uuid4().hex}"
            self._emit(
                "RECOVERY_STATUS_READ", event_sink=event_sink, **identity,
                recovery_id=recovery_id, recovery_outcome="status_read",
                idempotency_key=f"{recovery_id}:status-read",
            )
            assert self.child_supervisor is not None and spawn.child_id is not None
            try:
                handle = self.child_supervisor.status(spawn.child_id, operation_id=operation_key)
            except ChildAgentOperationConflict:
                self._emit(
                    "RECOVERY_HALTED", event_sink=event_sink, **identity,
                    recovery_id=recovery_id, reason_code="SPAWN_IDENTITY_CONFLICT",
                    idempotency_key=f"{recovery_id}:halted",
                )
                self._mark_indeterminate(
                    wave.group_id, reason_code="child_runtime_indeterminate", event_sink=event_sink,
                    diagnostics=("SPAWN_IDENTITY_CONFLICT",),
                )
                raise
            except ChildAgentSupervisorError:
                handle = None
            if handle is not None and (not isinstance(handle, ChildAgentHandle) or any(
                getattr(handle, name) != getattr(spawn, name)
                for name in (
                    "child_id", "operation_id", "parent_graph_identity", "stage_id",
                    "task_id", "task_instance_id", "attempt", "allowed_tools",
                    "allowed_memory_namespaces", "budget",
                )
            )):
                self._emit(
                    "RECOVERY_HALTED", event_sink=event_sink, **identity,
                    recovery_id=recovery_id, reason_code="SPAWN_IDENTITY_CONFLICT",
                    idempotency_key=f"{recovery_id}:halted",
                )
                self._mark_indeterminate(
                    wave.group_id, reason_code="child_runtime_indeterminate", event_sink=event_sink,
                    diagnostics=("SPAWN_IDENTITY_CONFLICT",),
                )
                raise HarnessValidationError("supervisor handle identity mismatch", code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH")
            if handle is not None and handle.state is ChildAgentState.LOST:
                self._emit(
                    "RECOVERY_HALTED", event_sink=event_sink, **identity,
                    recovery_id=recovery_id, reason_code="CHILD_NOT_TRACKABLE",
                    idempotency_key=f"{recovery_id}:halted",
                )
                self._mark_indeterminate(
                    wave.group_id, reason_code="child_runtime_indeterminate", event_sink=event_sink,
                    diagnostics=("CHILD_NOT_TRACKABLE",),
                )
                raise HarnessValidationError("supervisor child is not trackable", code="TASK_GROUP_RECOVERY_UNCONFIRMED")
            with self._lock:
                session.recovery_status_reads[operation_key] = (recovery_id, handle)
                if handle is not None:
                    worker = session.active_children.get(item.task_id, (None, None))[1]
                    session.active_children[item.task_id] = (handle, worker or _SupervisorTaskWorker(invoke, item))
        else:
            recovery_id, handle = pending
        status = "SPAWN_CONFIRMED" if handle is not None else "SPAWN_UNKNOWN"
        receipt = {
            "event_type": f"TASK_ATTEMPT_{status}", **identity,
            "spawn_status": status, "idempotency_key": operation_key,
            **({"child_id": handle.child_id} if handle is not None else {}),
        }
        try:
            self._emit(receipt.pop("event_type"), event_sink=event_sink, **receipt)
        except HarnessValidationError as exc:
            if exc.code != "task_plan_event_history_conflict":
                raise
            self._emit(
                "RECOVERY_HALTED", event_sink=event_sink, **identity,
                recovery_id=recovery_id, reason_code="SPAWN_IDENTITY_CONFLICT",
                idempotency_key=f"{recovery_id}:halted",
            )
            self._mark_indeterminate(
                wave.group_id, reason_code="child_runtime_indeterminate", event_sink=event_sink,
                diagnostics=("SPAWN_IDENTITY_CONFLICT",),
            )
            raise
        with self._lock:
            session.spawn_receipts[operation_key] = (status, handle.child_id if handle else None)
        self._emit(
            "RECOVERY_RECONCILED" if handle else "RECOVERY_HALTED",
            event_sink=event_sink, **identity, recovery_id=recovery_id,
            **({"recovery_outcome": status, "child_id": handle.child_id} if handle else {"reason_code": status}),
            idempotency_key=f"{recovery_id}:{'reconciled' if handle else 'halted'}",
        )
        with self._lock:
            session.reconciled_spawns.add(operation_key)

    def dispatch(
        self,
        request: ParallelDispatchRequest,
        invoke: Callable[[TaskInstance], TaskResultRecord],
        *,
        limits: ParentObservationLimits | None = None,
        finalize: bool = True,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        group = self.create_group(request, event_sink=event_sink)
        with self._lock:
            session = self._sessions[group.group_id]
        if not session.dispatch_lock.acquire(blocking=False):
            with self._lock:
                return self._result_for_session(session, request, limits=limits)
        try:
            return self._dispatch(
                request, invoke, limits=limits, finalize=finalize, event_sink=event_sink,
            )
        finally:
            session.dispatch_lock.release()

    def _dispatch(
        self,
        request: ParallelDispatchRequest,
        invoke: Callable[[TaskInstance], TaskResultRecord],
        *,
        limits: ParentObservationLimits | None = None,
        finalize: bool = True,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        group = self.create_group(request, event_sink=event_sink)
        with self._lock:
            session = self._sessions[group.group_id]
            if session.group.state in {
                DispatchGroupState.SUCCEEDED,
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
                DispatchGroupState.INDETERMINATE,
                DispatchGroupState.HALTED,
                DispatchGroupState.SUPERSEDED,
            }:
                return self._result_for_session(session, request, limits=limits)
            if any(wave.state is not DispatchWaveState.TERMINAL for wave in session.waves):
                return self._result_for_session(session, request, limits=limits, diagnostics=("ACTIVE_WAVE_PENDING",))
            # The request order is the scheduler's pinned
            # priority/dependency-depth/task-id/checksum order.  Never replace
            # it with coordinator-local task-id sorting.
            ordered = request.task_instances
            pending = tuple(
                item
                for item in ordered
                if item.task_id not in session.reserved
                and (
                    item.task_id not in session.results
                    or (
                        session.results[item.task_id].status is TaskLifecycle.FAILED
                        and item.attempt > session.results[item.task_id].attempt
                    )
                )
            )
            if set(item.task_id for item in pending) - set(group.task_ids):
                raise HarnessValidationError("dispatch task is outside group join scope", code="TASK_GROUP_SCOPE_MISMATCH")
            if session.group.state in {DispatchGroupState.JOINING, DispatchGroupState.REPLAN_PENDING}:
                return self._result_for_session(session, request, limits=limits)
            if pending:
                effective, degraded_reason = self._dispatch_parallelism(request)
            else:
                effective, degraded_reason = 1, None
            session.degraded_reason = session.degraded_reason or degraded_reason
            if session.degraded_reason is not None:
                self._emit("DEGRADED_SERIAL", event_sink=event_sink, group_id=group.group_id, reason_code=session.degraded_reason)

        pending_work = list(pending)
        ledger = TaskPlanBudgetLedger.from_snapshot(request.budget_snapshot)
        pool_state = {pool.pool_id: pool for pool in request.capacity_pools}
        capacity_snapshot = request.capacity_snapshot
        while pending_work:
            with self._lock:
                if not _GROUP_TRANSITIONS[session.group.state] or session.group.state in {
                    DispatchGroupState.JOINING, DispatchGroupState.REPLAN_PENDING,
                }:
                    break
                if len(session.waves) >= session.group.max_waves:
                    session.group = session.group.transitioned(DispatchGroupState.HALTED)
                    session.terminal_diagnostics = ("WAVE_LIMIT_EXCEEDED",)
                    self._emit(
                        "TASK_GROUP_HALTED", event_sink=event_sink,
                        group=session.group.to_dict(), group_id=group.group_id,
                        reason_code="WAVE_LIMIT_EXCEEDED",
                        idempotency_key=group.group_id,
                    )
                    break
                owner_scope = (
                    capacity_snapshot.owner_scope
                    if capacity_snapshot is not None
                    else f"{request.plan.run_id}/{request.plan.stage_id}/{request.plan.plan_id}"
                )
                occupied_resource_demands = tuple(
                    demand
                    for task_id in session.reserved
                    for demand in (request.task_capacity_demands.get(task_id),)
                    if demand is not None
                    and demand.resource_conflict_key is not None
                )
                packing = pack_first_fit(
                    [item.task_id for item in pending_work],
                    request.task_capacity_demands,
                    pool_state,
                    max_tasks=effective,
                    occupied_resource_demands=occupied_resource_demands,
                    owner_scope=owner_scope,
                    reservation_keys={item.task_id: item.idempotency_key for item in pending_work},
                    task_instances={item.task_id: item for item in pending_work},
                    budget_snapshot=ledger.snapshot(),
                    capacity_snapshot=capacity_snapshot,
                )
                if not packing.selected:
                    if packing.overflow and all(
                        packing.reasons[task_id] == "BUDGET_EXCEEDED"
                        for task_id in packing.overflow
                    ):
                        raise HarnessValidationError(
                            "no logically ready task fits the remaining budget",
                            code="BUDGET_EXCEEDED",
                        )
                    waiting_evidence = _capacity_waiting_evidence(
                        session.group,
                        packing,
                        budget_checksum=ledger.to_dict()["ledger_checksum"],
                    )
                    self._emit(
                        "TASK_GROUP_CAPACITY_WAITING",
                        event_sink=event_sink,
                        group_id=group.group_id,
                        waiting=waiting_evidence,
                        reason_code="CAPACITY_NOT_AVAILABLE",
                        idempotency_key=waiting_evidence["waiting_key"],
                    )
                    session.terminal_diagnostics = ("CAPACITY_WAITING",)
                    break
                # A later dispatch may observe newly available capacity for
                # the same durable group.  The waiting diagnostic describes
                # the previous non-terminal observation and must not leak
                # into the admitted wave's result.
                session.terminal_diagnostics = ()
                selected_ids = set(packing.selected)
                batch = tuple(item for item in pending_work if item.task_id in selected_ids)
                reservation_allocations = {
                    item.task_id: dict(item.allocations)
                    for item in packing.reservations
                }
                reservation_policy_checksums = {
                    item.task_id: dict(item.policy_checksums)
                    for item in packing.reservations
                }
                pool_reservations = {
                    item.task_id: item for item in packing.reservations
                }
                for item in batch:
                    reservation_allocations.setdefault(item.task_id, {})
                    reservation_policy_checksums.setdefault(item.task_id, {})
                wave = DispatchWave(
                    group.group_id,
                    session.next_wave_ordinal,
                    tuple(item.task_id for item in batch),
                    effective,
                    tuple(
                        TaskReservation(
                            item.task_id,
                            item.idempotency_key,
                            item.budget_snapshot.to_dict(),
                            capacity_allocations=reservation_allocations[item.task_id],
                            capacity_policy_checksums=reservation_policy_checksums[item.task_id],
                            capacity_reservation=pool_reservations.get(item.task_id),
                        )
                        for item in batch
                    ),
                    DispatchWaveState.ADMITTED,
                    execution_mode=(
                        "SERIAL" if self._requires_serial_fallback_transport()
                        else "INLINE_TEST" if self.child_supervisor is None
                        else "SUPERVISED"
                    ),
                    packing=packing,
                )
                admitted_ledger = TaskPlanBudgetLedger.from_snapshot(
                    packing.admitted_budget_snapshot
                )
                admitted_request = replace(request, budget_snapshot=admitted_ledger.snapshot())
                spawn_requests = (
                    tuple(self._spawn_request(admitted_request, wave, item) for item in batch)
                    if wave.execution_mode == "SUPERVISED" else ()
                )
                admission = {
                    "event_type": "TASK_WAVE_ADMITTED",
                    "group": session.group.to_dict(), "wave": wave.to_dict(),
                    "requested_parallelism": request.requested_parallelism or group.max_parallelism,
                    "effective_parallelism": wave.effective_parallelism,
                    "budget_before_checksum": ledger.to_dict()["ledger_checksum"],
                    "budget_after_checksum": admitted_ledger.to_dict()["ledger_checksum"],
                    "packing_checksum": packing.packing_checksum,
                    "queue_wait_ms": _elapsed_ms(session.started_at),
                    "idempotency_key": wave.wave_id,
                }
                intents = tuple(
                    {
                        "event_type": "TASK_ATTEMPT_SPAWN_INTENT",
                        "group_id": group.group_id, "wave_id": wave.wave_id,
                        "task_id": item.task_id, "task_instance_id": item.task_instance_id,
                        "attempt": item.attempt,
                        "operation_key": item.operation_id,
                        "idempotency_key": item.operation_id,
                        "budget_reservation": dict(item.budget),
                    }
                    for item in spawn_requests
                )
                # The embedded reservations and every spawn intent are one
                # durable commit. No local admission or child precedes it.
                self._emit_batch((admission, *intents), event_sink=event_sink)
                ledger = admitted_ledger
                if packing.capacity_after is not None:
                    capacity_snapshot = packing.capacity_after
                    pool_state = {
                        pool.pool_id: pool for pool in capacity_snapshot.pools
                    }
                pending_work = [item for item in pending_work if item.task_id not in {entry.task_id for entry in batch}]
                session.next_wave_ordinal += 1
                session.reserved.update(wave.task_ids)
                session.waves.append(wave)
                session.wave_instances[wave.wave_id] = batch
                session.wave_admitted_at[wave.wave_id] = monotonic()
                session.group = session.group.transitioned(DispatchGroupState.DISPATCHING)
            if capacity_now_ms() >= session.group.absolute_deadline_ms:
                self._mark_indeterminate(group.group_id, reason_code="group_runtime_deadline_exceeded", event_sink=event_sink)
                raise HarnessValidationError("dispatch group runtime deadline exceeded", code="TASK_GROUP_DEADLINE_EXCEEDED")
            with self._lock:
                current_session = self._sessions[group.group_id]
                if not _GROUP_TRANSITIONS[current_session.group.state]:
                    break
            try:
                outcome = self._run_wave(
                    session,
                    request,
                    wave,
                    batch,
                    invoke,
                    event_sink=event_sink,
                    spawn_requests=spawn_requests,
                )
            except BaseException as exc:
                self._mark_indeterminate(
                    group.group_id,
                    reason_code=(
                        exc.code
                        if isinstance(exc, HarnessValidationError) and exc.code
                        else "child_runtime_indeterminate"
                    ),
                    event_sink=event_sink,
                    diagnostics=(type(exc).__name__,),
                )
                raise
            with self._lock:
                session = self._sessions[group.group_id]
                if not _GROUP_TRANSITIONS[session.group.state]:
                    session.quarantined_task_ids.update(item.task_id for item in outcome.results)
                    for result in outcome.results:
                        self._record_attempt(session, wave, next(item for item in batch if item.task_id == result.task_id),
                                             outcome=TaskAttemptOutcome.QUARANTINED, result=result, event_sink=event_sink,
                                             receipt=session.attempt_receipts.get(result.task_instance_id), reason_code="group_closed")
                    break
                for result in sorted(outcome.results, key=lambda item: item.task_id):
                    self._record_attempt(session, wave, next(item for item in batch if item.task_id == result.task_id),
                                         result=result, event_sink=event_sink,
                                         receipt=session.attempt_receipts.get(result.task_instance_id))
                    session.results[result.task_id] = result
                session.reserved.difference_update(wave.task_ids)
                session.quarantined_task_ids.update(outcome.quarantined_task_ids)
                confirmed_terminal_task_ids = (
                    set(outcome.consumed_task_ids)
                    | set(outcome.released_task_ids)
                    | set(outcome.reclaimed_task_ids)
                )
                reservation_states = {
                    item.task_id: (
                        ReservationState.RELEASED
                        if item.task_id in outcome.released_task_ids
                        else ReservationState.CONSUMED
                        if item.task_id in confirmed_terminal_task_ids
                        else ReservationState.RESERVED
                    )
                    for item in wave.reservations
                }
                terminal_wave = replace(
                    wave.transitioned(
                        DispatchWaveState.TERMINAL,
                        terminal_outcome=_terminal_wave_outcome(outcome),
                    ),
                    reservations=tuple(
                        TaskReservation(
                            item.task_id,
                            item.idempotency_key,
                            item.budget,
                            reservation_states[item.task_id],
                            item.capacity_allocations,
                            item.capacity_policy_checksums,
                            capacity_reservation=(
                                item.capacity_reservation
                                if reservation_states[item.task_id]
                                is ReservationState.RESERVED
                                else item.capacity_reservation.settled(
                                    reservation_states[item.task_id],
                                    reservation_key=item.idempotency_key,
                                    expected_version=1,
                                )
                                if item.capacity_reservation is not None
                                else None
                            ),
                        )
                        for item in wave.reservations
                    ),
                )
                session.waves = [terminal_wave if item.wave_id == wave.wave_id else item for item in session.waves]
                session.group = session.group.transitioned(DispatchGroupState.RUNNING)
                dispatched_at = session.wave_dispatched_at.get(
                    wave.wave_id,
                    session.wave_admitted_at.get(wave.wave_id, session.started_at),
                )
                capacity_before_release = capacity_snapshot
                capacity_after_release = _capacity_release_transition(
                    capacity_before_release,
                    wave,
                    reservation_states,
                )
                if capacity_after_release is not None:
                    pool_state = {
                        pool.pool_id: pool
                        for pool in capacity_after_release.pools
                    }
                self._emit(
                    "TASK_WAVE_COMPLETED",
                    event_sink=event_sink,
                    group_id=group.group_id,
                    wave_id=wave.wave_id,
                    task_ids=list(wave.task_ids),
                    reservation_states={
                        task_id: state.value
                        for task_id, state in reservation_states.items()
                    },
                    child_states={
                        **{task_id: TaskLifecycle.FAILED.value for task_id in outcome.reclaimed_task_ids},
                        **{
                        item.task_id: item.status.value
                        for item in sorted(outcome.results, key=lambda item: item.task_id)
                        },
                    },
                    terminal_outcome=terminal_wave.terminal_outcome.value,
                    capacity_before=(
                        capacity_before_release.to_dict()
                        if capacity_before_release is not None else None
                    ),
                    capacity_after=(
                        capacity_after_release.to_dict()
                        if capacity_after_release is not None else None
                    ),
                    run_duration_ms=_elapsed_ms(dispatched_at),
                )
                capacity_snapshot = capacity_after_release
                if request.join_policy is JoinPolicy.FAIL_FAST and any(
                    item.status is TaskLifecycle.FAILED for item in outcome.results
                ):
                    session.group = session.group.transitioned(DispatchGroupState.FAILED)
                    session.terminal_diagnostics = ("TASK_FAILED",)
                    self._release_pending_waves(
                        session,
                        event_sink=event_sink,
                        reason_code="fail_fast",
                        capacity_snapshot=capacity_after_release,
                    )
                    self._emit(
                        "TASK_GROUP_FAILED",
                        event_sink=event_sink,
                        group=session.group.to_dict(),
                        reason_code="TASK_FAILED",
                        quarantined_task_ids=sorted(outcome.quarantined_task_ids),
                        group_duration_ms=_elapsed_ms(session.started_at),
                        idempotency_key=session.group.group_id,
                    )
                    break

        if finalize:
            return self.join(request, limits=limits, event_sink=event_sink)
        return self._result_for_session(self._sessions[group.group_id], request, limits=limits)

    def join(
        self,
        request: ParallelDispatchRequest,
        *,
        limits: ParentObservationLimits | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        group = self.create_group(request, event_sink=event_sink)
        with self._lock:
            session = self._sessions[group.group_id]
            if session.join_started_at is None:
                session.join_started_at = monotonic()
            expected = set(group.task_ids)
            received = set(session.results) | session.blocked_task_ids | session.failed_task_ids
            missing = sorted(expected - received)
            previous_state = session.group.state
            if session.group.state in {
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
                DispatchGroupState.INDETERMINATE,
                DispatchGroupState.HALTED,
                DispatchGroupState.SUPERSEDED,
            }:
                state = session.group.state
                diagnostics = session.terminal_diagnostics or (("TASK_FAILED",) if state is DispatchGroupState.FAILED else (state.value,))
            elif missing:
                # A join observation is not itself permission to close group
                # admission. Keep the live state active until all required
                # tasks are terminal; otherwise a later dispatch would be an
                # invalid JOINING -> DISPATCHING transition.
                state = session.group.state
                diagnostics = session.terminal_diagnostics or ("JOIN_WAITING",)
            else:
                state, diagnostics = self._terminal_state(session)
                if state is not session.group.state:
                    session.terminal_diagnostics = diagnostics
            if missing:
                result = self._result_for_session(session, request, limits=limits, diagnostics=diagnostics)
                return result
            if state in {
                DispatchGroupState.SUCCEEDED,
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
            } and state is not session.group.state and session.group.state not in {
                DispatchGroupState.JOINING,
                DispatchGroupState.REPLAN_PENDING,
            }:
                session.group = session.group.transitioned(DispatchGroupState.JOINING)
            session.group = session.group.transitioned(state)
            result = self._result_for_session(session, request, limits=limits, diagnostics=diagnostics)
            if state is DispatchGroupState.SUCCEEDED:
                event_type = "TASK_GROUP_JOINED"
            elif state is DispatchGroupState.FAILED:
                event_type = "TASK_GROUP_FAILED"
            elif state is DispatchGroupState.CANCELLED:
                event_type = "TASK_GROUP_CANCELLED"
            elif state is DispatchGroupState.INDETERMINATE:
                event_type = "TASK_GROUP_INDETERMINATE"
            else:
                event_type = "TASK_GROUP_JOIN_WAITING"
            if previous_state is state and state in {
                DispatchGroupState.SUCCEEDED,
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
                DispatchGroupState.INDETERMINATE,
                DispatchGroupState.HALTED,
                DispatchGroupState.SUPERSEDED,
            }:
                return result
            self._emit(
                event_type,
                event_sink=event_sink,
                group=session.group.to_dict(),
                observation=thaw_mapping(result.projected_observation),
                join_duration_ms=_elapsed_ms(session.join_started_at),
                group_duration_ms=_elapsed_ms(session.started_at),
                idempotency_key=session.group.group_id,
            )
            return result

    def sync_task_terminals(
        self,
        request: ParallelDispatchRequest,
        projection: TaskPlanProjection,
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        """Hydrate deterministic task terminal facts for a wait-all join.

        Blocked tasks have no attempt or worker result by contract. Their
        projection is therefore tracked separately from accepted results;
        failed task ids are tracked only to close the group with a typed
        partial-failure outcome.
        """

        group = self.create_group(request, event_sink=event_sink)
        if not projection.matches_plan_identity(request.plan):
            raise HarnessValidationError(
                "parallel terminal projection does not match dispatch group",
                code="TASK_GROUP_SCOPE_MISMATCH",
            )
        task_ids = set(group.task_ids)
        with self._lock:
            session = self._sessions[group.group_id]
            for state in projection.tasks:
                if state.task_id not in task_ids:
                    continue
                if state.status is TaskLifecycle.BLOCKED_DEPENDENCY:
                    session.blocked_task_ids.add(state.task_id)
                    session.blocked_task_checksums[state.task_id] = state.task_definition_checksum
                elif state.status is TaskLifecycle.FAILED:
                    session.failed_task_ids.add(state.task_id)
            unknown = (session.blocked_task_ids | session.failed_task_ids) - task_ids
            if unknown:
                raise HarnessValidationError(
                    "parallel terminal projection is outside group scope",
                    code="TASK_GROUP_SCOPE_MISMATCH",
                    details={"task_ids": sorted(unknown)},
                )

    def cancel(
        self,
        group_id: str,
        *,
        request: ParallelDispatchRequest | None = None,
        reason_code: str = "cancel_requested",
        limits: ParentObservationLimits | None = None,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> ParallelDispatchResult:
        with self._lock:
            session = self._sessions.get(group_id)
            if session is None:
                raise HarnessValidationError("unknown dispatch group", code="TASK_GROUP_NOT_FOUND")
            request = request or session.request
            if session.group.state in {
                DispatchGroupState.SUCCEEDED,
                DispatchGroupState.FAILED,
                DispatchGroupState.CANCELLED,
                DispatchGroupState.INDETERMINATE,
                DispatchGroupState.HALTED,
                DispatchGroupState.SUPERSEDED,
            }:
                return self._result_for_session(session, request, limits=limits)
            self._emit("TASK_GROUP_CANCEL_REQUESTED", event_sink=event_sink, group_id=group_id, reason_code=reason_code, idempotency_key=group_id)
            active = tuple(session.active_children.items())
        unconfirmed = False
        if self.child_supervisor is not None:
            for task_id, (handle, _worker) in active:
                operation = self.child_supervisor.cancel(handle.child_id, operation_id=handle.operation_id, reason=reason_code)
                instance = task_instance_for_attempt(request.plan, task_id, handle.attempt,
                                                     task_instance_id=handle.task_instance_id)
                wave = _wave_for_child(session, handle)
                receipt = operation.receipt
                candidate = _worker.result if _worker is not None else None
                if candidate is None and operation.result is not None and isinstance(operation.result.get("task_result"), Mapping):
                    candidate = TaskResultRecord.from_dict(operation.result["task_result"])
                outcome = (TaskAttemptOutcome.INDETERMINATE if receipt is None or not receipt.termination_confirmed
                           else TaskAttemptOutcome.CANCELLED if receipt.status is ChildAgentState.CANCELLED
                           else TaskAttemptOutcome.QUARANTINED)
                self._record_attempt(session, wave, instance, outcome=outcome,
                                     result=candidate if outcome is TaskAttemptOutcome.QUARANTINED else None,
                                     receipt=receipt, reason_code=reason_code, event_sink=event_sink)
                if operation.receipt is None or not operation.receipt.termination_confirmed:
                    unconfirmed = True
                    continue
                self.child_supervisor.close(handle.child_id, operation_id=handle.operation_id)
                with self._lock:
                    self._sessions[group_id].active_children.pop(task_id, None)
        with self._lock:
            session = self._sessions[group_id]
            recorded_instances = {item.task_instance_id for item in session.attempt_history.values()}
            for wave in session.waves:
                for instance in session.wave_instances.get(wave.wave_id, ()):
                    if instance.task_instance_id in recorded_instances:
                        continue
                    never_started = wave.execution_mode != "SUPERVISED" and wave.state is DispatchWaveState.ADMITTED
                    self._record_attempt(session, wave, instance,
                                         outcome=TaskAttemptOutcome.CANCELLED if never_started else TaskAttemptOutcome.INDETERMINATE,
                                         reason_code=reason_code, event_sink=event_sink)
                    unconfirmed = unconfirmed or not never_started
            self._release_pending_waves(session, event_sink=event_sink,
                                        reason_code="termination_unconfirmed" if unconfirmed else reason_code)
            state = DispatchGroupState.INDETERMINATE if unconfirmed else DispatchGroupState.CANCELLED
            session.group = session.group.transitioned(state)
            self._emit(
                "TASK_GROUP_INDETERMINATE" if unconfirmed else "TASK_GROUP_CANCELLED",
                event_sink=event_sink,
                group=session.group.to_dict(),
                group_id=group_id,
                reason_code=reason_code,
                group_duration_ms=_elapsed_ms(session.started_at),
                idempotency_key=group_id,
            )
            return self._result_for_session(session, request, limits=limits, diagnostics=(reason_code,))

    @staticmethod
    def _spawn_request(
        request: ParallelDispatchRequest,
        wave: DispatchWave,
        item: TaskInstance,
    ) -> ChildAgentSpawnRequest:
        if request.parent_graph_identity is None:
            raise HarnessValidationError(
                "supervised parallel dispatch requires parent Graph identity",
                code="TASK_GROUP_PARENT_IDENTITY_REQUIRED",
            )
        definition = next((task for task in request.plan.tasks if task.task_id == item.task_id), None)
        if definition is None or not definition.allowed_tools or not definition.allowed_memory_namespaces:
            raise HarnessValidationError(
                "supervised task is missing concrete capability admission",
                code="CHILD_CAPABILITY_ADMISSION_REQUIRED",
            )
        operation_id = spawn_operation_key(wave.group_id, wave.wave_id, item.task_instance_id, item.attempt)
        return ChildAgentSpawnRequest(
            parent_graph_identity=request.parent_graph_identity,
            stage_id=request.plan.stage_id,
            task_id=item.task_id,
            task_instance_id=item.task_instance_id,
            attempt=item.attempt,
            allowed_tools=tuple(definition.allowed_tools),
            allowed_memory_namespaces=tuple(definition.allowed_memory_namespaces),
            budget=child_budget_reservation(
                TaskPlanBudgetLedger.from_snapshot(request.budget_snapshot), item,
                group_id=wave.group_id, wave_id=wave.wave_id,
            ),
            operation_id=operation_id,
            child_id=f"parallel-{item.task_instance_id}",
            lease_seconds=min(request.max_group_runtime_seconds, 3600.0),
        )

    def _mark_wave_dispatched(
        self,
        session: _GroupSession,
        wave: DispatchWave,
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
    ) -> None:
        with self._lock:
            current = next((item for item in session.waves if item.wave_id == wave.wave_id), None)
            if current is not None and current.state is DispatchWaveState.RUNNING:
                return
            if not _GROUP_TRANSITIONS[session.group.state]:
                raise HarnessValidationError("group closed before dispatch", code="TASK_GROUP_DISPATCH_CLOSED")
            running_group = session.group.transitioned(DispatchGroupState.RUNNING)
            running = wave.transitioned(DispatchWaveState.DISPATCHING).transitioned(DispatchWaveState.RUNNING)
            dispatched_at = monotonic()
            queued_at = session.wave_admitted_at.get(wave.wave_id, session.started_at)
            self._emit(
                "TASK_WAVE_DISPATCHED", event_sink=event_sink,
                group_id=wave.group_id, wave_id=wave.wave_id,
                task_ids=list(wave.task_ids),
                queue_wait_ms=_elapsed_ms(queued_at, now=dispatched_at),
                idempotency_key=wave.wave_id,
            )
            session.group = running_group
            session.waves = [running if item.wave_id == wave.wave_id else item for item in session.waves]
            session.wave_dispatched_at[wave.wave_id] = dispatched_at

    def _record_attempt(
        self, session: _GroupSession, wave: DispatchWave, instance: TaskInstance,
        *, event_sink: Callable[[Mapping[str, Any]], Any] | None,
        outcome: TaskAttemptOutcome | None = None, result: TaskResultRecord | None = None,
        receipt: Any | None = None, reason_code: str | None = None,
        recovered_from: str | None = None, recovery_receipt: Any | None = None,
    ) -> TaskAttemptHistoryRecord:
        group = replace(session.group, state=DispatchGroupState.ADMITTED)
        admitted_wave = replace(wave, state=DispatchWaveState.ADMITTED, terminal_outcome=None,
                                reservations=tuple(replace(item, state=ReservationState.RESERVED) for item in wave.reservations))
        operation_key = spawn_operation_key(group.group_id, wave.wave_id, instance.task_instance_id, instance.attempt) if wave.execution_mode == "SUPERVISED" else None
        child_id = receipt.child_id if receipt is not None else (session.spawn_receipts.get(operation_key, (None, None))[1] if operation_key else None)
        kwargs = dict(group=group, wave=admitted_wave, operation_key=operation_key,
                      child_id=child_id, terminal_receipt=receipt,
                      recovered_from=recovered_from, recovery_receipt=recovery_receipt)
        if outcome is None and session.group.state in {
            DispatchGroupState.CANCELLED, DispatchGroupState.HALTED, DispatchGroupState.SUPERSEDED,
        }:
            outcome = TaskAttemptOutcome.QUARANTINED
            reason_code = "group_closed"
        if outcome is None:
            record = TaskAttemptHistoryRecord.for_result(session.request.plan, result, instance=instance, **kwargs)
        else:
            definition = next(item for item in session.request.plan.tasks if item.task_id == instance.task_id)
            record = TaskAttemptHistoryRecord(instance, definition.binding_checksum, outcome, result=result,
                                              reason_code=reason_code, **kwargs)
        with self._lock:
            previous = session.attempt_history.get(record.record_checksum)
            if previous is not None:
                return previous
            self._emit("TASK_ATTEMPT_RECORDED", event_sink=event_sink,
                       group_id=group.group_id, wave_id=wave.wave_id, task_id=instance.task_id,
                       task_instance_id=instance.task_instance_id, attempt=instance.attempt,
                       history_record=record.to_dict(), idempotency_key=record.record_checksum)
            session.attempt_history[record.record_checksum] = record
        return record

    def _run_wave(
        self,
        session: _GroupSession,
        request: ParallelDispatchRequest,
        wave: DispatchWave,
        batch: tuple[TaskInstance, ...],
        invoke: Callable[[TaskInstance], TaskResultRecord],
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        spawn_requests: tuple[ChildAgentSpawnRequest, ...],
    ) -> _WaveRunOutcome:
        if self._requires_serial_fallback_transport():
            if self.serial_executor is None:
                raise HarnessValidationError(
                    "serial fallback requires an explicit executor adapter",
                    code="TASK_GROUP_WAVE_ADAPTER_REQUIRED",
                )
            if wave.effective_parallelism != 1 or len(batch) != 1:
                raise HarnessValidationError(
                    "serial fallback wave must contain exactly one task",
                    code="TASK_GROUP_SERIAL_WAVE_INVALID",
                )
            item = batch[0]
            self._mark_wave_dispatched(session, wave, event_sink=event_sink)
            result = _validated_task_result(
                self.serial_executor.execute(item, invoke),
                item,
            )
            self._record_attempt(session, wave, item, result=result, event_sink=event_sink)
            return _WaveRunOutcome(
                (result,),
                consumed_task_ids=frozenset((item.task_id,)),
            )

        if self.child_supervisor is None:
            if not self._allow_test_executor:
                raise HarnessValidationError(
                    "parallel wave transport is unavailable",
                    code="TASK_GROUP_WAVE_ADAPTER_REQUIRED",
                )
            results: list[TaskResultRecord] = []
            released: set[str] = set()
            consumed: set[str] = set()
            quarantined: set[str] = set()
            self._mark_wave_dispatched(session, wave, event_sink=event_sink)
            with ThreadPoolExecutor(max_workers=wave.effective_parallelism, thread_name_prefix="newsroom-dispatch") as pool:
                futures = [(item, pool.submit(invoke, item)) for item in batch]
                pending = {future: item for item, future in futures}
                while pending:
                    completed, _ = wait(
                        tuple(pending),
                        return_when=FIRST_COMPLETED,
                    )
                    completed_items = sorted(
                        ((pending.pop(future), future) for future in completed),
                        key=lambda pair: pair[0].task_id,
                    )
                    should_stop = False
                    for item, future in completed_items:
                        result = future.result()
                        if not isinstance(result, TaskResultRecord):
                            raise HarnessValidationError("parallel worker returned invalid result", code="RESULT_SCHEMA_INVALID")
                        consumed.add(item.task_id)
                        if should_stop:
                            self._record_attempt(session, wave, item, outcome=TaskAttemptOutcome.QUARANTINED,
                                                 result=result, reason_code="fail_fast", event_sink=event_sink)
                            quarantined.add(item.task_id)
                            continue
                        self._record_attempt(session, wave, item, result=result, event_sink=event_sink)
                        results.append(result)
                        if (
                            request.join_policy is JoinPolicy.FAIL_FAST
                            and result.status is TaskLifecycle.FAILED
                        ):
                            should_stop = True
                    if not should_stop:
                        continue
                    self._emit(
                        "TASK_GROUP_CANCEL_REQUESTED",
                        event_sink=event_sink,
                        group_id=session.group.group_id,
                        reason_code="fail_fast",
                        idempotency_key=f"{session.group.group_id}:fail_fast",
                    )
                    for sibling_future, sibling in pending.items():
                        if sibling_future.cancel():
                            self._record_attempt(session, wave, sibling, outcome=TaskAttemptOutcome.CANCELLED,
                                                 reason_code="fail_fast", event_sink=event_sink)
                            released.add(sibling.task_id)
                            continue
                        late_result = None
                        try:
                            late_result = sibling_future.result()
                        except BaseException:
                            consumed.add(sibling.task_id)
                        else:
                            if not isinstance(late_result, TaskResultRecord):
                                raise HarnessValidationError(
                                    "parallel worker returned invalid result",
                                    code="RESULT_SCHEMA_INVALID",
                                )
                            consumed.add(sibling.task_id)
                        quarantined.add(sibling.task_id)
                        self._record_attempt(session, wave, sibling, outcome=TaskAttemptOutcome.QUARANTINED,
                                             result=late_result, reason_code="fail_fast", event_sink=event_sink)
                    break
            return _WaveRunOutcome(
                tuple(results),
                frozenset(released),
                frozenset(consumed),
                frozenset(quarantined),
            )

        definitions = {item.task_id: item for item in request.plan.tasks}
        children: list[tuple[TaskInstance, _SupervisorTaskWorker, ChildAgentHandle]] = []
        pending_children = [
            (
                item,
                _SupervisorTaskWorker(
                    invoke,
                    item,
                    spawn_request=spawn_request,
                    group_absolute_deadline_ms=request.group_absolute_deadline_ms,
                ),
                spawn_request,
            )
            for item, spawn_request in zip(batch, spawn_requests, strict=True)
        ]
        # Spawn the entire wave before waiting. This is the point at which the
        # supervisor, rather than a second executor, establishes overlap.
        try:
            handles = self.child_supervisor.spawn_batch(
                tuple(item[2] for item in pending_children),
                workers=tuple(item[1] for item in pending_children),
            )
        except BaseException:
            # Batch capacity is atomic, but a later worker/event admission can
            # still fail after an earlier child became durable. Recover those
            # deterministic handles so cancellation/reconciliation remains
            # possible from the indeterminate group.
            reconciled = []
            for item, worker, spawn_request in pending_children:
                if spawn_request.child_id is None:
                    continue
                try:
                    handle = self.child_supervisor.status(
                        spawn_request.child_id,
                        operation_id=spawn_request.operation_id,
                    )
                except ChildAgentSupervisorError:
                    reconciled.append((item, spawn_request, None))
                    continue
                with self._lock:
                    session.active_children[item.task_id] = (handle, worker)
                reconciled.append((item, spawn_request, handle))
            for item, spawn_request, handle in reconciled:
                if handle is None:
                    self._emit(
                        "TASK_ATTEMPT_SPAWN_UNKNOWN",
                        event_sink=event_sink,
                        group_id=session.group.group_id,
                        wave_id=wave.wave_id,
                        task_id=item.task_id,
                        task_instance_id=item.task_instance_id,
                        attempt=item.attempt,
                        operation_key=spawn_request.operation_id,
                        spawn_status="SPAWN_UNKNOWN",
                        idempotency_key=spawn_request.operation_id,
                    )
                    with self._lock:
                        session.spawn_receipts[spawn_request.operation_id] = ("SPAWN_UNKNOWN", None)
                    continue
                self._emit(
                    "TASK_ATTEMPT_SPAWN_CONFIRMED",
                    event_sink=event_sink,
                    group_id=session.group.group_id,
                    wave_id=wave.wave_id,
                    task_id=item.task_id,
                    task_instance_id=item.task_instance_id,
                    attempt=item.attempt,
                    operation_key=spawn_request.operation_id,
                    spawn_status="SPAWN_CONFIRMED",
                    child_id=handle.child_id,
                    idempotency_key=spawn_request.operation_id,
                )
                with self._lock:
                    session.spawn_receipts[spawn_request.operation_id] = ("SPAWN_CONFIRMED", handle.child_id)
            raise
        # Retain all handles before writing any receipt so a failed write
        # cannot hide a sibling that the supervisor already started.
        for (item, worker, _spawn_request), handle in zip(
            pending_children,
            handles,
            strict=True,
        ):
            children.append((item, worker, handle))
            with self._lock:
                session.active_children[item.task_id] = (handle, worker)
        for (item, _worker, _spawn_request), handle in zip(pending_children, handles, strict=True):
            self._emit(
                "TASK_ATTEMPT_SPAWN_CONFIRMED",
                event_sink=event_sink,
                group_id=session.group.group_id,
                wave_id=wave.wave_id,
                task_id=item.task_id,
                task_instance_id=item.task_instance_id,
                attempt=item.attempt,
                operation_key=_spawn_request.operation_id,
                spawn_status="SPAWN_CONFIRMED",
                child_id=handle.child_id,
                idempotency_key=_spawn_request.operation_id,
            )
            with self._lock:
                session.spawn_receipts[_spawn_request.operation_id] = ("SPAWN_CONFIRMED", handle.child_id)
        self._mark_wave_dispatched(session, wave, event_sink=event_sink)
        results: list[TaskResultRecord] = []
        released: set[str] = set()
        consumed: set[str] = set()
        quarantined: set[str] = set()
        deadline = monotonic() + request.max_join_wait_seconds
        reclaimed_task_ids: set[str] = set()
        try:
            pending = {
                item.task_id: (item, worker, handle)
                for item, worker, handle in children
            }
            while pending:
                progressed = False
                for task_id in sorted(tuple(pending)):
                    item, worker, handle = pending[task_id]
                    operation = self.child_supervisor.wait(
                        handle.child_id,
                        operation_id=handle.operation_id,
                        timeout_seconds=0,
                    )
                    receipt = operation.receipt
                    if receipt is None:
                        continue
                    session.attempt_receipts[item.task_instance_id] = receipt
                    progressed = True
                    pending.pop(task_id)
                    if receipt.status is ChildAgentState.LOST:
                        if (
                            receipt.termination_confirmed
                            and receipt.reason_code == "child_lease_expired"
                        ):
                            self._record_attempt(session, wave, item, outcome=TaskAttemptOutcome.RECLAIMED,
                                                 receipt=receipt, reason_code="child_lease_expired", event_sink=event_sink)
                            reclaimed_task_ids.add(item.task_id)
                            released.add(item.task_id)
                            self.child_supervisor.close(
                                handle.child_id,
                                operation_id=handle.operation_id,
                            )
                            with self._lock:
                                session.active_children.pop(item.task_id, None)
                            self._emit(
                                "TASK_GROUP_RECLAIMED",
                                event_sink=event_sink,
                                group_id=session.group.group_id,
                                wave_id=wave.wave_id,
                                task_ids=[item.task_id],
                                task_instance_id=item.task_instance_id,
                                attempt=item.attempt,
                                child_id=handle.child_id,
                                reason_code="child_lease_expired",
                                retry_eligible=(
                                    "child_lease_expired"
                                    in definitions[
                                        item.task_id
                                    ].normalized_retry_policy.retryable_reason_codes
                                    and item.attempt
                                    < definitions[
                                        item.task_id
                                    ].normalized_retry_policy.max_attempts
                                ),
                                idempotency_key=(
                                    f"{session.group.group_id}:"
                                    f"{item.task_instance_id}:reclaimed"
                                ),
                            )
                            continue
                        if receipt.reason_code in {
                            "child_lease_expired",
                            "termination_unconfirmed",
                        }:
                            self._record_attempt(session, wave, item, outcome=TaskAttemptOutcome.INDETERMINATE,
                                                 receipt=receipt, reason_code="lease_expiry_unconfirmed", event_sink=event_sink)
                            raise HarnessValidationError(
                                "child lease termination could not be confirmed",
                                code="TASK_GROUP_LEASE_UNCONFIRMED",
                                details={
                                    "task_id": item.task_id,
                                    "child_id": handle.child_id,
                                },
                            )
                    if receipt.status is not ChildAgentState.SUCCEEDED or worker.result is None:
                        terminal_outcome = (
                            TaskAttemptOutcome.FAILED if receipt.status is ChildAgentState.FAILED
                            else TaskAttemptOutcome.CANCELLED if receipt.status is ChildAgentState.CANCELLED
                            else TaskAttemptOutcome.INDETERMINATE
                        )
                        self._record_attempt(session, wave, item, outcome=terminal_outcome, receipt=receipt,
                                             reason_code=receipt.reason_code, event_sink=event_sink)
                        raise HarnessValidationError(
                            "child runtime did not produce a verified task result",
                            code="TASK_GROUP_INDETERMINATE",
                            details={"task_id": item.task_id, "child_state": receipt.status.value},
                        )
                    result = _validated_supervised_task_result(
                        worker.result,
                        item,
                        request.plan,
                    )
                    self._record_attempt(session, wave, item, result=result, receipt=receipt, event_sink=event_sink)
                    results.append(result)
                    consumed.add(item.task_id)
                    self.child_supervisor.close(
                        handle.child_id,
                        operation_id=handle.operation_id,
                    )
                    with self._lock:
                        session.active_children.pop(item.task_id, None)
                    if (
                        request.join_policy is JoinPolicy.FAIL_FAST
                        and result.status is TaskLifecycle.FAILED
                    ):
                        self._emit(
                            "TASK_GROUP_CANCEL_REQUESTED",
                            event_sink=event_sink,
                            group_id=session.group.group_id,
                            reason_code="fail_fast",
                            idempotency_key=f"{session.group.group_id}:fail_fast",
                        )
                        for sibling_task_id in sorted(tuple(pending)):
                            sibling, _sibling_worker, sibling_handle = pending[
                                sibling_task_id
                            ]
                            cancelled = self.child_supervisor.cancel(
                                sibling_handle.child_id,
                                operation_id=sibling_handle.operation_id,
                                reason="fail_fast",
                            )
                            cancelled_receipt = cancelled.receipt
                            if cancelled_receipt is not None:
                                session.attempt_receipts[sibling.task_instance_id] = cancelled_receipt
                            if (
                                cancelled_receipt is None
                                or not cancelled_receipt.termination_confirmed
                            ):
                                self._record_attempt(session, wave, sibling, outcome=TaskAttemptOutcome.INDETERMINATE,
                                                     receipt=cancelled_receipt, reason_code="termination_unconfirmed", event_sink=event_sink)
                                raise HarnessValidationError(
                                    "fail-fast sibling cancellation could not be confirmed",
                                    code="TASK_GROUP_INDETERMINATE",
                                    details={
                                        "task_id": sibling.task_id,
                                        "child_id": sibling_handle.child_id,
                                    },
                                )
                            self.child_supervisor.close(
                                sibling_handle.child_id,
                                operation_id=sibling_handle.operation_id,
                            )
                            with self._lock:
                                session.active_children.pop(sibling.task_id, None)
                            pending.pop(sibling_task_id)
                            if cancelled_receipt.status is ChildAgentState.CANCELLED:
                                self._record_attempt(session, wave, sibling, outcome=TaskAttemptOutcome.CANCELLED,
                                                     receipt=cancelled_receipt, reason_code="fail_fast", event_sink=event_sink)
                                released.add(sibling.task_id)
                            else:
                                late_result = _sibling_worker.result
                                if late_result is None and cancelled.result is not None and isinstance(cancelled.result.get("task_result"), Mapping):
                                    late_result = TaskResultRecord.from_dict(cancelled.result["task_result"])
                                self._record_attempt(session, wave, sibling, outcome=TaskAttemptOutcome.QUARANTINED,
                                                     result=late_result, receipt=cancelled_receipt, reason_code="fail_fast", event_sink=event_sink)
                                consumed.add(sibling.task_id)
                                quarantined.add(sibling.task_id)
                        break
                if pending and not progressed:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise HarnessValidationError(
                            "child join exceeded its bounded deadline",
                            code="TASK_GROUP_INDETERMINATE",
                        )
                    sleep(min(0.01, remaining))
        except BaseException:
            # Keep active handles indexed for an explicit cancel/reconcile call;
            # terminal supervisor receipts prevent duplicate operations.
            raise
        return _WaveRunOutcome(
            tuple(results),
            frozenset(released),
            frozenset(consumed),
            frozenset(quarantined),
            frozenset(reclaimed_task_ids),
        )

    def _release_pending_waves(
        self,
        session: _GroupSession,
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        reason_code: str,
        capacity_snapshot: CapacityScopeSnapshot | None = None,
    ) -> CapacityScopeSnapshot | None:
        pending = set(session.reserved)
        release_confirmed = reason_code in {"fail_fast", "cancel_requested", "group_runtime_deadline_exceeded"}
        if release_confirmed:
            session.reserved.clear()
        if not pending:
            return capacity_snapshot
        current_capacity = capacity_snapshot
        if current_capacity is None:
            recorded_snapshots = tuple(
                wave.packing.capacity_after
                for wave in session.waves
                if pending.intersection(wave.task_ids)
                and wave.packing.capacity_after is not None
            )
            if recorded_snapshots:
                current_capacity = max(
                    recorded_snapshots,
                    key=lambda snapshot: snapshot.revision,
                )
        updated: list[DispatchWave] = []
        for wave in session.waves:
            if not (pending & set(wave.task_ids)):
                updated.append(wave)
                continue
            if reason_code == "child_runtime_indeterminate" and wave.state is not DispatchWaveState.TERMINAL:
                # Keep the admitted wave and reservations addressable after a
                # process crash.  A terminal INDETERMINATE wave cannot carry
                # the later audited receipt-to-dispatch transition.
                updated.append(wave)
                continue
            reservations = tuple(
                item.settled(ReservationState.RELEASED)
                if release_confirmed and item.task_id in pending
                else item
                for item in wave.reservations
            )
            terminal_outcome = (
                DispatchWaveTerminalOutcome.DEADLINE_EXCEEDED
                if reason_code == "group_runtime_deadline_exceeded"
                else (
                    DispatchWaveTerminalOutcome.CANCELLED
                    if release_confirmed
                    else DispatchWaveTerminalOutcome.INDETERMINATE
                )
            )
            updated.append(
                replace(
                    wave.transitioned(
                        DispatchWaveState.TERMINAL,
                        terminal_outcome=terminal_outcome,
                    ),
                    reservations=reservations,
                )
            )
            reservation_states = {
                item.task_id: item.state for item in reservations
            }
            capacity_before_release = current_capacity
            capacity_after_release = _capacity_release_transition(
                capacity_before_release,
                wave,
                reservation_states,
            )
            self._emit(
                "TASK_WAVE_COMPLETED",
                event_sink=event_sink,
                group_id=session.group.group_id,
                wave_id=wave.wave_id,
                task_ids=list(wave.task_ids),
                reservation_states={
                    item.task_id: item.state.value for item in reservations
                },
                child_states={
                    item.task_id: TaskLifecycle.FAILED.value
                    for item in reservations
                },
                reason_code=reason_code,
                terminal_outcome=terminal_outcome.value,
                capacity_before=(
                    capacity_before_release.to_dict()
                    if capacity_before_release is not None else None
                ),
                capacity_after=(
                    capacity_after_release.to_dict()
                    if capacity_after_release is not None else None
                ),
            )
            current_capacity = capacity_after_release
        session.waves = updated
        return current_capacity

    def _mark_indeterminate(
        self,
        group_id: str,
        *,
        reason_code: str,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
        diagnostics: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            session = self._sessions.get(group_id)
            if session is None:
                return
            if session.group.state is DispatchGroupState.INDETERMINATE:
                return
            recorded_instances = {item.task_instance_id for item in session.attempt_history.values()}
            for wave in session.waves:
                for instance in session.wave_instances.get(wave.wave_id, ()):
                    if instance.task_instance_id not in recorded_instances:
                        self._record_attempt(session, wave, instance, outcome=TaskAttemptOutcome.INDETERMINATE,
                                             reason_code=reason_code, event_sink=event_sink)
            self._release_pending_waves(session, event_sink=event_sink, reason_code=reason_code)
            indeterminate_group = session.group.transitioned(
                DispatchGroupState.INDETERMINATE
            )
            self._emit(
                "TASK_GROUP_INDETERMINATE",
                event_sink=event_sink,
                group=indeterminate_group.to_dict(),
                group_id=group_id,
                reason_code=reason_code,
                diagnostics=list(diagnostics),
                group_duration_ms=_elapsed_ms(session.started_at),
                idempotency_key=group_id,
            )
            # Publish the canonical transition before advancing local state.
            # Otherwise a failed durable write leaves this coordinator at
            # INDETERMINATE and a retry returns early, permanently hiding the
            # missing terminal fact from recovery and offline replay.
            session.group = indeterminate_group

    def _terminal_state(self, session: _GroupSession) -> tuple[DispatchGroupState, tuple[str, ...]]:
        results = tuple(session.results.values())
        failed = [item for item in results if item.status is TaskLifecycle.FAILED]
        if failed or session.failed_task_ids:
            return DispatchGroupState.FAILED, ("TASK_FAILED",)
        if session.blocked_task_ids:
            return DispatchGroupState.FAILED, ("DEPENDENCY_BLOCKED", "REQUIRED_ROLE_MISSING")
        roles = [item.output_roles for item in results if item.status is TaskLifecycle.SUCCEEDED]
        output_roles = [role for values in roles for role in values]
        if len(output_roles) != len(set(output_roles)):
            return DispatchGroupState.FAILED, ("OUTPUT_ROLE_CONFLICT",)
        required = set(session.group.required_output_roles)
        if not required.issubset(output_roles):
            return DispatchGroupState.FAILED, ("REQUIRED_ROLE_MISSING",)
        if any(item.status is not TaskLifecycle.SUCCEEDED for item in results):
            return DispatchGroupState.FAILED, ("TASK_FAILED",)
        return DispatchGroupState.SUCCEEDED, ()

    def _result_for_session(
        self,
        session: _GroupSession,
        request: ParallelDispatchRequest,
        *,
        limits: ParentObservationLimits | None,
        diagnostics: tuple[str, ...] | None = None,
    ) -> ParallelDispatchResult:
        ordered = tuple(session.results[key] for key in sorted(session.results))
        blocked_summaries = tuple(
            {
                "task_id": task_id,
                "status": TaskLifecycle.BLOCKED_DEPENDENCY.value,
                "attempt": 0,
                "output_roles": [],
                "result_ref": None,
                "checksum": session.blocked_task_checksums[task_id],
            }
            for task_id in sorted(session.blocked_task_ids)
        )
        # Blocked summaries are intentionally not TaskResultRecord instances;
        # they are projection facts and must not imply a fabricated attempt.
        blocked_summary_by_id = {
            item["task_id"]: item for item in blocked_summaries
        }
        aggregate_checksum: str | None = None
        aggregate_ref: str | None = None
        if session.group.state is DispatchGroupState.SUCCEEDED:
            aggregate_checksum = canonical_payload_checksum({"group_id": session.group.group_id, "results": [{"task_id": item.task_id, "result_checksum": item.result_checksum} for item in ordered]})
            aggregate_ref = f"artifact://task-plan/{aggregate_checksum.removeprefix('sha256:')}"
        refs = tuple(item.result_ref for item in ordered if item.result_ref)
        if diagnostics is None:
            diagnostics = session.terminal_diagnostics or (
                ()
                if session.group.state is DispatchGroupState.SUCCEEDED
                else (
                    ("JOIN_WAITING",)
                    if session.group.state
                    in {
                        DispatchGroupState.ADMITTED,
                        DispatchGroupState.DISPATCHING,
                        DispatchGroupState.RUNNING,
                        DispatchGroupState.JOINING,
                    }
                    else ("TASK_FAILED",)
                )
            )
        observation = ParentObservation(
            session.group.run_id,
            session.group.stage_id,
            session.group.plan_version,
            session.group.group_id,
            session.group.state.value,
            tuple(
                [
                    {"task_id": item.task_id, "status": item.status.value, "attempt": item.attempt, "output_roles": list(item.output_roles), "result_ref": item.result_ref, "checksum": item.result_checksum}
                    for item in ordered
                ]
                + list(blocked_summary_by_id.values())
            ),
            aggregate_ref=aggregate_ref,
            aggregate_checksum=aggregate_checksum,
            diagnostics=diagnostics,
            refs=refs,
            requested_parallelism=request.requested_parallelism or session.group.max_parallelism,
            effective_parallelism=max(
                (item.effective_parallelism for item in session.waves),
                default=0,
            ),
            wave_summaries=tuple({"wave_id": item.wave_id, "ordinal": item.ordinal, "status": item.state.value, "task_ids": list(item.task_ids)} for item in session.waves),
        )
        projection_limits = limits or ParentObservationLimits()
        projected_observation = observation.project(projection_limits)
        return ParallelDispatchResult(
            session.group,
            tuple(session.waves),
            ordered,
            observation,
            aggregate_ref,
            aggregate_checksum,
            projected_observation,
            attempt_history=tuple(session.attempt_history.values()),
        )

    def _emit(
        self,
        event_type: str,
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None = None,
        **payload: Any,
    ) -> None:
        sink = event_sink or self.event_sink
        if sink is not None:
            sink({"event_type": event_type, **payload})

    def _emit_batch(
        self,
        events: tuple[Mapping[str, Any], ...],
        *,
        event_sink: Callable[[Mapping[str, Any]], Any] | None,
    ) -> None:
        sink = event_sink or self.event_sink
        if sink is None:
            return
        if not isinstance(sink, ParallelEventSink):
            raise HarnessValidationError(
                "wave admission requires an atomic event batch sink",
                code="TASK_WAVE_ATOMIC_SINK_REQUIRED",
            )
        sink.append_batch(events)


def _admission_policy_checksum(request: ParallelDispatchRequest) -> str:
    """Bind admission policy while deliberately excluding live availability."""

    return canonical_payload_checksum({
        "requested_parallelism": request.requested_parallelism,
        "capability_capacity": request.capability_capacity,
        "supervisor_capacity": request.supervisor_capacity,
        "serial_fallback": request.serial_fallback,
        "join_policy": request.join_policy.value,
        "side_effect_class": request.side_effect_class.value,
        "resource_conflict_key": request.resource_conflict_key,
        "max_waves": request.max_waves,
        "max_tasks_per_group": request.max_tasks_per_group,
        "max_group_runtime_seconds": request.max_group_runtime_seconds,
        "max_join_wait_seconds": request.max_join_wait_seconds,
        "capacity_pools": [
            {
                "pool_id": pool.pool_id,
                "capacity": pool.capacity,
                "policy_version": pool.policy_version,
                "policy_checksum": pool.policy_checksum,
                "owner_scope": pool.owner_scope,
                "reservation_key": pool.reservation_key,
            }
            for pool in sorted(request.capacity_pools, key=lambda item: item.pool_id)
        ],
        "task_capacity_demands": [
            request.task_capacity_demands[task_id].to_dict()
            for task_id in sorted(request.task_capacity_demands)
        ],
        **({"capacity_policy_checksum": request.capacity_policy.policy_checksum} if request.capacity_policy is not None else {}),
    })


def _reuse_admission_clock(
    proposed: DispatchGroup,
    admitted: DispatchGroup,
) -> DispatchGroup:
    """Compare a redelivery against the first admission's fixed clock.

    ``ParallelDispatchRequest`` assigns the admission instant when a caller
    does not already have durable group evidence.  A retry of the same
    logical group may therefore arrive with a later locally observed instant.
    The first admission owns both timestamps; replacing only those fields
    keeps the immutable checksum comparison strict for every policy and
    identity field while making exact redelivery independent of wall-clock
    observation time.
    """

    if proposed.group_id != admitted.group_id:
        return proposed
    return replace(
        proposed,
        admitted_at_ms=admitted.admitted_at_ms,
        absolute_deadline_ms=admitted.absolute_deadline_ms,
    )


def _elapsed_ms(started_at: float, *, now: float | None = None) -> int:
    finished_at = monotonic() if now is None else now
    return max(0, int(round((finished_at - started_at) * 1000)))


def _terminal_wave_outcome(outcome: _WaveRunOutcome) -> DispatchWaveTerminalOutcome:
    """Derive the immutable terminal classification from verified wave facts."""

    results = tuple(outcome.results)
    if outcome.reclaimed_task_ids:
        if not results:
            return DispatchWaveTerminalOutcome.RECLAIMED
        return (DispatchWaveTerminalOutcome.PARTIAL_FAILED
                if any(item.status is TaskLifecycle.SUCCEEDED for item in results)
                else DispatchWaveTerminalOutcome.FAILED)
    if not results:
        return (
            DispatchWaveTerminalOutcome.CANCELLED
            if outcome.released_task_ids
            else DispatchWaveTerminalOutcome.INDETERMINATE
        )
    succeeded = sum(item.status is TaskLifecycle.SUCCEEDED for item in results)
    failed = sum(item.status is TaskLifecycle.FAILED for item in results)
    if failed and succeeded:
        return DispatchWaveTerminalOutcome.PARTIAL_FAILED
    if failed:
        return DispatchWaveTerminalOutcome.FAILED
    if succeeded == len(results):
        return DispatchWaveTerminalOutcome.SUCCEEDED
    return DispatchWaveTerminalOutcome.INDETERMINATE


def _capacity_release_transition(
    before: CapacityScopeSnapshot | None,
    wave: DispatchWave,
    reservation_states: Mapping[str, ReservationState],
) -> CapacityScopeSnapshot | None:
    """Propose one fenced capacity release after confirmed settlement only."""

    if before is None:
        return None
    pools = {pool.pool_id: pool for pool in before.pools}
    affected: set[str] = set()
    for reservation in wave.reservations:
        state = reservation_states[reservation.task_id]
        if state is ReservationState.RESERVED:
            continue
        for pool_id, quantity in reservation.capacity_allocations.items():
            pool = pools.get(pool_id)
            if pool is None or pool.reserved < quantity:
                raise HarnessValidationError(
                    "capacity release differs from authoritative reservation state",
                    code="CAPACITY_RESERVATION_CONFLICT",
                )
            pools[pool_id] = replace(pool, reserved=pool.reserved - quantity)
            affected.add(pool_id)
    if not affected:
        return before
    return before.with_reserved_pools(tuple(
        replace(
            pool,
            reservation_version=(
                pool.reservation_version + 1
                if pool.pool_id in affected
                else pool.reservation_version
            ),
        )
        for pool in sorted(pools.values(), key=lambda item: item.pool_id)
    ))


def _wave_for_child(session: _GroupSession, handle: ChildAgentHandle) -> DispatchWave:
    for wave in session.waves:
        if spawn_operation_key(session.group.group_id, wave.wave_id, handle.task_instance_id, handle.attempt) == handle.operation_id:
            return wave
    raise HarnessValidationError("child has no admitted wave", code="TASK_GROUP_RECOVERY_IDENTITY_MISMATCH")


def _validated_task_result(
    result: TaskResultRecord,
    task_instance: TaskInstance,
) -> TaskResultRecord:
    if not isinstance(result, TaskResultRecord):
        raise HarnessValidationError(
            "parallel worker returned invalid result",
            code="RESULT_SCHEMA_INVALID",
        )
    if (
        result.task_id != task_instance.task_id
        or result.task_instance_id != task_instance.task_instance_id
        or result.attempt != task_instance.attempt
    ):
        raise HarnessValidationError(
            "worker result task identity mismatch",
            code="RESULT_IDENTITY_MISMATCH",
        )
    return result


def _validated_supervised_task_result(
    result: TaskResultRecord,
    task_instance: TaskInstance,
    plan: ValidatedTaskPlan,
) -> TaskResultRecord:
    """Enforce the immutable attempt and plan boundary at child join time."""

    if not isinstance(result, TaskResultRecord):
        raise HarnessValidationError(
            "parallel worker returned invalid result",
            code="RESULT_SCHEMA_INVALID",
        )
    if (
        result.task_id != task_instance.task_id
        or result.task_instance_id != task_instance.task_instance_id
        or result.attempt != task_instance.attempt
        or not result.matches_plan_identity(plan)
    ):
        raise HarnessValidationError(
            "supervised worker result does not match its admitted attempt",
            code="RESULT_IDENTITY_MISMATCH",
            details={
                "task_id": task_instance.task_id,
                "task_instance_id": task_instance.task_instance_id,
                "attempt": task_instance.attempt,
            },
        )
    definition = next(
        (item for item in plan.tasks if item.task_id == task_instance.task_id),
        None,
    )
    if definition is None:
        raise HarnessValidationError(
            "supervised worker result references an unknown task",
            code="RESULT_IDENTITY_MISMATCH",
        )
    if (
        result.worker_ref != definition.worker_ref
        or result.task_checksum != definition.task_definition_checksum
        or result.binding_checksum != definition.binding_checksum
        or (
            result.status is TaskLifecycle.SUCCEEDED
            and (
                result.output_schema_ref != definition.task.output_contract.schema_ref
                or result.output_roles != (definition.output_role,)
            )
        )
    ):
        raise HarnessValidationError(
            "supervised worker result evidence does not match its accepted task",
            code="RESULT_IDENTITY_MISMATCH",
            details={"task_id": task_instance.task_id},
        )
    return result


def _non_negative_mapping(value: Mapping[str, int], field_name: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(f"{field_name} must be an object", code="PLAN_SCHEMA_INVALID")
    normalized: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise HarnessValidationError(f"{field_name} values must be non-negative", code="PLAN_SCHEMA_INVALID")
        normalized[str(key)] = item
    return frozen_mapping(normalized, field_name)


def _bounded_summary(value: Mapping[str, Any], maximum: int) -> dict[str, Any]:
    result = dict(value)
    summary = result.get("summary")
    if isinstance(summary, str) and len(summary.encode("utf-8")) > maximum:
        result["summary"] = truncate_observation_text(summary, maximum)
        result["summary_checksum"] = canonical_payload_checksum({"summary": summary})
        result["summary_truncated"] = True
    return result


def _encoded_json_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


__all__ = [
    "JoinPolicy",
    "DispatchGroupState",
    "DispatchWaveState",
    "DispatchWaveTerminalOutcome",
    "ReservationState",
    "SideEffectClass",
    "CapabilityCapacity",
    "ParentObservationLimits",
    "TaskReservation",
    "DispatchGroup",
    "DispatchWave",
    "ParallelDispatchRequest",
    "ParentObservation",
    "ParallelDispatchResult",
    "ParallelAgentCoordinator",
]
