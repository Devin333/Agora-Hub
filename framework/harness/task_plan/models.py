from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
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
from framework.harness.task_plan.forbidden import ensure_candidate_only
from framework.harness.task_plan.identity import TaskPlanStageIdentity
from framework.harness.task_plan.schema import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
    RESOLVED_TASK_DEFINITION_SCHEMA,
    TASK_DEFINITION_SCHEMA,
    TASK_RESULT_REFERENCE_SCHEMA,
    TaskPlanContractKind,
)
from framework.shared.time import format_datetime, parse_datetime


@dataclass(frozen=True, slots=True)
class TaskBudget:
    max_turns: int = 1
    max_tool_calls: int = 0
    max_memory_ops: int = 0
    max_output_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_turns", positive_int(self.max_turns, "max_turns"))
        for field_name in ("max_tool_calls", "max_memory_ops", "max_output_tokens"):
            object.__setattr__(self, field_name, non_negative_int(getattr(self, field_name), field_name))

    def to_dict(self) -> dict[str, int]:
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_memory_ops": self.max_memory_ops,
            "max_output_tokens": self.max_output_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset({"max_turns", "max_tool_calls", "max_memory_ops", "max_output_tokens"}),
            model=cls.__name__,
        )
        return cls(**payload)

    def plus(self, other: TaskBudget) -> TaskBudget:
        return TaskBudget(
            max_turns=self.max_turns + other.max_turns,
            max_tool_calls=self.max_tool_calls + other.max_tool_calls,
            max_memory_ops=self.max_memory_ops + other.max_memory_ops,
            max_output_tokens=self.max_output_tokens + other.max_output_tokens,
        )

    def exceeds(self, limit: TaskBudget) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in ("max_turns", "max_tool_calls", "max_memory_ops", "max_output_tokens")
            if getattr(self, field_name) > getattr(limit, field_name)
        )


@dataclass(frozen=True, slots=True)
class PlanBuildBudget:
    max_builder_calls: int = 1
    max_turns: int = 1
    max_tool_calls: int = 0

    def __post_init__(self) -> None:
        for field_name in ("max_builder_calls", "max_turns"):
            object.__setattr__(self, field_name, positive_int(getattr(self, field_name), field_name))
        object.__setattr__(self, "max_tool_calls", non_negative_int(self.max_tool_calls, "max_tool_calls"))

    def to_dict(self) -> dict[str, int]:
        return {
            "max_builder_calls": self.max_builder_calls,
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset({"max_builder_calls", "max_turns", "max_tool_calls"}),
            model=cls.__name__,
        )
        return cls(**payload)

    def exceeds(self, limit: PlanBuildBudget) -> tuple[str, ...]:
        return tuple(
            field_name
            for field_name in ("max_builder_calls", "max_turns", "max_tool_calls")
            if getattr(self, field_name) > getattr(limit, field_name)
        )


