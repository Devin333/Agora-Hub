from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    REF_KIND_PLANNING,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefResolutionPort,
    normalize_ref_descriptors,
    validate_ref_configuration,
)
from framework.harness.task_plan.binding import TaskPlanCapabilityRegistry
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    frozen_mapping,
    exact_reference,
    identifier,
    stable_text_tuple,
    task_reference_producer,
    task_output_reference_producer,
    thaw_mapping,
)
from framework.harness.task_plan.dag import task_dependency_depths
from framework.harness.task_plan.identity import TaskPlanStageIdentity
from framework.harness.task_plan.models import (
    PlanCandidate,
    ResolvedTaskSpec,
    TaskBudget,
    TaskPlanLimits,
    TaskSpec,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding


TASK_PLAN_VALIDATOR_VERSION = "newsroom.harness-task-plan-validator/v1"


@dataclass(frozen=True, slots=True)
class TaskPlanValidationContext:
    run_id: str
    stage_binding: TaskPlanStageBinding
    available_input_refs: tuple[str, ...]
    future_stage_input_refs: tuple[str, ...] = ()
    registered_gate_refs: tuple[str, ...] = ()
    registered_aggregator_refs: tuple[str, ...] = ()
    remaining_task_budget: TaskBudget | Mapping[str, Any] | None = None
    ref_authority: RefAuthority | None = None
    ref_policy: RefAccessPolicy | None = None
    ref_resolution: RefResolutionPort | None = None
    ref_descriptors: Mapping[str, RefDescriptor] = dataclass_field(default_factory=dict)
    stage_identity: TaskPlanStageIdentity = dataclass_field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        if not isinstance(self.stage_binding, TaskPlanStageBinding):
            raise TypeError("stage_binding must be TaskPlanStageBinding")
        object.__setattr__(
            self,
            "stage_identity",
            TaskPlanStageIdentity(self.run_id, self.stage_binding),
        )
        object.__setattr__(self, "available_input_refs", stable_text_tuple(self.available_input_refs, "available_input_refs", item_kind="reference"))
        object.__setattr__(self, "future_stage_input_refs", stable_text_tuple(self.future_stage_input_refs, "future_stage_input_refs", item_kind="reference"))
        object.__setattr__(self, "registered_gate_refs", stable_text_tuple(self.registered_gate_refs, "registered_gate_refs", item_kind="exact_reference"))
        object.__setattr__(self, "registered_aggregator_refs", stable_text_tuple(self.registered_aggregator_refs, "registered_aggregator_refs", item_kind="exact_reference"))
        descriptors = normalize_ref_descriptors(self.ref_descriptors)
        validate_ref_configuration(self.ref_authority, self.ref_policy, self.ref_resolution, descriptors)
        if self.ref_authority is not None:
            self.ref_authority.require_scope(self.ref_policy, run_id=self.run_id, stage_id=self.stage_id)
        object.__setattr__(self, "ref_descriptors", descriptors)
        budget = self.remaining_task_budget
        if budget is not None and not isinstance(budget, TaskBudget):
            if not isinstance(budget, Mapping):
                raise HarnessValidationError("remaining_task_budget must be TaskBudget", code="invalid_task_plan_validation_context")
            budget = TaskBudget.from_dict(budget)
        object.__setattr__(self, "remaining_task_budget", budget)

    @property
    def graph_id(self) -> str:
        return self.stage_identity.graph_id

    @property
    def graph_version(self) -> str:
        return self.stage_identity.graph_version

    @property
    def graph_ref(self) -> str:
        return self.stage_identity.graph_ref

    @property
    def stage_id(self) -> str:
        return self.stage_binding.stage_id

    @property
    def graph_checksum(self) -> str:
        return self.stage_binding.graph_checksum


@dataclass(frozen=True, slots=True)
class TaskPlanDiagnostic:
    code: str
    message: str
    phase: str
    task_id: str | None = None
    field: str | None = None
    details: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", identifier(self.code, "diagnostic.code"))
        object.__setattr__(self, "phase", identifier(self.phase, "diagnostic.phase"))
        message = str(self.message).strip()
        if not message or len(message) > 512:
            raise HarnessValidationError("TaskPlan diagnostic message must be bounded", code="invalid_task_plan_diagnostic")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "task_id", str(self.task_id) if self.task_id is not None else None)
        object.__setattr__(self, "field", str(self.field) if self.field is not None else None)
        object.__setattr__(self, "details", frozen_mapping(self.details, "diagnostic.details"))

    @property
    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (self.phase, self.code, self.task_id or "", self.field or "", canonical_payload_checksum(self.to_dict()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "phase": self.phase,
            "task_id": self.task_id,
            "field": self.field,
            "details": thaw_mapping(self.details),
        }


@dataclass(frozen=True, slots=True)
class TaskPlanValidationResult:
    candidate_checksum: str
    policy_ref: str
    accepted: bool
    diagnostics: tuple[TaskPlanDiagnostic, ...]
    resolved_tasks: tuple[ResolvedTaskSpec, ...] = ()
    limits: TaskPlanLimits | None = None
    validator_version: str = TASK_PLAN_VALIDATOR_VERSION
    result_checksum: str = dataclass_field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_checksum", checksum(self.candidate_checksum, "candidate_checksum"))
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
        diagnostics = tuple(sorted(self.diagnostics, key=lambda item: item.sort_key))
        object.__setattr__(self, "diagnostics", diagnostics)
        resolved = tuple(sorted(self.resolved_tasks, key=lambda item: (item.task_id, item.task_definition_checksum)))
        object.__setattr__(self, "resolved_tasks", resolved)
        if self.accepted and (diagnostics or not resolved or self.limits is None):
            raise HarnessValidationError("accepted validation must have resolved tasks and no diagnostics", code="invalid_task_plan_validation_result")
        if not self.accepted and resolved:
            raise HarnessValidationError("rejected validation must not expose partial resolved tasks", code="invalid_task_plan_validation_result")
        object.__setattr__(self, "result_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "validator_version": self.validator_version,
            "candidate_checksum": self.candidate_checksum,
            "policy_ref": self.policy_ref,
            "accepted": self.accepted,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "resolved_tasks": [item.to_dict() for item in self.resolved_tasks],
            "limits": self.limits.to_dict() if self.limits else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "result_checksum": self.result_checksum}

    def require_valid(self) -> tuple[ResolvedTaskSpec, ...]:
        if not self.accepted:
            raise HarnessValidationError(
                "TaskPlan candidate validation failed",
                code="task_plan_candidate_rejected",
                details={"candidate_checksum": self.candidate_checksum, "reason_codes": [item.code for item in self.diagnostics]},
            )
        return self.resolved_tasks


class TaskPlanValidator:
    """Pure, fail-closed candidate validator and plan builder."""

    def validate(
        self,
        candidate: PlanCandidate,
        *,
        policy: TaskPlanPolicy,
        capabilities: TaskPlanCapabilityRegistry,
        context: TaskPlanValidationContext,
    ) -> TaskPlanValidationResult:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        if not isinstance(policy, TaskPlanPolicy):
            raise TypeError("policy must be TaskPlanPolicy")
        if not callable(getattr(capabilities, "resolve", None)):
            raise TypeError("capabilities must expose resolve(capability, policy)")
        if not isinstance(context, TaskPlanValidationContext):
            raise TypeError("context must be TaskPlanValidationContext")
        diagnostics: list[TaskPlanDiagnostic] = []
        if (
            policy.exact_ref != context.stage_binding.policy_ref
            or policy.stage_id != context.stage_binding.stage_id
            or policy.required_output_roles
            != context.stage_binding.required_output_roles
        ):
            diagnostics.append(
                _diag(
                    "task_plan_policy_mismatch",
                    "policy does not match the frozen Graph stage binding",
                    "identity",
                    field="policy_ref",
                )
            )
        for name in ("run_id", "stage_id", "graph_checksum"):
            if getattr(candidate, name) != getattr(context, name):
                diagnostics.append(_diag(f"candidate_{name}_mismatch", f"candidate {name} does not match stage context", "identity", field=name))
        if not candidate.matches_stage_identity(
            context.stage_identity
        ):
            diagnostics.append(
                _diag(
                    "task_plan_candidate_graph_identity_mismatch",
                    "candidate Graph identity does not match stage context",
                    "identity",
                    field="stage_identity_checksum",
                )
            )
        if candidate.stage_id != policy.stage_id:
            diagnostics.append(_diag("candidate_policy_stage_mismatch", "candidate stage does not match policy", "identity", field="stage_id"))
        if len(candidate.tasks) > policy.max_tasks:
            diagnostics.append(_diag("task_count_exceeded", "candidate exceeds max_tasks", "policy", details={"max_tasks": policy.max_tasks}))
        if candidate.requested_max_parallelism > policy.max_parallelism:
            diagnostics.append(_diag("parallelism_exceeded", "candidate requests too much parallelism", "policy"))
        if candidate.requested_plan_budget.exceeds(policy.limits.plan_build_budget):
            diagnostics.append(_diag("plan_build_budget_exceeded", "candidate exceeds plan-builder budget", "policy"))
        if not set(candidate.input_context_refs).issubset(set(policy.allowed_input_refs)):
            diagnostics.append(_diag("candidate_input_not_allowed", "candidate requests context outside policy", "dataflow", details={"refs": sorted(set(candidate.input_context_refs) - set(policy.allowed_input_refs))}))
        if context.ref_authority is not None:
            if context.ref_policy is None:
                diagnostics.append(_diag("ref_authority_policy_required", "reference authority requires a pinned access policy", "refs"))
            else:
                for ref in candidate.input_context_refs:
                    self._authorize_ref(
                        ref,
                        expected_kind=REF_KIND_INPUT,
                        policy=context.ref_policy,
                        context=context,
                        diagnostics=diagnostics,
                        field="input_context_refs",
                    )
                for ref in candidate.source_observation_refs:
                    self._authorize_ref(
                        ref,
                        expected_kind=REF_KIND_PLANNING,
                        policy=context.ref_policy,
                        context=context,
                        diagnostics=diagnostics,
                        field="source_observation_refs",
                    )
        candidate_roles = set(candidate.required_output_roles)
        missing = sorted(set(policy.required_output_roles) - candidate_roles)
        extra = sorted(candidate_roles - set(policy.allowed_output_roles))
        if missing or extra:
            diagnostics.append(_diag("required_output_roles_mismatch", "candidate output roles do not match policy", "outputs", details={"missing": missing, "extra": extra}))

        by_id: dict[str, TaskSpec] = {}
        duplicates = [task_id for task_id, count in Counter(task.task_id for task in candidate.tasks).items() if count > 1]
        for task_id in sorted(duplicates):
            diagnostics.append(_diag("duplicate_task_id", "task ids must be unique", "dag", task_id=task_id))
        for task in candidate.tasks:
            by_id.setdefault(task.task_id, task)
        depths = self._validate_dag(by_id, policy.max_depth, diagnostics)
        resolved: dict[str, ResolvedTaskSpec] = {}
        aggregate = dict.fromkeys(policy.aggregate_task_budget.to_dict(), 0)
        for task in candidate.tasks:
            self._validate_task(task, by_id, policy, capabilities, context, diagnostics)
            self._validate_task_refs(task, by_id, context, diagnostics)
            if task.task_id in resolved:
                continue
            try:
                binding = capabilities.resolve(task.worker_capability, policy)
                resolved[task.task_id] = binding.resolve_task(task, policy)
                for field_name, value in resolved[task.task_id].normalized_budget.to_dict().items():
                    aggregate[field_name] += value
            except HarnessValidationError as exc:
                diagnostics.append(_diag(exc.code or "task_plan_binding_invalid", str(exc), "binding", task_id=task.task_id, details=exc.details or {}))
            except Exception as exc:
                diagnostics.append(_diag("task_plan_binding_invalid", str(exc), "binding", task_id=task.task_id))
        aggregate_limit = policy.aggregate_task_budget
        aggregate_exceeded = [name for name, value in aggregate.items() if value > getattr(aggregate_limit, name)]
        if aggregate_exceeded:
            diagnostics.append(_diag("aggregate_budget_exceeded", "candidate exceeds aggregate task budget", "budget", details={"fields": aggregate_exceeded}))
        if context.remaining_task_budget is not None:
            remaining = context.remaining_task_budget.to_dict()
            remaining_exceeded = sorted(set(aggregate) ^ set(remaining))
            remaining_exceeded.extend(name for name, value in aggregate.items() if name in remaining and value > remaining[name])
            if remaining_exceeded:
                diagnostics.append(_diag("remaining_budget_exceeded", "candidate exceeds remaining run budget", "budget", details={"fields": remaining_exceeded}))
        self._validate_outputs(by_id, depths, policy, context, diagnostics)
        diagnostics = _bounded(diagnostics, policy.max_validation_diagnostics)
        accepted = not diagnostics
        return TaskPlanValidationResult(
            candidate_checksum=candidate.candidate_checksum,
            policy_ref=policy.exact_ref,
            accepted=accepted,
            diagnostics=tuple(diagnostics),
            resolved_tasks=tuple(resolved.values()) if accepted else (),
            limits=policy.limits if accepted else None,
        )

    def accept(
        self,
        candidate: PlanCandidate,
        policy: TaskPlanPolicy,
        capabilities: TaskPlanCapabilityRegistry,
        *,
        plan_id: str | None = None,
        accepted_at: str,
        context: TaskPlanValidationContext,
        aggregator_registered: bool = True,
    ) -> ValidatedTaskPlan:
        if not isinstance(context, TaskPlanValidationContext):
            raise TypeError("context must be TaskPlanValidationContext")
        result = self.validate(candidate, policy=policy, capabilities=capabilities, context=context)
        if not aggregator_registered:
            raise HarnessValidationError("deterministic aggregator is unavailable", code="task_plan_aggregator_unavailable")
        result.require_valid()
        if plan_id is None:
            plan_id = canonical_payload_checksum({"candidate_checksum": candidate.candidate_checksum, "stage_id": candidate.stage_id})
        return ValidatedTaskPlan.from_candidate(
            candidate,
            plan_id=plan_id,
            version=1,
            parent_plan_id=None,
            source_candidate_ref=candidate.candidate_checksum,
            policy_ref=policy.exact_ref,
            policy_checksum=policy.policy_checksum,
            tasks=result.resolved_tasks,
            required_output_roles=policy.required_output_roles,
            limits=policy.limits,
            accepted_at=accepted_at,
        )

    @staticmethod
    def _validate_dag(by_id: Mapping[str, TaskSpec], max_depth: int, diagnostics: list[TaskPlanDiagnostic]) -> dict[str, int]:
        try:
            return task_dependency_depths(
                {task_id: task.depends_on for task_id, task in by_id.items()},
                max_depth=max_depth,
            )
        except HarnessValidationError as exc:
            diagnostic_code = {
                "task_plan_dependency_cycle": "dependency_cycle",
                "task_plan_no_executable_root": "no_executable_root",
                "task_plan_unreachable_task": "unreachable_task",
            }.get(exc.code or "", exc.code or "task_plan_dag_invalid")
            diagnostics.append(
                _diag(
                    diagnostic_code,
                    str(exc),
                    "dag",
                    task_id=(exc.details or {}).get("task_id"),
                    details=exc.details or {},
                )
            )
            return {}

    @staticmethod
    def _validate_task(task: TaskSpec, by_id: Mapping[str, TaskSpec], policy: TaskPlanPolicy, capabilities: TaskPlanCapabilityRegistry, context: TaskPlanValidationContext, diagnostics: list[TaskPlanDiagnostic]) -> None:
        if task.worker_capability not in policy.allowed_worker_capabilities:
            diagnostics.append(_diag("capability_not_allowed", "task capability is not allowed by policy", "policy", task_id=task.task_id))
        if not set(task.requested_tools).issubset(policy.allowed_tool_ids):
            diagnostics.append(_diag("tool_not_allowed", "task requests a tool outside policy", "policy", task_id=task.task_id))
        if not set(task.requested_memory_namespaces).issubset(policy.allowed_memory_namespaces):
            diagnostics.append(_diag("memory_not_allowed", "task requests memory outside policy", "policy", task_id=task.task_id))
        if task.output_contract.schema_ref not in policy.allowed_output_schema_refs:
            diagnostics.append(_diag("output_schema_not_allowed", "task output schema is outside policy", "outputs", task_id=task.task_id, details={"schema_ref": task.output_contract.schema_ref}))
        unknown_gates = set(task.acceptance_criteria.gate_refs) - set(policy.allowed_gate_refs)
        if unknown_gates:
            diagnostics.append(_diag("gate_not_allowed", "task requests a gate outside policy", "policy", task_id=task.task_id, details={"gates": sorted(unknown_gates)}))
        if not set(task.acceptance_criteria.gate_refs).issubset(context.registered_gate_refs):
            diagnostics.append(_diag("gate_unregistered", "task references an unregistered gate", "binding", task_id=task.task_id))
        for ref in task.input_refs:
            if ref in context.future_stage_input_refs:
                diagnostics.append(_diag("future_stage_reference", "task references a future stage input", "dataflow", task_id=task.task_id, details={"ref": ref}))
                continue
            try:
                producer = task_output_reference_producer(ref, tuple(by_id))
            except HarnessValidationError as exc:
                diagnostics.append(_diag(exc.code, str(exc), "dataflow", task_id=task.task_id, details={"ref": ref}))
                continue
            if producer is not None:
                if producer not in by_id:
                    diagnostics.append(_diag("task_plan_unknown_dependency", "task input references an unknown producer", "dataflow", task_id=task.task_id, details={"ref": ref, "producer_task_id": producer}))
                    continue
                if producer not in task.depends_on:
                    diagnostics.append(_diag("task_plan_task_input_dependency_missing", "task input reference must be declared as a dependency", "dataflow", task_id=task.task_id, details={"ref": ref, "producer_task_id": producer}))
                if by_id[producer].output_contract.output_role not in policy.shared_dependency_output_roles:
                    diagnostics.append(_diag("REF_UNAUTHORIZED", "dependency output role is not approved for sharing", "refs", task_id=task.task_id, details={"ref": ref}))
            elif ref not in policy.allowed_input_refs or ref not in context.available_input_refs:
                diagnostics.append(_diag("task_plan_input_reference_unavailable", "task input reference is not authorized and available in this stage", "dataflow", task_id=task.task_id, details={"ref": ref}))
        for ref in task.output_contract.metadata.values():
            if isinstance(ref, str) and ref in {"route", "quality_passed", "publish_artifact", "write_memory", "halt_graph"}:
                diagnostics.append(_diag("forbidden_control_field", "task output metadata contains a control field", "forbidden", task_id=task.task_id))

    def _validate_task_refs(
        self,
        task: TaskSpec,
        by_id: Mapping[str, TaskSpec],
        context: TaskPlanValidationContext,
        diagnostics: list[TaskPlanDiagnostic],
    ) -> None:
        if context.ref_authority is None or context.ref_policy is None:
            return
        for ref in task.input_refs:
            if task_reference_producer(ref, tuple(by_id)) is None:
                self._authorize_ref(
                    ref,
                    expected_kind=REF_KIND_INPUT,
                    policy=context.ref_policy,
                    context=context,
                    diagnostics=diagnostics,
                    task_id=task.task_id,
                    field="input_refs",
                )
        for namespace in task.requested_memory_namespaces:
            try:
                descriptor = context.ref_authority.authorize_memory_namespace_ref(
                    namespace,
                    context.ref_policy,
                    resolver=context.ref_resolution,
                    descriptors=context.ref_descriptors,
                )
                if descriptor.namespace != namespace:
                    raise HarnessValidationError("task memory namespace does not match its descriptor", code="REF_IDENTITY_CONFLICT")
            except HarnessValidationError as exc:
                diagnostics.append(
                    _diag(
                        exc.code or "REF_UNAUTHORIZED",
                        str(exc),
                        "refs",
                        task_id=task.task_id,
                        field="requested_memory_namespaces",
                        details=exc.details or {"namespace": namespace},
                    )
                )

    @staticmethod
    def _authorize_ref(
        ref: str,
        *,
        expected_kind: str,
        policy: RefAccessPolicy,
        context: TaskPlanValidationContext,
        diagnostics: list[TaskPlanDiagnostic],
        task_id: str | None = None,
        field: str,
    ) -> None:
        try:
            context.ref_authority.authorize_ref(
                ref,
                policy,
                resolver=context.ref_resolution,
                descriptors=context.ref_descriptors,
                expected_kind=expected_kind,
            )
        except HarnessValidationError as exc:
            diagnostics.append(
                _diag(
                    exc.code or "REF_UNAUTHORIZED",
                    str(exc),
                    "refs",
                    task_id=task_id,
                    field=field,
                    details=exc.details or {"ref": ref},
                )
            )

    @staticmethod
    def _validate_outputs(by_id: Mapping[str, TaskSpec], depths: Mapping[str, int], policy: TaskPlanPolicy, context: TaskPlanValidationContext, diagnostics: list[TaskPlanDiagnostic]) -> None:
        producers: dict[str, list[str]] = {}
        for task in by_id.values():
            producers.setdefault(task.output_contract.output_role, []).append(task.task_id)
        for role in policy.required_output_roles:
            if role not in producers:
                diagnostics.append(_diag("missing_required_output_role", "required output role has no producer", "outputs", details={"role": role}))
        for role, task_ids in producers.items():
            if role not in policy.allowed_output_roles:
                diagnostics.append(_diag("output_role_not_allowed", "task output role is not allowed", "outputs", details={"role": role}))
            if len(task_ids) > 1 and role not in policy.deterministic_aggregator_refs:
                diagnostics.append(_diag("output_role_conflict", "multiple tasks produce one role without an aggregator", "outputs", details={"role": role, "task_ids": sorted(task_ids)}))
            if len(task_ids) > 1 and policy.deterministic_aggregator_refs.get(role) not in context.registered_aggregator_refs:
                diagnostics.append(_diag("aggregator_unregistered", "declared deterministic aggregator is unavailable", "outputs", details={"role": role}))
        required_producers = {task_id for role in policy.required_output_roles for task_id in producers.get(role, ())}
        if required_producers:
            reverse: dict[str, set[str]] = {task_id: set() for task_id in by_id}
            for task in by_id.values():
                for dependency in task.depends_on:
                    reverse.setdefault(dependency, set()).add(task.task_id)
            reachable: set[str] = set()
            queue = deque(task_id for task_id, task in by_id.items() if not task.depends_on)
            while queue:
                task_id = queue.popleft()
                if task_id in reachable:
                    continue
                reachable.add(task_id)
                queue.extend(sorted(reverse.get(task_id, ())))
            for task_id in sorted(set(by_id) - reachable):
                diagnostics.append(_diag("unreachable_task", "task is not reachable from a stage root", "dag", task_id=task_id))
            for task_id in sorted(required_producers - reachable):
                diagnostics.append(_diag("unreachable_required_output", "required output producer is unreachable from a root", "dag", task_id=task_id))


def _diag(code: str, message: str, phase: str, *, task_id: str | None = None, field: str | None = None, details: Mapping[str, Any] | None = None) -> TaskPlanDiagnostic:
    return TaskPlanDiagnostic(code=code, message=message, phase=phase, task_id=task_id, field=field, details=details or {})


def _bounded(diagnostics: list[TaskPlanDiagnostic], limit: int) -> list[TaskPlanDiagnostic]:
    ordered: list[TaskPlanDiagnostic] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for diagnostic in sorted(diagnostics, key=lambda item: item.sort_key):
        key = (diagnostic.code, diagnostic.task_id, diagnostic.field)
        if key not in seen:
            ordered.append(diagnostic)
            seen.add(key)
    if len(ordered) > limit:
        ordered = ordered[:limit]
    return ordered


__all__ = ["TASK_PLAN_VALIDATOR_VERSION", "TaskPlanDiagnostic", "TaskPlanValidationContext", "TaskPlanValidationResult", "TaskPlanValidator"]
