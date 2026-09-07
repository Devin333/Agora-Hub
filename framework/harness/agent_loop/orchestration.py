from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from framework.agent.models import DelegateBatchCandidate, DelegateBatchProposal
from framework.agent.models.orchestration import (
    AGENT_ORCHESTRATION_REQUEST_SCHEMA,
    AGENT_ORCHESTRATION_RESULT_SCHEMA,
    PARENT_OBSERVATION_SCHEMA,
    AgentOrchestrationPort,
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    ParentObservation,
    ParentObservationLimits,
    ParentTaskSummary,
    ParentWaveSummary,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.capability import TaskCapabilityRegistry
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.models import (
    PlanBuildBudget,
    PlanCandidate,
    TaskAcceptanceCriteria,
    TaskOutputContract,
    TaskRetryPolicy,
    TaskSpec,
)
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.task_plan.planning_observation import (
    PlanningObservationReceipt,
    PlanningObservationRequest,
)
from framework.harness.task_plan.policy import TaskPlanPolicy, TaskPlanPolicyRegistry
from framework.harness.task_plan.ports import TaskPlanStageRequest
from framework.harness.task_plan.stage import TaskPlanStageRunner
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.harness.task_plan.store import TaskPlanStorePort
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.time import utc_now


@dataclass(frozen=True, slots=True)
class AgentOrchestrationTaskProfile:
    """Trusted task shape for one generic AgentLoop capability.

    A ``delegate_batch`` candidate may choose an objective, declared input refs,
    and dependency edges.  It must never choose the output schema, gates,
    retries, tool grants, memory grants, or worker implementation.
    """

    capability_hint: str
    output_role: str
    output_schema_ref: str
    gate_refs: tuple[str, ...]
    retry_policy: TaskRetryPolicy = TaskRetryPolicy()

    def __post_init__(self) -> None:
        # Reuse the executable TaskPlan contracts as the validation authority.
        TaskOutputContract(self.output_schema_ref, self.output_role)
        TaskAcceptanceCriteria(self.gate_refs)
        if not isinstance(self.retry_policy, TaskRetryPolicy):
            raise TypeError("retry_policy must be TaskRetryPolicy")
        if not isinstance(self.capability_hint, str) or not self.capability_hint.strip():
            raise ValueError("capability_hint must be a non-empty string")


class HarnessAgentOrchestrationRuntime:
    """Production AgentLoop-to-Harness delegation runtime.

    This is deliberately a concrete adapter over the existing TaskPlan and
    child-runtime boundaries, rather than a callback supplied by AgentLoop.
    It pins all executable authority at composition time and exposes only a
    bounded, security-projected join result back to the parent loop.
    """

    def __init__(
        self,
        *,
        stage_binding: TaskPlanStageBinding,
        policy_registry: TaskPlanPolicyRegistry,
        capability_registry: TaskCapabilityRegistry,
        store: TaskPlanStorePort,
        stage_runner: TaskPlanStageRunner,
        child_supervisor: ChildAgentSupervisor,
        task_profiles: tuple[AgentOrchestrationTaskProfile, ...],
        require_durable_store: bool = True,
    ) -> None:
        if not isinstance(stage_binding, TaskPlanStageBinding):
            raise TypeError("stage_binding must be TaskPlanStageBinding")
        if not isinstance(policy_registry, TaskPlanPolicyRegistry):
            raise TypeError("policy_registry must be TaskPlanPolicyRegistry")
        if not isinstance(capability_registry, TaskCapabilityRegistry):
            raise TypeError("capability_registry must be TaskCapabilityRegistry")
        if not isinstance(store, TaskPlanStorePort):
            raise TypeError("store must implement TaskPlanStorePort")
        if not isinstance(stage_runner, TaskPlanStageRunner):
            raise TypeError("stage_runner must be TaskPlanStageRunner")
        if not isinstance(child_supervisor, ChildAgentSupervisor):
            raise TypeError("child_supervisor must be ChildAgentSupervisor")
        if not isinstance(require_durable_store, bool):
            raise TypeError("require_durable_store must be boolean")
        profiles = tuple(task_profiles)
        if not profiles or not all(isinstance(item, AgentOrchestrationTaskProfile) for item in profiles):
            raise TypeError("task_profiles must contain AgentOrchestrationTaskProfile values")
        by_capability = {item.capability_hint: item for item in profiles}
        if len(by_capability) != len(profiles):
            raise ValueError("task_profiles must have unique capability_hint values")
        if require_durable_store and not isinstance(store, DurableTaskPlanStore):
            raise ValueError("production AgentLoop orchestration requires DurableTaskPlanStore")
        if stage_runner.store is not store:
            raise ValueError("AgentLoop orchestration runner must use the configured store")
        if stage_runner.capability_registry is not capability_registry:
            raise ValueError("AgentLoop orchestration runner must use the configured capability registry")
        coordinator = stage_runner.parallel_coordinator
        if not isinstance(coordinator, ParallelAgentCoordinator):
            raise ValueError("AgentLoop orchestration runner requires ParallelAgentCoordinator")
        if coordinator.child_supervisor is not child_supervisor:
            raise ValueError("AgentLoop orchestration coordinator must use the configured child supervisor")
        if not callable(stage_runner.worker_executor):
            raise ValueError("AgentLoop orchestration runner requires a worker executor")
        self._stage_binding = stage_binding
        self._policy_registry = policy_registry
        self._capability_registry = capability_registry
        self._store = store
        self._stage_runner = stage_runner
        self._child_supervisor = child_supervisor
        self._profiles = by_capability

    def observe_for_planning(
        self,
        request: PlanningObservationRequest,
    ) -> PlanningObservationReceipt:
        """Expose the Harness-owned observation ingress to a composed planner."""

        if not isinstance(request, PlanningObservationRequest):
            raise TypeError("request must be PlanningObservationRequest")
        policies = tuple(
            policy
            for policy in self._policy_registry.policies
            if policy.stage_id == request.stage_id
            and policy.policy_checksum == request.policy_checksum
        )
        if len(policies) != 1 or request.stage_id != self._stage_binding.stage_id:
            raise HarnessValidationError(
                "planning observation request is outside orchestration scope",
                code="planning_observation_request_scope_mismatch",
            )
        policy = policies[0]
        return self._stage_runner.observe_for_planning(
            TaskPlanStageRequest(
                run_id=request.run_id,
                stage_binding=self._stage_binding,
                context_refs={},
                policy=policy,
                policy_ref=policy.exact_ref,
                accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
                metadata={
                    "planner_turn_id": request.planner_turn_id,
                    "planning_correlation_id": request.correlation_id,
                },
            ),
            request,
        )

    def dispatch(self, request: AgentOrchestrationRequest) -> AgentOrchestrationResult:
        return self._dispatch_checked(request, recover=False)

    def recover_submission(self, request: AgentOrchestrationRequest) -> AgentOrchestrationResult:
        """Harness-only recovery ingress; never exposed by the Agent dispatch port."""
        return self._dispatch_checked(request, recover=True)

    def _dispatch_checked(self, request: AgentOrchestrationRequest, *, recover: bool) -> AgentOrchestrationResult:
        if not isinstance(request, AgentOrchestrationRequest):
            raise TypeError("request must be AgentOrchestrationRequest")
        try:
            return self._dispatch(request, recover=recover)
        except HarnessValidationError as exc:
            return _rejected_orchestration_result(request, exc.code or "agent_orchestration_rejected")
        except (TypeError, ValueError) as exc:
            return _rejected_orchestration_result(request, "agent_orchestration_contract_invalid")
        except Exception:
            return _rejected_orchestration_result(request, "agent_orchestration_runtime_failed")

    def _dispatch(self, request: AgentOrchestrationRequest, *, recover: bool = False) -> AgentOrchestrationResult:
        if request.run_id is None or request.execution_identity is None:
            raise HarnessValidationError(
                "production AgentLoop orchestration requires Graph execution identity",
                code="agent_orchestration_identity_required",
            )
        parent_identity = request.execution_identity
        self._require_parent_graph(parent_identity)
        policy = self._policy_registry.resolve(
            request.policy_ref,
            stage_id=self._stage_binding.stage_id,
        )
        self._require_policy(policy, request)
        submission_identity = CandidateDedupIdentity(
            run_id=request.run_id,
            stage_id=self._stage_binding.stage_id,
            parent_turn_id=request.parent_turn_id,
            action_correlation_id=request.candidate.correlation_id,
        )
        source_checksum = canonical_payload_checksum(request.candidate.to_dict())
        original = self._store.candidate_submission(submission_identity)
        if original is not None and original.candidate_checksum != source_checksum:
            raise HarnessValidationError(
                "candidate checksum conflicts with the original submission",
                code="CANDIDATE_IDEMPOTENCY_CONFLICT",
            )
        if recover and original is None:
            raise HarnessValidationError("recovery requires an existing submission", code="task_plan_submission_missing")
        candidate = None if original is not None else self._materialize_candidate(request, policy)
        created = False
        if original is None:
            admitted = self._store.submit_candidate(
                candidate, submission_identity,
                accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
                candidate_checksum=source_checksum, exclusive_stage=True,
            )
            original, created = admitted.submission, admitted.created
        stage_identity = self._task_plan_execution_identity(parent_identity, request.candidate)
        context_refs = _context_refs_for_candidate(request.candidate)
        stage_request = TaskPlanStageRequest(
            run_id=request.run_id,
            stage_binding=self._stage_binding,
            context_refs=context_refs,
            policy=policy,
            policy_ref=policy.exact_ref,
            accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
            candidate=candidate,
            submission_identity=submission_identity,
            source_candidate_checksum=source_checksum,
            execution_identity=stage_identity,
            metadata={
                "parent_agent_id": request.parent_agent_id,
                "delegate_batch_correlation_id": request.candidate.correlation_id,
                "parent_graph_checkpoint_ref": request.graph_checkpoint_ref,
            },
        )
        if not created and not recover:
            run_result = self._stage_runner.recorded_submission_result(stage_request)
            if run_result is None:
                return _rejected_orchestration_result(request, "task_plan_submission_resume_required")
        else:
            run_result = self._stage_runner.run(stage_request)
        if _reason_from_worker_result(run_result, "") in {
            "CANDIDATE_IDEMPOTENCY_CONFLICT",
            "task_plan_submission_binding_conflict",
            "task_plan_submission_scope_unavailable",
            "task_plan_submission_identity_required",
            "task_plan_candidate_conflict",
            "task_plan_submission_result_invalid",
        }:
            return _rejected_orchestration_result(
                request, _reason_from_worker_result(run_result, "")
            )
        return self._joined_result(request, run_result)

    def _require_parent_graph(self, identity: GraphExecutionIdentity) -> None:
        expected = (
            self._stage_binding.graph_id,
            self._stage_binding.graph_version,
            self._stage_binding.graph.identity_ref.exact_ref,
            self._stage_binding.graph_checksum,
        )
        actual = (
            identity.graph_id,
            identity.graph_version,
            identity.graph_ref,
            identity.graph_checksum,
        )
        if actual != expected:
            raise HarnessValidationError(
                "AgentLoop parent identity is outside the configured TaskPlan Graph",
                code="agent_orchestration_graph_identity_mismatch",
            )

    def _require_policy(self, policy: TaskPlanPolicy, request: AgentOrchestrationRequest) -> None:
        if policy.exact_ref != self._stage_binding.policy_ref:
            raise HarnessValidationError(
                "AgentLoop orchestration policy is outside the frozen stage binding",
                code="agent_orchestration_policy_mismatch",
            )
        if request.max_tasks_per_group > policy.max_tasks_per_group:
            raise HarnessValidationError(
                "AgentLoop task limit exceeds the pinned TaskPlan policy",
                code="agent_orchestration_task_limit_exceeded",
            )
        if (
            policy.capability_capacity is None
            or policy.available_concurrency_reservations is None
        ):
            raise HarnessValidationError(
                "AgentLoop orchestration policy does not declare bounded parallel capacity",
                code="agent_orchestration_parallel_policy_missing",
            )

    def _materialize_candidate(
        self,
        request: AgentOrchestrationRequest,
        policy: TaskPlanPolicy,
    ) -> PlanCandidate:
        candidate = request.candidate
        tasks = tuple(
            self._materialize_task(proposal, policy)
            for proposal in candidate.tasks
        )
        task_roles = {item.output_contract.output_role for item in tasks}
        if task_roles != set(policy.required_output_roles):
            raise HarnessValidationError(
                "delegate_batch output roles do not match the pinned policy",
                code="agent_orchestration_required_roles_mismatch",
            )
        task_identity = TaskPlanStageRequest(
            run_id=request.run_id or "missing-run-id",
            stage_binding=self._stage_binding,
            context_refs=_context_refs_for_candidate(candidate),
            policy=policy,
            policy_ref=policy.exact_ref,
            accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
            execution_identity=self._task_plan_execution_identity(
                request.execution_identity,
                candidate,
            ) if request.execution_identity is not None else None,
        ).stage_identity
        return PlanCandidate.for_stage(
            stage_identity=task_identity,
            candidate_id=f"agent-loop:{candidate.correlation_id}",
            input_context_refs=tuple(sorted({ref for task in tasks for ref in task.input_refs})),
            tasks=tasks,
            required_output_roles=policy.required_output_roles,
            generated_by="harness.agent-loop@1",
            requested_plan_budget=PlanBuildBudget(
                max_builder_calls=1,
                max_turns=1,
                max_tool_calls=0,
            ),
            requested_max_parallelism=candidate.parallelism_hint or 1,
            metadata={"delegate_batch_correlation_id": candidate.correlation_id},
        )

    def _materialize_task(
        self,
        proposal: DelegateBatchProposal,
        policy: TaskPlanPolicy,
    ) -> TaskSpec:
        profile = self._profiles.get(proposal.capability_hint)
        if profile is None:
            raise HarnessValidationError(
                "delegate_batch capability is not registered for orchestration",
                code="agent_orchestration_capability_unavailable",
            )
        if proposal.output_role != profile.output_role:
            raise HarnessValidationError(
                "delegate_batch output role differs from the trusted capability profile",
                code="agent_orchestration_output_role_mismatch",
            )
        if profile.capability_hint not in policy.allowed_worker_capabilities:
            raise HarnessValidationError(
                "delegate_batch capability is outside the pinned policy",
                code="agent_orchestration_capability_not_allowed",
            )
        self._capability_registry.resolve(profile.capability_hint, policy)
        return TaskSpec(
            task_id=proposal.logical_task_id,
            objective=proposal.objective,
            worker_capability=profile.capability_hint,
            input_refs=proposal.input_refs,
            output_contract=TaskOutputContract(profile.output_schema_ref, profile.output_role),
            acceptance_criteria=TaskAcceptanceCriteria(profile.gate_refs),
            depends_on=proposal.depends_on,
            budget_request=policy.per_task_budget,
            retry_policy=profile.retry_policy,
        )

    def _task_plan_execution_identity(
        self,
        parent: GraphExecutionIdentity | None,
        candidate: DelegateBatchCandidate,
    ) -> GraphExecutionIdentity:
        if parent is None:
            raise HarnessValidationError(
                "AgentLoop orchestration requires parent Graph identity",
                code="agent_orchestration_identity_required",
            )
        suffix = candidate.correlation_id
        return GraphExecutionIdentity(
            run_id=parent.run_id,
            graph_id=parent.graph_id,
            graph_version=parent.graph_version,
            graph_ref=parent.graph_ref,
            graph_checksum=parent.graph_checksum,
            node_id=self._stage_binding.node_id,
            node_instance_id=f"{parent.node_instance_id}:delegate:{suffix}",
            activity_id=f"{parent.activity_id}:delegate:{suffix}",
            attempt=parent.attempt,
        )

    def _joined_result(
        self,
        request: AgentOrchestrationRequest,
        run_result: Any,
    ) -> AgentOrchestrationResult:
        plan = self._store.plan(request.run_id or "", self._stage_binding.stage_id)
        if plan is None:
            reason = _reason_from_worker_result(run_result, "agent_orchestration_plan_unavailable")
            return _rejected_orchestration_result(request, reason)
        results = self._store.results_for(
            plan.run_id,
            plan.stage_id,
            plan.plan_id,
            plan.version,
        )
        events = self._store.read_events(plan.run_id, plan.stage_id)
        group, waves = _orchestration_group_projection(
            events,
            plan=plan,
        )
        status = _worker_result_status(run_result)
        succeeded = status == "succeeded" and group.get("state") == "SUCCEEDED"
        summaries = tuple(
            ParentTaskSummary(
                logical_task_id=item.task_id,
                status=item.status.value,
                summary=f"role={','.join(item.output_roles)}" if item.output_roles else None,
                result_ref=item.result_ref,
                result_checksum=item.result_checksum,
                output_roles=tuple(item.output_roles),
                terminal_reason=item.error_code,
            )
            for item in sorted(results, key=lambda item: item.task_id)
        )
        result_refs = tuple(
            item.result_ref
            for item in summaries
            if item.result_ref is not None and item.result_checksum is not None
        )
        output = getattr(run_result, "output", {})
        aggregate_ref = output.get("aggregate_ref") if isinstance(output, Mapping) else None
        aggregate_checksum = output.get("aggregate_checksum") if isinstance(output, Mapping) else None
        if not succeeded:
            aggregate_ref = None
            aggregate_checksum = None
        diagnostics = _joined_diagnostics(
            results=results,
            events=events,
            run_result=run_result,
            include_fallback=not succeeded,
        )
        telemetry = _joined_telemetry(events=events, plan=plan, group=group)
        covered_roles = tuple(
            sorted({role for item in results for role in getattr(item, "output_roles", ())})
        )
        terminal_reason = None if succeeded else _reason_from_worker_result(
            run_result,
            "agent_orchestration_group_not_succeeded",
        )
        observation = ParentObservation(
            group_id=str(group["group_id"]),
            group_status=str(group["state"]).casefold(),
            plan_version=str(plan.version),
            task_summaries=summaries,
            wave_summaries=waves,
            aggregate_ref=aggregate_ref,
            aggregate_checksum=aggregate_checksum,
            diagnostics=diagnostics,
            result_refs=result_refs,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            correlation_id=request.candidate.correlation_id,
            requested_parallelism=telemetry["requested_parallelism"],
            effective_parallelism=telemetry["effective_parallelism"],
            budget_usage=telemetry["budget_usage"],
            retry_count=telemetry["retry_count"],
            replan_count=telemetry["replan_count"],
            recovery_outcome=telemetry["recovery_outcome"],
            degraded_reason=telemetry["degraded_reason"],
            terminal_reason=terminal_reason,
            required_output_roles=tuple(getattr(plan, "required_output_roles", ())),
            covered_output_roles=covered_roles,
        )
        return AgentOrchestrationResult(
            status="succeeded" if succeeded else "partial_failure",
            observation=observation,
            reason_code=None if succeeded else _reason_from_worker_result(run_result, "agent_orchestration_group_not_succeeded"),
        )


AgentOrchestrationDispatch = Callable[
    [AgentOrchestrationRequest], AgentOrchestrationResult
]


class HarnessAgentOrchestrationPort:
    """Validate the AgentLoop boundary before delegating to Harness-owned dispatch.

    The injected dispatcher is the only component allowed to resolve workers,
    admission, group/wave lifecycle, and deterministic result gates.  This
    adapter deliberately owns none of those decisions; it protects the
    generic AgentLoop entrypoint from accepting a malformed joined result.
    """

    def __init__(self, *, dispatch: AgentOrchestrationDispatch) -> None:
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        self._dispatch = dispatch

    def dispatch(self, request: AgentOrchestrationRequest) -> AgentOrchestrationResult:
        if not isinstance(request, AgentOrchestrationRequest):
            raise TypeError("request must be AgentOrchestrationRequest")
        result = self._dispatch(request)
        if not isinstance(result, AgentOrchestrationResult):
            raise TypeError("Harness dispatcher must return AgentOrchestrationResult")
        _validate_harness_joined_result(request=request, result=result)
        return result


@dataclass(frozen=True, slots=True)
class AgentOrchestrationBinding:
    """Production composition state for the optional AgentLoop capability."""

    feature_enabled: bool
    port: AgentOrchestrationPort | None
    availability_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.feature_enabled, bool):
            raise TypeError("feature_enabled must be boolean")
        if self.port is not None and not isinstance(self.port, AgentOrchestrationPort):
            raise TypeError("port must implement AgentOrchestrationPort or be None")
        if self.availability_reason is not None and (
            not isinstance(self.availability_reason, str)
            or not self.availability_reason.strip()
        ):
            raise ValueError("availability_reason must be a canonical string or None")
        if self.port is not None and self.availability_reason is not None:
            raise ValueError("available orchestration binding cannot carry an availability reason")

    @property
    def available(self) -> bool:
        return self.feature_enabled and self.port is not None

    @classmethod
    def from_dispatch(
        cls,
        *,
        feature_enabled: bool,
        dispatch: AgentOrchestrationDispatch | None,
    ) -> "AgentOrchestrationBinding":
        if not isinstance(feature_enabled, bool):
            raise TypeError("feature_enabled must be boolean")
        if dispatch is None:
            return cls(
                feature_enabled=feature_enabled,
                port=None,
                availability_reason=(
                    "agent_orchestration_unavailable" if feature_enabled else "feature_disabled"
                ),
            )
        return cls(
            feature_enabled=feature_enabled,
            port=HarnessAgentOrchestrationPort(dispatch=dispatch),
        )


