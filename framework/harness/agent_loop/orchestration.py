from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping

from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.agent.models import AgentSpec, DelegateBatchCandidate, DelegateBatchProposal
from framework.agent.models.orchestration import (
    AGENT_ORCHESTRATION_REQUEST_SCHEMA,
    AGENT_ORCHESTRATION_RESULT_SCHEMA,
    PARENT_OBSERVATION_REJECTED_SCHEMA,
    PARENT_OBSERVATION_SCHEMA,
    AgentOrchestrationPort,
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    AgentSubmissionReceipt,
    ParentObservation,
    ParentObservationLimits,
    ParentTaskSummary,
    ParentWaveSummary,
)
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.agent_loop.child_executor import HarnessSubAgentTaskExecutor
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import RefAuthority
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot,
    RefSnapshotPhase,
    SnapshotRefResolutionPort,
)
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.capability import TaskCapabilityRegistry
from framework.harness.task_plan.continuation import (
    PARENT_CONTINUATION_EVENT,
    ParentContinuation,
    continuation_from_event,
)
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
from framework.harness.task_plan.store import TaskPlanEvent, TaskPlanStorePort
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.task_plan.canonical import canonical_payload_checksum, task_reference_producer
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
        ref_admission_service: HarnessRefAdmissionService | None = None,
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
        if ref_admission_service is not None and not isinstance(
            ref_admission_service, HarnessRefAdmissionService
        ):
            raise TypeError("ref_admission_service must be HarnessRefAdmissionService")
        if require_durable_store and (
            ref_admission_service is None
            or getattr(ref_admission_service.store, "is_durable", False) is not True
            or not stage_binding.is_agent_delegation
        ):
            raise ValueError(
                "production orchestration requires a declared AgentLoop stage and durable input admission"
            )
        self._ref_admission_service = ref_admission_service
        if require_durable_store:
            self.require_production_child_authority()

    def require_production_child_authority(self) -> None:
        """Validate the child dependency owners, including manual compositions."""
        executor = self._stage_runner.worker_executor
        if not isinstance(executor, HarnessSubAgentTaskExecutor):
            raise TypeError("worker_executor must be HarnessSubAgentTaskExecutor")
        if self._stage_runner.worker_result_recovery != executor.recover:
            raise ValueError("child execution and recovery must use the same executor")
        policy = self._policy_registry.resolve(self._stage_binding.policy_ref, stage_id=self._stage_binding.stage_id)
        executor.require_production_bindings(
            store=self._store, admission=self._ref_admission_service,
            task_policy=policy,
            verifier=self._stage_runner.result_verifier,
            bindings=tuple(self._capability_registry.resolve(profile.capability_hint, policy) for profile in self._profiles.values()),
            gate_refs=tuple(ref for profile in self._profiles.values() for ref in profile.gate_refs),
        )

    @property
    def has_durable_input_admission(self) -> bool:
        return (
            self._stage_binding.is_agent_delegation
            and self._ref_admission_service is not None
            and getattr(self._ref_admission_service.store, "is_durable", False) is True
        )

    @property
    def composition_state(self) -> AgentOrchestrationCompositionState:
        """Return the rollout state implied by the bound coordinator transport."""

        coordinator = self._stage_runner.parallel_coordinator
        # A serial adapter may be retained as an explicit fallback while the
        # production child supervisor remains bound.  In that configuration
        # the live transport is still parallel; serial degradation applies
        # only when the supervisor is absent and the adapter owns dispatch.
        if coordinator.child_supervisor is not None:
            return AgentOrchestrationCompositionState.ENABLED_PARALLEL
        if coordinator.serial_executor is not None:
            return AgentOrchestrationCompositionState.DEGRADED_SERIAL
        return AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE

    def admit_parent_inputs(
        self, task: Mapping[str, Any], *, agent: AgentSpec,
    ) -> RefAuthoritySnapshot:
        """Harness worker ingress; the Agent dispatch port cannot issue grants."""
        if self._ref_admission_service is None or not self._stage_binding.is_agent_delegation:
            raise HarnessValidationError("parent input admission is unavailable", code="REF_SNAPSHOT_MISSING")
        declared = agent.metadata.get("agent_orchestration")
        if not isinstance(declared, Mapping) or declared.get("policy_ref") != self._stage_binding.policy_ref:
            raise HarnessValidationError("parent Agent policy differs from its Graph binding", code="REF_POLICY_SCOPE_MISMATCH")
        policy = self._policy_registry.resolve(
            self._stage_binding.policy_ref, stage_id=self._stage_binding.stage_id,
        )
        snapshot = self._ref_admission_service.admit_graph_inputs(
            task, stage_binding=self._stage_binding, task_policy=policy,
        )
        # Durable child lifecycle events are tenant-bound and fail closed when
        # a parent run has not been registered. Register the exact admitted
        # run/tenant pair before TaskPlan execution can reach child spawn.
        self._child_supervisor.register_run_scope(
            snapshot.execution_identity.run_id,
            snapshot.policy.tenant_id,
        )
        if not isinstance(self._store, DurableTaskPlanStore):
            raise HarnessValidationError(
                "parent execution context requires durable TaskPlan storage",
                code="task_plan_parent_execution_context_store_unavailable",
            )
        raw_context = task.get(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
        if not isinstance(raw_context, Mapping):
            raise HarnessValidationError(
                "parent execution context is missing after input admission",
                code="task_plan_parent_execution_context_missing",
            )
        context = HarnessGraphActivityTaskContext.from_dict(raw_context)
        self._store.register_parent_execution_context(
            context,
            execution_identity=snapshot.execution_identity,
            stage_id=snapshot.stage_id,
            stage_binding_checksum=snapshot.stage_binding_checksum,
        )
        return snapshot

    def parent_memory_recall(self, snapshot: RefAuthoritySnapshot) -> ExecutionMemoryRecallPort:
        if self._ref_admission_service is None:
            raise HarnessValidationError("parent memory admission is unavailable", code="REF_SNAPSHOT_MISSING")
        return self._ref_admission_service.memory_recall(
            snapshot, execution_identity=snapshot.execution_identity,
        )

    def _input_ref_options(
        self, identity: GraphExecutionIdentity | None, policy: TaskPlanPolicy,
    ) -> dict[str, Any]:
        if self._ref_admission_service is None:
            return {}
        if identity is None:
            raise HarnessValidationError("input authority requires Graph execution identity", code="REF_SNAPSHOT_BINDING_MISMATCH")
        self._require_parent_graph(identity)
        snapshot = self._ref_admission_service.store.find(
            run_id=identity.run_id,
            binding_key=RefAuthoritySnapshot.admission_binding_key(
                identity, self._stage_binding.stage_id, self._stage_binding.binding_checksum,
            ),
        )
        if snapshot is None:
            raise HarnessValidationError("parent input grant is missing", code="REF_SNAPSHOT_MISSING")
        if (
            snapshot.phase is not RefSnapshotPhase.INPUT_ADMISSION
            or snapshot.execution_identity != identity
            or snapshot.stage_binding_checksum != self._stage_binding.binding_checksum
            or snapshot.task_policy_checksum != policy.policy_checksum
        ):
            raise HarnessValidationError("parent input grant differs from the execution", code="REF_SNAPSHOT_BINDING_MISMATCH")
        return {
            "ref_authority": RefAuthority(),
            "ref_policy": snapshot.policy,
            "ref_resolution": SnapshotRefResolutionPort(snapshot),
        }

    def observe_for_planning(
        self,
        request: PlanningObservationRequest,
        *,
        execution_identity: GraphExecutionIdentity | None = None,
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
                execution_identity=execution_identity,
                **self._input_ref_options(execution_identity, policy),
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
        ref_options = self._input_ref_options(parent_identity, policy)
        context_refs = _context_refs_for_candidate(request.candidate)
        submission_identity = CandidateDedupIdentity(
            run_id=request.run_id,
            stage_id=self._stage_binding.stage_id,
            parent_turn_id=request.parent_turn_id,
            action_correlation_id=request.candidate.correlation_id,
        )
        source_checksum = canonical_payload_checksum(request.candidate.to_dict())
        original = self._store.candidate_submission(submission_identity)
        if original is not None and original.candidate_checksum != source_checksum:
            return _rejected_orchestration_result(
                request,
                "CANDIDATE_IDEMPOTENCY_CONFLICT",
                submission_receipt=self._submission_receipt(
                    request,
                    original,
                    dedup_status="conflict",
                    wait_status="rejected",
                ),
            )
        if recover and original is None:
            raise HarnessValidationError("recovery requires an existing submission", code="task_plan_submission_missing")
        # A conflicting payload retains its original dedup diagnostic. All
        # other external inputs are authorized before any submission write.
        TaskPlanStageRequest(
            run_id=request.run_id, stage_binding=self._stage_binding,
            context_refs=context_refs, policy=policy,
            accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
            execution_identity=parent_identity, **ref_options,
        )
        candidate = None if original is not None else self._materialize_candidate(request, policy)
        created = False
        if original is None:
            admitted = self._store.submit_candidate(
                candidate, submission_identity,
                accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
                candidate_checksum=source_checksum, exclusive_stage=True,
            )
            original, created = admitted.submission, admitted.created
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
            execution_identity=parent_identity,
            **ref_options,
            metadata={
                "parent_agent_id": request.parent_agent_id,
                "delegate_batch_correlation_id": request.candidate.correlation_id,
                "parent_graph_checkpoint_ref": request.graph_checkpoint_ref,
            },
        )
        if not created and not recover:
            run_result = self._stage_runner.recorded_submission_result(stage_request)
            if run_result is None:
                return _rejected_orchestration_result(
                    request,
                    "task_plan_submission_resume_required",
                    submission_receipt=self._submission_receipt(
                        request,
                        original,
                        dedup_status="accepted",
                        wait_status="pending",
                    ),
                )
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
                request,
                _reason_from_worker_result(run_result, ""),
                submission_receipt=self._submission_receipt(
                    request,
                    original,
                    dedup_status="accepted",
                    wait_status="rejected",
                ),
            )
        waiting_for_capacity = (
            _reason_from_worker_result(run_result, "")
            == "TASK_GROUP_CAPACITY_WAITING"
        )
        return self._joined_result(
            request,
            run_result,
            submission_receipt=self._submission_receipt(
                request,
                original,
                dedup_status="accepted",
                wait_status="pending" if waiting_for_capacity else "terminal",
            ),
        )

    def _submission_receipt(
        self,
        request: AgentOrchestrationRequest,
        submission: Any,
        *,
        dedup_status: str,
        wait_status: str,
    ) -> AgentSubmissionReceipt:
        plan = self._store.plan(request.run_id or "", self._stage_binding.stage_id)
        group_id = group_checksum = None
        if plan is not None:
            for event in self._store.read_events(plan.run_id, plan.stage_id):
                payload = getattr(event, "payload", {})
                snapshot = payload.get("group") if isinstance(payload, Mapping) else None
                if isinstance(snapshot, Mapping) and snapshot.get("plan_id") == plan.plan_id and snapshot.get("plan_version") == plan.version:
                    group_id = snapshot.get("group_id")
                    group_checksum = snapshot.get("group_checksum")
        return AgentSubmissionReceipt(
            submission_id=submission.submission_id,
            dedup_key=submission.identity.dedup_key,
            candidate_ref=submission.candidate_ref,
            record_checksum=submission.record_checksum,
            group_id=group_id,
            group_checksum=group_checksum,
            plan_id=submission.plan_id,
            plan_version=str(plan.version) if plan is not None else None,
            plan_checksum=plan.plan_checksum if plan is not None else None,
            dedup_status=dedup_status,
            wait_status=wait_status,
            accepted_at=submission.accepted_at,
            candidate_checksum=submission.candidate_checksum,
            run_id=submission.identity.run_id,
            stage_id=submission.identity.stage_id,
            parent_turn_id=submission.identity.parent_turn_id,
            action_correlation_id=submission.identity.action_correlation_id,
            admission_id=submission.admission_id,
        )

    def _require_parent_graph(self, identity: GraphExecutionIdentity) -> None:
        expected = (
            self._stage_binding.graph_id,
            self._stage_binding.graph_version,
            self._stage_binding.graph.identity_ref.exact_ref,
            self._stage_binding.graph_checksum,
            self._stage_binding.node_id,
        )
        actual = (
            identity.graph_id,
            identity.graph_version,
            identity.graph_ref,
            identity.graph_checksum,
            identity.node_id,
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
            execution_identity=request.execution_identity,
            **self._input_ref_options(request.execution_identity, policy),
        ).stage_identity
        return PlanCandidate.for_stage(
            stage_identity=task_identity,
            candidate_id=f"agent-loop:{candidate.correlation_id}",
            input_context_refs=tuple(_context_refs_for_candidate(candidate).values()),
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

    def _joined_result(
        self,
        request: AgentOrchestrationRequest,
        run_result: Any,
        *,
        submission_receipt: AgentSubmissionReceipt | None = None,
    ) -> AgentOrchestrationResult:
        plan = self._store.plan(request.run_id or "", self._stage_binding.stage_id)
        if plan is None:
            reason = _reason_from_worker_result(run_result, "agent_orchestration_plan_unavailable")
            return _rejected_orchestration_result(
                request,
                reason,
                submission_receipt=submission_receipt,
            )
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
        reason_code = _reason_from_worker_result(
            run_result,
            "agent_orchestration_group_not_succeeded",
        )
        waiting_for_capacity = (
            status == "blocked" and reason_code == "TASK_GROUP_CAPACITY_WAITING"
        )
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
        terminal_reason = None if succeeded or waiting_for_capacity else reason_code
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
        self._persist_parent_continuation(
            request,
            plan,
            events,
            group=group,
            submission_receipt=submission_receipt,
        )
        return AgentOrchestrationResult(
            status=(
                "succeeded" if succeeded
                else "waiting" if waiting_for_capacity
                else "partial_failure"
            ),
            observation=observation,
            reason_code=None if succeeded else reason_code,
            submission_receipt=submission_receipt,
        )

    def _persist_parent_continuation(
        self,
        request: AgentOrchestrationRequest,
        plan: Any,
        events: tuple[Any, ...],
        *,
        group: Mapping[str, Any],
        submission_receipt: AgentSubmissionReceipt | None,
    ) -> None:
        """Persist one checksum-bound wake-up for the parent turn.

        The observation delivered to an AgentLoop parent is derived from the
        canonical group join event.  Keeping the continuation as a separate
        TaskPlan event makes delivery restartable without re-running workers;
        repeated dispatches reuse the existing terminal version.
        """

        if submission_receipt is None:
            return
        group_id = group.get("group_id")
        if not isinstance(group_id, str) or not group_id:
            return
        canonical_observation = _latest_group_observation(events, group_id)
        if canonical_observation is None:
            return
        observation_checksum = canonical_observation.get("observation_checksum")
        if not isinstance(observation_checksum, str) or not observation_checksum:
            return
        group_state = group.get("state")
        if not isinstance(group_state, str) or not group_state:
            return
        terminal_states = {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
            "INDETERMINATE",
            "HALTED",
            "SUPERSEDED",
        }
        status = "DELIVERED" if group_state in terminal_states else "PENDING"
        prior = []
        for event in events:
            if getattr(event, "event_type", None) != PARENT_CONTINUATION_EVENT:
                continue
            try:
                continuation = continuation_from_event(event)
            except HarnessValidationError:
                continue
            if (
                continuation.run_id == plan.run_id
                and continuation.stage_id == plan.stage_id
                and continuation.parent_turn_id == request.parent_turn_id
                and continuation.submission_id == submission_receipt.submission_id
            ):
                prior.append(continuation)
        group_prior = [item for item in prior if item.group_id == group_id]
        current = max(group_prior, key=lambda item: item.observation_version, default=None)
        latest_version = max(
            (item.observation_version for item in prior),
            default=0,
        )
        if current is not None and (
            current.observation_checksum == observation_checksum
            and current.status == status
            and current.group_state == group_state
        ):
            return
        if current is not None and current.observation_checksum == observation_checksum:
            # A delivery-state transition for the same canonical observation
            # keeps its identity. A changed observation is a new version.
            version = current.observation_version
        else:
            version = latest_version + 1
        continuation = ParentContinuation(
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            parent_turn_id=request.parent_turn_id,
            observation_id=f"parent-observation:{group_id}",
            observation_version=version,
            group_id=group_id,
            observation_checksum=observation_checksum,
            status=status,
            submission_id=submission_receipt.submission_id,
            group_state=group_state,
            metadata={"correlation_id": request.candidate.correlation_id},
        )
        sequence = max((getattr(event, "sequence", 0) for event in events), default=0) + 1
        event = TaskPlanEvent.for_plan(
            PARENT_CONTINUATION_EVENT,
            plan,
            sequence=sequence,
            input_checksum=observation_checksum,
            payload={
                "event_type": PARENT_CONTINUATION_EVENT,
                "parallel_event_idempotency_key": (
                    f"parent-continuation:{group_id}:{version}"
                ),
                "idempotency_key": continuation.continuation_checksum,
                "continuation": continuation.to_dict(),
            },
        )
        try:
            self._store.append_event(event)
        except HarnessValidationError as exc:
            # Two parent deliveries may race after the same durable join.  A
            # sequence collision is safe only when the committed event is the
            # exact continuation we intended to write.
            if exc.code != "task_plan_sequence_conflict":
                raise
            latest = self._store.read_events(plan.run_id, plan.stage_id)
            if not any(
                getattr(item, "event_type", None) == PARENT_CONTINUATION_EVENT
                and getattr(item, "event_checksum", None) == event.event_checksum
                for item in latest
            ):
                raise


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


class AgentOrchestrationCompositionState(StrEnum):
    """Immutable rollout state selected by the Harness composition root."""

    FEATURE_DISABLED = "FEATURE_DISABLED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DEGRADED_SERIAL = "DEGRADED_SERIAL"
    ENABLED_PARALLEL = "ENABLED_PARALLEL"


# The AgentLoop-facing name is retained as a discoverable alias for callers
# that describe the state in terms of the Graph composition rather than the
# orchestration port.
AgentLoopCompositionState = AgentOrchestrationCompositionState


@dataclass(frozen=True, slots=True)
class AgentOrchestrationBinding:
    """Production composition state for the optional AgentLoop capability."""

    feature_enabled: bool
    port: AgentOrchestrationPort | None
    availability_reason: str | None = None
    composition_state: AgentOrchestrationCompositionState | str | None = None

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
        state = self.composition_state
        if state is None:
            state = (
                AgentOrchestrationCompositionState.FEATURE_DISABLED
                if not self.feature_enabled
                else (
                    AgentOrchestrationCompositionState.ENABLED_PARALLEL
                    if self.port is not None
                    else AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE
                )
            )
        elif not isinstance(state, AgentOrchestrationCompositionState):
            try:
                state = AgentOrchestrationCompositionState(state)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "composition_state must be a supported AgentLoop composition state"
                ) from exc
        object.__setattr__(self, "composition_state", state)

        if state is AgentOrchestrationCompositionState.FEATURE_DISABLED:
            if self.feature_enabled or self.port is not None:
                raise ValueError("FEATURE_DISABLED requires a disabled feature and no port")
        elif state is AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE:
            if not self.feature_enabled or self.port is not None:
                raise ValueError(
                    "DEPENDENCY_UNAVAILABLE requires an enabled feature without a port"
                )
        elif not self.feature_enabled or self.port is None:
            raise ValueError(
                f"{state} requires an enabled feature with an orchestration port"
            )

    @property
    def available(self) -> bool:
        return (
            self.composition_state
            in {
                AgentOrchestrationCompositionState.DEGRADED_SERIAL,
                AgentOrchestrationCompositionState.ENABLED_PARALLEL,
            }
            and self.feature_enabled
            and self.port is not None
        )

    @classmethod
    def from_dispatch(
        cls,
        *,
        feature_enabled: bool,
        dispatch: AgentOrchestrationDispatch | None,
        composition_state: AgentOrchestrationCompositionState | str | None = None,
    ) -> "AgentOrchestrationBinding":
        if not isinstance(feature_enabled, bool):
            raise TypeError("feature_enabled must be boolean")
        state = composition_state
        if state is None:
            state = (
                AgentOrchestrationCompositionState.FEATURE_DISABLED
                if not feature_enabled
                else (
                    AgentOrchestrationCompositionState.ENABLED_PARALLEL
                    if dispatch is not None
                    else AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE
                )
            )
        else:
            try:
                state = AgentOrchestrationCompositionState(state)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "composition_state must be a supported AgentLoop composition state"
                ) from exc

        if state is AgentOrchestrationCompositionState.FEATURE_DISABLED:
            return cls(
                feature_enabled=False,
                port=None,
                availability_reason="feature_disabled",
                composition_state=state,
            )
        if dispatch is None:
            return cls(
                feature_enabled=feature_enabled,
                port=None,
                availability_reason="agent_orchestration_unavailable",
                composition_state=state,
            )
        if not feature_enabled:
            raise ValueError("enabled composition state requires feature_enabled=True")
        return cls(
            feature_enabled=feature_enabled,
            port=HarnessAgentOrchestrationPort(dispatch=dispatch),
            composition_state=state,
        )


