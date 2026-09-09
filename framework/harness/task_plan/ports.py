from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.policy import HarnessBudgetSnapshot
from framework.harness.ref_authority import (
    REF_KIND_INPUT,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefResolutionPort,
    normalize_ref_descriptors,
    validate_ref_configuration,
)
from framework.harness.task_plan.canonical import (
    checksum,
    exact_reference,
    frozen_mapping,
    identifier,
    reference,
    required_text,
    thaw_mapping,
)
from framework.harness.task_plan.identity import TaskPlanStageIdentity
from framework.harness.task_plan.models import PlanCandidate, ResolvedTaskSpec
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.task_plan.store import TaskResultRecord
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity

if TYPE_CHECKING:
    from framework.harness.task_plan.verification import (
        TaskPlanResultVerificationRequest,
    )


@dataclass(frozen=True, slots=True)
class PlanBuildRequest:
    run_id: str
    stage_binding: TaskPlanStageBinding
    context_refs: Mapping[str, str]
    policy: TaskPlanPolicy
    budget: HarnessBudgetSnapshot | Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    execution_identity: GraphExecutionIdentity | None = None
    ref_authority: RefAuthority | None = None
    ref_policy: RefAccessPolicy | None = None
    ref_resolution: RefResolutionPort | None = None
    ref_descriptors: Mapping[str, RefDescriptor] = field(default_factory=dict)
    stage_identity: TaskPlanStageIdentity = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        if not isinstance(self.stage_binding, TaskPlanStageBinding):
            raise TypeError("stage_binding must be TaskPlanStageBinding")
        object.__setattr__(
            self,
            "stage_identity",
            TaskPlanStageIdentity(self.run_id, self.stage_binding),
        )
        if not isinstance(self.policy, TaskPlanPolicy):
            raise TypeError("policy must be TaskPlanPolicy")
        _require_stage_policy_binding(
            self.policy,
            self.stage_binding,
            owner="plan builder",
        )
        if not isinstance(self.context_refs, Mapping):
            raise HarnessValidationError(
                "plan builder context_refs must be an object of logical names to references",
                code="task_plan_invalid_context_refs",
            )
        context_refs = {
            identifier(key, "context_refs.key"): reference(value, "context_refs.value")
            for key, value in self.context_refs.items()
        }
        denied = sorted(set(context_refs.values()) - set(self.policy.allowed_input_refs))
        if denied:
            raise HarnessValidationError(
                "plan builder context references exceed the pinned stage policy",
                code="task_plan_input_reference_unavailable",
                details={"refs": denied},
            )
        _authorize_context_references(
            context_refs.values(),
            run_id=self.run_id,
            stage_id=self.stage_id,
            authority=self.ref_authority,
            ref_policy=self.ref_policy,
            resolver=self.ref_resolution,
            descriptors=self.ref_descriptors,
        )
        object.__setattr__(self, "ref_descriptors", normalize_ref_descriptors(self.ref_descriptors))
        object.__setattr__(
            self,
            "context_refs",
            frozen_mapping(dict(sorted(context_refs.items())), "plan_build.context_refs"),
        )
        if self.budget is not None and not isinstance(self.budget, (HarnessBudgetSnapshot, Mapping)):
            raise HarnessValidationError(
                "plan builder budget must be a HarnessBudgetSnapshot or canonical mapping",
                code="task_plan_invalid_plan_build_budget",
            )
        if isinstance(self.budget, Mapping):
            object.__setattr__(self, "budget", frozen_mapping(self.budget, "plan_build.budget"))
        if self.execution_identity is not None:
            if not isinstance(self.execution_identity, GraphExecutionIdentity):
                raise TypeError("execution_identity must be GraphExecutionIdentity")
            mismatches = tuple(
                field_name
                for field_name, expected, actual in (
                    ("run_id", self.run_id, self.execution_identity.run_id),
                    ("graph_id", self.graph_id, self.execution_identity.graph_id),
                    (
                        "graph_version",
                        self.graph_version,
                        self.execution_identity.graph_version,
                    ),
                    ("graph_ref", self.graph_ref, self.execution_identity.graph_ref),
                    (
                        "graph_checksum",
                        self.graph_checksum,
                        self.execution_identity.graph_checksum,
                    ),
                    (
                        "node_id",
                        self.stage_binding.node_id,
                        self.execution_identity.node_id,
                    ),
                )
                if expected != actual
            )
            if mismatches:
                raise HarnessValidationError(
                    "plan builder execution identity is outside the frozen Graph stage",
                    code="task_plan_execution_identity_mismatch",
                    details={"mismatches": list(mismatches)},
                )
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "plan_build.metadata"))

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

    def to_dict(self) -> dict[str, Any]:
        budget = self.budget.to_dict() if hasattr(self.budget, "to_dict") else thaw_mapping(self.budget or {})
        payload = {
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "graph_checksum": self.graph_checksum,
            "stage_binding_ref": self.stage_binding.binding_checksum,
            "context_refs": thaw_mapping(self.context_refs),
            "policy_ref": self.policy.exact_ref,
            "budget": budget,
        }
        payload.update(
            {
                "stage_identity_schema": self.stage_identity.schema_version,
                "stage_identity_checksum": self.stage_identity.identity_checksum,
                "graph_schema_version": self.stage_identity.graph_schema_version,
                "compiler_version": self.stage_identity.compiler_version,
                "condition_policy_version": self.stage_identity.condition_policy_version,
                "graph_id": self.graph_id,
                "graph_version": self.graph_version,
                "graph_ref": self.graph_ref,
            }
        )
        if self.execution_identity is not None:
            payload["execution_identity"] = self.execution_identity.to_dict()
        return payload