def _context_refs_for_candidate(candidate: DelegateBatchCandidate) -> dict[str, str]:
    refs = tuple(
        sorted(
            {
                ref
                for proposal in candidate.tasks
                for ref in proposal.input_refs
                if not ref.startswith("task://") and not ref.startswith("task:")
            }
        )
    )
    return {
        f"input_{index}": ref
        for index, ref in enumerate(refs, start=1)
    }


def _rejected_orchestration_result(
    request: AgentOrchestrationRequest,
    reason_code: str,
) -> AgentOrchestrationResult:
    return AgentOrchestrationResult(
        status="rejected",
        reason_code=reason_code,
        observation=ParentObservation(
            group_id=f"rejected:{request.candidate.correlation_id}",
            group_status="rejected",
            plan_version="0",
            diagnostics=(reason_code,),
        ),
    )


def _worker_result_status(value: Any) -> str:
    status = getattr(value, "status", None)
    if hasattr(status, "value"):
        status = status.value
    return str(status).casefold() if status is not None else "failed"


def _reason_from_worker_result(value: Any, fallback: str) -> str:
    diagnostics = getattr(value, "diagnostics", {})
    if isinstance(diagnostics, Mapping):
        reason = diagnostics.get("reason_code")
        if isinstance(reason, str) and reason:
            return reason
    return fallback