@dataclass(frozen=True, slots=True)
class TaskRetryPolicy:
    max_attempts: int = 1
    retryable_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_attempts", positive_int(self.max_attempts, "max_attempts"))
        object.__setattr__(
            self,
            "retryable_reason_codes",
            stable_text_tuple(self.retryable_reason_codes, "retryable_reason_codes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "retryable_reason_codes": list(self.retryable_reason_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset({"max_attempts", "retryable_reason_codes"}),
            model=cls.__name__,
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class TaskOutputContract:
    schema_ref: str
    output_role: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_ref", exact_reference(self.schema_ref, "schema_ref"))
        object.__setattr__(self, "output_role", identifier(self.output_role, "output_role"))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "output_contract.metadata"))
        ensure_candidate_only(self.to_dict(), path="$.output_contract")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_ref": self.schema_ref,
            "output_role": self.output_role,
            "metadata": thaw_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        ensure_candidate_only(value, path="$.output_contract")
        payload = exact_keys(
            value,
            required=frozenset({"schema_ref", "output_role", "metadata"}),
            model=cls.__name__,
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class TaskAcceptanceCriteria:
    gate_refs: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gate_refs",
            stable_text_tuple(
                self.gate_refs,
                "acceptance_criteria.gate_refs",
                allow_empty=False,
                item_kind="exact_reference",
            ),
        )
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "acceptance_criteria.metadata"))
        ensure_candidate_only(self.to_dict(), path="$.acceptance_criteria")

    def to_dict(self) -> dict[str, Any]:
        return {"gate_refs": list(self.gate_refs), "metadata": thaw_mapping(self.metadata)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        ensure_candidate_only(value, path="$.acceptance_criteria")
        payload = exact_keys(
            value,
            required=frozenset({"gate_refs", "metadata"}),
            model=cls.__name__,
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    objective: str
    worker_capability: str
    input_refs: tuple[str, ...]
    output_contract: TaskOutputContract | Mapping[str, Any]
    acceptance_criteria: TaskAcceptanceCriteria | Mapping[str, Any]
    depends_on: tuple[str, ...] = ()
    requested_tools: tuple[str, ...] = ()
    requested_memory_namespaces: tuple[str, ...] = ()
    budget_request: TaskBudget | Mapping[str, Any] = field(default_factory=TaskBudget)
    retry_policy: TaskRetryPolicy | Mapping[str, Any] = field(default_factory=TaskRetryPolicy)
    priority: int = 0
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = TASK_DEFINITION_SCHEMA
    task_definition_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.TASK_DEFINITION,
            self.schema_version,
        )
        output_contract = _model(self.output_contract, TaskOutputContract, "output_contract")
        criteria = _model(self.acceptance_criteria, TaskAcceptanceCriteria, "acceptance_criteria")
        budget = _model(self.budget_request, TaskBudget, "budget_request")
        retry = _model(self.retry_policy, TaskRetryPolicy, "retry_policy")
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        object.__setattr__(self, "objective", required_text(self.objective, "objective", max_length=4096))
        object.__setattr__(self, "worker_capability", identifier(self.worker_capability, "worker_capability"))
        object.__setattr__(
            self,
            "input_refs",
            stable_text_tuple(self.input_refs, "input_refs", allow_empty=False, item_kind="reference"),
        )
        object.__setattr__(self, "output_contract", output_contract)
        object.__setattr__(self, "acceptance_criteria", criteria)
        object.__setattr__(self, "depends_on", stable_text_tuple(self.depends_on, "depends_on"))
        object.__setattr__(self, "requested_tools", stable_text_tuple(self.requested_tools, "requested_tools"))
        object.__setattr__(
            self,
            "requested_memory_namespaces",
            stable_text_tuple(self.requested_memory_namespaces, "requested_memory_namespaces"),
        )
        object.__setattr__(self, "budget_request", budget)
        object.__setattr__(self, "retry_policy", retry)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise HarnessValidationError(
                "TaskSpec priority must be an integer",
                code="invalid_task_priority",
                details={"task_id": self.task_id},
            )
        object.__setattr__(self, "diagnostics", frozen_mapping(self.diagnostics, "task.diagnostics"))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "task.metadata"))
        ensure_candidate_only(self.checksum_projection(), path=f"$.tasks.{self.task_id}")
        object.__setattr__(self, "task_definition_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "objective": self.objective,
            "depends_on": list(self.depends_on),
            "worker_capability": self.worker_capability,
            "input_refs": list(self.input_refs),
            "output_contract": self.output_contract.to_dict(),
            "acceptance_criteria": self.acceptance_criteria.to_dict(),
            "requested_tools": list(self.requested_tools),
            "requested_memory_namespaces": list(self.requested_memory_namespaces),
            "budget_request": self.budget_request.to_dict(),
            "retry_policy": self.retry_policy.to_dict(),
            "priority": self.priority,
            "diagnostics": thaw_mapping(self.diagnostics),
            "metadata": thaw_mapping(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "task_definition_checksum": self.task_definition_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        ensure_candidate_only(value, path="$.task")
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "task_id",
                    "objective",
                    "depends_on",
                    "worker_capability",
                    "input_refs",
                    "output_contract",
                    "acceptance_criteria",
                    "requested_tools",
                    "requested_memory_namespaces",
                    "budget_request",
                    "retry_policy",
                    "priority",
                    "diagnostics",
                    "metadata",
                    "task_definition_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("task_definition_checksum"), "task_definition_checksum")
        task = cls(**payload)
        _verify_checksum(supplied, task.task_definition_checksum, "task_definition_checksum", cls.__name__)
        return task


_GRAPH_ONLY_MODEL_IDENTITY_FIELDS = frozenset(
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


def _stage_identity_kwargs(identity: TaskPlanStageIdentity) -> dict[str, Any]:
    if not isinstance(identity, TaskPlanStageIdentity):
        raise TypeError("stage_identity must be TaskPlanStageIdentity")
    return {
        "run_id": identity.run_id,
        "stage_id": identity.stage_id,
        "graph_checksum": identity.graph_checksum,
        "graph_id": identity.graph_id,
        "graph_version": identity.graph_version,
        "graph_ref": identity.graph_ref,
        "graph_schema_version": identity.graph_schema_version,
        "compiler_version": identity.compiler_version,
        "condition_policy_version": identity.condition_policy_version,
        "stage_binding_checksum": identity.stage_binding_checksum,
        "stage_identity_schema": identity.schema_version,
        "stage_identity_checksum": identity.identity_checksum,
    }


def _normalize_model_identity(
    model: Any,
    *,
    graph_only_schema: str,
) -> None:
    object.__setattr__(model, "run_id", identifier(model.run_id, "run_id"))
    object.__setattr__(model, "stage_id", identifier(model.stage_id, "stage_id"))
    object.__setattr__(
        model,
        "graph_checksum",
        checksum(model.graph_checksum, "graph_checksum"),
    )
    if model.schema_version != graph_only_schema:
        raise HarnessValidationError(
            "TaskPlan contract schema does not select a supported identity",
            code="task_plan_identity_schema_mismatch",
        )
    _normalize_graph_only_identity_fields(model)


def _normalize_graph_bound_identity(
    model: Any,
    *,
    graph_only_schema: str,
) -> None:
    if model.schema_version != graph_only_schema:
        raise HarnessValidationError(
            "TaskPlan contract schema does not select a supported identity",
            code="task_plan_identity_schema_mismatch",
        )
    object.__setattr__(
        model,
        "graph_checksum",
        checksum(model.graph_checksum, "graph_checksum"),
    )
    _normalize_graph_only_identity_fields(model)


def _normalize_graph_only_identity_fields(model: Any) -> None:
    normalized = {
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
    expected_versions = {
        "graph_schema_version": GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        "compiler_version": HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        "condition_policy_version": HARNESS_CONDITION_POLICY_VERSION,
        "stage_identity_schema": GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    }
    mismatched = {
        name: {"expected": expected, "actual": normalized[name]}
        for name, expected in expected_versions.items()
        if normalized[name] != expected
    }
    if normalized["graph_ref"] != (
        f"{normalized['graph_id']}@{normalized['graph_version']}"
    ):
        mismatched["graph_ref"] = {
            "expected": f"{normalized['graph_id']}@{normalized['graph_version']}",
            "actual": normalized["graph_ref"],
        }
    if mismatched:
        raise HarnessValidationError(
            "Graph-only TaskPlan identity versions do not match",
            code="task_plan_graph_identity_mismatch",
            details={"mismatched": mismatched},
        )
    for name, value in normalized.items():
        object.__setattr__(model, name, value)
    expected_identity_checksum = canonical_payload_checksum(
        _stage_identity_checksum_projection(model)
    )
    if model.stage_identity_checksum != expected_identity_checksum:
        raise HarnessValidationError(
            "Graph-only TaskPlan stage identity checksum does not match",
            code="task_plan_stage_identity_checksum_invalid",
            details={
                "expected": expected_identity_checksum,
                "actual": model.stage_identity_checksum,
            },
        )


def _stage_identity_checksum_projection(model: Any) -> dict[str, Any]:
    return {
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


def _model_identity_projection(model: Any) -> dict[str, Any]:
    return {
        "run_id": model.run_id,
        "graph_id": model.graph_id,
        "graph_version": model.graph_version,
        "graph_ref": model.graph_ref,
        "graph_schema_version": model.graph_schema_version,
        "compiler_version": model.compiler_version,
        "condition_policy_version": model.condition_policy_version,
        "stage_id": model.stage_id,
        "graph_checksum": model.graph_checksum,
        "stage_binding_checksum": model.stage_binding_checksum,
        "stage_identity_schema": model.stage_identity_schema,
        "stage_identity_checksum": model.stage_identity_checksum,
    }


def _matches_stage_identity(model: Any, identity: TaskPlanStageIdentity) -> bool:
    if not isinstance(identity, TaskPlanStageIdentity):
        raise TypeError("identity must be TaskPlanStageIdentity")
    return (
        _stage_identity_checksum_projection(model)
        == identity.checksum_projection()
        and model.stage_identity_checksum == identity.identity_checksum
    )


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    candidate_id: str
    run_id: str
    stage_id: str
    graph_checksum: str
    input_context_refs: tuple[str, ...]
    tasks: tuple[TaskSpec, ...]
    required_output_roles: tuple[str, ...]
    generated_by: str
    requested_plan_budget: PlanBuildBudget | Mapping[str, Any]
    requested_max_parallelism: int = 1
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source_observation_refs: tuple[str, ...] = ()
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    schema_version: str = GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA
    candidate_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.PLAN_CANDIDATE,
            self.schema_version,
        )
        object.__setattr__(self, "candidate_id", identifier(self.candidate_id, "candidate_id"))
        _normalize_model_identity(
            self,
            graph_only_schema=GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
        )
        object.__setattr__(
            self,
            "input_context_refs",
            stable_text_tuple(
                self.input_context_refs,
                "input_context_refs",
                allow_empty=False,
                item_kind="reference",
            ),
        )
        tasks = tuple(_model(item, TaskSpec, "tasks") for item in self.tasks)
        if not tasks:
            raise HarnessValidationError(
                "PlanCandidate must contain at least one task",
                code="task_plan_empty_candidate",
            )
        object.__setattr__(
            self,
            "tasks",
            tuple(sorted(tasks, key=lambda item: (item.task_id, item.task_definition_checksum))),
        )
        object.__setattr__(
            self,
            "required_output_roles",
            stable_text_tuple(
                self.required_output_roles,
                "required_output_roles",
                allow_empty=False,
            ),
        )
        object.__setattr__(self, "generated_by", required_text(self.generated_by, "generated_by"))
        object.__setattr__(
            self,
            "requested_plan_budget",
            _model(self.requested_plan_budget, PlanBuildBudget, "requested_plan_budget"),
        )
        object.__setattr__(
            self,
            "requested_max_parallelism",
            positive_int(self.requested_max_parallelism, "requested_max_parallelism"),
        )
        object.__setattr__(self, "diagnostics", frozen_mapping(self.diagnostics, "candidate.diagnostics"))
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "candidate.metadata"))
        object.__setattr__(
            self,
            "source_observation_refs",
            stable_text_tuple(
                self.source_observation_refs,
                "source_observation_refs",
                item_kind="reference",
            ),
        )
        ensure_candidate_only(self.checksum_projection(), path="$.candidate")
        object.__setattr__(self, "candidate_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        projection = {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            **_model_identity_projection(self),
            "input_context_refs": list(self.input_context_refs),
            "tasks": [task.to_dict() for task in self.tasks],
            "required_output_roles": list(self.required_output_roles),
            "generated_by": self.generated_by,
            "requested_plan_budget": self.requested_plan_budget.to_dict(),
            "requested_max_parallelism": self.requested_max_parallelism,
            "diagnostics": thaw_mapping(self.diagnostics),
            "metadata": thaw_mapping(self.metadata),
        }
        if self.source_observation_refs:
            projection["source_observation_refs"] = list(
                self.source_observation_refs
            )
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "candidate_checksum": self.candidate_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA

    def matches_stage_identity(self, identity: TaskPlanStageIdentity) -> bool:
        return _matches_stage_identity(self, identity)

    @classmethod
    def for_stage(
        cls,
        *,
        stage_identity: TaskPlanStageIdentity,
        candidate_id: str,
        input_context_refs: tuple[str, ...],
        tasks: tuple[TaskSpec, ...],
        required_output_roles: tuple[str, ...],
        generated_by: str,
        requested_plan_budget: PlanBuildBudget | Mapping[str, Any],
        requested_max_parallelism: int = 1,
        diagnostics: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_observation_refs: tuple[str, ...] = (),
    ) -> Self:
        return cls(
            candidate_id=candidate_id,
            input_context_refs=input_context_refs,
            tasks=tasks,
            required_output_roles=required_output_roles,
            generated_by=generated_by,
            requested_plan_budget=requested_plan_budget,
            requested_max_parallelism=requested_max_parallelism,
            diagnostics=diagnostics or {},
            metadata=metadata or {},
            source_observation_refs=source_observation_refs,
            schema_version=GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
            **_stage_identity_kwargs(stage_identity),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        ensure_candidate_only(value, path="$.candidate")
        schema_version = value.get("schema_version")
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.PLAN_CANDIDATE,
            str(schema_version),
        )
        identity_fields = _GRAPH_ONLY_MODEL_IDENTITY_FIELDS
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "candidate_id",
                    "run_id",
                    "stage_id",
                    "graph_checksum",
                    "input_context_refs",
                    "tasks",
                    "required_output_roles",
                    "generated_by",
                    "requested_plan_budget",
                    "requested_max_parallelism",
                    "diagnostics",
                    "metadata",
                    "candidate_checksum",
                }
            )
            | identity_fields,
            optional=frozenset({"source_observation_refs"}),
            model=cls.__name__,
        )
        payload.setdefault("source_observation_refs", ())
        supplied = checksum(payload.pop("candidate_checksum"), "candidate_checksum")
        raw_tasks = payload.pop("tasks")
        if isinstance(raw_tasks, (str, bytes)) or not isinstance(raw_tasks, Sequence):
            raise HarnessValidationError("PlanCandidate tasks must be an array", code="invalid_task_plan_payload")
        candidate = cls(tasks=tuple(TaskSpec.from_dict(item) for item in raw_tasks), **payload)
        _verify_checksum(supplied, candidate.candidate_checksum, "candidate_checksum", cls.__name__)
        return candidate