@runtime_checkable
class PlanCandidateBuilderPort(Protocol):
    def build_candidate(self, request: PlanBuildRequest) -> PlanCandidate: ...


class HarnessPlanCandidateBuilder:
    """Adapter for a worker with an explicit bounded planning entrypoint."""

    def __init__(self, worker: Any) -> None:
        self.worker = worker

    def build_candidate(self, request: PlanBuildRequest) -> PlanCandidate:
        generate = getattr(self.worker, "generate_bounded", None)
        if not callable(generate):
            raise HarnessValidationError(
                "plan builder requires a bounded worker entrypoint",
                code="task_plan_bounded_builder_required",
            )
        result = generate(
            {
                "stage": request.to_dict(),
                "policy": _planner_policy_projection(request.policy),
            },
            timeout_seconds=float(request.policy.planning_timeout_seconds),
            max_turns=request.policy.max_plan_build_turns,
            max_tool_calls=request.policy.max_plan_build_tool_calls,
            max_transport_attempts=1,
        )
        if not isinstance(result, HarnessWorkerResult):
            raise HarnessValidationError(
                "plan builder must return HarnessWorkerResult",
                code="task_plan_invalid_builder_result",
            )
        if result.status.value != "succeeded":
            raise HarnessValidationError(
                "plan builder failed to produce a candidate",
                code="task_plan_builder_failed",
                details={"status": result.status.value},
            )
        if frozenset(result.output) != {"candidate"} or not isinstance(result.output["candidate"], Mapping):
            raise HarnessValidationError(
                "plan builder output must contain exactly one candidate object",
                code="task_plan_invalid_builder_result",
            )
        payload = result.output["candidate"]
        candidate = payload if isinstance(payload, PlanCandidate) else PlanCandidate.from_dict(payload)
        if not candidate.matches_stage_identity(request.stage_identity):
            raise HarnessValidationError("plan builder returned candidate outside the requested stage", code="task_plan_candidate_scope_mismatch")
        if not set(candidate.input_context_refs).issubset(request.context_refs.values()):
            raise HarnessValidationError("plan builder referenced context outside policy", code="task_plan_input_reference_unavailable")
        return candidate


class FakePlanCandidateBuilder:
    def __init__(self, candidate: PlanCandidate | Mapping[str, Any] | Exception) -> None:
        self.candidate = candidate
        self.calls: list[PlanBuildRequest] = []

    def build_candidate(self, request: PlanBuildRequest) -> PlanCandidate:
        self.calls.append(request)
        if isinstance(self.candidate, Exception):
            raise self.candidate
        return self.candidate if isinstance(self.candidate, PlanCandidate) else PlanCandidate.from_dict(self.candidate)