def _joined_diagnostics(
    *,
    results: tuple[Any, ...],
    events: tuple[Any, ...],
    run_result: Any,
    include_fallback: bool,
) -> tuple[str, ...]:
    """Project only typed gate/retry/recovery facts from durable Harness state."""

    diagnostics: set[str] = set()
    for result in results:
        task_id = getattr(result, "task_id", None)
        if not isinstance(task_id, str) or not task_id:
            continue
        error_code = getattr(result, "error_code", None)
        if isinstance(error_code, str) and error_code:
            diagnostics.add(f"task:{task_id}:failed:{error_code}")
        for gate_ref in getattr(result, "verified_gate_refs", ()):
            if isinstance(gate_ref, str) and gate_ref:
                diagnostics.add(f"task:{task_id}:gate_verified:{gate_ref}")
    lifecycle_events = {
        "TASK_RETRY_SCHEDULED",
        "TASK_GROUP_RECOVERY",
        "TASK_GROUP_REPLAN_PENDING",
        "TASK_GROUP_INDETERMINATE",
        "TASK_GROUP_CANCELLED",
        "TASK_GROUP_HALTED",
    }
    for event in events:
        event_type = getattr(event, "event_type", None)
        if event_type not in lifecycle_events:
            continue
        reason_code = getattr(event, "reason_code", None)
        if isinstance(reason_code, str) and reason_code:
            diagnostics.add(f"{event_type}:{reason_code}")
        else:
            diagnostics.add(str(event_type))
    if include_fallback:
        diagnostics.add(
            _reason_from_worker_result(
                run_result,
                "agent_orchestration_group_not_succeeded",
            )
        )
    return tuple(sorted(diagnostics))