@dataclass(frozen=True, slots=True)
class TaskPlanLimits:
    max_tasks: int
    max_depth: int
    max_parallelism: int
    max_replans: int
    max_task_attempts: int
    plan_build_budget: PlanBuildBudget | Mapping[str, Any]
    per_task_budget: TaskBudget | Mapping[str, Any]
    aggregate_task_budget: TaskBudget | Mapping[str, Any]

    def __post_init__(self) -> None:
        for field_name in ("max_tasks", "max_depth", "max_parallelism", "max_task_attempts"):
            object.__setattr__(self, field_name, positive_int(getattr(self, field_name), field_name))
        object.__setattr__(self, "max_replans", non_negative_int(self.max_replans, "max_replans"))
        object.__setattr__(self, "plan_build_budget", _model(self.plan_build_budget, PlanBuildBudget, "plan_build_budget"))
        object.__setattr__(self, "per_task_budget", _model(self.per_task_budget, TaskBudget, "per_task_budget"))
        object.__setattr__(
            self,
            "aggregate_task_budget",
            _model(self.aggregate_task_budget, TaskBudget, "aggregate_task_budget"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_tasks": self.max_tasks,
            "max_depth": self.max_depth,
            "max_parallelism": self.max_parallelism,
            "max_replans": self.max_replans,
            "max_task_attempts": self.max_task_attempts,
            "plan_build_budget": self.plan_build_budget.to_dict(),
            "per_task_budget": self.per_task_budget.to_dict(),
            "aggregate_task_budget": self.aggregate_task_budget.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "max_tasks",
                    "max_depth",
                    "max_parallelism",
                    "max_replans",
                    "max_task_attempts",
                    "plan_build_budget",
                    "per_task_budget",
                    "aggregate_task_budget",
                }
            ),
            model=cls.__name__,
        )
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class ResolvedTaskSpec:
    task: TaskSpec | Mapping[str, Any]
    worker_ref: str
    worker_contract_ref: str
    allowed_tools: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    gate_refs: tuple[str, ...]
    normalized_budget: TaskBudget | Mapping[str, Any]
    normalized_retry_policy: TaskRetryPolicy | Mapping[str, Any]
    subagent_id: str | None = None
    schema_version: str = RESOLVED_TASK_DEFINITION_SCHEMA
    task_definition_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.RESOLVED_TASK_DEFINITION,
            self.schema_version,
        )
        task = _model(self.task, TaskSpec, "task")
        budget = _model(self.normalized_budget, TaskBudget, "normalized_budget")
        retry = _model(self.normalized_retry_policy, TaskRetryPolicy, "normalized_retry_policy")
        object.__setattr__(self, "task", task)
        object.__setattr__(self, "worker_ref", exact_reference(self.worker_ref, "worker_ref"))
        object.__setattr__(
            self,
            "worker_contract_ref",
            exact_reference(self.worker_contract_ref, "worker_contract_ref"),
        )
        object.__setattr__(self, "allowed_tools", stable_text_tuple(self.allowed_tools, "allowed_tools"))
        object.__setattr__(
            self,
            "allowed_memory_namespaces",
            stable_text_tuple(self.allowed_memory_namespaces, "allowed_memory_namespaces"),
        )
        object.__setattr__(
            self,
            "gate_refs",
            stable_text_tuple(self.gate_refs, "gate_refs", allow_empty=False, item_kind="exact_reference"),
        )
        object.__setattr__(self, "normalized_budget", budget)
        object.__setattr__(self, "normalized_retry_policy", retry)
        object.__setattr__(self, "subagent_id", optional_text(self.subagent_id, "subagent_id"))
        object.__setattr__(self, "task_definition_checksum", canonical_payload_checksum(self.checksum_projection()))

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def depends_on(self) -> tuple[str, ...]:
        return self.task.depends_on

    @property
    def output_role(self) -> str:
        return self.task.output_contract.output_role

    @property
    def priority(self) -> int:
        return self.task.priority

    @property
    def binding_checksum(self) -> str:
        """Checksum of the immutable execution boundary pinned in this task."""
        return canonical_payload_checksum(
            {
                "worker_ref": self.worker_ref,
                "worker_contract_ref": self.worker_contract_ref,
                "allowed_tools": list(self.allowed_tools),
                "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
                "subagent_id": self.subagent_id,
            }
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task": self.task.to_dict(),
            "worker_ref": self.worker_ref,
            "worker_contract_ref": self.worker_contract_ref,
            "allowed_tools": list(self.allowed_tools),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "gate_refs": list(self.gate_refs),
            "normalized_budget": self.normalized_budget.to_dict(),
            "normalized_retry_policy": self.normalized_retry_policy.to_dict(),
            "subagent_id": self.subagent_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "task_definition_checksum": self.task_definition_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "task",
                    "worker_ref",
                    "worker_contract_ref",
                    "allowed_tools",
                    "allowed_memory_namespaces",
                    "gate_refs",
                    "normalized_budget",
                    "normalized_retry_policy",
                    "subagent_id",
                    "task_definition_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("task_definition_checksum"), "task_definition_checksum")
        payload["task"] = TaskSpec.from_dict(payload["task"])
        resolved = cls(**payload)
        _verify_checksum(supplied, resolved.task_definition_checksum, "task_definition_checksum", cls.__name__)
        return resolved


@dataclass(frozen=True, slots=True)
class ValidatedTaskPlan:
    plan_id: str
    run_id: str
    stage_id: str
    graph_checksum: str
    version: int
    parent_plan_id: str | None
    source_candidate_ref: str
    policy_ref: str
    tasks: tuple[ResolvedTaskSpec, ...]
    required_output_roles: tuple[str, ...]
    limits: TaskPlanLimits | Mapping[str, Any]
    accepted_at: str
    policy_checksum: str | None = None
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    schema_version: str = GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
    plan_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.VALIDATED_PLAN,
            self.schema_version,
        )
        object.__setattr__(self, "plan_id", identifier(self.plan_id, "plan_id"))
        _normalize_model_identity(
            self,
            graph_only_schema=GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
        )
        object.__setattr__(self, "version", positive_int(self.version, "version"))
        object.__setattr__(
            self,
            "parent_plan_id",
            identifier(self.parent_plan_id, "parent_plan_id") if self.parent_plan_id is not None else None,
        )
        if self.policy_checksum is None:
            raise HarnessValidationError(
                "Graph-only ValidatedTaskPlan requires a policy checksum",
                code="task_plan_policy_checksum_required",
            )
        object.__setattr__(self, "source_candidate_ref", reference(self.source_candidate_ref, "source_candidate_ref"))
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
        object.__setattr__(
            self,
            "policy_checksum",
            checksum(self.policy_checksum, "policy_checksum")
            if self.policy_checksum is not None
            else None,
        )
        tasks = tuple(_model(item, ResolvedTaskSpec, "tasks") for item in self.tasks)
        if not tasks:
            raise HarnessValidationError("ValidatedTaskPlan must contain tasks", code="task_plan_empty_plan")
        object.__setattr__(self, "tasks", tuple(sorted(tasks, key=lambda item: (item.task_id, item.task_definition_checksum))))
        object.__setattr__(
            self,
            "required_output_roles",
            stable_text_tuple(self.required_output_roles, "required_output_roles", allow_empty=False),
        )
        object.__setattr__(self, "limits", _model(self.limits, TaskPlanLimits, "limits"))
        object.__setattr__(self, "accepted_at", _timestamp(self.accepted_at, "accepted_at"))
        object.__setattr__(self, "plan_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        projection = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            **_model_identity_projection(self),
            "version": self.version,
            "parent_plan_id": self.parent_plan_id,
            "source_candidate_ref": self.source_candidate_ref,
            "policy_ref": self.policy_ref,
            "tasks": [task.to_dict() for task in self.tasks],
            "required_output_roles": list(self.required_output_roles),
            "limits": self.limits.to_dict(),
            "accepted_at": self.accepted_at,
        }
        if self.policy_checksum is not None:
            projection["policy_checksum"] = self.policy_checksum
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "plan_checksum": self.plan_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA

    def matches_stage_identity(self, identity: TaskPlanStageIdentity) -> bool:
        return _matches_stage_identity(self, identity)

    def shares_stage_identity(self, other: Self) -> bool:
        if not isinstance(other, ValidatedTaskPlan):
            raise TypeError("other must be ValidatedTaskPlan")
        return (
            _model_identity_projection(self) == _model_identity_projection(other)
        )

    @classmethod
    def from_candidate(
        cls,
        candidate: PlanCandidate,
        *,
        plan_id: str,
        version: int,
        parent_plan_id: str | None,
        source_candidate_ref: str,
        policy_ref: str,
        policy_checksum: str,
        tasks: tuple[ResolvedTaskSpec, ...],
        required_output_roles: tuple[str, ...],
        limits: TaskPlanLimits | Mapping[str, Any],
        accepted_at: str,
    ) -> Self:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        identity_values = {
            "run_id": candidate.run_id,
            "stage_id": candidate.stage_id,
            "graph_checksum": candidate.graph_checksum,
        }
        identity_values.update(
            {
                name: getattr(candidate, name)
                for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS
            }
        )
        return cls(
            plan_id=plan_id,
            version=version,
            parent_plan_id=parent_plan_id,
            source_candidate_ref=source_candidate_ref,
            policy_ref=policy_ref,
            policy_checksum=policy_checksum,
            tasks=tasks,
            required_output_roles=required_output_roles,
            limits=limits,
            accepted_at=accepted_at,
            schema_version=GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
            **identity_values,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        schema_version = value.get("schema_version")
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.VALIDATED_PLAN,
            str(schema_version),
        )
        identity_fields = _GRAPH_ONLY_MODEL_IDENTITY_FIELDS
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "plan_id",
                    "run_id",
                    "stage_id",
                    "graph_checksum",
                    "version",
                    "parent_plan_id",
                    "source_candidate_ref",
                    "policy_ref",
                    "tasks",
                    "required_output_roles",
                    "limits",
                    "accepted_at",
                    "plan_checksum",
                }
            )
            | identity_fields,
            optional=frozenset({"policy_checksum"}),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("plan_checksum"), "plan_checksum")
        raw_tasks = payload.pop("tasks")
        if isinstance(raw_tasks, (str, bytes)) or not isinstance(raw_tasks, Sequence):
            raise HarnessValidationError("ValidatedTaskPlan tasks must be an array", code="invalid_task_plan_payload")
        plan = cls(tasks=tuple(ResolvedTaskSpec.from_dict(item) for item in raw_tasks), **payload)
        _verify_checksum(supplied, plan.plan_checksum, "plan_checksum", cls.__name__)
        return plan