@dataclass(frozen=True, slots=True)
class TaskPlanStageRequest:
    run_id: str
    stage_binding: TaskPlanStageBinding
    context_refs: Mapping[str, str]
    policy: TaskPlanPolicy
    accepted_at: str
    budget: HarnessBudgetSnapshot | Mapping[str, Any] | None = None
    candidate: PlanCandidate | None = None
    execution_identity: GraphExecutionIdentity | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    policy_ref: str | None = None
    submission_identity: CandidateDedupIdentity | None = None
    source_candidate_checksum: str | None = None
    ref_authority: RefAuthority | None = None
    ref_policy: RefAccessPolicy | None = None
    ref_resolution: RefResolutionPort | None = None
    ref_descriptors: Mapping[str, RefDescriptor] = field(default_factory=dict)
    stage_identity: TaskPlanStageIdentity = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        if not isinstance(self.stage_binding, TaskPlanStageBinding):
            raise TypeError("stage_binding must be TaskPlanStageBinding")
        object.__setattr__(
            self,
            "stage_identity",
            TaskPlanStageIdentity(self.run_id, self.stage_binding),
        )
        if not isinstance(self.policy, TaskPlanPolicy):
            raise TypeError("policy must be TaskPlanPolicy")
        _require_stage_policy_binding(
            self.policy,
            self.stage_binding,
            owner="TaskPlan stage request",
        )
        if self.policy_ref is not None:
            object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))
            if self.policy_ref != self.stage_binding.policy_ref:
                raise HarnessValidationError(
                    "TaskPlan stage request policy ref is not pinned to supplied policy",
                    code="task_plan_policy_mismatch",
                )
        else:
            object.__setattr__(self, "policy_ref", self.stage_binding.policy_ref)
        context = self.context_refs
        if not isinstance(context, Mapping):
            raise HarnessValidationError(
                "TaskPlan stage context_refs must be an object",
                code="task_plan_invalid_context_refs",
            )
        normalized_context = {
            identifier(key, "context_refs.key"): reference(value, "context_refs.value")
            for key, value in context.items()
        }
        denied = sorted(set(normalized_context.values()) - set(self.policy.allowed_input_refs))
        if denied:
            raise HarnessValidationError(
                "TaskPlan stage context references exceed policy",
                code="task_plan_input_reference_unavailable",
                details={"refs": denied},
            )
        _authorize_context_references(
            normalized_context.values(),
            run_id=self.run_id,
            stage_id=self.stage_id,
            authority=self.ref_authority,
            ref_policy=self.ref_policy,
            resolver=self.ref_resolution,
            descriptors=self.ref_descriptors,
        )
        object.__setattr__(self, "ref_descriptors", normalize_ref_descriptors(self.ref_descriptors))
        object.__setattr__(
            self,
            "context_refs",
            frozen_mapping(dict(sorted(normalized_context.items())), "task_plan.context_refs"),
        )
        accepted_at = required_text(self.accepted_at, "accepted_at")
        from framework.shared.time import parse_datetime

        parsed = parse_datetime(accepted_at)
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HarnessValidationError(
                "accepted_at must be an RFC3339 timezone-aware timestamp",
                code="invalid_task_plan_timestamp",
            )
        object.__setattr__(self, "accepted_at", accepted_at)
        if self.budget is not None and not isinstance(self.budget, (HarnessBudgetSnapshot, Mapping)):
            raise HarnessValidationError(
                "TaskPlan stage budget must be a HarnessBudgetSnapshot or mapping",
                code="task_plan_invalid_plan_build_budget",
            )
        if isinstance(self.budget, Mapping):
            object.__setattr__(self, "budget", frozen_mapping(self.budget, "task_plan.budget"))
        if self.candidate is not None and not isinstance(self.candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        if (
            self.candidate is not None
            and not self.candidate.matches_stage_identity(self.stage_identity)
        ):
            raise HarnessValidationError(
                "TaskPlan candidate is outside the frozen Graph stage binding",
                code="task_plan_candidate_scope_mismatch",
            )
        if self.execution_identity is not None:
            if not isinstance(self.execution_identity, GraphExecutionIdentity):
                raise TypeError("execution_identity must be GraphExecutionIdentity")
            mismatches = tuple(
                field_name
                for field_name, expected, actual in (
                    ("run_id", self.run_id, self.execution_identity.run_id),
                    ("graph_id", self.graph_id, self.execution_identity.graph_id),
                    (
                        "graph_version",
                        self.graph_version,
                        self.execution_identity.graph_version,
                    ),
                    ("graph_ref", self.graph_ref, self.execution_identity.graph_ref),
                    (
                        "graph_checksum",
                        self.graph_checksum,
                        self.execution_identity.graph_checksum,
                    ),
                    (
                        "node_id",
                        self.stage_binding.node_id,
                        self.execution_identity.node_id,
                    ),
                )
                if expected != actual
            )
            if mismatches:
                raise HarnessValidationError(
                    "TaskPlan execution identity is outside the frozen Graph stage",
                    code="task_plan_execution_identity_mismatch",
                    details={"mismatches": list(mismatches)},
                )
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "task_plan.metadata"))
        if self.submission_identity is not None:
            if not isinstance(self.submission_identity, CandidateDedupIdentity):
                raise TypeError("submission_identity must be CandidateDedupIdentity")
            if (self.submission_identity.run_id, self.submission_identity.stage_id) != (
                self.run_id, self.stage_id
            ):
                raise HarnessValidationError(
                    "candidate submission is outside the frozen stage",
                    code="task_plan_submission_scope_mismatch",
                )
        if self.source_candidate_checksum is not None:
            if self.submission_identity is None:
                raise HarnessValidationError(
                    "source candidate checksum requires submission identity",
                    code="task_plan_submission_identity_required",
                )
            object.__setattr__(self, "source_candidate_checksum", checksum(
                self.source_candidate_checksum, "source_candidate_checksum"
            ))

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