def _joined_telemetry(
    *,
    events: tuple[Any, ...],
    plan: Any,
    group: Mapping[str, Any],
) -> dict[str, Any]:
    """Collect bounded, non-sensitive facts from durable group events."""

    requested = 0
    effective = 0
    budget_usage: dict[str, Any] = {}
    retry_count = 0
    replan_count = 0
    recovery_outcome = None
    degraded_reason = None
    for event in events:
        if getattr(event, "plan_id", None) != plan.plan_id or getattr(event, "plan_version", None) != plan.version:
            continue
        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            payload = {}
        if event_type == "TASK_GROUP_ADMITTED":
            requested = payload.get("requested_parallelism", requested)
            effective = payload.get("effective_parallelism", effective)
        elif event_type == "TASK_RETRY_SCHEDULED":
            retry_count += 1
        elif event_type in {"TASK_GROUP_REPLAN_PENDING", "TASK_GROUP_REPLANNED"}:
            replan_count += 1
        elif event_type == "TASK_GROUP_RECOVERY":
            recovery_outcome = payload.get("outcome") or payload.get("reason_code") or "recovered"
        elif event_type == "DEGRADED_SERIAL":
            degraded_reason = payload.get("reason_code") or payload.get("reason") or "serial_fallback"
        usage = payload.get("budget_usage")
        if isinstance(usage, Mapping):
            budget_usage.update(dict(usage))
    if not requested:
        requested = group.get("max_parallelism", 0)
    if not effective:
        effective = requested
    return {
        "requested_parallelism": requested,
        "effective_parallelism": effective,
        "budget_usage": budget_usage,
        "retry_count": retry_count,
        "replan_count": replan_count,
        "recovery_outcome": recovery_outcome,
        "degraded_reason": degraded_reason,
    }


