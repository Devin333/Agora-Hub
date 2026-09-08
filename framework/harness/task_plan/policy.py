from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Self

from framework.agent.models.orchestration import ParentObservationLimits
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    exact_reference,
    frozen_mapping,
    identifier,
    positive_int,
    required_text,
    stable_text_tuple,
    thaw_mapping,
)
from framework.harness.task_plan.models import PlanBuildBudget, TaskBudget, TaskPlanLimits
from framework.harness.task_plan.capacity_policy import TaskCapacityPolicy
from framework.harness.task_plan.schema import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    TASK_PLAN_POLICY_SCHEMA,
    TASK_PLAN_RUNTIME_VERSION,
    TaskPlanContractKind,
)


@dataclass(frozen=True, slots=True)
class TaskPlanPolicy:
    policy_id: str
    version: str
    stage_id: str
    allowed_worker_capabilities: tuple[str, ...]
    allowed_subagent_ids: tuple[str, ...]
    allowed_tool_ids: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    allowed_input_refs: tuple[str, ...]
    allowed_output_roles: tuple[str, ...]
    required_output_roles: tuple[str, ...]
    allowed_output_schema_refs: tuple[str, ...]
    allowed_gate_refs: tuple[str, ...]
    deterministic_aggregator_refs: Mapping[str, str]
    pinned_capability_bindings: Mapping[str, str]
    required_worker_contract_refs: Mapping[str, str]
    max_tasks: int
    max_depth: int
    max_parallelism: int
    max_replans: int
    max_task_attempts: int
    max_plan_build_calls: int
    max_plan_build_turns: int
    max_plan_build_tool_calls: int
    per_task_budget: TaskBudget | Mapping[str, Any]
    aggregate_task_budget: TaskBudget | Mapping[str, Any]
    max_validation_diagnostics: int = 64
    allow_nested_subagents: bool = False
    join_policy: str = "wait_all"
    serial_fallback: bool = False
    max_tasks_per_group: int | None = None
    max_waves: int = 16
    max_group_runtime_seconds: int = 900
    max_join_wait_seconds: int = 300
    capability_capacity: int | None = None
    available_concurrency_reservations: int | None = None
    side_effect_class: str = "READ_ONLY"
    resource_conflict_key: str | None = None
    parent_observation_limits: Mapping[str, int] = field(
        default_factory=lambda: ParentObservationLimits().to_dict()
    )
    max_planning_tool_calls: int = 3
    planning_timeout_seconds: int = 30
    shared_dependency_output_roles: tuple[str, ...] = ()
    capacity_policy: TaskCapacityPolicy | Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    runtime_version: str = TASK_PLAN_RUNTIME_VERSION
    schema_version: str = TASK_PLAN_POLICY_SCHEMA
    policy_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.POLICY,
            self.schema_version,
        )
        policy_id = identifier(self.policy_id, "policy_id")
        version = required_text(self.version, "version")
        exact_reference(f"{policy_id}@{version}", "policy_ref")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "stage_id", identifier(self.stage_id, "stage_id"))
        capabilities = stable_text_tuple(
            self.allowed_worker_capabilities,
            "allowed_worker_capabilities",
            allow_empty=False,
        )
        object.__setattr__(self, "allowed_worker_capabilities", capabilities)
        object.__setattr__(self, "allowed_subagent_ids", stable_text_tuple(self.allowed_subagent_ids, "allowed_subagent_ids"))
        object.__setattr__(self, "allowed_tool_ids", stable_text_tuple(self.allowed_tool_ids, "allowed_tool_ids"))
        object.__setattr__(
            self,
            "allowed_memory_namespaces",
            stable_text_tuple(self.allowed_memory_namespaces, "allowed_memory_namespaces"),
        )
        object.__setattr__(
            self,
            "allowed_input_refs",
            stable_text_tuple(self.allowed_input_refs, "allowed_input_refs", allow_empty=False, item_kind="reference"),
        )
        allowed_roles = stable_text_tuple(self.allowed_output_roles, "allowed_output_roles", allow_empty=False)
        required_roles = stable_text_tuple(self.required_output_roles, "required_output_roles", allow_empty=False)
        if not set(required_roles).issubset(allowed_roles):
            raise HarnessValidationError(
                "required output roles must be allowed by the TaskPlan policy",
                code="invalid_task_plan_policy_roles",
                details={"policy_ref": self.exact_ref},
            )
        object.__setattr__(self, "allowed_output_roles", allowed_roles)
        shared_roles = stable_text_tuple(self.shared_dependency_output_roles, "shared_dependency_output_roles")
        if not set(shared_roles).issubset(allowed_roles):
            raise HarnessValidationError("dependency sharing roles must be allowed output roles", code="REF_UNAUTHORIZED")
        object.__setattr__(self, "shared_dependency_output_roles", shared_roles)
        object.__setattr__(self, "required_output_roles", required_roles)
        object.__setattr__(
            self,
            "allowed_output_schema_refs",
            stable_text_tuple(
                self.allowed_output_schema_refs,
                "allowed_output_schema_refs",
                allow_empty=False,
                item_kind="exact_reference",
            ),
        )
        object.__setattr__(
            self,
            "allowed_gate_refs",
            stable_text_tuple(
                self.allowed_gate_refs,
                "allowed_gate_refs",
                allow_empty=False,
                item_kind="exact_reference",
            ),
        )
        aggregators = _exact_ref_mapping(
            self.deterministic_aggregator_refs,
            "deterministic_aggregator_refs",
            allowed_keys=set(allowed_roles),
        )
        pinned = _exact_ref_mapping(
            self.pinned_capability_bindings,
            "pinned_capability_bindings",
            allowed_keys=set(capabilities),
        )
        contracts = _exact_ref_mapping(
            self.required_worker_contract_refs,
            "required_worker_contract_refs",
            allowed_keys=set(capabilities),
        )
        object.__setattr__(self, "deterministic_aggregator_refs", aggregators)
        object.__setattr__(self, "pinned_capability_bindings", pinned)
        object.__setattr__(self, "required_worker_contract_refs", contracts)
        missing_bindings = sorted(set(capabilities) - set(pinned))
        if missing_bindings:
            raise HarnessValidationError(
                "TaskPlan policy must pin one worker binding for every capability",
                code="incomplete_task_plan_policy_bindings",
                details={"capabilities": missing_bindings},
            )
        missing_contracts = sorted(set(capabilities) - set(contracts))
        if missing_contracts:
            raise HarnessValidationError(
                "TaskPlan policy must pin one worker contract for every capability",
                code="incomplete_task_plan_policy_contracts",
                details={"capabilities": missing_contracts},
            )
        if self.max_tasks_per_group is None:
            object.__setattr__(self, "max_tasks_per_group", max(self.max_tasks, 8))
        for field_name in (
            "max_tasks",
            "max_depth",
            "max_parallelism",
            "max_task_attempts",
            "max_plan_build_calls",
            "max_plan_build_turns",
            "max_validation_diagnostics",
        ):
            object.__setattr__(self, field_name, positive_int(getattr(self, field_name), field_name))
        if self.max_validation_diagnostics > 256:
            raise HarnessValidationError(
                "max_validation_diagnostics exceeds the framework safety bound",
                code="invalid_task_plan_limit",
                details={"maximum": 256},
            )
        if isinstance(self.max_replans, bool) or not isinstance(self.max_replans, int) or self.max_replans < 0:
            raise HarnessValidationError("max_replans must be a non-negative integer", code="invalid_task_plan_limit")
        if (
            isinstance(self.max_plan_build_tool_calls, bool)
            or not isinstance(self.max_plan_build_tool_calls, int)
            or self.max_plan_build_tool_calls < 0
        ):
            raise HarnessValidationError(
                "max_plan_build_tool_calls must be a non-negative integer",
                code="invalid_task_plan_limit",
            )
        object.__setattr__(self, "per_task_budget", _budget(self.per_task_budget, "per_task_budget"))
        object.__setattr__(
            self,
            "aggregate_task_budget",
            _budget(self.aggregate_task_budget, "aggregate_task_budget"),
        )
        if self.per_task_budget.exceeds(self.aggregate_task_budget):
            raise HarnessValidationError(
                "per-task budget must fit within aggregate task budget",
                code="invalid_task_plan_budget_policy",
            )
        if not isinstance(self.allow_nested_subagents, bool):
            raise HarnessValidationError(
                "allow_nested_subagents must be a boolean",
                code="invalid_task_plan_policy",
            )
        if self.join_policy not in {"wait_all", "fail_fast"}:
            raise HarnessValidationError("join_policy must be wait_all or fail_fast", code="invalid_task_plan_policy")
        if not isinstance(self.serial_fallback, bool):
            raise HarnessValidationError("serial_fallback must be a boolean", code="invalid_task_plan_policy")
        for field_name in (
            "max_tasks_per_group",
            "max_waves",
            "max_group_runtime_seconds",
            "max_join_wait_seconds",
            "planning_timeout_seconds",
        ):
            object.__setattr__(self, field_name, positive_int(getattr(self, field_name), field_name))
        if self.max_tasks_per_group < self.max_tasks:
            raise HarnessValidationError("max_tasks_per_group must cover max_tasks", code="invalid_task_plan_limit")
        for field_name in ("capability_capacity", "available_concurrency_reservations"):
            value = getattr(self, field_name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise HarnessValidationError(f"{field_name} must be non-negative", code="invalid_task_plan_limit")
        if isinstance(self.max_planning_tool_calls, bool) or not isinstance(self.max_planning_tool_calls, int) or self.max_planning_tool_calls < 0:
            raise HarnessValidationError("max_planning_tool_calls must be non-negative", code="invalid_task_plan_limit")
        if self.side_effect_class not in {"READ_ONLY", "EXTERNAL_IDEMPOTENT", "MUTATING_SERIAL", "FENCED_MUTATION"}:
            raise HarnessValidationError("invalid side_effect_class", code="invalid_task_plan_policy")
        if self.resource_conflict_key is not None and (
            not isinstance(self.resource_conflict_key, str) or not self.resource_conflict_key.strip()
        ):
            raise HarnessValidationError("resource_conflict_key must be non-empty", code="invalid_task_plan_policy")
        observation_limits = dict(self.parent_observation_limits)
        required_observation_limits = set(ParentObservationLimits().to_dict())
        if set(observation_limits) != required_observation_limits:
            raise HarnessValidationError("parent_observation_limits has invalid fields", code="invalid_task_plan_policy")
        try:
            observation_limits = ParentObservationLimits.from_dict(observation_limits).to_dict()
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(str(exc), code="invalid_task_plan_limit") from exc
        object.__setattr__(self, "parent_observation_limits", frozen_mapping(observation_limits, "parent_observation_limits"))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "policy.metadata"))
        capacity_policy = self.capacity_policy
        if isinstance(capacity_policy, Mapping):
            capacity_policy = TaskCapacityPolicy.from_dict(capacity_policy)
        if capacity_policy is not None:
            if not isinstance(capacity_policy, TaskCapacityPolicy):
                raise HarnessValidationError("invalid task capacity policy", code="CAPACITY_POLICY_INVALID")
            if capacity_policy.stage_id != self.stage_id:
                raise HarnessValidationError("capacity policy belongs to a different stage", code="CAPACITY_POLICY_SCOPE_MISMATCH")
            if set(capacity_policy.worker_rules) - set(self.pinned_capability_bindings.values()):
                raise HarnessValidationError("capacity rule must reference a pinned worker binding", code="CAPACITY_POLICY_INVALID")
        object.__setattr__(self, "capacity_policy", capacity_policy)
        object.__setattr__(self, "runtime_version", required_text(self.runtime_version, "runtime_version"))
        object.__setattr__(self, "policy_checksum", canonical_payload_checksum(self.checksum_projection()))

    @property
    def exact_ref(self) -> str:
        return f"{self.policy_id}@{self.version}"

    @property
    def aggregator_ref(self) -> str | None:
        """Compatibility view for policies with one shared merge contract."""
        refs = tuple(sorted(set(self.deterministic_aggregator_refs.values())))
        return refs[0] if len(refs) == 1 else None

    @property
    def allow_plan_patches(self) -> bool:
        return self.max_replans > 0

    @property
    def allowed_patch_operations(self) -> tuple[str, ...]:
        return (
            "ADD_REPLACEMENT_TASK",
            "SKIP_PENDING_TASK",
            "UPDATE_PENDING_DEPENDENCY",
        )

    @property
    def limits(self) -> TaskPlanLimits:
        return TaskPlanLimits(
            max_tasks=self.max_tasks,
            max_depth=self.max_depth,
            max_parallelism=self.max_parallelism,
            max_replans=self.max_replans,
            max_task_attempts=self.max_task_attempts,
            plan_build_budget=PlanBuildBudget(
                max_builder_calls=self.max_plan_build_calls,
                max_turns=self.max_plan_build_turns,
                max_tool_calls=self.max_plan_build_tool_calls,
            ),
            per_task_budget=self.per_task_budget,
            aggregate_task_budget=self.aggregate_task_budget,
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_version": self.runtime_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "stage_id": self.stage_id,
            "allowed_worker_capabilities": list(self.allowed_worker_capabilities),
            "allowed_subagent_ids": list(self.allowed_subagent_ids),
            "allowed_tool_ids": list(self.allowed_tool_ids),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "allowed_input_refs": list(self.allowed_input_refs),
            "allowed_output_roles": list(self.allowed_output_roles),
            "required_output_roles": list(self.required_output_roles),
            "allowed_output_schema_refs": list(self.allowed_output_schema_refs),
            "allowed_gate_refs": list(self.allowed_gate_refs),
            "deterministic_aggregator_refs": thaw_mapping(self.deterministic_aggregator_refs),
            "pinned_capability_bindings": thaw_mapping(self.pinned_capability_bindings),
            "required_worker_contract_refs": thaw_mapping(self.required_worker_contract_refs),
            "max_tasks": self.max_tasks,
            "max_depth": self.max_depth,
            "max_parallelism": self.max_parallelism,
            "max_replans": self.max_replans,
            "max_task_attempts": self.max_task_attempts,
            "max_plan_build_calls": self.max_plan_build_calls,
            "max_plan_build_turns": self.max_plan_build_turns,
            "max_plan_build_tool_calls": self.max_plan_build_tool_calls,
            "per_task_budget": self.per_task_budget.to_dict(),
            "aggregate_task_budget": self.aggregate_task_budget.to_dict(),
            "max_validation_diagnostics": self.max_validation_diagnostics,
            "allow_nested_subagents": self.allow_nested_subagents,
            "join_policy": self.join_policy,
            "serial_fallback": self.serial_fallback,
            "max_tasks_per_group": self.max_tasks_per_group,
            "max_waves": self.max_waves,
            "max_group_runtime_seconds": self.max_group_runtime_seconds,
            "max_join_wait_seconds": self.max_join_wait_seconds,
            "capability_capacity": self.capability_capacity,
            "available_concurrency_reservations": self.available_concurrency_reservations,
            "side_effect_class": self.side_effect_class,
            "resource_conflict_key": self.resource_conflict_key,
            "parent_observation_limits": thaw_mapping(self.parent_observation_limits),
            "max_planning_tool_calls": self.max_planning_tool_calls,
            "planning_timeout_seconds": self.planning_timeout_seconds,
            **({"shared_dependency_output_roles": list(self.shared_dependency_output_roles)}
               if self.shared_dependency_output_roles else {}),
            **({"capacity_policy": self.capacity_policy.to_dict()} if self.capacity_policy is not None else {}),
            "metadata": thaw_mapping(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "policy_checksum": self.policy_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        required = frozenset(
            {
                "schema_version",
                "runtime_version",
                "policy_id",
                "version",
                "stage_id",
                "allowed_worker_capabilities",
                "allowed_subagent_ids",
                "allowed_tool_ids",
                "allowed_memory_namespaces",
                "allowed_input_refs",
                "allowed_output_roles",
                "required_output_roles",
                "allowed_output_schema_refs",
                "allowed_gate_refs",
                "deterministic_aggregator_refs",
                "pinned_capability_bindings",
                "required_worker_contract_refs",
                "max_tasks",
                "max_depth",
                "max_parallelism",
                "max_replans",
                "max_task_attempts",
                "max_plan_build_calls",
                "max_plan_build_turns",
                "max_plan_build_tool_calls",
                "per_task_budget",
                "aggregate_task_budget",
                "max_validation_diagnostics",
                "allow_nested_subagents",
                "metadata",
                "policy_checksum",
            }
        )
        optional = frozenset(
            {
                "join_policy",
                "serial_fallback",
                "max_tasks_per_group",
                "max_waves",
                "max_group_runtime_seconds",
                "max_join_wait_seconds",
                "capability_capacity",
                "available_concurrency_reservations",
                "side_effect_class",
                "resource_conflict_key",
                "parent_observation_limits",
                "max_planning_tool_calls",
                "planning_timeout_seconds",
                "shared_dependency_output_roles",
                "capacity_policy",
            }
        )
        payload = exact_keys(value, required=required, optional=optional, model=cls.__name__)
        legacy = not (set(payload) & optional)
        payload.setdefault("join_policy", "wait_all")
        payload.setdefault("serial_fallback", False)
        payload.setdefault("max_tasks_per_group", None)
        payload.setdefault("max_waves", 16)
        payload.setdefault("max_group_runtime_seconds", 900)
        payload.setdefault("max_join_wait_seconds", 300)
        payload.setdefault("capability_capacity", None)
        payload.setdefault("available_concurrency_reservations", None)
        payload.setdefault("side_effect_class", "READ_ONLY")
        payload.setdefault("resource_conflict_key", None)
        payload.setdefault(
            "parent_observation_limits",
            ParentObservationLimits().to_dict(),
        )
        payload.setdefault("max_planning_tool_calls", 3)
        payload.setdefault("planning_timeout_seconds", 30)
        supplied = checksum(payload.pop("policy_checksum"), "policy_checksum")
        policy = cls(**payload)
        legacy_projection = policy.checksum_projection()
        for field_name in optional:
            legacy_projection.pop(field_name, None)
        legacy_checksum = canonical_payload_checksum(legacy_projection)
        if supplied != policy.policy_checksum and (not legacy or supplied != legacy_checksum):
            raise HarnessValidationError(
                "TaskPlanPolicy checksum does not match canonical content",
                code="task_plan_checksum_mismatch",
                details={"model": cls.__name__, "field": "policy_checksum"},
            )
        return policy


class TaskPlanPolicyRegistry:
    def __init__(
        self,
        policies: Iterable[TaskPlanPolicy] = (),
        *,
        runtime_version: str = TASK_PLAN_RUNTIME_VERSION,
    ) -> None:
        self.runtime_version = required_text(runtime_version, "runtime_version")
        self._by_ref: dict[str, TaskPlanPolicy] = {}
        for policy in policies:
            self.register(policy)

    @property
    def policies(self) -> tuple[TaskPlanPolicy, ...]:
        return tuple(self._by_ref[key] for key in sorted(self._by_ref))

    def register(self, policy: TaskPlanPolicy) -> None:
        if not isinstance(policy, TaskPlanPolicy):
            raise TypeError("policy must be TaskPlanPolicy")
        if policy.exact_ref in self._by_ref:
            raise HarnessValidationError(
                "TaskPlan policy reference is already registered",
                code="duplicate_task_plan_policy",
                details={"policy_ref": policy.exact_ref},
            )
        self._by_ref[policy.exact_ref] = policy

    def resolve(self, policy_ref: str, *, stage_id: str | None = None) -> TaskPlanPolicy:
        exact = exact_reference(policy_ref, "policy_ref")
        policy = self._by_ref.get(exact)
        if policy is None:
            raise HarnessValidationError(
                "exact TaskPlan policy is not registered",
                code="unknown_task_plan_policy",
                details={"policy_ref": exact},
            )
        if stage_id is not None and policy.stage_id != identifier(stage_id, "stage_id"):
            raise HarnessValidationError(
                "TaskPlan policy is registered for another stage",
                code="incompatible_task_plan_policy",
                details={"policy_ref": exact, "stage_id": str(stage_id)},
            )
        self._require_compatible(policy)
        return policy

    def resolve_stage(self, stage_id: str, *, policy_ref: str | None = None) -> TaskPlanPolicy:
        stage = identifier(stage_id, "stage_id")
        if policy_ref is not None:
            return self.resolve(policy_ref, stage_id=stage)
        candidates = tuple(policy for policy in self.policies if policy.stage_id == stage)
        if not candidates:
            raise HarnessValidationError(
                "dynamic stage has no registered TaskPlan policy",
                code="missing_task_plan_policy",
                details={"stage_id": stage},
            )
        if len(candidates) != 1:
            raise HarnessValidationError(
                "dynamic stage policy resolution is ambiguous",
                code="ambiguous_task_plan_policy",
                details={"stage_id": stage, "policy_refs": [item.exact_ref for item in candidates]},
            )
        self._require_compatible(candidates[0])
        return candidates[0]

    def _require_compatible(self, policy: TaskPlanPolicy) -> None:
        if policy.runtime_version != self.runtime_version:
            raise HarnessValidationError(
                "TaskPlan policy runtime version is incompatible",
                code="incompatible_task_plan_policy_version",
                details={
                    "policy_ref": policy.exact_ref,
                    "policy_runtime_version": policy.runtime_version,
                    "runtime_version": self.runtime_version,
                },
            )


def _budget(value: TaskBudget | Mapping[str, Any], field_name: str) -> TaskBudget:
    if isinstance(value, TaskBudget):
        return value
    if isinstance(value, Mapping):
        return TaskBudget.from_dict(value)
    raise HarnessValidationError(
        f"{field_name} must be TaskBudget",
        code="invalid_task_plan_budget_policy",
        details={"field": field_name},
    )


def _exact_ref_mapping(
    value: Mapping[str, str],
    field_name: str,
    *,
    allowed_keys: set[str],
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_task_plan_policy",
            details={"field": field_name},
        )
    normalized: dict[str, str] = {}
    for raw_key, raw_ref in value.items():
        key = identifier(raw_key, field_name)
        if key not in allowed_keys:
            raise HarnessValidationError(
                f"{field_name} contains a key outside its policy allowlist",
                code="invalid_task_plan_policy",
                details={"field": field_name, "key": key},
            )
        normalized[key] = exact_reference(raw_ref, field_name)
    return MappingProxyType(dict(sorted(normalized.items())))


__all__ = ["TaskPlanPolicy", "TaskPlanPolicyRegistry"]
