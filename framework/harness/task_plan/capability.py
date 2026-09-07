from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from framework.harness.context.models import (
    ContextEnvelope,
    ContextGraphIdentity,
    ContextTaskExecutionIdentity,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.policy import HarnessBudgetSnapshot
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import RefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, SnapshotRefResolutionPort
from framework.harness.subagents.context import SubAgentContextBuilder
from framework.harness.subagents.models import (
    SUBAGENT_INVOCATION_SCHEMA_V3,
    SubAgentInvocation,
    SubAgentResult,
    SubAgentSpec,
)
from framework.harness.subagents.runtime import SubAgentRuntime
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    exact_reference,
    identifier,
    required_text,
)
from framework.harness.task_plan.models import (
    ResolvedTaskSpec,
    TaskInstance,
    TaskRetryPolicy,
    TaskSpec,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.schema import TASK_CAPABILITY_BINDING_SCHEMA, TASK_PLAN_RUNTIME_VERSION
from framework.harness.task_plan.verification import (
    task_plan_subagent_attempt_identity,
)
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.activity import HarnessWorkerType
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True, slots=True)
class TaskCapabilityRegistration:
    capability: str
    worker_binding: HarnessWorkerBinding
    worker_contract_ref: str
    input_schema_ref: str
    output_schema_ref: str
    subagent_spec: SubAgentSpec | None = None
    active: bool = True
    runtime_version: str = TASK_PLAN_RUNTIME_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", identifier(self.capability, "capability"))
        if not isinstance(self.worker_binding, HarnessWorkerBinding):
            raise HarnessValidationError(
                "TaskPlan capability requires a pinned HarnessWorkerBinding",
                code="invalid_task_capability_binding",
            )
        object.__setattr__(
            self,
            "worker_contract_ref",
            exact_reference(self.worker_contract_ref, "worker_contract_ref"),
        )
        object.__setattr__(self, "input_schema_ref", exact_reference(self.input_schema_ref, "input_schema_ref"))
        object.__setattr__(self, "output_schema_ref", exact_reference(self.output_schema_ref, "output_schema_ref"))
        if not isinstance(self.active, bool):
            raise HarnessValidationError("capability active must be a boolean", code="invalid_task_capability_binding")
        object.__setattr__(self, "runtime_version", required_text(self.runtime_version, "runtime_version"))
        if self.worker_binding.worker_type is HarnessWorkerType.SUBAGENT:
            if not isinstance(self.subagent_spec, SubAgentSpec):
                raise HarnessValidationError(
                    "subagent capability requires a SubAgentSpec",
                    code="invalid_task_capability_binding",
                    details={"worker_ref": self.worker_ref},
                )
        elif self.subagent_spec is not None:
            raise HarnessValidationError(
                "non-subagent capability must not carry a SubAgentSpec",
                code="invalid_task_capability_binding",
                details={"worker_ref": self.worker_ref},
            )

    @property
    def worker_ref(self) -> str:
        return self.worker_binding.reference.exact_ref

    @property
    def binding_checksum(self) -> str:
        return canonical_payload_checksum(self.checksum_projection())

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_CAPABILITY_BINDING_SCHEMA,
            "runtime_version": self.runtime_version,
            "capability": self.capability,
            "worker_ref": self.worker_ref,
            "worker_type": self.worker_binding.worker_type.value,
            "worker_contract_ref": self.worker_contract_ref,
            "input_schema_ref": self.input_schema_ref,
            "output_schema_ref": self.output_schema_ref,
            "subagent_spec": self.subagent_spec.to_dict() if self.subagent_spec else None,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityBinding:
    registration: TaskCapabilityRegistration
    policy_ref: str
    allowed_tools: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    binding_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.registration, TaskCapabilityRegistration):
            raise TypeError("registration must be TaskCapabilityRegistration")
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
        object.__setattr__(self, "allowed_tools", tuple(sorted(set(self.allowed_tools))))
        object.__setattr__(
            self,
            "allowed_memory_namespaces",
            tuple(sorted(set(self.allowed_memory_namespaces))),
        )
        object.__setattr__(self, "binding_checksum", canonical_payload_checksum(self.checksum_projection()))

    @property
    def capability(self) -> str:
        return self.registration.capability

    @property
    def worker_ref(self) -> str:
        return self.registration.worker_ref

    @property
    def worker_contract_ref(self) -> str:
        return self.registration.worker_contract_ref

    @property
    def subagent_spec(self) -> SubAgentSpec | None:
        return self.registration.subagent_spec

    def checksum_projection(self) -> dict[str, Any]:
        return {
            **self.registration.checksum_projection(),
            "policy_ref": self.policy_ref,
            "allowed_tools": list(self.allowed_tools),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "binding_checksum": self.binding_checksum}

    def resolve_task(self, task: TaskSpec, policy: TaskPlanPolicy) -> ResolvedTaskSpec:
        if task.worker_capability != self.capability or policy.exact_ref != self.policy_ref:
            raise HarnessValidationError(
                "resolved capability does not match task or policy identity",
                code="task_capability_binding_mismatch",
                details={"task_id": task.task_id},
            )
        if task.output_contract.schema_ref != self.registration.output_schema_ref:
            raise HarnessValidationError(
                "task output schema is incompatible with its pinned worker contract",
                code="incompatible_task_capability_binding",
                details={"task_id": task.task_id, "worker_ref": self.worker_ref},
            )
        denied_tools = sorted(set(task.requested_tools) - set(self.allowed_tools))
        if denied_tools:
            raise HarnessValidationError(
                "task requests tools outside its resolved binding",
                code="task_tool_binding_not_allowed",
                details={"task_id": task.task_id, "denied": denied_tools},
            )
        denied_memory = sorted(
            set(task.requested_memory_namespaces) - set(self.allowed_memory_namespaces)
        )
        if denied_memory:
            raise HarnessValidationError(
                "task requests memory outside its resolved binding",
                code="task_memory_binding_not_allowed",
                details={"task_id": task.task_id, "denied": denied_memory},
            )
        denied_gates = sorted(set(task.acceptance_criteria.gate_refs) - set(policy.allowed_gate_refs))
        if denied_gates:
            raise HarnessValidationError(
                "task requests deterministic gates outside policy",
                code="task_gate_not_allowed",
                details={"task_id": task.task_id, "denied": denied_gates},
            )
        if task.retry_policy.max_attempts > policy.max_task_attempts:
            raise HarnessValidationError(
                "task retry request exceeds policy",
                code="task_retry_limit_exceeded",
                details={"task_id": task.task_id},
            )
        if task.budget_request.exceeds(policy.per_task_budget):
            raise HarnessValidationError(
                "task budget request exceeds policy",
                code="task_budget_exceeded",
                details={"task_id": task.task_id},
            )
        return ResolvedTaskSpec(
            task=task,
            worker_ref=self.worker_ref,
            worker_contract_ref=self.worker_contract_ref,
            subagent_id=self.subagent_spec.subagent_id if self.subagent_spec else None,
            allowed_tools=self.allowed_tools,
            allowed_memory_namespaces=self.allowed_memory_namespaces,
            gate_refs=task.acceptance_criteria.gate_refs,
            normalized_budget=task.budget_request,
            normalized_retry_policy=TaskRetryPolicy(
                max_attempts=task.retry_policy.max_attempts,
                retryable_reason_codes=task.retry_policy.retryable_reason_codes,
            ),
        )