def _orchestration_group_projection(
    events: tuple[Any, ...],
    *,
    plan: Any,
) -> tuple[Mapping[str, Any], tuple[ParentWaveSummary, ...]]:
    group: Mapping[str, Any] | None = None
    waves: dict[str, ParentWaveSummary] = {}
    for event in events:
        if getattr(event, "plan_id", None) != plan.plan_id or getattr(event, "plan_version", None) != plan.version:
            continue
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        snapshot = payload.get("group")
        if isinstance(snapshot, Mapping):
            if (
                snapshot.get("run_id") != plan.run_id
                or snapshot.get("stage_id") != plan.stage_id
                or snapshot.get("plan_id") != plan.plan_id
                or snapshot.get("plan_version") != plan.version
            ):
                raise HarnessValidationError(
                    "AgentLoop orchestration group identity does not match its accepted plan",
                    code="agent_orchestration_group_identity_mismatch",
                )
            group = snapshot
        wave = payload.get("wave")
        if isinstance(wave, Mapping):
            wave_id = wave.get("wave_id")
            ordinal = wave.get("ordinal")
            state = wave.get("state")
            if (
                wave.get("group_id") != (group or {}).get("group_id")
                or not isinstance(wave_id, str)
                or isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or not isinstance(state, str)
            ):
                raise HarnessValidationError(
                    "AgentLoop orchestration wave identity is invalid",
                    code="agent_orchestration_wave_identity_mismatch",
                )
            waves[wave_id] = ParentWaveSummary(
                wave_id=wave_id,
                ordinal=ordinal,
                status=state.casefold(),
                task_ids=tuple(wave.get("task_ids", ())),
                effective_parallelism=int(wave.get("effective_parallelism", 0) or 0),
                degraded_reason=wave.get("degraded_reason"),
            )
        if getattr(event, "event_type", None) == "TASK_WAVE_COMPLETED":
            wave_id = payload.get("wave_id")
            prior = waves.get(wave_id) if isinstance(wave_id, str) else None
            if prior is not None:
                waves[wave_id] = ParentWaveSummary(
                    wave_id=prior.wave_id,
                    ordinal=prior.ordinal,
                    status="terminal",
                    task_ids=prior.task_ids,
                    effective_parallelism=prior.effective_parallelism,
                    degraded_reason=prior.degraded_reason,
                )
    if group is None or not isinstance(group.get("group_id"), str) or not isinstance(group.get("state"), str):
        raise HarnessValidationError(
            "AgentLoop orchestration has no durable dispatch group",
            code="agent_orchestration_group_missing",
        )
    return group, tuple(sorted(waves.values(), key=lambda item: item.ordinal))