class PlanPatchOperationType(StrEnum):
    ADD_REPLACEMENT_TASK = "ADD_REPLACEMENT_TASK"
    SKIP_PENDING_TASK = "SKIP_PENDING_TASK"
    UPDATE_PENDING_DEPENDENCY = "UPDATE_PENDING_DEPENDENCY"


@dataclass(frozen=True, slots=True)
class PlanPatchOperation:
    operation: PlanPatchOperationType | str
    target_task_id: str | None = None
    replacement_task: TaskSpec | Mapping[str, Any] | None = None
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        operation = PlanPatchOperationType(self.operation)
        target = identifier(self.target_task_id, "target_task_id") if self.target_task_id is not None else None
        replacement = self.replacement_task
        if replacement is not None:
            replacement = _model(replacement, TaskSpec, "replacement_task")
        depends_on = stable_text_tuple(self.depends_on, "depends_on")
        if target is None:
            raise HarnessValidationError(
                "task patch operation requires target_task_id",
                code="invalid_task_patch_operation",
            )
        if operation is PlanPatchOperationType.ADD_REPLACEMENT_TASK and replacement is None:
            raise HarnessValidationError(
                "ADD_REPLACEMENT_TASK requires replacement_task",
                code="invalid_task_patch_operation",
            )
        if operation is PlanPatchOperationType.ADD_REPLACEMENT_TASK and depends_on:
            raise HarnessValidationError(
                "ADD_REPLACEMENT_TASK must use replacement_task dependencies",
                code="invalid_task_patch_operation",
            )
        if operation is not PlanPatchOperationType.ADD_REPLACEMENT_TASK and replacement is not None:
            raise HarnessValidationError(
                "replacement_task is only valid for ADD_REPLACEMENT_TASK",
                code="invalid_task_patch_operation",
            )
        if operation is not PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY and depends_on:
            raise HarnessValidationError(
                "depends_on is only valid for UPDATE_PENDING_DEPENDENCY",
                code="invalid_task_patch_operation",
            )
        if operation is PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY and replacement is not None:
            raise HarnessValidationError(
                "replacement_task is not valid for UPDATE_PENDING_DEPENDENCY",
                code="invalid_task_patch_operation",
            )
        if operation is PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY and not depends_on:
            raise HarnessValidationError(
                "UPDATE_PENDING_DEPENDENCY requires depends_on",
                code="invalid_task_patch_operation",
            )
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "target_task_id", target)
        object.__setattr__(self, "replacement_task", replacement)
        object.__setattr__(self, "depends_on", depends_on)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "target_task_id": self.target_task_id,
            "replacement_task": self.replacement_task.to_dict() if self.replacement_task else None,
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset({"operation", "target_task_id", "replacement_task", "depends_on"}),
            model=cls.__name__,
        )
        replacement = payload.get("replacement_task")
        if replacement is not None:
            payload["replacement_task"] = TaskSpec.from_dict(replacement)
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class PlanPatch:
    patch_id: str
    run_id: str
    stage_id: str
    base_plan_id: str
    base_plan_version: int
    reason_code: str
    source_candidate_ref: str
    operations: tuple[PlanPatchOperation, ...]
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    graph_checksum: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    schema_version: str = GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA
    patch_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(TaskPlanContractKind.PLAN_PATCH, self.schema_version)
        object.__setattr__(self, "patch_id", identifier(self.patch_id, "patch_id"))
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        object.__setattr__(self, "stage_id", identifier(self.stage_id, "stage_id"))
        object.__setattr__(self, "base_plan_id", identifier(self.base_plan_id, "base_plan_id"))
        object.__setattr__(self, "base_plan_version", positive_int(self.base_plan_version, "base_plan_version"))
        object.__setattr__(self, "reason_code", identifier(self.reason_code, "reason_code"))
        object.__setattr__(self, "source_candidate_ref", reference(self.source_candidate_ref, "source_candidate_ref"))
        _normalize_graph_bound_identity(
            self,
            graph_only_schema=GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
        )
        operations = tuple(_model(item, PlanPatchOperation, "operations") for item in self.operations)
        if not operations:
            raise HarnessValidationError("PlanPatch must contain operations", code="empty_task_plan_patch")
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "patch_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "patch_id": self.patch_id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "base_plan_id": self.base_plan_id,
            "base_plan_version": self.base_plan_version,
            "reason_code": self.reason_code,
            "source_candidate_ref": self.source_candidate_ref,
            "operations": [operation.to_dict() for operation in self.operations],
        }
        payload.update(
            {
                "graph_id": self.graph_id,
                "graph_version": self.graph_version,
                "graph_ref": self.graph_ref,
                "graph_schema_version": self.graph_schema_version,
                "compiler_version": self.compiler_version,
                "condition_policy_version": self.condition_policy_version,
                "graph_checksum": self.graph_checksum,
                "stage_binding_checksum": self.stage_binding_checksum,
                "stage_identity_schema": self.stage_identity_schema,
                "stage_identity_checksum": self.stage_identity_checksum,
            }
        )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "patch_checksum": self.patch_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA

    @classmethod
    def for_plan(
        cls,
        plan: ValidatedTaskPlan,
        *,
        patch_id: str,
        reason_code: str,
        source_candidate_ref: str,
        operations: tuple[PlanPatchOperation, ...],
    ) -> Self:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        graph_identity = {
            name: getattr(plan, name)
            for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        }
        return cls(
            patch_id=patch_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            base_plan_id=plan.plan_id,
            base_plan_version=plan.version,
            reason_code=reason_code,
            source_candidate_ref=source_candidate_ref,
            operations=operations,
            schema_version=GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
            **graph_identity,
        )

    def matches_plan_identity(self, plan: ValidatedTaskPlan) -> bool:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if (
            self.run_id != plan.run_id
            or self.stage_id != plan.stage_id
            or self.base_plan_id != plan.plan_id
            or self.base_plan_version != plan.version
        ):
            return False
        return all(
            getattr(self, name) == getattr(plan, name)
            for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        schema_version = value.get("schema_version")
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.PLAN_PATCH,
            schema_version,
        )
        graph_identity = _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "patch_id",
                    "run_id",
                    "stage_id",
                    "base_plan_id",
                    "base_plan_version",
                    "reason_code",
                    "source_candidate_ref",
                    "operations",
                    "patch_checksum",
                }
            )
            | graph_identity,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("patch_checksum"), "patch_checksum")
        raw_operations = payload.pop("operations")
        if isinstance(raw_operations, (str, bytes)) or not isinstance(raw_operations, Sequence):
            raise HarnessValidationError("PlanPatch operations must be an array", code="invalid_task_plan_payload")
        patch = cls(operations=tuple(PlanPatchOperation.from_dict(item) for item in raw_operations), **payload)
        _verify_checksum(supplied, patch.patch_checksum, "patch_checksum", cls.__name__)
        return patch


