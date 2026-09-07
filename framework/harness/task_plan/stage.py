from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from typing import Any, Callable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.scheduler import HarnessScheduler
from framework.harness.task_plan.aggregator import TaskPlanAggregator
from framework.harness.task_plan.binding import TaskPlanCapabilityRegistry
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskLifecycle, ValidatedTaskPlan, TaskInstance, TaskPlanProjection
from framework.harness.task_plan.ports import (
    PlanBuildRequest,
    PlanCandidateBuilderPort,
    TaskPlanResultVerifierPort,
    TaskPlanStageRequest,
    TaskPlanStageRunnerPort,
)
from framework.harness.task_plan.patches import TaskPlanPatchValidator
from framework.harness.task_plan.models import PlanPatch
from framework.harness.task_plan.observability import task_plan_metric_samples
from framework.harness.task_plan.checkpoint import (
    TaskPlanCheckpoint,
    TaskPlanCheckpointStorePort,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer, TaskPlanReplayReport
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    task_instance_for_attempt,
)
from framework.harness.task_plan.store import (
    TaskPlanEvent,
    TaskPlanStorePort,
    TaskResultRecord,
    _task_plan_event_identity_kwargs,
)
from framework.harness.task_plan.submission import CandidateSubmission
from framework.harness.task_plan.submission_result import submission_result_from_event
from framework.harness.task_plan.validation import TaskPlanValidationContext, TaskPlanValidator
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.harness.task_plan.canonical import canonical_payload_checksum, thaw_mapping
from framework.harness.task_plan.dependency import (
    TASK_BLOCKED_UPSTREAM_FAILURE,
    block_dependency_task,
    dependency_blocked_task_ids,
    dependency_blocking_predecessor_ids,
)
from framework.harness.task_plan.parallel import (
    DispatchGroup,
    DispatchGroupState,
    DispatchWave,
    JoinPolicy,
    ParallelAgentCoordinator,
    ParallelDispatchRequest,
    ParallelEventSink,
    ParentObservationLimits,
    SideEffectClass,
    child_budget_reservation,
)
from framework.harness.task_plan.planning_observation import (
    PlanningObservationPort,
    PlanningObservationReceipt,
    PlanningObservationRequest,
)
from framework.harness.task_plan.verification import (
    TaskPlanResultVerificationRequest,
    TaskPlanResultVerifier,
)
from framework.harness.graph.activity import HarnessWorkerType