def _validate_harness_joined_result(
    *,
    request: AgentOrchestrationRequest,
    result: AgentOrchestrationResult,
) -> None:
    """Reject a dispatcher result that cannot belong to the submitted group."""

    task_ids = {item.logical_task_id for item in request.candidate.tasks}
    summaries = result.observation.task_summaries
    summary_ids = [item.logical_task_id for item in summaries]
    unexpected = sorted(set(summary_ids) - task_ids)
    if unexpected:
        raise ValueError(
            "Harness joined observation contains tasks outside the submitted candidate: "
            f"{unexpected}"
        )
    if len(summary_ids) != len(set(summary_ids)):
        raise ValueError("Harness joined observation contains duplicate logical task ids")
    # This is validation only. AgentLoop receives the separately projected view.
    result.observation.project(request.parent_observation_limits)


__all__ = [
    "AGENT_ORCHESTRATION_REQUEST_SCHEMA",
    "AGENT_ORCHESTRATION_RESULT_SCHEMA",
    "PARENT_OBSERVATION_SCHEMA",
    "AgentOrchestrationPort",
    "AgentOrchestrationDispatch",
    "AgentOrchestrationBinding",
    "AgentOrchestrationRequest",
    "AgentOrchestrationResult",
    "AgentOrchestrationTaskProfile",
    "HarnessAgentOrchestrationPort",
    "HarnessAgentOrchestrationRuntime",
    "ParentObservation",
    "ParentObservationLimits",
    "ParentTaskSummary",
    "ParentWaveSummary",
]
