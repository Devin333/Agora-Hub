from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.binding import TaskPlanCapabilityRegistry
from framework.harness.task_plan.canonical import reference, task_reference_producer
from framework.harness.task_plan.dag import task_dependency_depths
from framework.harness.task_plan.models import (
    PlanPatch,
    PlanPatchOperationType,
    ResolvedTaskSpec,
    TaskLifecycle,
    TaskPlanProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.policy import TaskPlanPolicy


class TaskPlanPatchValidator:
    """Validates and materialises bounded immutable plan patches."""

    def apply(
        self,
        plan: ValidatedTaskPlan,
        patch: PlanPatch,
        projection: TaskPlanProjection,
        policy: TaskPlanPolicy,
        capability_registry: TaskPlanCapabilityRegistry,
        *,
        accepted_at: str,
        available_input_refs: Iterable[str],
    ) -> ValidatedTaskPlan:
        if not isinstance(plan, ValidatedTaskPlan) or not isinstance(patch, PlanPatch) or not isinstance(projection, TaskPlanProjection):
            raise TypeError("plan, patch and projection must use TaskPlan contracts")
        self.require_policy_identity(plan, policy)
        if patch.base_plan_id != plan.plan_id or patch.base_plan_version != plan.version:
            raise HarnessValidationError("patch base plan is stale", code="task_plan_stale_patch")
        if not patch.matches_plan_identity(plan):
            raise HarnessValidationError("patch scope does not match plan", code="task_plan_patch_scope_mismatch")
        if not projection.matches_plan_identity(plan):
            raise HarnessValidationError("projection does not match patch base", code="task_plan_projection_mismatch")
        if plan.version - 1 >= policy.max_replans:
            raise HarnessValidationError(
                "TaskPlan replan budget is exhausted",
                code="task_plan_replan_exhausted",
                details={"max_replans": policy.max_replans},
            )
        definitions = {item.task_id: item for item in plan.tasks}
        states = {item.task_id: item for item in projection.tasks}
        next_definitions = dict(definitions)
        skipped: set[str] = set()
        replacements: dict[str, str] = {}
        targeted: set[str] = set()
        for operation in patch.operations:
            if operation.target_task_id in targeted:
                raise HarnessValidationError(
                    "a patch may modify each target task only once",
                    code="task_plan_patch_duplicate_target",
                    details={"task_id": operation.target_task_id},
                )
            if operation.target_task_id is not None:
                targeted.add(operation.target_task_id)
            if operation.operation is PlanPatchOperationType.ADD_REPLACEMENT_TASK:
                replacement = operation.replacement_task
                if replacement is None:
                    raise HarnessValidationError("replacement operation requires replacement_task", code="task_plan_patch_operation_not_allowed")
                if operation.target_task_id is None or operation.target_task_id not in definitions:
                    raise HarnessValidationError("replacement target is unknown", code="task_plan_patch_unknown_task")
                target_state = states[operation.target_task_id]
                if target_state.status not in {
                    TaskLifecycle.PENDING,
                    TaskLifecycle.READY,
                    TaskLifecycle.FAILED,
                }:
                    raise HarnessValidationError("replacement may only target an unstarted or failed task", code="task_plan_patch_task_not_pending")
                if replacement.task_id in next_definitions:
                    raise HarnessValidationError("replacement task id already exists", code="task_plan_duplicate_task_id")
                binding = capability_registry.resolve(replacement.worker_capability, policy)
                next_definitions[replacement.task_id] = binding.resolve_task(replacement, policy)
                replacements[operation.target_task_id] = replacement.task_id
            elif operation.operation is PlanPatchOperationType.SKIP_PENDING_TASK:
                target = operation.target_task_id
                if target is None or target not in next_definitions:
                    raise HarnessValidationError("skip target is unknown", code="task_plan_patch_unknown_task")
                state = states[target]
                if state.status not in {TaskLifecycle.PENDING, TaskLifecycle.READY, TaskLifecycle.FAILED}:
                    raise HarnessValidationError("only pending tasks may be skipped", code="task_plan_patch_task_not_pending")
                role = next_definitions[target].output_role
                if role in policy.required_output_roles:
                    raise HarnessValidationError("required output task cannot be skipped", code="task_plan_patch_required_role")
                skipped.add(target)
            elif operation.operation is PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY:
                target = operation.target_task_id
                if target is None or target not in next_definitions:
                    raise HarnessValidationError("dependency target is unknown", code="task_plan_patch_unknown_task")
                state = states[target]
                if state.status not in {TaskLifecycle.PENDING, TaskLifecycle.READY, TaskLifecycle.FAILED}:
                    raise HarnessValidationError("only not-started tasks may change dependencies", code="task_plan_patch_task_not_pending")
                old = next_definitions[target]
                next_definitions[target] = _with_dependencies(old, operation.depends_on)
            else:
                raise HarnessValidationError("unsupported PlanPatch operation", code="task_plan_patch_operation_not_allowed")
        if len(next_definitions) > policy.max_tasks:
            raise HarnessValidationError("patch exceeds max_tasks", code="task_plan_task_limit_exceeded")
        task_dependency_depths(
            {task_id: definition.depends_on for task_id, definition in next_definitions.items()},
            max_depth=policy.max_depth,
        )
        for target_task_id, replacement_task_id in replacements.items():
            target_definition = definitions[target_task_id]
            replacement = next_definitions[replacement_task_id]
            if replacement.output_role != target_definition.output_role:
                raise HarnessValidationError(
                    "replacement task must preserve the target output role",
                    code="task_plan_replacement_output_role_mismatch",
                    details={
                        "target_task_id": target_task_id,
                        "target_role": target_definition.output_role,
                        "replacement_task_id": replacement_task_id,
                        "replacement_role": replacement.output_role,
                    },
                )
            if target_task_id in replacement.depends_on:
                raise HarnessValidationError(
                    "replacement task cannot depend on the task it replaces",
                    code="task_plan_replacement_dependency_invalid",
                    details={"target_task_id": target_task_id, "replacement_task_id": replacement_task_id},
                )
        if replacements:
            rewired: dict[str, ResolvedTaskSpec] = {}
            for task_id, resolved in next_definitions.items():
                dependencies = tuple(
                    replacements.get(dependency, dependency)
                    for dependency in resolved.depends_on
                )
                input_refs = tuple(
                    _rewire_task_reference(input_ref, replacements)
                    for input_ref in resolved.task.input_refs
                )
                if dependencies != resolved.depends_on or input_refs != resolved.task.input_refs:
                    rewired[task_id] = _with_task_graph(
                        resolved,
                        depends_on=dependencies,
                        input_refs=input_refs,
                    )
            next_definitions.update(rewired)
            # Re-run the graph validator after rewriting references.  A
            # replacement can change edge direction even when the original
            # plan was acyclic, so the pre-rewire check is not sufficient.
            task_dependency_depths(
                {
                    task_id: definition.depends_on
                    for task_id, definition in next_definitions.items()
                },
                max_depth=policy.max_depth,
            )
        available = frozenset(
            reference(item, "available_input_refs") for item in available_input_refs
        )
        totals = {"max_turns": 0, "max_tool_calls": 0, "max_memory_ops": 0, "max_output_tokens": 0}
        producers: dict[str, list[str]] = {}
        for task_id, resolved in next_definitions.items():
            for name, value in resolved.normalized_budget.to_dict().items():
                totals[name] += value
            if task_id not in replacements and (
                states.get(task_id, None) is None
                or states[task_id].status is not TaskLifecycle.FAILED
            ):
                producers.setdefault(resolved.output_role, []).append(task_id)
            if resolved.task.output_contract.schema_ref not in policy.allowed_output_schema_refs:
                raise HarnessValidationError("patch output schema is outside policy", code="task_plan_output_schema_not_allowed")
            if resolved.output_role not in policy.allowed_output_roles:
                raise HarnessValidationError(
                    "patch output role is outside policy",
                    code="task_plan_role_not_allowed",
                    details={"task_id": task_id, "role": resolved.output_role},
                )
            for input_ref in resolved.task.input_refs:
                producer = task_reference_producer(input_ref, tuple(next_definitions))
                if producer is not None:
                    if producer not in next_definitions:
                        raise HarnessValidationError(
                            "patch input references an unknown task",
                            code="task_plan_unknown_dependency",
                            details={"task_id": task_id, "producer_task_id": producer},
                        )
                    if producer not in resolved.depends_on:
                        raise HarnessValidationError(
                            "patch input reference must be declared as a dependency",
                            code="task_plan_task_input_dependency_missing",
                            details={"task_id": task_id, "producer_task_id": producer},
                        )
                elif input_ref not in policy.allowed_input_refs or input_ref not in available:
                    raise HarnessValidationError(
                        "patch input reference is outside policy",
                        code="task_plan_input_reference_unavailable",
                        details={"task_id": task_id, "input_ref": input_ref},
                    )
        exceeded = [name for name, value in totals.items() if value > getattr(policy.aggregate_task_budget, name)]
        if exceeded:
            raise HarnessValidationError("patch exceeds aggregate budget", code="task_plan_budget_exceeded", details={"fields": exceeded})
        previous_totals = {name: 0 for name in totals}
        for resolved in plan.tasks:
            for name, value in resolved.normalized_budget.to_dict().items():
                previous_totals[name] += value
        incremental = {name: max(totals[name] - previous_totals[name], 0) for name in totals}
        from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

        reserved = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget).allocated_totals()
        incremental_exceeded = [
            name
            for name in totals
            if reserved[name] + incremental[name] > getattr(policy.aggregate_task_budget, name)
        ]
        if incremental_exceeded:
            raise HarnessValidationError(
                "patch exceeds remaining aggregate budget",
                code="task_plan_budget_exceeded",
                details={"fields": incremental_exceeded},
            )
        missing = sorted(set(policy.required_output_roles) - set(producers))
        if missing:
            raise HarnessValidationError("patch removes a required output role", code="task_plan_patch_required_role", details={"roles": missing})
        unauthorized = sorted(set(producers) - set(policy.allowed_output_roles))
        if unauthorized:
            raise HarnessValidationError(
                "patch creates an unauthorized output role",
                code="task_plan_role_not_allowed",
                details={"roles": unauthorized},
            )
        conflicts = sorted(role for role, task_ids in producers.items() if len(task_ids) > 1 and role not in policy.deterministic_aggregator_refs)
        if conflicts:
            raise HarnessValidationError("patch creates an output role conflict", code="task_plan_output_conflict", details={"roles": conflicts})
        if skipped:
            # Skipping is represented by the next projection/event; the plan
            # definition remains immutable and therefore replayable.
            pass
        return replace(
            plan,
            plan_id=f"{plan.plan_id}.v{plan.version + 1}",
            version=plan.version + 1,
            parent_plan_id=plan.plan_id,
            source_candidate_ref=patch.patch_checksum,
            policy_ref=plan.policy_ref,
            policy_checksum=plan.policy_checksum,
            tasks=tuple(next_definitions[key] for key in sorted(next_definitions)),
            required_output_roles=plan.required_output_roles,
            limits=plan.limits,
            accepted_at=accepted_at,
        )

    accept = apply

    @staticmethod
    def require_policy_identity(plan: ValidatedTaskPlan, policy: TaskPlanPolicy) -> None:
        if (
            plan.policy_ref != policy.exact_ref
            or plan.policy_checksum is None
            or plan.policy_checksum != policy.policy_checksum
        ):
            raise HarnessValidationError(
                "TaskPlan patch policy does not match the accepted Plan",
                code="task_plan_policy_mismatch",
                details={
                    "plan_policy_ref": plan.policy_ref,
                    "supplied_policy_ref": policy.exact_ref,
                    "plan_policy_checksum": plan.policy_checksum,
                    "supplied_policy_checksum": policy.policy_checksum,
                    "plan_version": plan.version,
                },
            )