@runtime_checkable
class TaskPlanStageRunnerPort(Protocol):
    def run(self, request: TaskPlanStageRequest) -> HarnessWorkerResult: ...


@runtime_checkable
class TaskPlanResultVerifierPort(Protocol):
    def verify(
        self,
        result: HarnessWorkerResult,
        *,
        task: ResolvedTaskSpec,
        request: TaskPlanResultVerificationRequest,
    ) -> TaskResultRecord: ...


def _planner_policy_projection(policy: TaskPlanPolicy) -> dict[str, Any]:
    return {
        "policy_ref": policy.exact_ref,
        "policy_checksum": policy.policy_checksum,
        "stage_id": policy.stage_id,
        "allowed_worker_capabilities": list(policy.allowed_worker_capabilities),
        "allowed_tool_ids": list(policy.allowed_tool_ids),
        "allowed_memory_namespaces": list(policy.allowed_memory_namespaces),
        "allowed_input_refs": list(policy.allowed_input_refs),
        "allowed_output_roles": list(policy.allowed_output_roles),
        "required_output_roles": list(policy.required_output_roles),
        "allowed_output_schema_refs": list(policy.allowed_output_schema_refs),
        "allowed_gate_refs": list(policy.allowed_gate_refs),
        "aggregated_output_roles": sorted(policy.deterministic_aggregator_refs),
        "limits": policy.limits.to_dict(),
    }


def _require_stage_policy_binding(
    policy: TaskPlanPolicy,
    stage_binding: TaskPlanStageBinding,
    *,
    owner: str,
) -> None:
    if (
        policy.stage_id != stage_binding.stage_id
        or policy.exact_ref != stage_binding.policy_ref
        or policy.required_output_roles != stage_binding.required_output_roles
    ):
        raise HarnessValidationError(
            f"{owner} policy is not pinned to the frozen Graph stage",
            code="task_plan_policy_mismatch",
            details={
                "stage_binding_ref": stage_binding.binding_checksum,
                "expected_policy_ref": stage_binding.policy_ref,
                "actual_policy_ref": policy.exact_ref,
                "expected_required_output_roles": list(
                    stage_binding.required_output_roles
                ),
                "actual_required_output_roles": list(policy.required_output_roles),
            },
        )


def _authorize_context_references(
    refs: Iterable[str],
    *,
    run_id: str,
    stage_id: str,
    authority: RefAuthority | None,
    ref_policy: RefAccessPolicy | None,
    resolver: RefResolutionPort | None,
    descriptors: Mapping[str, RefDescriptor],
) -> None:
    descriptors = normalize_ref_descriptors(descriptors)
    validate_ref_configuration(authority, ref_policy, resolver, descriptors)
    if authority is None:
        return
    authority.require_scope(ref_policy, run_id=run_id, stage_id=stage_id)
    for ref in refs:
        authority.authorize_ref(
            ref,
            ref_policy,
            resolver=resolver,
            descriptors=descriptors,
            expected_kind=REF_KIND_INPUT,
        )


__all__ = [
    "FakePlanCandidateBuilder",
    "HarnessPlanCandidateBuilder",
    "PlanBuildRequest",
    "PlanCandidateBuilderPort",
    "TaskPlanResultVerifierPort",
    "TaskPlanStageRequest",
    "TaskPlanStageRunnerPort",
]