@dataclass(frozen=True, slots=True)
class TaskResultReference:
    result_ref: str
    result_checksum: str
    output_role: str
    output_schema_ref: str
    schema_version: str = TASK_RESULT_REFERENCE_SCHEMA
    reference_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.RESULT_REFERENCE,
            self.schema_version,
        )
        object.__setattr__(self, "result_ref", reference(self.result_ref, "result_ref"))
        object.__setattr__(self, "result_checksum", checksum(self.result_checksum, "result_checksum"))
        object.__setattr__(self, "output_role", identifier(self.output_role, "output_role"))
        object.__setattr__(
            self,
            "output_schema_ref",
            exact_reference(self.output_schema_ref, "output_schema_ref"),
        )
        object.__setattr__(self, "reference_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "result_ref": self.result_ref,
            "result_checksum": self.result_checksum,
            "output_role": self.output_role,
            "output_schema_ref": self.output_schema_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "reference_checksum": self.reference_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "result_ref",
                    "result_checksum",
                    "output_role",
                    "output_schema_ref",
                    "reference_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("reference_checksum"), "reference_checksum")
        result = cls(**payload)
        _verify_checksum(supplied, result.reference_checksum, "reference_checksum", cls.__name__)
        return result


@dataclass(frozen=True, slots=True)
class TaskInstance:
    run_id: str
    stage_id: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    task_id: str
    task_definition_checksum: str
    task_instance_id: str
    attempt: int
    worker_ref: str
    idempotency_key: str
    fencing_token: str
    budget_snapshot: TaskBudget | Mapping[str, Any]
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
    schema_version: str = GRAPH_ONLY_TASK_INSTANCE_SCHEMA
    instance_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.TASK_INSTANCE,
            self.schema_version,
        )
        for field_name in ("run_id", "stage_id", "plan_id", "task_id", "task_instance_id"):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        _normalize_graph_bound_identity(
            self,
            graph_only_schema=GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
        )
        object.__setattr__(self, "plan_version", positive_int(self.plan_version, "plan_version"))
        object.__setattr__(self, "plan_checksum", checksum(self.plan_checksum, "plan_checksum"))
        object.__setattr__(
            self,
            "task_definition_checksum",
            checksum(self.task_definition_checksum, "task_definition_checksum"),
        )
        object.__setattr__(self, "attempt", positive_int(self.attempt, "attempt"))
        object.__setattr__(self, "worker_ref", exact_reference(self.worker_ref, "worker_ref"))
        object.__setattr__(self, "idempotency_key", identifier(self.idempotency_key, "idempotency_key"))
        object.__setattr__(self, "fencing_token", identifier(self.fencing_token, "fencing_token"))
        object.__setattr__(self, "budget_snapshot", _model(self.budget_snapshot, TaskBudget, "budget_snapshot"))
        object.__setattr__(self, "instance_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
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
                "graph_checksum": self.graph_checksum,
                "stage_binding_checksum": self.stage_binding_checksum,
                "stage_identity_schema": self.stage_identity_schema,
                "stage_identity_checksum": self.stage_identity_checksum,
            }
        )
        payload.update({
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "task_id": self.task_id,
            "task_definition_checksum": self.task_definition_checksum,
            "task_instance_id": self.task_instance_id,
            "attempt": self.attempt,
            "worker_ref": self.worker_ref,
            "idempotency_key": self.idempotency_key,
            "fencing_token": self.fencing_token,
            "budget_snapshot": self.budget_snapshot.to_dict(),
        })
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "instance_checksum": self.instance_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_TASK_INSTANCE_SCHEMA

    def matches_plan_identity(self, plan: ValidatedTaskPlan) -> bool:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if (
            self.run_id != plan.run_id
            or self.stage_id != plan.stage_id
            or self.plan_id != plan.plan_id
            or self.plan_version != plan.version
            or self.plan_checksum != plan.plan_checksum
        ):
            return False
        return all(
            getattr(self, name) == getattr(plan, name)
            for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        )

    def matches_plan_projection_identity(
        self,
        projection: TaskPlanProjection,
    ) -> bool:
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        if (
            self.run_id != projection.run_id
            or self.stage_id != projection.stage_id
            or self.plan_id != projection.plan_id
            or self.plan_version != projection.plan_version
            or self.plan_checksum != projection.plan_checksum
        ):
            return False
        return all(
            getattr(self, name) == getattr(projection, name)
            for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        schema_version = value.get("schema_version")
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.TASK_INSTANCE,
            schema_version,
        )
        graph_identity = _GRAPH_ONLY_MODEL_IDENTITY_FIELDS | {"graph_checksum"}
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "run_id",
                    "stage_id",
                    "plan_id",
                    "plan_version",
                    "plan_checksum",
                    "task_id",
                    "task_definition_checksum",
                    "task_instance_id",
                    "attempt",
                    "worker_ref",
                    "idempotency_key",
                    "fencing_token",
                    "budget_snapshot",
                    "instance_checksum",
                }
            )
            | graph_identity,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("instance_checksum"), "instance_checksum")
        instance = cls(**payload)
        _verify_checksum(supplied, instance.instance_checksum, "instance_checksum", cls.__name__)
        return instance