class TaskCapabilityRegistry:
    def __init__(
        self,
        registrations: Iterable[TaskCapabilityRegistration] = (),
        *,
        runtime_version: str = TASK_PLAN_RUNTIME_VERSION,
    ) -> None:
        self.runtime_version = required_text(runtime_version, "runtime_version")
        self._by_capability: dict[str, list[TaskCapabilityRegistration]] = defaultdict(list)
        self._worker_refs: set[str] = set()
        for registration in registrations:
            self.register(registration)

    @property
    def registrations(self) -> tuple[TaskCapabilityRegistration, ...]:
        return tuple(
            sorted(
                (item for values in self._by_capability.values() for item in values),
                key=lambda item: (item.capability, item.worker_ref),
            )
        )

    def all(self) -> tuple[TaskCapabilityRegistration, ...]:
        """Stable read-only view used by inspection and test composition."""
        return self.registrations

    def register(self, registration: TaskCapabilityRegistration) -> None:
        if not isinstance(registration, TaskCapabilityRegistration):
            raise TypeError("registration must be TaskCapabilityRegistration")
        if registration.worker_ref in self._worker_refs:
            raise HarnessValidationError(
                "worker binding is already registered for a TaskPlan capability",
                code="duplicate_task_capability_binding",
                details={"worker_ref": registration.worker_ref},
            )
        self._by_capability[registration.capability].append(registration)
        self._worker_refs.add(registration.worker_ref)

    def resolve(self, capability: str, policy: TaskPlanPolicy) -> ResolvedCapabilityBinding:
        requested = identifier(capability, "capability")
        if requested not in policy.allowed_worker_capabilities:
            raise HarnessValidationError(
                "TaskPlan capability is not allowed by policy",
                code="task_capability_not_allowed",
                details={"capability": requested, "policy_ref": policy.exact_ref},
            )
        registrations = tuple(self._by_capability.get(requested, ()))
        pinned_ref = policy.pinned_capability_bindings.get(requested)
        if pinned_ref is not None:
            pinned = tuple(item for item in registrations if item.worker_ref == pinned_ref)
            if not pinned:
                raise HarnessValidationError(
                    "pinned TaskPlan capability binding is missing",
                    code="missing_task_capability_binding",
                    details={"capability": requested, "worker_ref": pinned_ref},
                )
            registration = pinned[0]
            if not registration.active:
                raise HarnessValidationError(
                    "pinned TaskPlan capability binding is stale",
                    code="stale_task_capability_binding",
                    details={"capability": requested, "worker_ref": pinned_ref},
                )
        else:
            active = tuple(item for item in registrations if item.active)
            if not active:
                raise HarnessValidationError(
                    "TaskPlan capability has no active binding",
                    code="missing_task_capability_binding",
                    details={"capability": requested},
                )
            if len(active) != 1:
                raise HarnessValidationError(
                    "TaskPlan capability binding is ambiguous",
                    code="ambiguous_task_capability_binding",
                    details={
                        "capability": requested,
                        "worker_refs": [item.worker_ref for item in sorted(active, key=lambda item: item.worker_ref)],
                    },
                )
            registration = active[0]
        if registration.runtime_version != self.runtime_version:
            raise HarnessValidationError(
                "TaskPlan capability binding runtime version is incompatible",
                code="incompatible_task_capability_binding",
                details={"capability": requested, "worker_ref": registration.worker_ref},
            )
        required_contract = policy.required_worker_contract_refs.get(requested)
        if required_contract is not None and required_contract != registration.worker_contract_ref:
            raise HarnessValidationError(
                "TaskPlan worker contract does not match pinned policy",
                code="incompatible_task_capability_binding",
                details={"capability": requested, "worker_ref": registration.worker_ref},
            )
        spec = registration.subagent_spec
        if spec is not None:
            if spec.subagent_id not in policy.allowed_subagent_ids:
                raise HarnessValidationError(
                    "resolved subagent is not allowed by TaskPlan policy",
                    code="task_subagent_not_allowed",
                    details={"capability": requested, "subagent_id": spec.subagent_id},
                )
            allowed_tools = tuple(sorted(set(policy.allowed_tool_ids).intersection(spec.allowed_tools)))
            allowed_memory = tuple(
                sorted(set(policy.allowed_memory_namespaces).intersection(spec.allowed_memory_namespaces))
            )
            if not allowed_tools or not allowed_memory:
                raise HarnessValidationError(
                    "TaskPlan policy and SubAgentSpec have no compatible tool or memory boundary",
                    code="incompatible_task_capability_binding",
                    details={"capability": requested, "worker_ref": registration.worker_ref},
                )
        else:
            allowed_tools = policy.allowed_tool_ids
            allowed_memory = policy.allowed_memory_namespaces
        return ResolvedCapabilityBinding(
            registration=registration,
            policy_ref=policy.exact_ref,
            allowed_tools=allowed_tools,
            allowed_memory_namespaces=allowed_memory,
        )