def _with_dependencies(task: ResolvedTaskSpec, depends_on: tuple[str, ...]) -> ResolvedTaskSpec:
    return _with_task_graph(task, depends_on=depends_on)


def _with_task_graph(
    task: ResolvedTaskSpec,
    *,
    depends_on: tuple[str, ...] | None = None,
    input_refs: tuple[str, ...] | None = None,
) -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task=replace(
            task.task,
            depends_on=task.depends_on if depends_on is None else depends_on,
            input_refs=task.task.input_refs if input_refs is None else input_refs,
        ),
        worker_ref=task.worker_ref,
        worker_contract_ref=task.worker_contract_ref,
        allowed_tools=task.allowed_tools,
        allowed_memory_namespaces=task.allowed_memory_namespaces,
        gate_refs=task.gate_refs,
        normalized_budget=task.normalized_budget,
        normalized_retry_policy=task.normalized_retry_policy,
        subagent_id=task.subagent_id,
    )


def _rewire_task_reference(value: str, replacements: dict[str, str]) -> str:
    producer = task_reference_producer(value, tuple(replacements))
    if producer is None or producer not in replacements:
        return value
    replacement = replacements[producer]
    if value.startswith("task://"):
        prefix, remainder = "task://", value.removeprefix("task://")
        suffix = remainder[len(producer):]
        return f"{prefix}{replacement}{suffix}"
    if value.startswith("task:"):
        prefix, remainder = "task:", value.removeprefix("task:")
        suffix = remainder[len(producer):]
        return f"{prefix}{replacement}{suffix}"
    return replacement


__all__ = ["TaskPlanPatchValidator"]