class TaskLifecycle(StrEnum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    BLOCKED_DEPENDENCY = "blocked_dependency"


@dataclass(frozen=True, slots=True)
class TaskProjection:
    task_id: str
    task_definition_checksum: str
    status: TaskLifecycle | str
    attempts: int = 0
    active_instance_id: str | None = None
    result: TaskResultReference | Mapping[str, Any] | None = None
    failure_reason_code: str | None = None
    schema_version: str = GRAPH_ONLY_TASK_PROJECTION_SCHEMA
    projection_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.TASK_PROJECTION,
            self.schema_version,
        )
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        object.__setattr__(
            self,
            "task_definition_checksum",
            checksum(self.task_definition_checksum, "task_definition_checksum"),
        )
        status = TaskLifecycle(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "attempts", non_negative_int(self.attempts, "attempts"))
        object.__setattr__(
            self,
            "active_instance_id",
            identifier(self.active_instance_id, "active_instance_id") if self.active_instance_id is not None else None,
        )
        result = self.result
        if result is not None:
            result = _model(result, TaskResultReference, "result")
        object.__setattr__(self, "result", result)
        object.__setattr__(
            self,
            "failure_reason_code",
            identifier(self.failure_reason_code, "failure_reason_code") if self.failure_reason_code is not None else None,
        )
        if status is TaskLifecycle.SUCCEEDED and result is None:
            raise HarnessValidationError(
                "succeeded task projection requires a committed result reference",
                code="invalid_task_projection",
                details={"task_id": self.task_id},
            )
        if result is not None and status is not TaskLifecycle.SUCCEEDED:
            raise HarnessValidationError(
                "only succeeded task projection may carry a result reference",
                code="invalid_task_projection",
                details={"task_id": self.task_id},
            )
        object.__setattr__(self, "projection_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "task_definition_checksum": self.task_definition_checksum,
            "status": self.status.value,
            "attempts": self.attempts,
            "active_instance_id": self.active_instance_id,
            "result": self.result.to_dict() if self.result else None,
            "failure_reason_code": self.failure_reason_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "projection_checksum": self.projection_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_TASK_PROJECTION_SCHEMA

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.TASK_PROJECTION,
            value.get("schema_version"),
        )
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "task_id",
                    "task_definition_checksum",
                    "status",
                    "attempts",
                    "active_instance_id",
                    "result",
                    "failure_reason_code",
                    "projection_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("projection_checksum"), "projection_checksum")
        if payload["result"] is not None:
            payload["result"] = TaskResultReference.from_dict(payload["result"])
        projection = cls(**payload)
        _verify_checksum(supplied, projection.projection_checksum, "projection_checksum", cls.__name__)
        return projection