def _context_refs_for_candidate(candidate: DelegateBatchCandidate) -> dict[str, str]:
    refs = tuple(
        sorted(
            {
                ref
                for proposal in candidate.tasks
                for ref in proposal.input_refs
                if task_reference_producer(ref, tuple(item.logical_task_id for item in candidate.tasks)) is None
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
    *,
    submission_receipt: AgentSubmissionReceipt | None = None,
) -> AgentOrchestrationResult:
    return AgentOrchestrationResult(
        status="rejected",
        reason_code=reason_code,
        observation=ParentObservation(
            group_id=None,
            group_status="rejected",
            plan_version=None,
            diagnostics=(reason_code,),
            schema_version=PARENT_OBSERVATION_REJECTED_SCHEMA,
        ),
        submission_receipt=submission_receipt,
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


def _latest_group_observation(
    events: tuple[Any, ...],
    group_id: str,
) -> Mapping[str, Any] | None:
    """Return the latest checksum-validated observation emitted by the group."""

    latest: Mapping[str, Any] | None = None
    for event in events:
        if getattr(event, "event_type", None) not in {
            "TASK_GROUP_JOIN_WAITING",
            "TASK_GROUP_JOINED",
            "TASK_GROUP_FAILED",
            "TASK_GROUP_CANCELLED",
            "TASK_GROUP_INDETERMINATE",
            "TASK_GROUP_HALTED",
            "TASK_GROUP_SUPERSEDED",
        }:
            continue
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        observation = payload.get("observation")
        if not isinstance(observation, Mapping) or observation.get("group_id") != group_id:
            continue
        supplied = observation.get("observation_checksum")
        if not isinstance(supplied, str) or not supplied:
            continue
        expected = canonical_payload_checksum(
            {
                key: value
                for key, value in observation.items()
                if key != "observation_checksum"
            }
        )
        if supplied != expected:
            raise HarnessValidationError(
                "parent continuation source observation checksum is invalid",
                code="parent_continuation_observation_mismatch",
            )
        latest = observation
    return latest


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
    "AgentOrchestrationCompositionState",
    "AgentLoopCompositionState",
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