def _same_spawn_receipt(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    """Compare the immutable identity carried by one spawn receipt."""

    fields = (
        "event_type",
        "group_id",
        "wave_id",
        "task_id",
        "task_instance_id",
        "attempt",
        "operation_key",
        "spawn_status",
        "child_id",
    )
    return all(left.get(field) == right.get(field) for field in fields)


class TaskPlanStageRunner(TaskPlanStageRunnerPort):
    """Harness-owned PLAN -> EXECUTE -> VERIFY runner for a dynamic stage."""

    def __init__(
        self,
        *,
        candidate_builder: PlanCandidateBuilderPort,
        capability_registry: TaskPlanCapabilityRegistry,
        store: TaskPlanStorePort,
        validator: TaskPlanValidator | None = None,
        scheduler: HarnessScheduler | None = None,
        aggregator: TaskPlanAggregator | None = None,
        result_verifier: TaskPlanResultVerifierPort | None = None,
        worker_executor: Any | None = None,
        worker_result_recovery: Any | None = None,
        parallel_coordinator: ParallelAgentCoordinator | None = None,
        child_supervisor_capacity: int | None = None,
        planning_observation_port: PlanningObservationPort | None = None,
        metrics_sink: Callable[[tuple[Any, ...]], Any] | None = None,
        checkpoint_store: TaskPlanCheckpointStorePort | None = None,
    ) -> None:
        if not isinstance(store, TaskPlanStorePort):
            raise TypeError("store must implement TaskPlanStorePort")
        if result_verifier is not None and not isinstance(
            result_verifier, TaskPlanResultVerifierPort
        ):
            raise TypeError("result_verifier must implement TaskPlanResultVerifierPort")
        self.candidate_builder = candidate_builder
        self.capability_registry = capability_registry
        self.store = store
        self.validator = validator or TaskPlanValidator()
        if scheduler is not None and not isinstance(scheduler, HarnessScheduler):
            raise TypeError("scheduler must be HarnessScheduler")
        self.scheduler = scheduler or HarnessScheduler()
        self.aggregator = aggregator or TaskPlanAggregator()
        self.result_verifier = result_verifier or TaskPlanResultVerifier()
        self.worker_executor = worker_executor
        if worker_result_recovery is not None and not callable(worker_result_recovery):
            raise TypeError("worker_result_recovery must be callable")
        self.worker_result_recovery = worker_result_recovery
        if parallel_coordinator is not None and not isinstance(
            parallel_coordinator, ParallelAgentCoordinator
        ):
            raise TypeError("parallel_coordinator must be ParallelAgentCoordinator")
        if child_supervisor_capacity is not None and (
            isinstance(child_supervisor_capacity, bool)
            or not isinstance(child_supervisor_capacity, int)
            or child_supervisor_capacity < 0
        ):
            raise ValueError("child_supervisor_capacity must be a non-negative integer")
        self.parallel_coordinator = parallel_coordinator
        self.child_supervisor_capacity = child_supervisor_capacity
        if planning_observation_port is not None and not isinstance(
            planning_observation_port,
            PlanningObservationPort,
        ):
            raise TypeError(
                "planning_observation_port must implement PlanningObservationPort"
            )
        self.planning_observation_port = planning_observation_port
        if metrics_sink is not None and not callable(metrics_sink):
            raise TypeError("metrics_sink must be callable")
        self.metrics_sink = metrics_sink
        if checkpoint_store is not None and not isinstance(
            checkpoint_store,
            TaskPlanCheckpointStorePort,
        ):
            raise TypeError(
                "checkpoint_store must implement TaskPlanCheckpointStorePort"
            )
        self.checkpoint_store = checkpoint_store
        self.patch_validator = TaskPlanPatchValidator()

    def observe_for_planning(
        self,
        stage_request: TaskPlanStageRequest,
        observation_request: PlanningObservationRequest,
    ) -> PlanningObservationReceipt:
        """Execute one Harness-admitted read-only fact request for a planner."""

        if not isinstance(stage_request, TaskPlanStageRequest):
            raise TypeError("stage_request must be TaskPlanStageRequest")
        if not isinstance(observation_request, PlanningObservationRequest):
            raise TypeError("observation_request must be PlanningObservationRequest")
        if self.planning_observation_port is None:
            raise HarnessValidationError(
                "planning observation runtime is unavailable",
                code="planning_observation_port_required",
            )
        expected = (
            stage_request.run_id,
            stage_request.stage_id,
            stage_request.policy.policy_checksum,
        )
        actual = (
            observation_request.run_id,
            observation_request.stage_id,
            observation_request.policy_checksum,
        )
        if actual != expected:
            raise HarnessValidationError(
                "planning observation request is outside the stage policy scope",
                code="planning_observation_request_scope_mismatch",
            )
        return self.planning_observation_port.observe(observation_request)

    def apply_patch(self, request: TaskPlanStageRequest, patch: PlanPatch) -> ValidatedTaskPlan:
        """Validate and durably accept one immutable plan patch version."""
        current = self.store.plan(request.run_id, request.stage_id)
        if current is None:
            raise HarnessValidationError("cannot patch a stage without an accepted plan", code="task_plan_missing_plan")
        _require_plan_stage_binding(current, request)
        self.patch_validator.require_policy_identity(current, request.policy)
        projection = self.store.load_projection(request.run_id, request.stage_id)
        self.store.append_patch(patch, accepted=False)
        try:
            next_plan = self.patch_validator.apply(
                current,
                patch,
                projection,
                request.policy,
                self.capability_registry,
                accepted_at=_required_observation_time(request),
                available_input_refs=tuple(request.context_refs.values()),
            )
        except HarnessValidationError as exc:
            self.store.append_event(
                TaskPlanEvent.for_plan(
                    "PLAN_PATCH_REJECTED",
                    current,
                    input_checksum=patch.patch_checksum,
                    reason_code=exc.code or "task_plan_patch_rejected",
                    payload={"patch_ref": patch.patch_checksum},
                    sequence=self._next_sequence(request),
                )
            )
            raise
        skip_ids = {
            operation.target_task_id
            for operation in patch.operations
            if operation.operation.value == "SKIP_PENDING_TASK"
            and operation.target_task_id is not None
        }
        self.store.accept_patched_plan(
            patch,
            next_plan,
            skipped_task_ids=tuple(sorted(skip_ids)),
        )
        return next_plan

    def run(self, request: TaskPlanStageRequest) -> HarnessWorkerResult:
        plan: ValidatedTaskPlan | None = None
        try:
            plan = self._ensure_plan(request)
            recorded_result = self._recorded_submission_result(request, plan)
            if recorded_result is not None:
                return recorded_result
            self._execute_plan(request, plan)
            projection = self.store.load_projection(request.run_id, request.stage_id)
            results = self.store.results_for(request.run_id, request.stage_id, plan.plan_id, plan.version)
            aggregate = self.aggregator.aggregate(results, request.policy)
            self.store.append_event(TaskPlanEvent.for_plan(
                "STAGE_OUTPUT_AGGREGATED",
                plan,
                input_checksum=aggregate.aggregate_checksum,
                output_refs=tuple(aggregate.output_refs_by_role.values()),
                payload={
                    "aggregate_ref": aggregate.aggregate_ref,
                    "aggregate_checksum": aggregate.aggregate_checksum,
                    "output_refs_by_role": dict(aggregate.output_refs_by_role),
                    "result_refs": list(aggregate.result_refs),
                    "branch_refs": [dict(item) for item in aggregate.branch_refs],
                },
                sequence=self._next_sequence(request),
            ))
            self._persist_checkpoint(request, plan)
            result = HarnessWorkerResult(
                status=HarnessWorkerStatus.SUCCEEDED,
                output={
                    "aggregate_ref": aggregate.aggregate_ref,
                    "aggregate_checksum": aggregate.aggregate_checksum,
                    "output_refs_by_role": dict(aggregate.output_refs_by_role),
                    "analysis_branch_refs": [dict(item) for item in aggregate.branch_refs],
                },
                diagnostics={"plan_id": plan.plan_id, "plan_version": plan.version, "projection_checksum": projection.projection_checksum},
            )
            self.store.append_event(TaskPlanEvent.for_plan(
                "TASK_PLAN_VERIFIED",
                plan,
                input_checksum=aggregate.aggregate_checksum,
                output_refs=tuple(aggregate.output_refs_by_role.values()),
                payload=self._submission_result_payload(request, result),
                sequence=self._next_sequence(request),
            ))
            self._persist_checkpoint(request, plan)
            self._publish_metrics(plan)
            return result
        except HarnessValidationError as exc:
            reason_code = exc.code or "task_plan_failure"
            result = HarnessWorkerResult(
                status=HarnessWorkerStatus.BLOCKED,
                error=reason_code if request.submission_identity is not None else str(exc),
                diagnostics={"reason_code": reason_code},
            )
            if exc.code in {
                "CANDIDATE_IDEMPOTENCY_CONFLICT",
                "task_plan_submission_binding_conflict",
                "task_plan_submission_scope_unavailable",
                "task_plan_submission_identity_required",
                "task_plan_candidate_conflict",
                "task_plan_submission_result_invalid",
            }:
                return result
            self._halt(request, exc.code or "task_plan_failure", result=result)
            self._publish_metrics(plan or self.store.plan(request.run_id, request.stage_id))
            return result
        except Exception as exc:
            result = HarnessWorkerResult(
                status=HarnessWorkerStatus.FAILED,
                error="task_plan_stage_failure" if request.submission_identity is not None else str(exc),
                diagnostics={"reason_code": "task_plan_stage_failure"},
            )
            self._halt(request, "task_plan_stage_failure", result=result)
            self._publish_metrics(plan or self.store.plan(request.run_id, request.stage_id))
            return result

    def _ensure_plan(self, request: TaskPlanStageRequest) -> ValidatedTaskPlan:
        if request.policy_ref is not None and request.policy_ref != request.policy.exact_ref:
            raise HarnessValidationError("TaskPlan request policy ref is not pinned to the supplied policy", code="task_plan_policy_mismatch")
        existing = self.store.plan(request.run_id, request.stage_id)
        if existing is not None:
            _require_plan_stage_binding(existing, request)
            self.patch_validator.require_policy_identity(existing, request.policy)
        submission = self._existing_submission(request, existing)
        if existing is not None:
            initial = self.store.plan(request.run_id, request.stage_id, version=1)
            candidate_ref = (
                submission.candidate_ref if submission is not None
                else request.candidate.candidate_checksum if request.candidate is not None
                else None
            )
            if candidate_ref is not None and (
                initial is None or initial.source_candidate_ref != candidate_ref
            ):
                raise HarnessValidationError(
                    "submitted candidate differs from the accepted stage candidate",
                    code="task_plan_candidate_conflict",
                )
            return existing
        candidate = request.candidate
        if candidate is None and submission is not None:
            candidate = self.store.candidate_for(
                request.run_id, request.stage_id, submission.candidate_ref
            )
            if candidate is None:
                raise HarnessValidationError(
                    "persisted submission candidate is unavailable",
                    code="task_plan_artifact_missing",
                )
        if candidate is None:
            candidate = self.candidate_builder.build_candidate(PlanBuildRequest(
                run_id=request.run_id,
                stage_binding=request.stage_binding,
                context_refs=request.context_refs,
                policy=request.policy,
                budget=request.budget,
                metadata=request.metadata,
                execution_identity=request.execution_identity,
            ))
        if not candidate.matches_stage_identity(request.stage_identity):
            raise HarnessValidationError(
                "candidate does not match the frozen stage",
                code="task_plan_candidate_scope_mismatch",
            )
        if request.submission_identity is not None:
            submission = self.store.admit_candidate_submission(
                candidate,
                request.submission_identity,
                accepted_at=request.accepted_at,
                candidate_checksum=request.source_candidate_checksum or (
                    submission.candidate_checksum if submission is not None else None
                ),
            )
        self._validate_planning_observations(request, candidate)
        if submission is None:
            self.store.append_candidate(candidate)
        context = TaskPlanValidationContext(
            run_id=request.run_id,
            stage_binding=request.stage_binding,
            available_input_refs=tuple(request.context_refs.values()),
            registered_gate_refs=_registered_gate_refs(
                self.result_verifier,
                fallback=request.policy.allowed_gate_refs,
            ),
            registered_aggregator_refs=self.aggregator.registry.refs,
        )
        missing_aggregators = sorted({ref for ref in request.policy.deterministic_aggregator_refs.values() if not self.aggregator.registry.contains(ref)})
        if missing_aggregators:
            raise HarnessValidationError("deterministic TaskPlan aggregator is unavailable", code="task_plan_aggregator_unavailable", details={"aggregator_refs": missing_aggregators})
        result = self.validator.validate(candidate, policy=request.policy, capabilities=self.capability_registry, context=context)
        if not result.accepted:
            self.store.append_rejected_candidate(candidate, reason_code=result.diagnostics[0].code if result.diagnostics else "task_plan_candidate_rejected")
            result.require_valid()
        plan = ValidatedTaskPlan.from_candidate(
            candidate,
            plan_id=(
                submission.plan_id if submission is not None else
                canonical_payload_checksum({"candidate_checksum": candidate.candidate_checksum, "stage_id": request.stage_id})
            ),
            version=1,
            parent_plan_id=None,
            source_candidate_ref=candidate.candidate_checksum,
            policy_ref=request.policy.exact_ref,
            policy_checksum=request.policy.policy_checksum,
            tasks=result.resolved_tasks,
            required_output_roles=request.policy.required_output_roles,
            limits=request.policy.limits,
            accepted_at=submission.accepted_at if submission is not None else _required_observation_time(request),
        )
        self.store.accept_plan(plan)
        return plan

    def _existing_submission(
        self, request: TaskPlanStageRequest, existing: ValidatedTaskPlan | None
    ) -> CandidateSubmission | None:
        identity = request.submission_identity
        if identity is None:
            if self.store.submissions_for(request.run_id, request.stage_id):
                raise HarnessValidationError(
                    "stage admission requires its original candidate submission identity",
                    code="task_plan_submission_identity_required",
                )
            return None
        submissions = self.store.submissions_for(request.run_id, request.stage_id)
        matching = next((item for item in submissions if item.identity == identity), None)
        if matching is None:
            if existing is not None or submissions:
                raise HarnessValidationError(
                    "stage execution scope is already bound to another submission",
                    code="task_plan_submission_scope_unavailable",
                )
            return None
        if request.candidate is not None:
            matching = self.store.admit_candidate_submission(
                request.candidate,
                identity,
                accepted_at=request.accepted_at,
                candidate_checksum=request.source_candidate_checksum,
            )
        elif request.source_candidate_checksum is not None and (
            request.source_candidate_checksum != matching.candidate_checksum
        ):
            raise HarnessValidationError(
                "candidate checksum conflicts with the original submission",
                code="CANDIDATE_IDEMPOTENCY_CONFLICT",
            )
        return matching

    def _submission_result_payload(
        self, request: TaskPlanStageRequest, result: HarnessWorkerResult
    ) -> dict[str, Any]:
        if request.submission_identity is None:
            return {}
        return {
            "submission_key": request.submission_identity.dedup_key,
            "terminal_result": result.to_dict(),
            "terminal_result_checksum": result.candidate_result_ref,
        }

    def _recorded_submission_result(
        self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan
    ) -> HarnessWorkerResult | None:
        if request.submission_identity is None:
            return None
        for event in reversed(self.store.read_events(request.run_id, request.stage_id)):
            if event.plan_id != plan.plan_id or event.plan_version != plan.version:
                continue
            if event.event_type not in {"TASK_PLAN_VERIFIED", "TASK_PLAN_HALTED"}:
                continue
            payload = event.payload
            if payload.get("submission_key") != request.submission_identity.dedup_key:
                raise HarnessValidationError(
                    "recorded terminal outcome lost its submission identity",
                    code="task_plan_submission_result_invalid",
                )
            try:
                self._replay_history(request, plan)
                return submission_result_from_event(event)
            except HarnessValidationError as exc:
                raise HarnessValidationError(
                    "recorded submission outcome has unverifiable history",
                    code="task_plan_submission_result_invalid",
                ) from exc
        return None

    def _validate_planning_observations(
        self,
        request: TaskPlanStageRequest,
        candidate: Any,
    ) -> None:
        source_refs = tuple(getattr(candidate, "source_observation_refs", ()))
        if not source_refs:
            return
        if self.planning_observation_port is None:
            raise HarnessValidationError(
                "planning observation receipts require a configured validation port",
                code="planning_observation_port_required",
            )
        planner_turn_id = candidate.metadata.get("planner_turn_id")
        if not isinstance(planner_turn_id, str) or not planner_turn_id.strip():
            raise HarnessValidationError(
                "candidate planning observation refs require planner_turn_id metadata",
                code="planning_observation_planner_turn_missing",
            )
        self.planning_observation_port.validate_source_refs(
            source_refs,
            run_id=request.run_id,
            stage_id=request.stage_id,
            planner_turn_id=planner_turn_id,
            policy_checksum=request.policy.policy_checksum,
        )

    def _execute_plan(self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan) -> None:
        if self.parallel_coordinator is None:
            if self._parallel_runtime_requested(request.policy, plan):
                raise HarnessValidationError(
                    "parallel TaskPlan runtime requires an explicit coordinator",
                    code=(
                        "TASK_GROUP_WAVE_ADAPTER_REQUIRED"
                        if request.policy.serial_fallback
                        else "task_plan_parallel_runtime_required"
                    ),
                )
            return self._execute_plan_serial(request, plan)
        return self._execute_plan_parallel(request, plan)

    def _execute_plan_serial(self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan) -> None:
        max_rounds = max(1, plan.limits.max_tasks * plan.limits.max_task_attempts + plan.limits.max_replans + 1)
        for _ in range(max_rounds):
            projection = self.store.load_projection(request.run_id, request.stage_id)
            if self._recover_committed_subagent_results(request, plan, projection):
                continue
            if self._propagate_dependency_blocks(request, plan, projection):
                continue
            if self._recover_failed_task_retries(request, plan, projection):
                continue
            decision = self.scheduler.next_task_plan_decision(
                projection,
                plan.limits.max_parallelism,
                plan=plan,
                policy=request.policy,
                available_input_refs=tuple(request.context_refs.values()),
            )
            if not decision.task_requests:
                pending = [item.task_id for item in projection.tasks if item.status in {TaskLifecycle.PENDING, TaskLifecycle.READY, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}]
                failed = [item.task_id for item in projection.tasks if item.status is TaskLifecycle.FAILED]
                if pending or failed:
                    raise HarnessValidationError("TaskPlan cannot make further progress", code="task_plan_task_blocked", details={"pending": pending, "failed": failed, "blocked": list(decision.blocked_task_ids)})
                return
            for task_request in decision.task_requests:
                current = self.store.load_projection(request.run_id, request.stage_id)
                reserved = self.scheduler.reserve_task_plan_tasks(
                    current,
                    TaskPlanReadyDecision((task_request,)),
                )
                self._commit_task_transition(
                    request,
                    plan,
                    task_request,
                    "TASK_READY",
                    reserved,
                )
                projection = self.scheduler.mark_task_plan_dispatched(
                    self.store.load_projection(request.run_id, request.stage_id),
                    task_request,
                )
                self._commit_task_transition(
                    request,
                    plan,
                    task_request,
                    "TASK_DISPATCHED",
                    projection,
                )
                projection = self.scheduler.mark_task_plan_started(
                    self.store.load_projection(request.run_id, request.stage_id),
                    task_request,
                )
                self._commit_task_transition(
                    request,
                    plan,
                    task_request,
                    "TASK_STARTED",
                    projection,
                )
                result = self._invoke(
                    task_request,
                    plan,
                    request.policy,
                    execution_identity=request.execution_identity,
                )
                self.store.append_result(result)
                if result.status is TaskLifecycle.FAILED:
                    resolved = next(item for item in plan.tasks if item.task_id == result.task_id)
                    retryable_codes = set(resolved.normalized_retry_policy.retryable_reason_codes)
                    if (
                        result.error_code in retryable_codes
                        and result.attempt < resolved.normalized_retry_policy.max_attempts
                    ):
                        current = self.store.load_projection(request.run_id, request.stage_id)
                        retry_tasks = tuple(replace(item, status=TaskLifecycle.PENDING, active_instance_id=None, failure_reason_code=None) if item.task_id == result.task_id else item for item in current.tasks)
                        sequence = self._next_sequence(request)
                        retry_projection = replace(
                            current,
                            tasks=retry_tasks,
                            last_sequence=sequence,
                        )
                        self.store.commit_event(TaskPlanEvent.for_plan(
                            "TASK_RETRY_SCHEDULED", plan,
                            task_id=result.task_id, task_instance_id=result.task_instance_id, attempt=result.attempt,
                            input_checksum=result.result_checksum, reason_code=result.error_code, sequence=sequence,
                        ), retry_projection)
                    elif result.error_code not in retryable_codes:
                        while self._propagate_dependency_blocks(
                            request,
                            plan,
                            self.store.load_projection(request.run_id, request.stage_id),
                        ):
                            pass
                        raise HarnessValidationError(
                            "task failure is outside the task retry policy",
                            code="task_plan_retry_not_allowed",
                            details={
                                "task_id": result.task_id,
                                "attempt": result.attempt,
                                "error_code": result.error_code,
                                "retryable_reason_codes": sorted(retryable_codes),
                            },
                        )
                    else:
                        while self._propagate_dependency_blocks(
                            request,
                            plan,
                            self.store.load_projection(request.run_id, request.stage_id),
                        ):
                            pass
                        raise HarnessValidationError("task retry budget exhausted", code="task_plan_retry_exhausted", details={"task_id": result.task_id})
        raise HarnessValidationError("TaskPlan execution exceeded bounded rounds", code="task_plan_execution_bound_exceeded")

    def _execute_plan_parallel(self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan) -> None:
        policy = request.policy
        limits = ParentObservationLimits(**dict(policy.parent_observation_limits))
        admission = self._parallel_request(request, plan, task_instances=())
        event_sink = self._parallel_event_sink(request, plan)
        self._recover_parallel_spawn_admission(request, plan, event_sink)
        group = self.parallel_coordinator.create_group(admission, event_sink=event_sink)
        max_rounds = max(
            1,
            plan.limits.max_tasks * plan.limits.max_task_attempts
            + plan.limits.max_replans
            + policy.max_waves
            + 1,
        )
        for _ in range(max_rounds):
            projection = self.store.load_projection(request.run_id, request.stage_id)
            recovered_any = self._recover_committed_subagent_results(
                request,
                plan,
                projection,
            )
            projection = self.store.load_projection(request.run_id, request.stage_id)
            blocked_now = self._propagate_dependency_blocks(request, plan, projection)
            if blocked_now:
                projection = self.store.load_projection(request.run_id, request.stage_id)
                self.parallel_coordinator.sync_task_terminals(
                    admission,
                    projection,
                    event_sink=event_sink,
                )
                continue
            durable_results = self.store.results_for(
                request.run_id,
                request.stage_id,
                plan.plan_id,
                plan.version,
            )
            durable_result_history = self.store.result_history_for(
                request.run_id,
                request.stage_id,
                plan.plan_id,
                plan.version,
            )
            # Recovery must run even when the parent store has no result yet:
            # a child may have durably reached SUCCEEDED before a process crash
            # interrupted the parent result append. The coordinator can then
            # recover its checksum-bound terminal envelope and hand the typed
            # result back to this stage for the normal store transition.
            durable_result_history_checksums = {
                (item.task_id, item.result_checksum)
                for item in durable_result_history
            }
            recovered_dispatch = self.parallel_coordinator.recover(
                admission,
                durable_results,
                historical_wave_ordinals=self._historical_parallel_wave_ordinals(
                    request,
                    group_id=group.group_id,
                ),
                limits=limits,
                event_sink=event_sink,
            )
            coordinator_results = tuple(
                item
                for item in recovered_dispatch.results
                if (
                    (item.task_id, item.result_checksum)
                    not in durable_result_history_checksums
                )
            )
            if coordinator_results:
                for result in coordinator_results:
                    self.store.append_result(result)
                for result in coordinator_results:
                    self._handle_parallel_result(request, plan, result)
                recovered_any = True
            if recovered_any:
                continue
            if (
                any(item.status is TaskLifecycle.BLOCKED_DEPENDENCY for item in projection.tasks)
                and any(item.status is TaskLifecycle.FAILED for item in projection.tasks)
            ):
                self.parallel_coordinator.sync_task_terminals(
                    admission,
                    projection,
                    event_sink=event_sink,
                )
                joined = self.parallel_coordinator.join(
                    admission,
                    limits=limits,
                    event_sink=event_sink,
                )
                if not joined.succeeded:
                    raise HarnessValidationError(
                        "parallel TaskPlan group did not satisfy join",
                        code="task_plan_parallel_join_failed",
                        details={"group_id": joined.group.group_id, "diagnostics": list(joined.observation.diagnostics)},
                    )
                return
            dispatch_capacity = self.parallel_coordinator.dispatch_parallelism(admission)
            if self._recover_failed_task_retries(request, plan, projection):
                continue
            decision = self.scheduler.next_task_plan_decision(
                projection,
                dispatch_capacity,
                plan=plan,
                policy=policy,
                available_input_refs=tuple(request.context_refs.values()),
            )
            if not decision.task_requests:
                pending = [
                    item.task_id
                    for item in projection.tasks
                    if item.status
                    in {
                        TaskLifecycle.PENDING,
                        TaskLifecycle.READY,
                        TaskLifecycle.DISPATCHED,
                        TaskLifecycle.RUNNING,
                    }
                ]
                failed = [
                    item.task_id
                    for item in projection.tasks
                    if item.status is TaskLifecycle.FAILED
                ]
                if pending or failed:
                    if any(item.status is TaskLifecycle.BLOCKED_DEPENDENCY for item in projection.tasks):
                        self.parallel_coordinator.sync_task_terminals(
                            admission,
                            projection,
                            event_sink=event_sink,
                        )
                        joined = self.parallel_coordinator.join(
                            admission,
                            limits=limits,
                            event_sink=event_sink,
                        )
                        if not joined.succeeded:
                            raise HarnessValidationError(
                                "parallel TaskPlan group did not satisfy join",
                                code="task_plan_parallel_join_failed",
                                details={"group_id": joined.group.group_id, "diagnostics": list(joined.observation.diagnostics)},
                            )
                        return
                    raise HarnessValidationError(
                        "TaskPlan cannot make further progress",
                        code="task_plan_task_blocked",
                        details={
                            "pending": pending,
                            "failed": failed,
                            "blocked": list(decision.blocked_task_ids),
                        },
                    )
                joined = self.parallel_coordinator.join(
                    admission,
                    limits=limits,
                    event_sink=event_sink,
                )
                if not joined.succeeded:
                    raise HarnessValidationError(
                        "parallel TaskPlan group did not satisfy join",
                        code="task_plan_parallel_join_failed",
                        details={"group_id": joined.group.group_id, "diagnostics": list(joined.observation.diagnostics)},
                    )
                return

            dispatched = self.parallel_coordinator.dispatch(
                self._parallel_request(
                    request,
                    plan,
                    task_instances=decision.task_requests,
                ),
                lambda instance: self._invoke(
                    instance,
                    plan,
                    policy,
                    execution_identity=request.execution_identity,
                ),
                limits=limits,
                finalize=False,
                event_sink=event_sink,
            )
            for result in dispatched.results:
                self.store.append_result(result)
            for result in dispatched.results:
                self._handle_parallel_result(request, plan, result)
        raise HarnessValidationError(
            "parallel TaskPlan execution exceeded bounded rounds",
            code="task_plan_execution_bound_exceeded",
        )

    def _propagate_dependency_blocks(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        projection: Any,
    ) -> bool:
        """Atomically close the next deterministic dependency-block target.

        One event/projection pair is committed per target so a crash between
        targets can resume from the same causal DAG closure without inventing
        an attempt or result for the blocked task.
        """

        targets = dependency_blocked_task_ids(plan, projection)
        if not targets:
            return False
        task_id = targets[0]
        predecessors = dependency_blocking_predecessor_ids(plan, projection, task_id)
        next_projection = block_dependency_task(plan, projection, task_id)
        sequence = self._next_sequence(request)
        self.store.commit_event(
            TaskPlanEvent.for_plan(
                "TASK_BLOCKED_UPSTREAM_FAILURE",
                plan,
                task_id=task_id,
                input_checksum=projection.projection_checksum,
                reason_code=TASK_BLOCKED_UPSTREAM_FAILURE,
                payload={
                    "blocking_predecessor_ids": list(predecessors),
                    "projection_checksum": projection.projection_checksum,
                },
                sequence=sequence,
            ),
            replace(next_projection, last_sequence=sequence),
        )
        self._persist_checkpoint(request, plan)
        return True

    def _handle_parallel_result(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        result: TaskResultRecord,
    ) -> None:
        if result.status is not TaskLifecycle.FAILED:
            return
        resolved = next(item for item in plan.tasks if item.task_id == result.task_id)
        retryable_codes = set(resolved.normalized_retry_policy.retryable_reason_codes)
        if result.error_code in retryable_codes and result.attempt < resolved.normalized_retry_policy.max_attempts:
            current = self.store.load_projection(request.run_id, request.stage_id)
            retry_tasks = tuple(
                replace(
                    item,
                    status=TaskLifecycle.PENDING,
                    active_instance_id=None,
                    failure_reason_code=None,
                )
                if item.task_id == result.task_id
                else item
                for item in current.tasks
            )
            sequence = self._next_sequence(request)
            self.store.commit_event(
                TaskPlanEvent.for_plan(
                    "TASK_RETRY_SCHEDULED",
                    plan,
                    task_id=result.task_id,
                    task_instance_id=result.task_instance_id,
                    attempt=result.attempt,
                    input_checksum=result.result_checksum,
                    reason_code=result.error_code,
                    sequence=sequence,
                ),
                replace(current, tasks=retry_tasks, last_sequence=sequence),
            )
            return
        if result.error_code not in retryable_codes:
            while self._propagate_dependency_blocks(
                request,
                plan,
                self.store.load_projection(request.run_id, request.stage_id),
            ):
                pass
            raise HarnessValidationError(
                "task failure is outside the task retry policy",
                code="task_plan_retry_not_allowed",
                details={
                    "task_id": result.task_id,
                    "attempt": result.attempt,
                    "error_code": result.error_code,
                    "retryable_reason_codes": sorted(retryable_codes),
                },
            )
        while self._propagate_dependency_blocks(
            request,
            plan,
            self.store.load_projection(request.run_id, request.stage_id),
        ):
            pass
        raise HarnessValidationError(
            "task retry budget exhausted",
            code="task_plan_retry_exhausted",
            details={"task_id": result.task_id},
        )

    @staticmethod
    def _parallel_runtime_requested(policy: Any, plan: ValidatedTaskPlan) -> bool:
        del plan
        return any(
            getattr(policy, name, None) is not None
            for name in (
                "capability_capacity",
                "available_concurrency_reservations",
            )
        ) or bool(getattr(policy, "metadata", {}).get("parallel_orchestration", False))

    def _parallel_request(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        *,
        task_instances: tuple[TaskInstance, ...],
    ) -> ParallelDispatchRequest:
        policy = request.policy
        return ParallelDispatchRequest(
            plan=plan,
            task_instances=tuple(task_instances),
            budget_snapshot=self.store.load_projection(request.run_id, request.stage_id).consumed_budget,
            requested_parallelism=policy.max_parallelism,
            capability_capacity=policy.capability_capacity,
            supervisor_capacity=self.child_supervisor_capacity,
            available_concurrency_reservations=policy.available_concurrency_reservations,
            serial_fallback=policy.serial_fallback,
            join_policy=JoinPolicy(policy.join_policy),
            correlation_id=(
                request.submission_identity.dedup_key
                if request.submission_identity is not None else
                f"{request.run_id}-{request.stage_id}-plan-{plan.version}"
            ),
            group_task_ids=tuple(item.task_id for item in plan.tasks),
            side_effect_class=SideEffectClass(policy.side_effect_class),
            resource_conflict_key=policy.resource_conflict_key,
            max_waves=policy.max_waves,
            max_tasks_per_group=policy.max_tasks_per_group,
            max_group_runtime_seconds=policy.max_group_runtime_seconds,
            max_join_wait_seconds=policy.max_join_wait_seconds,
            parent_graph_identity=request.execution_identity,
        )

    def _parallel_group_id(self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan) -> str:
        return "dg_" + canonical_payload_checksum(
            {"run_id": request.run_id, "stage_id": request.stage_id, "plan_id": plan.plan_id}
        ).removeprefix("sha256:")[:32]

    def _recover_parallel_spawn_admission(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        event_sink: ParallelEventSink,
    ) -> None:
        history = self.store.read_events(request.run_id, request.stage_id)
        if not any(event.event_type == "TASK_WAVE_ADMITTED" for event in history):
            return
        report = self._replay_history(request, plan)
        pending = [wave for wave in report.parallel_waves.values()
                   if wave["state"] in {"ADMITTED", "RUNNING"} and wave["execution_mode"] == "SUPERVISED"
                   and report.parallel_groups[wave["group_id"]]["plan_id"] == plan.plan_id]
        if len(pending) > 1:
            raise HarnessValidationError(
                "multiple admitted waves require explicit recovery arbitration",
                code="TASK_GROUP_RECOVERY_WAVE_INVALID",
            )
        for projected_wave in pending:
            wave_id = projected_wave["wave_id"]
            wave = DispatchWave.from_dict(thaw_mapping(projected_wave))
            group = DispatchGroup.from_dict(thaw_mapping(report.parallel_groups[wave.group_id]))
            intents = tuple(thaw_mapping(event.payload) for event in history
                            if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT"
                            and event.payload.get("wave_id") == wave_id)
            instances = tuple(task_instance_for_attempt(
                plan, intent["task_id"], intent["attempt"],
                task_instance_id=intent["task_instance_id"],
            ) for intent in intents)
            recovered = self.parallel_coordinator.reconcile_spawn_intents(
                self._parallel_request(request, plan, task_instances=instances),
                intents,
                lambda instance: self._invoke(instance, plan, request.policy,
                                              execution_identity=request.execution_identity),
                admitted_waves=(wave,), admitted_group=group, event_sink=event_sink,
            )
            if recovered.group.state is DispatchGroupState.INDETERMINATE:
                raise HarnessValidationError(
                    "spawn recovery has an unknown child outcome",
                    code="task_plan_spawn_recovery_indeterminate",
                )

    def _historical_parallel_wave_ordinals(
        self,
        request: TaskPlanStageRequest,
        *,
        group_id: str,
    ) -> tuple[int, ...]:
        """Read admitted wave ordinals without reconstructing live workers."""

        ordinals: list[int] = []
        for event in self.store.read_events(request.run_id, request.stage_id):
            if event.event_type != "TASK_WAVE_ADMITTED":
                continue
            wave = event.payload.get("wave")
            if not isinstance(wave, Mapping) or wave.get("group_id") != group_id:
                continue
            ordinal = wave.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
                raise HarnessValidationError(
                    "durable parallel wave ordinal is invalid",
                    code="TASK_GROUP_RECOVERY_WAVE_INVALID",
                )
            ordinals.append(ordinal)
        if len(ordinals) != len(set(ordinals)):
            raise HarnessValidationError(
                "durable parallel wave ordinals are duplicated",
                code="TASK_GROUP_RECOVERY_WAVE_INVALID",
            )
        return tuple(sorted(ordinals))

    def _parallel_event_sink(self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan):
        return ParallelEventSink(
            append=lambda event: self._record_parallel_events(request, plan, (event,)),
            append_batch=lambda events: self._record_parallel_events(request, plan, events),
        )

    def _record_parallel_events(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        events: tuple[Mapping[str, Any], ...],
    ) -> None:
        history = self.store.read_events(request.run_id, request.stage_id)
        recorded = {
            item.payload.get("parallel_event_idempotency_key"): item
            for item in history
            if "parallel_event_idempotency_key" in item.payload
        }
        batch_event_checksums: dict[str, str] = {}
        spawn_receipt_by_operation: dict[str, Mapping[str, Any]] = {}
        for item in history:
            if item.event_type not in {
                "TASK_ATTEMPT_SPAWN_CONFIRMED",
                "TASK_ATTEMPT_SPAWN_UNKNOWN",
            }:
                continue
            operation_key = item.payload.get("operation_key")
            if not isinstance(operation_key, str) or not operation_key:
                raise HarnessValidationError(
                    "durable spawn receipt is missing its operation key",
                    code="task_plan_event_history_conflict",
                )
            existing = spawn_receipt_by_operation.get(operation_key)
            if existing is not None and not _same_spawn_receipt(existing, item.payload):
                raise HarnessValidationError(
                    "durable spawn receipts conflict for one operation",
                    code="task_plan_event_history_conflict",
                )
            spawn_receipt_by_operation[operation_key] = item.payload
        batch: list[TaskPlanEvent] = []
        reused = 0
        for event in events:
            if not isinstance(event, Mapping) or not isinstance(event.get("event_type"), str):
                raise HarnessValidationError("parallel event type is missing", code="task_plan_parallel_event_invalid")
            event_type = event["event_type"]
            if event_type in {
                "TASK_ATTEMPT_SPAWN_CONFIRMED",
                "TASK_ATTEMPT_SPAWN_UNKNOWN",
            }:
                operation_key = event.get("operation_key")
                if not isinstance(operation_key, str) or not operation_key:
                    raise HarnessValidationError(
                        "spawn receipt is missing its operation key",
                        code="task_plan_parallel_event_invalid",
                    )
                existing_receipt = spawn_receipt_by_operation.get(operation_key)
                if existing_receipt is not None and not _same_spawn_receipt(
                    existing_receipt,
                    event,
                ):
                    raise HarnessValidationError(
                        "spawn receipt conflicts with durable operation evidence",
                        code="task_plan_event_history_conflict",
                    )
                spawn_receipt_by_operation[operation_key] = event
            event_checksum = canonical_payload_checksum(event)
            idempotency_key = event.get("idempotency_key")
            durable_key = f"{event_type}:{idempotency_key or event_checksum}"
            existing = recorded.get(durable_key)
            if existing is not None:
                if existing.input_checksum != event_checksum:
                    raise HarnessValidationError(
                        "parallel event idempotency key has conflicting content",
                        code="task_plan_event_history_conflict",
                    )
                reused += 1
                continue
            existing_batch_checksum = batch_event_checksums.get(durable_key)
            if existing_batch_checksum is not None:
                if existing_batch_checksum != event_checksum:
                    raise HarnessValidationError(
                        "parallel event idempotency key has conflicting content",
                        code="task_plan_event_history_conflict",
                    )
                continue
            batch.append(
                TaskPlanEvent.for_plan(
                    event_type, plan,
                    input_checksum=event_checksum,
                    reason_code=event.get("reason_code") if isinstance(event.get("reason_code"), str) else None,
                    payload={**event, "parallel_event_idempotency_key": durable_key},
                    sequence=len(history) + len(batch) + 1,
                )
            )
            batch_event_checksums[durable_key] = event_checksum
        if reused:
            if batch:
                raise HarnessValidationError("atomic admission is partially present", code="task_plan_event_history_conflict")
            return
        if not batch:
            return
        current = self.store.load_projection(request.run_id, request.stage_id)
        transitions: list[TaskPlanEvent] = []
        projections: list[TaskPlanProjection] = []
        projected = current

        def append_transition(instance: TaskInstance, event_type: str, next_projection: TaskPlanProjection) -> None:
            nonlocal projected
            sequence = len(history) + len(transitions) + 1
            transitions.append(TaskPlanEvent.for_plan(
                event_type, plan, task_id=instance.task_id,
                task_instance_id=instance.task_instance_id, attempt=instance.attempt,
                input_checksum=instance.task_definition_checksum, sequence=sequence,
            ))
            projected = replace(next_projection, last_sequence=sequence)
            projections.append(projected)

        admissions = [event for event in batch if event.event_type == "TASK_WAVE_ADMITTED"]
        if admissions:
            instances = self._validate_wave_admission(plan, tuple(batch), current)
            for instance in instances:
                reserved = self.scheduler.reserve_task_plan_tasks(projected, TaskPlanReadyDecision((instance,)))
                if next(state for state in projected.tasks if state.task_id == instance.task_id).status is not TaskLifecycle.READY:
                    append_transition(instance, "TASK_READY", reserved)
                else:
                    projected = reserved
        elif any(event.event_type == "TASK_ATTEMPT_SPAWN_INTENT" for event in batch):
            raise HarnessValidationError("spawn intent requires atomic wave admission", code="task_plan_parallel_event_invalid")
        for event in batch:
            sequence = len(history) + len(transitions) + 1
            transitions.append(replace(event, sequence=sequence))
            projected = replace(projected, last_sequence=sequence)
            projections.append(projected)
            if event.event_type == "TASK_WAVE_DISPATCHED":
                for instance in self._dispatched_wave_instances(plan, event, history, projected):
                    append_transition(instance, "TASK_DISPATCHED", self.scheduler.mark_task_plan_dispatched(projected, instance))
                    append_transition(instance, "TASK_STARTED", self.scheduler.mark_task_plan_started(projected, instance))
        self.store.commit_events(
            tuple(transitions), tuple(projections),
            expected_projection_checksum=current.projection_checksum,
        )
        self._persist_checkpoint(request, plan)

    @staticmethod
    def _validate_wave_admission(
        plan: ValidatedTaskPlan,
        events: tuple[TaskPlanEvent, ...],
        projection: TaskPlanProjection,
    ) -> tuple[TaskInstance, ...]:
        admission = events[0]
        if admission.event_type != "TASK_WAVE_ADMITTED" or any(event.event_type != "TASK_ATTEMPT_SPAWN_INTENT" for event in events[1:]):
            raise HarnessValidationError("wave admission requires one complete intent batch", code="task_plan_parallel_event_invalid")
        payload = admission.payload
        group = DispatchGroup.from_dict(thaw_mapping(payload["group"]))
        wave = DispatchWave.from_dict(thaw_mapping(payload["wave"]))
        if wave.state.value != "ADMITTED":
            raise HarnessValidationError("wave admission requires an admitted snapshot", code="task_plan_parallel_event_invalid")
        if (
            (group.run_id, group.stage_id, group.plan_id, group.plan_version)
            != (plan.run_id, plan.stage_id, plan.plan_id, plan.version)
            or wave.group_id != group.group_id
            or dict(group.budget_envelope) != plan.limits.aggregate_task_budget.to_dict()
        ):
            raise HarnessValidationError("wave admission owner differs from plan", code="task_plan_parallel_event_invalid")
        ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
        if payload.get("budget_before_checksum") != ledger.to_dict()["ledger_checksum"]:
            raise HarnessValidationError("wave admission uses a stale ledger", code="task_plan_budget_checksum_mismatch")
        states = {state.task_id: state for state in projection.tasks}
        if not set(wave.task_ids).issubset(states) or any(states[task_id].status not in {TaskLifecycle.PENDING, TaskLifecycle.READY} for task_id in wave.task_ids):
            raise HarnessValidationError("wave admission requires ready candidates", code="task_plan_parallel_event_invalid")
        instances = tuple(task_instance_for_attempt(
            plan, task_id,
            states[task_id].attempts + int(states[task_id].status is TaskLifecycle.PENDING),
        ) for task_id in wave.task_ids)
        for instance, reservation in zip(instances, wave.reservations, strict=True):
            if reservation.task_id != instance.task_id or reservation.idempotency_key != instance.idempotency_key or dict(reservation.budget) != instance.budget_snapshot.to_dict() or reservation.state.value != "RESERVED":
                raise HarnessValidationError("wave reservation differs from accepted attempt", code="task_plan_budget_identity_conflict")
        admitted = ledger.reserve(instances)
        if payload.get("budget_after_checksum") != admitted.to_dict()["ledger_checksum"]:
            raise HarnessValidationError("wave admission budget checksum differs", code="task_plan_budget_checksum_mismatch")
        expected = instances if wave.execution_mode == "SUPERVISED" else ()
        if len(events) - 1 != len(expected):
            raise HarnessValidationError("wave admission has incomplete spawn intents", code="task_plan_parallel_event_invalid")
        for instance, event in zip(expected, events[1:], strict=True):
            budget = child_budget_reservation(admitted, instance, group_id=group.group_id, wave_id=wave.wave_id)
            identity = {
                "group_id": group.group_id, "wave_id": wave.wave_id,
                "task_id": instance.task_id, "task_instance_id": instance.task_instance_id,
                "attempt": instance.attempt, "operation_key": budget["reservation_key"],
                "idempotency_key": budget["reservation_key"], "budget_reservation": budget,
            }
            if any(event.payload.get(name) != value for name, value in identity.items()):
                raise HarnessValidationError("spawn intent differs from wave ledger", code="task_plan_budget_identity_conflict")
        return instances

    @staticmethod
    def _dispatched_wave_instances(
        plan: ValidatedTaskPlan,
        event: TaskPlanEvent,
        history: tuple[TaskPlanEvent, ...],
        projection: TaskPlanProjection,
    ) -> tuple[TaskInstance, ...]:
        matching = [item for item in history if item.event_type == "TASK_WAVE_ADMITTED" and item.payload["wave"]["wave_id"] == event.payload.get("wave_id")]
        if len(matching) != 1:
            raise HarnessValidationError("dispatch has no unique admitted wave", code="task_plan_parallel_event_invalid")
        wave = DispatchWave.from_dict(thaw_mapping(matching[0].payload["wave"]))
        if tuple(event.payload.get("task_ids", ())) != wave.task_ids or event.payload.get("group_id") != wave.group_id:
            raise HarnessValidationError("dispatch differs from admitted wave", code="task_plan_parallel_event_invalid")
        states = {state.task_id: state for state in projection.tasks}
        instances = tuple(task_instance_for_attempt(plan, task_id, states[task_id].attempts) for task_id in wave.task_ids)
        if wave.execution_mode == "SUPERVISED":
            for instance in instances:
                receipts = [item for item in history if item.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED"
                            and item.payload.get("wave_id") == wave.wave_id and item.payload.get("task_id") == instance.task_id
                            and item.payload.get("task_instance_id") == instance.task_instance_id and item.payload.get("attempt") == instance.attempt
                            and item.payload.get("child_id")]
                if len(receipts) != 1:
                    raise HarnessValidationError("dispatch requires confirmed spawn receipts", code="task_plan_parallel_event_invalid")
        return instances

    def _recover_failed_task_retries(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        projection: Any,
    ) -> bool:
        """Complete a retry decision interrupted after result persistence.

        ``append_result`` durably records a failed attempt before the retry
        decision event is appended.  A process restart in that small window
        must re-use the same policy decision instead of treating the failed
        projection as an opaque blocker or invoking the worker directly.
        """

        definitions = {item.task_id: item for item in plan.tasks}
        for state in projection.tasks:
            if state.status is not TaskLifecycle.FAILED:
                continue
            definition = definitions[state.task_id]
            retryable_codes = set(
                definition.normalized_retry_policy.retryable_reason_codes
            )
            reason_code = state.failure_reason_code
            if reason_code not in retryable_codes:
                raise HarnessValidationError(
                    "task failure is outside the task retry policy",
                    code="task_plan_retry_not_allowed",
                    details={
                        "task_id": state.task_id,
                        "attempt": state.attempts,
                        "error_code": reason_code,
                        "retryable_reason_codes": sorted(retryable_codes),
                    },
                )
            if state.attempts >= definition.normalized_retry_policy.max_attempts:
                raise HarnessValidationError(
                    "task retry budget exhausted",
                    code="task_plan_retry_exhausted",
                    details={
                        "task_id": state.task_id,
                        "attempt": state.attempts,
                    },
                )
            result_checksum = _failed_result_checksum(
                self.store.read_events(request.run_id, request.stage_id),
                task_id=state.task_id,
                task_instance_id=state.active_instance_id,
                attempt=state.attempts,
            )
            if result_checksum is None:
                raise HarnessValidationError(
                    "failed TaskPlan retry is missing durable result evidence",
                    code="task_plan_retry_evidence_missing",
                    details={"task_id": state.task_id, "attempt": state.attempts},
                )
            current = self.store.load_projection(request.run_id, request.stage_id)
            retry_tasks = tuple(
                replace(
                    item,
                    status=TaskLifecycle.PENDING,
                    active_instance_id=None,
                    failure_reason_code=None,
                )
                if item.task_id == state.task_id
                else item
                for item in current.tasks
            )
            sequence = self._next_sequence(request)
            self.store.commit_event(
                TaskPlanEvent.for_plan(
                    "TASK_RETRY_SCHEDULED",
                    plan,
                    task_id=state.task_id,
                    task_instance_id=state.active_instance_id,
                    attempt=state.attempts,
                    input_checksum=result_checksum,
                    reason_code=reason_code,
                    sequence=sequence,
                ),
                replace(
                    current,
                    tasks=retry_tasks,
                    last_sequence=sequence,
                ),
            )
            self._persist_checkpoint(request, plan)
            return True
        return False

    def _recover_committed_subagent_results(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        projection: Any,
    ) -> bool:
        if self.worker_result_recovery is None:
            return False
        recovered_any = False
        definitions = {item.task_id: item for item in plan.tasks}
        for state in projection.tasks:
            definition = definitions[state.task_id]
            if definition.subagent_id is None or state.status not in {
                TaskLifecycle.DISPATCHED,
                TaskLifecycle.RUNNING,
            }:
                continue
            if state.active_instance_id is None or state.attempts <= 0:
                raise HarnessValidationError(
                    "active subagent task is missing attempt identity",
                    code="task_plan_recovery_attempt_missing",
                )
            instance = task_instance_for_attempt(
                plan,
                state.task_id,
                state.attempts,
                task_instance_id=state.active_instance_id,
            )
            binding = self.capability_registry.resolve(
                definition.task.worker_capability,
                request.policy,
            )
            candidate = (
                self.worker_result_recovery(binding, instance)
                if request.execution_identity is None
                else self.worker_result_recovery(
                    binding,
                    instance,
                    request.execution_identity,
                )
            )
            if candidate is None:
                raise HarnessValidationError(
                    "active subagent attempt has no committed outcome receipt",
                    code="task_plan_subagent_attempt_indeterminate",
                    details={"task_id": state.task_id, "attempt": state.attempts},
                )
            if not isinstance(candidate, HarnessWorkerResult):
                raise HarnessValidationError(
                    "subagent result recovery returned invalid evidence",
                    code="task_plan_result_invalid",
                )
            verified = self.result_verifier.verify(
                candidate,
                task=definition,
                request=TaskPlanResultVerificationRequest(
                    plan=plan,
                    task=definition,
                    instance=instance,
                    worker_result=candidate,
                    execution_identity=request.execution_identity,
                ),
            )
            self.store.append_result(verified)
            recovered_any = True
        return recovered_any

    def _commit_task_transition(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        event_type: str,
        projection: Any,
    ) -> None:
        sequence = self._next_sequence(request)
        self.store.commit_event(
            TaskPlanEvent.for_plan(
                event_type,
                plan,
                task_id=instance.task_id,
                task_instance_id=instance.task_instance_id,
                attempt=instance.attempt,
                input_checksum=instance.task_definition_checksum,
                sequence=sequence,
            ),
            replace(projection, last_sequence=sequence),
        )
        self._persist_checkpoint(request, plan)

    def _invoke(
        self,
        task_request: TaskInstance,
        plan: ValidatedTaskPlan,
        policy: Any,
        *,
        execution_identity: Any | None,
    ) -> TaskResultRecord:
        resolved = next(item for item in plan.tasks if item.task_id == task_request.task_id)
        worker_binding = self.capability_registry.resolve(resolved.task.worker_capability, policy)
        worker_result = self._call_binding(
            worker_binding,
            task_request,
            execution_identity=execution_identity,
        )
        if not isinstance(worker_result, HarnessWorkerResult):
            raise HarnessValidationError("dynamic worker returned invalid result", code="task_plan_result_invalid")
        verified = self.result_verifier.verify(
            worker_result,
            task=resolved,
            request=TaskPlanResultVerificationRequest(
                plan=plan,
                task=resolved,
                instance=task_request,
                worker_result=worker_result,
                execution_identity=execution_identity,
            ),
        )
        return verified

    def _call_binding(
        self,
        binding: Any,
        request: TaskInstance,
        *,
        execution_identity: Any | None,
    ) -> HarnessWorkerResult:
        if self.worker_executor is not None:
            value = (
                self.worker_executor(binding, request)
                if execution_identity is None
                else self.worker_executor(binding, request, execution_identity)
            )
        else:
            if (
                binding.registration.worker_binding.worker_type
                is HarnessWorkerType.SUBAGENT
            ):
                raise HarnessValidationError(
                    "dynamic subagent tasks require the resolved SubAgent runtime adapter",
                    code="task_plan_subagent_runtime_required",
                )
            implementation = binding.registration.worker_binding.implementation
            execute = getattr(implementation, "execute", None)
            if not callable(execute):
                raise HarnessValidationError("dynamic worker binding has no runtime executor", code="task_plan_binding_unavailable")
            value = execute({
                "task": request.to_dict(),
                "allowed_tools": list(binding.allowed_tools),
                "allowed_memory_namespaces": list(binding.allowed_memory_namespaces),
            })
        if isinstance(value, HarnessWorkerResult):
            return value
        if isinstance(value, dict):
            return HarnessWorkerResult(**value)
        raise HarnessValidationError("dynamic worker returned invalid result", code="task_plan_result_invalid")

    def _publish_metrics(self, plan: ValidatedTaskPlan | None) -> None:
        """Publish a derived inspection snapshot without changing run outcome."""

        if self.metrics_sink is None or plan is None:
            return
        try:
            projection = self.store.load_projection(plan.run_id, plan.stage_id)
            samples = task_plan_metric_samples(
                projection,
                plan,
                self.store.read_events(plan.run_id, plan.stage_id),
            )
            self.metrics_sink(samples)
        except Exception:
            # Metrics are strictly observational. A faulty exporter must not
            # rewrite a verified task-plan decision or turn a completed run
            # into a retryable execution failure.
            return

    def _persist_checkpoint(
        self,
        request: TaskPlanStageRequest,
        plan: ValidatedTaskPlan,
    ) -> None:
        """Persist a replay-derived checkpoint after a durable lifecycle fact."""

        if self.checkpoint_store is None:
            return
        report = self._replay_history(request, plan)
        checkpoint = TaskPlanCheckpoint.from_replay(
            f"checkpoint-{plan.plan_id}-{report.projection.last_sequence}",
            plan,
            report,
            created_at=_required_observation_time(request),
        )
        self.checkpoint_store.save(checkpoint)

    def _replay_history(
        self, request: TaskPlanStageRequest, plan: ValidatedTaskPlan
    ) -> TaskPlanReplayReport:
        events = self.store.read_events(request.run_id, request.stage_id)
        results = self.store.result_history_for(
            request.run_id,
            request.stage_id,
            plan.plan_id,
            plan.version,
        )
        plan_history = tuple(
            candidate
            for version in range(1, plan.version + 1)
            if (candidate := self.store.plan(request.run_id, request.stage_id, version))
            is not None
        )
        if not plan_history:
            raise HarnessValidationError(
                "TaskPlan history is missing its accepted plan",
                code="task_plan_replay_plan_missing",
            )
        patch_reader = getattr(self.store, "patches_for", None)
        patches = (
            tuple(patch_reader(request.run_id, request.stage_id))
            if callable(patch_reader)
            else ()
        )
        return TaskPlanReplayReducer().replay(
            plan_history,
            events,
            results=results,
            patches=patches,
            require_terminal_events=False,
            require_latest_plan=False,
            apply_unterminated_results=False,
        )

    def _halt(
        self, request: TaskPlanStageRequest, reason_code: str,
        *, result: HarnessWorkerResult | None = None,
    ) -> None:
        try:
            plan = self.store.plan(request.run_id, request.stage_id)
            diagnostic_ref = canonical_payload_checksum({"reason_code": reason_code})
            payload = {"diagnostic_ref": diagnostic_ref}
            if result is not None:
                payload.update(self._submission_result_payload(request, result))
            event = (
                TaskPlanEvent.for_plan(
                    "TASK_PLAN_HALTED",
                    plan,
                    reason_code=reason_code,
                    payload=payload,
                    sequence=self._next_sequence(request),
                )
                if plan is not None
                else TaskPlanEvent(
                    "TASK_PLAN_HALTED",
                    **_task_plan_event_identity_kwargs(request.stage_identity),
                    reason_code=reason_code,
                    payload=payload,
                    sequence=self._next_sequence(request),
                )
            )
            self.store.append_event(event)
        except Exception as exc:
            raise HarnessValidationError(
                "TaskPlan halt could not be durably committed",
                code="task_plan_halt_persistence_failed",
                details={
                    "run_id": request.run_id,
                    "stage_id": request.stage_id,
                    "plan_version": plan.version if "plan" in locals() and plan is not None else None,
                    "reason_code": reason_code,
                    "diagnostic_ref": canonical_payload_checksum({"reason_code": reason_code}),
                },
            ) from exc

    def _next_sequence(self, request: TaskPlanStageRequest) -> int:
        return len(self.store.read_events(request.run_id, request.stage_id)) + 1


def _required_observation_time(request: TaskPlanStageRequest) -> str:
    value = request.accepted_at
    if not isinstance(value, str) or not value.strip():
        raise HarnessValidationError(
            "TaskPlan stage requires an explicit accepted_at observation",
            code="task_plan_observation_time_required",
        )
    return value


def _registered_gate_refs(
    verifier: TaskPlanResultVerifierPort,
    *,
    fallback: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return only gate refs exposed by the concrete verifier registry.

    ``fallback`` remains a source-compatible argument for older callers, but
    policy allowlists are never treated as executable gate registration.
    """

    del fallback
    refs = getattr(verifier, "registered_gate_refs", None)
    if refs is None:
        return ()
    return tuple(refs)


def _failed_result_checksum(
    events: tuple[TaskPlanEvent, ...],
    *,
    task_id: str,
    task_instance_id: str | None,
    attempt: int,
) -> str | None:
    for event in reversed(events):
        if (
            event.event_type != "TASK_RESULT_REJECTED"
            or event.task_id != task_id
            or event.task_instance_id != task_instance_id
            or event.attempt != attempt
        ):
            continue
        value = event.payload.get("result_checksum")
        return value if isinstance(value, str) else None
    return None


def _require_plan_stage_binding(
    plan: ValidatedTaskPlan,
    request: TaskPlanStageRequest,
) -> None:
    if (
        not plan.matches_stage_identity(request.stage_identity)
        or plan.policy_ref != request.stage_binding.policy_ref
    ):
        raise HarnessValidationError(
            "existing TaskPlan is outside the frozen Graph stage binding",
            code="task_plan_pinned_version_mismatch",
            details={
                "expected_stage_binding_ref": request.stage_binding.binding_checksum,
                "expected_stage_identity": request.stage_identity.to_dict(),
                "actual_plan_identity": plan.to_dict(),
            },
        )


__all__ = ["TaskPlanStageRunner"]