@dataclass(frozen=True, slots=True)
class TaskPlanProjection:
    run_id: str
    stage_id: str
    graph_checksum: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    policy_ref: str
    tasks: tuple[TaskProjection, ...]
    consumed_budget: Mapping[str, Any]
    last_sequence: int
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    schema_version: str = GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA
    projection_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_executable(
            TaskPlanContractKind.PLAN_PROJECTION,
            self.schema_version,
        )
        for field_name in ("run_id", "stage_id", "plan_id"):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        object.__setattr__(self, "graph_checksum", checksum(self.graph_checksum, "graph_checksum"))
        _normalize_graph_bound_identity(
            self,
            graph_only_schema=GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
        )
        object.__setattr__(self, "plan_version", positive_int(self.plan_version, "plan_version"))
        object.__setattr__(self, "plan_checksum", checksum(self.plan_checksum, "plan_checksum"))
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
        tasks = tuple(_model(item, TaskProjection, "tasks") for item in self.tasks)
        identities = [task.task_id for task in tasks]
        if len(identities) != len(set(identities)):
            raise HarnessValidationError(
                "TaskPlanProjection task ids must be unique",
                code="duplicate_task_projection",
            )
        expected_task_schema = GRAPH_ONLY_TASK_PROJECTION_SCHEMA
        mismatched_task_schemas = sorted(
            {
                task.schema_version
                for task in tasks
                if task.schema_version != expected_task_schema
            }
        )
        if mismatched_task_schemas:
            raise HarnessValidationError(
                "TaskPlanProjection contains mixed task projection schemas",
                code="task_plan_projection_schema_mismatch",
                details={
                    "expected": expected_task_schema,
                    "actual": mismatched_task_schemas,
                },
            )
        object.__setattr__(self, "tasks", tuple(sorted(tasks, key=lambda item: item.task_id)))
        object.__setattr__(self, "consumed_budget", frozen_mapping(self.consumed_budget, "consumed_budget"))
        if "ledger" in self.consumed_budget:
            from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

            ledger = TaskPlanBudgetLedger.from_snapshot(self.consumed_budget)
            if (ledger.run_id, ledger.stage_id, ledger.policy_ref) != (self.run_id, self.stage_id, self.policy_ref):
                raise HarnessValidationError(
                    "TaskPlan budget owner differs from projection identity",
                    code="task_plan_budget_identity_conflict",
                )
        object.__setattr__(self, "last_sequence", non_negative_int(self.last_sequence, "last_sequence"))
        object.__setattr__(self, "projection_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
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
        payload.update({
            "stage_id": self.stage_id,
            "graph_checksum": self.graph_checksum,
            "stage_binding_checksum": self.stage_binding_checksum,
            "stage_identity_schema": self.stage_identity_schema,
            "stage_identity_checksum": self.stage_identity_checksum,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "policy_ref": self.policy_ref,
            "tasks": [task.to_dict() for task in self.tasks],
            "consumed_budget": thaw_mapping(self.consumed_budget),
            "last_sequence": self.last_sequence,
        })
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "projection_checksum": self.projection_checksum}

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version == GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA

    def matches_plan_identity(self, plan: ValidatedTaskPlan) -> bool:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if (
            self.run_id != plan.run_id
            or self.stage_id != plan.stage_id
            or self.graph_checksum != plan.graph_checksum
            or self.plan_id != plan.plan_id
            or self.plan_version != plan.version
            or self.plan_checksum != plan.plan_checksum
            or self.policy_ref != plan.policy_ref
        ):
            return False
        return all(
            getattr(self, name) == getattr(plan, name)
            for name in _GRAPH_ONLY_MODEL_IDENTITY_FIELDS
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        schema_version = value.get("schema_version")
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.PLAN_PROJECTION,
            schema_version,
        )
        graph_identity = _GRAPH_ONLY_MODEL_IDENTITY_FIELDS
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "run_id",
                    "stage_id",
                    "graph_checksum",
                    "plan_id",
                    "plan_version",
                    "plan_checksum",
                    "policy_ref",
                    "tasks",
                    "consumed_budget",
                    "last_sequence",
                    "projection_checksum",
                }
            )
            | graph_identity,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("projection_checksum"), "projection_checksum")
        raw_tasks = payload.pop("tasks")
        if isinstance(raw_tasks, (str, bytes)) or not isinstance(raw_tasks, Sequence):
            raise HarnessValidationError("TaskPlanProjection tasks must be an array", code="invalid_task_plan_payload")
        projection = cls(tasks=tuple(TaskProjection.from_dict(item) for item in raw_tasks), **payload)
        _verify_checksum(supplied, projection.projection_checksum, "projection_checksum", cls.__name__)
        return projection