class ResolvedSubAgentTaskAdapter:
    """Invokes a resolved dynamic task through the existing SubAgent gate runtime."""

    def __init__(
        self,
        runtime: SubAgentRuntime,
        *,
        context_builder: SubAgentContextBuilder | None = None,
        ref_admission_service: "HarnessRefAdmissionService | None" = None,
    ) -> None:
        if not isinstance(runtime, SubAgentRuntime):
            raise TypeError("runtime must be SubAgentRuntime")
        self._runtime = runtime
        self._context_builder = context_builder or SubAgentContextBuilder()
        if ref_admission_service is not None and not isinstance(ref_admission_service, HarnessRefAdmissionService):
            raise TypeError("ref_admission_service must be HarnessRefAdmissionService")
        self._ref_admission_service = ref_admission_service

    def invoke(
        self,
        *,
        plan: ValidatedTaskPlan,
        resolved_task: ResolvedTaskSpec,
        binding: ResolvedCapabilityBinding,
        instance: TaskInstance,
        context_pack: ContextEnvelope,
        budget_snapshot: HarnessBudgetSnapshot,
        execution_identity: GraphExecutionIdentity,
    ) -> SubAgentResult:
        invocation = self.build_invocation(
            plan=plan,
            resolved_task=resolved_task,
            binding=binding,
            instance=instance,
            context_pack=context_pack,
            budget_snapshot=budget_snapshot,
            execution_identity=execution_identity,
        )
        return self._runtime.invoke(invocation)

    def recover(
        self,
        *,
        plan: ValidatedTaskPlan,
        resolved_task: ResolvedTaskSpec,
        binding: ResolvedCapabilityBinding,
        instance: TaskInstance,
        context_pack: ContextEnvelope,
        budget_snapshot: HarnessBudgetSnapshot,
        execution_identity: GraphExecutionIdentity,
    ) -> SubAgentResult | None:
        invocation = self.build_invocation(
            plan=plan,
            resolved_task=resolved_task,
            binding=binding,
            instance=instance,
            context_pack=context_pack,
            budget_snapshot=budget_snapshot,
            execution_identity=execution_identity,
        )
        return self._runtime.recover(invocation)

    def build_invocation(
        self,
        *,
        plan: ValidatedTaskPlan,
        resolved_task: ResolvedTaskSpec,
        binding: ResolvedCapabilityBinding,
        instance: TaskInstance,
        context_pack: ContextEnvelope,
        budget_snapshot: HarnessBudgetSnapshot,
        execution_identity: GraphExecutionIdentity,
    ) -> SubAgentInvocation:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if not isinstance(instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        if not isinstance(context_pack, ContextEnvelope):
            raise HarnessValidationError(
                "dynamic task context must be a ContextEnvelope",
                code="task_plan_result_identity_mismatch",
            )
        accepted_task = next(
            (item for item in plan.tasks if item.task_id == instance.task_id),
            None,
        )
        if accepted_task != resolved_task:
            raise HarnessValidationError(
                "dynamic task invocation is outside the accepted plan",
                code="task_plan_result_identity_mismatch",
            )
        if binding.worker_ref != resolved_task.worker_ref:
            raise HarnessValidationError(
                "dynamic task worker binding changed before dispatch",
                code="stale_task_capability_binding",
                details={"task_id": resolved_task.task_id},
            )
        graph_identity, task_identity = task_plan_context_identities(
            plan,
            instance,
            execution_identity=execution_identity,
        )
        if (
            context_pack.phase != "EXECUTE"
            or not context_pack.matches_graph_identity(graph_identity)
            or not context_pack.matches_task_execution_identity(task_identity)
        ):
            raise HarnessValidationError(
                "Graph dynamic task context does not match its accepted attempt",
                code="task_plan_result_identity_mismatch",
            )
        source_spec = binding.subagent_spec
        if source_spec is None:
            raise HarnessValidationError(
                "resolved dynamic task is not a subagent binding",
                code="incompatible_task_capability_binding",
                details={"task_id": resolved_task.task_id},
            )
        bounded_spec = SubAgentSpec(
            subagent_id=source_spec.subagent_id,
            role=source_spec.role,
            purpose=source_spec.purpose,
            input_schema=source_spec.input_schema,
            output_schema=source_spec.output_schema,
            allowed_tools=resolved_task.allowed_tools,
            allowed_memory_namespaces=resolved_task.allowed_memory_namespaces,
            context_policy=source_spec.context_policy,
            budget={
                "max_turns": resolved_task.normalized_budget.max_turns,
                "max_tool_calls": resolved_task.normalized_budget.max_tool_calls,
                "max_memory_ops": resolved_task.normalized_budget.max_memory_ops,
            },
            metadata={**source_spec.metadata, "task_binding_checksum": binding.binding_checksum},
        )
        child_run_id = (
            f"{plan.run_id}:{plan.stage_id}:{instance.task_instance_id}"
        )
        invocation_id = f"invocation://{child_run_id}"
        attempt_identity = task_plan_subagent_attempt_identity(
            plan,
            instance,
            invocation_id=invocation_id,
            child_run_id=child_run_id,
            subagent_id=bounded_spec.subagent_id,
            context_pack=context_pack,
        )
        ref_options = {}
        if self._ref_admission_service is not None:
            root = self._ref_admission_service.store.find(
                run_id=plan.run_id,
                binding_key=RefAuthoritySnapshot.admission_binding_key(
                    execution_identity, plan.stage_id, plan.stage_binding_checksum,
                ),
            )
            if root is None or root.task_policy_checksum != plan.policy_checksum:
                raise HarnessValidationError(
                    "child input admission grant is missing or outside the accepted policy",
                    code="REF_SNAPSHOT_MISSING",
                )
            child_grant = self._ref_admission_service.admit_child_inputs(
                root, attempt_identity=attempt_identity,
                input_refs=resolved_task.task.input_refs,
            )
            ref_options = {
                "ref_authority": RefAuthority(),
                "ref_policy": child_grant.policy,
                "ref_resolution": SnapshotRefResolutionPort(child_grant),
            }
        envelope = self._context_builder.build(
            parent_run_id=plan.run_id,
            child_run_id=child_run_id,
            spec=bounded_spec,
            context_pack=context_pack,
            input_refs=resolved_task.task.input_refs,
            memory_context_refs=(),
            budget_snapshot=budget_snapshot,
            **ref_options,
        )
        metadata = {
            "input_refs": list(resolved_task.task.input_refs),
            "task_id": resolved_task.task_id,
            "task_definition_checksum": resolved_task.task_definition_checksum,
        }
        metadata.update(
            {
                    "plan_id": plan.plan_id,
                    "plan_version": plan.version,
                    "plan_checksum": plan.plan_checksum,
                    "stage_identity_checksum": plan.stage_identity_checksum,
            }
        )
        invocation = SubAgentInvocation(
            invocation_id=invocation_id,
            parent_run_id=plan.run_id,
            child_run_id=child_run_id,
            stage_id=plan.stage_id,
            task_id=resolved_task.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            observed_at=plan.accepted_at,
            subagent_spec=bounded_spec,
            input_refs=resolved_task.task.input_refs,
            context_envelope=envelope,
            budget_snapshot=budget_snapshot,
            metadata=metadata,
            attempt_identity=attempt_identity,
            schema_version=SUBAGENT_INVOCATION_SCHEMA_V3,
        )
        return invocation


def task_plan_context_identities(
    plan: ValidatedTaskPlan,
    instance: TaskInstance,
    *,
    execution_identity: GraphExecutionIdentity,
) -> tuple[ContextGraphIdentity, ContextTaskExecutionIdentity]:
    """Project accepted Graph TaskPlan identity into the neutral context owner."""

    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    if not isinstance(instance, TaskInstance):
        raise TypeError("instance must be TaskInstance")
    if not isinstance(execution_identity, GraphExecutionIdentity):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    task = next((item for item in plan.tasks if item.task_id == instance.task_id), None)
    if (
        not plan.is_graph_only
        or not instance.is_graph_only
        or not instance.matches_plan_identity(plan)
        or task is None
        or task.task_definition_checksum != instance.task_definition_checksum
    ):
        raise HarnessValidationError(
            "Graph context identity requires an exact accepted TaskPlan attempt",
            code="task_plan_result_identity_mismatch",
        )
    if (
        execution_identity.run_id != plan.run_id
        or execution_identity.graph_id != plan.graph_id
        or execution_identity.graph_version != plan.graph_version
        or execution_identity.graph_ref != plan.graph_ref
        or execution_identity.graph_checksum != plan.graph_checksum
    ):
        raise HarnessValidationError(
            "Graph context execution identity is outside the accepted TaskPlan",
            code="task_plan_execution_identity_mismatch",
        )
    return (
        ContextGraphIdentity(
            run_id=plan.run_id,
            graph_id=plan.graph_id,
            graph_version=plan.graph_version,
            graph_ref=plan.graph_ref,
            graph_schema_version=plan.graph_schema_version,
            compiler_version=plan.compiler_version,
            condition_policy_version=plan.condition_policy_version,
            graph_checksum=plan.graph_checksum,
            stage_id=plan.stage_id,
            stage_binding_checksum=plan.stage_binding_checksum,
            stage_identity_schema=plan.stage_identity_schema,
            stage_identity_checksum=plan.stage_identity_checksum,
            node_id=execution_identity.node_id,
            node_instance_id=execution_identity.node_instance_id,
            activity_id=execution_identity.activity_id,
            activity_attempt=execution_identity.attempt,
        ),
        ContextTaskExecutionIdentity(
            plan_id=plan.plan_id,
            plan_version=plan.version,
            plan_checksum=plan.plan_checksum,
            task_id=instance.task_id,
            task_definition_checksum=instance.task_definition_checksum,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
        ),
    )


__all__ = [
    "ResolvedCapabilityBinding",
    "ResolvedSubAgentTaskAdapter",
    "TaskCapabilityRegistration",
    "TaskCapabilityRegistry",
    "task_plan_context_identities",
]