def _model(value: Any, model_type: type, field_name: str):
    if isinstance(value, model_type):
        return value
    if isinstance(value, Mapping):
        return model_type.from_dict(value)
    raise HarnessValidationError(
        f"{field_name} must be {model_type.__name__}",
        code="invalid_task_plan_model",
        details={"field": field_name, "expected": model_type.__name__},
    )


def _verify_checksum(supplied: str, computed: str, field_name: str, model: str) -> None:
    if supplied != computed:
        raise HarnessValidationError(
            f"{model} checksum does not match canonical content",
            code="task_plan_checksum_mismatch",
            details={"model": model, "field": field_name},
        )


def _timestamp(value: Any, field_name: str) -> str:
    text = required_text(value, field_name)
    parsed = parse_datetime(text)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarnessValidationError(
            f"{field_name} must be an RFC3339 timezone-aware timestamp",
            code="invalid_task_plan_timestamp",
            details={"field": field_name},
        )
    formatted = format_datetime(parsed)
    if formatted is None:
        raise AssertionError("timezone-aware timestamp did not format")
    return formatted


__all__ = [
    "PlanBuildBudget",
    "PlanCandidate",
    "PlanPatch",
    "PlanPatchOperation",
    "PlanPatchOperationType",
    "ResolvedTaskSpec",
    "TaskAcceptanceCriteria",
    "TaskBudget",
    "TaskInstance",
    "TaskLifecycle",
    "TaskOutputContract",
    "TaskPlanLimits",
    "TaskPlanProjection",
    "TaskProjection",
    "TaskResultReference",
    "TaskRetryPolicy",
    "TaskSpec",
    "ValidatedTaskPlan",
    "TaskStatus",
    "TaskPlanBudget",
    "TASK_SPEC_SCHEMA",
    "TASK_PLAN_SCHEMA",
    "TASK_RESULT_SCHEMA",
]

# Names used by early TaskPlan fixtures remain harmless read-only aliases;
# canonical storage and checksums still use the versioned contracts above.
TaskStatus = TaskLifecycle
TaskPlanBudget = TaskBudget
TASK_SPEC_SCHEMA = TASK_DEFINITION_SCHEMA
TASK_PLAN_SCHEMA = GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
TASK_RESULT_SCHEMA = TASK_RESULT_REFERENCE_SCHEMA
