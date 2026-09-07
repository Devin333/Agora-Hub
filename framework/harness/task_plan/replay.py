from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from framework.harness.artifacts import ArtifactReferenceVerifierPort
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.transcript import SubAgentTranscriptStorePort
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    frozen_mapping,
    identifier,
    non_negative_int,
    reference,
    stable_text_tuple,
    task_reference_producer,
    thaw_mapping,
)
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import (
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskResultReference,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    TaskPlanScheduler,
    task_instance_for_attempt,
)
from framework.harness.task_plan.dependency import (
    TASK_BLOCKED_UPSTREAM_FAILURE,
    block_dependency_task,
    dependency_blocked_task_ids,
    dependency_blocking_predecessor_ids,
)
from framework.harness.task_plan.store import (
    TaskPlanEvent,
    TaskResultRecord,
    _projection_for_plan,
    _settle_result_budget,
)
from framework.harness.task_plan.models import PlanPatch
from framework.harness.task_plan.parallel_state import (
    DispatchGroupState,
    DispatchWaveState,
    DispatchWaveTerminalOutcome,
    validate_group_transition,
    validate_wave_transition,
)
from framework.harness.task_plan.submission import CandidateSubmission, submissions_from_events
from framework.harness.task_plan.submission_result import submission_result_from_event


TASK_PLAN_REPLAY_REDUCER_VERSION_V3 = "newsroom.harness-task-plan-replay/v3"
TASK_PLAN_REPLAY_REDUCER_VERSION = TASK_PLAN_REPLAY_REDUCER_VERSION_V3
TASK_PLAN_REPLAY_REDUCER_VERSIONS = (TASK_PLAN_REPLAY_REDUCER_VERSION_V3,)
_GRAPH_REPLAY_IDENTITY_FIELDS = (
    "graph_id",
    "graph_version",
    "graph_ref",
    "graph_schema_version",
    "compiler_version",
    "condition_policy_version",
    "graph_checksum",
    "stage_binding_checksum",
    "stage_identity_schema",
    "stage_identity_checksum",
)
_ACTIVE_TASK_STATES = frozenset(
    {TaskLifecycle.READY, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}
)
_TASK_RESULT_EVENTS = frozenset(
    {"TASK_RESULT_ACCEPTED", "TASK_RESULT_REJECTED"}
)
_TASK_TERMINAL_EVENTS = frozenset({"TASK_COMPLETED", "TASK_FAILED"})
_PARALLEL_EVENT_TYPES = frozenset(
    {
        "TASK_GROUP_ADMITTED",
        "TASK_WAVE_ADMITTED",
        "TASK_ATTEMPT_SPAWN_INTENT",
        "TASK_ATTEMPT_SPAWN_CONFIRMED",
        "TASK_ATTEMPT_SPAWN_UNKNOWN",
        "TASK_WAVE_DISPATCHED",
        "TASK_WAVE_COMPLETED",
        "TASK_GROUP_JOIN_WAITING",
        "TASK_GROUP_JOINED",
        "TASK_GROUP_FAILED",
        "TASK_GROUP_REPLAN_PENDING",
        "TASK_GROUP_CANCEL_REQUESTED",
        "TASK_GROUP_CANCELLED",
        "TASK_GROUP_INDETERMINATE",
        "TASK_GROUP_HALTED",
        "TASK_GROUP_SUPERSEDED",
        "TASK_GROUP_RECLAIMED",
        "TASK_GROUP_RECOVERY",
        "RECOVERY_STATUS_READ",
        "RECOVERY_RECONCILED",
        "RECOVERY_HALTED",
        "DEGRADED_SERIAL",
    }
)
_PARALLEL_GROUP_STATES = frozenset(item.value for item in DispatchGroupState)
_PARALLEL_WAVE_STATES = frozenset(item.value for item in DispatchWaveState)
_PARALLEL_WAVE_TERMINAL_OUTCOMES = frozenset(
    item.value for item in DispatchWaveTerminalOutcome
)
_PARALLEL_RESERVATION_STATES = frozenset({"RESERVED", "CONSUMED", "RELEASED"})


@dataclass(frozen=True, slots=True)
class TaskPlanReplayReport:
    projection: TaskPlanProjection
    active_task_instances: tuple[TaskInstance, ...]
    ready_order: tuple[str, ...]
    accepted_output_refs: tuple[str, ...]
    pending_terminal_results: tuple[TaskResultRecord, ...]
    retry_counts: Mapping[str, int]
    replan_count: int
    event_history_checksum: str
    aggregate_ref: str | None = None
    aggregate_checksum: str | None = None
    verified: bool = True
    reducer_version: str = TASK_PLAN_REPLAY_REDUCER_VERSION
    parallel_groups: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_waves: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_reservations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_diagnostics: tuple[Mapping[str, Any], ...] = ()
    parallel_event_sequence: int = 0
    replay_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        instances = tuple(self.active_task_instances)
        if any(not isinstance(item, TaskInstance) for item in instances):
            raise TypeError("active_task_instances must contain TaskInstance values")
        instance_ids = [item.task_instance_id for item in instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise HarnessValidationError(
                "TaskPlan replay contains duplicate active task instances",
                code="task_plan_replay_duplicate_instance",
            )
        ready_order = tuple(identifier(item, "ready_order") for item in self.ready_order)
        if len(ready_order) != len(set(ready_order)):
            raise HarnessValidationError(
                "TaskPlan replay ready order contains duplicates",
                code="task_plan_replay_duplicate_ready_task",
            )
        active_task_ids = {item.task_id for item in instances}
        if set(ready_order) != active_task_ids:
            raise HarnessValidationError(
                "TaskPlan replay ready order does not match active task instances",
                code="task_plan_replay_ready_order_mismatch",
            )
        output_refs = tuple(sorted(reference(item, "accepted_output_refs") for item in self.accepted_output_refs))
        if len(output_refs) != len(set(output_refs)):
            raise HarnessValidationError(
                "TaskPlan replay accepted output refs contain duplicates",
                code="task_plan_replay_duplicate_output_ref",
            )
        pending_results = tuple(self.pending_terminal_results)
        if any(not isinstance(item, TaskResultRecord) for item in pending_results):
            raise TypeError("pending_terminal_results must contain TaskResultRecord values")
        pending_identities = [
            (item.task_instance_id, item.attempt, item.plan_version)
            for item in pending_results
        ]
        if len(pending_identities) != len(set(pending_identities)):
            raise HarnessValidationError(
                "TaskPlan replay contains duplicate pending terminal results",
                code="task_plan_replay_result_conflict",
            )
        retry_counts = {
            identifier(task_id, "retry_counts.task_id"): non_negative_int(count, "retry_counts.count")
            for task_id, count in self.retry_counts.items()
        }
        expected_reducer_version = TASK_PLAN_REPLAY_REDUCER_VERSION_V3
        if self.reducer_version != expected_reducer_version:
            raise HarnessValidationError(
                "unsupported TaskPlan replay reducer",
                code="unsupported_task_plan_replay_reducer",
                details={
                    "expected": expected_reducer_version,
                    "reducer_version": str(self.reducer_version),
                },
            )
        if any(
            not item.matches_plan_projection_identity(self.projection)
            for item in instances
        ):
            raise HarnessValidationError(
                "TaskPlan replay active attempt identity does not match projection",
                code="task_plan_replay_identity_mismatch",
            )
        if any(
            not _result_matches_projection_identity(item, self.projection)
            for item in pending_results
        ):
            raise HarnessValidationError(
                "TaskPlan replay pending result identity does not match projection",
                code="task_plan_replay_result_mismatch",
            )
        object.__setattr__(self, "active_task_instances", instances)
        object.__setattr__(self, "ready_order", ready_order)
        object.__setattr__(self, "accepted_output_refs", output_refs)
        object.__setattr__(
            self,
            "pending_terminal_results",
            tuple(
                sorted(
                    pending_results,
                    key=lambda item: (
                        item.task_id,
                        item.attempt,
                        item.result_checksum,
                    ),
                )
            ),
        )
        object.__setattr__(self, "retry_counts", frozen_mapping(dict(sorted(retry_counts.items())), "retry_counts"))
        object.__setattr__(self, "replan_count", non_negative_int(self.replan_count, "replan_count"))
        object.__setattr__(self, "event_history_checksum", checksum(self.event_history_checksum, "event_history_checksum"))
        object.__setattr__(self, "aggregate_ref", reference(self.aggregate_ref, "aggregate_ref") if self.aggregate_ref else None)
        object.__setattr__(self, "aggregate_checksum", checksum(self.aggregate_checksum, "aggregate_checksum") if self.aggregate_checksum else None)
        if (self.aggregate_ref is None) != (self.aggregate_checksum is None):
            raise HarnessValidationError(
                "TaskPlan replay aggregate ref and checksum must be present together",
                code="task_plan_replay_aggregate_mismatch",
            )
        if not isinstance(self.verified, bool):
            raise TypeError("verified must be bool")
        parallel_groups = _freeze_parallel_projection_mapping(
            self.parallel_groups,
            "parallel_groups",
        )
        parallel_waves = _freeze_parallel_projection_mapping(
            self.parallel_waves,
            "parallel_waves",
        )
        parallel_reservations = _freeze_parallel_projection_mapping(
            self.parallel_reservations,
            "parallel_reservations",
        )
        diagnostics = tuple(
            frozen_mapping(item, "parallel_diagnostics.item")
            for item in self.parallel_diagnostics
        )
        if any(not isinstance(item, Mapping) for item in self.parallel_diagnostics):
            raise TypeError("parallel_diagnostics must contain mappings")
        _validate_parallel_report_projection(
            parallel_groups,
            parallel_waves,
            parallel_reservations,
        )
        object.__setattr__(self, "parallel_groups", parallel_groups)
        object.__setattr__(self, "parallel_waves", parallel_waves)
        object.__setattr__(self, "parallel_reservations", parallel_reservations)
        object.__setattr__(self, "parallel_diagnostics", diagnostics)
        parallel_event_sequence = non_negative_int(
            self.parallel_event_sequence,
            "parallel_event_sequence",
        )
        has_parallel_facts = bool(
            parallel_groups
            or parallel_waves
            or parallel_reservations
            or diagnostics
        )
        if has_parallel_facts and parallel_event_sequence < 1:
            raise HarnessValidationError(
                "parallel replay facts require an event sequence",
                code="task_plan_replay_parallel_sequence_missing",
            )
        if not has_parallel_facts and parallel_event_sequence:
            raise HarnessValidationError(
                "parallel replay event sequence requires replay facts",
                code="task_plan_replay_parallel_sequence_unexpected",
            )
        if parallel_event_sequence > self.projection.last_sequence:
            raise HarnessValidationError(
                "parallel replay event sequence exceeds projection history",
                code="task_plan_replay_parallel_sequence_mismatch",
            )
        object.__setattr__(self, "parallel_event_sequence", parallel_event_sequence)
        object.__setattr__(self, "replay_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        projection = {
            "reducer_version": self.reducer_version,
            "projection": self.projection.to_dict(),
            "active_task_instances": [item.to_dict() for item in self.active_task_instances],
            "ready_order": list(self.ready_order),
            "accepted_output_refs": list(self.accepted_output_refs),
            "pending_terminal_results": [
                item.to_dict() for item in self.pending_terminal_results
            ],
            "retry_counts": thaw_mapping(self.retry_counts),
            "replan_count": self.replan_count,
            "event_history_checksum": self.event_history_checksum,
            "aggregate_ref": self.aggregate_ref,
            "aggregate_checksum": self.aggregate_checksum,
            "verified": self.verified,
        }
        # Preserve the established v2 checksum for histories that predate
        # parallel orchestration. Parallel facts become checksum-relevant only
        # once a durable parallel event has actually been reduced.
        if self.parallel_groups or self.parallel_waves or self.parallel_reservations or self.parallel_diagnostics:
            projection["parallel_groups"] = {
                key: thaw_mapping(value)
                for key, value in self.parallel_groups.items()
            }
            projection["parallel_waves"] = {
                key: thaw_mapping(value)
                for key, value in self.parallel_waves.items()
            }
            projection["parallel_reservations"] = {
                key: thaw_mapping(value)
                for key, value in self.parallel_reservations.items()
            }
            projection["parallel_diagnostics"] = [
                thaw_mapping(item) for item in self.parallel_diagnostics
            ]
            projection["parallel_event_sequence"] = self.parallel_event_sequence
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "replay_checksum": self.replay_checksum}


class TaskPlanReplayReducer:
    """Pure reducer over recorded plans, events, and result references only."""

    def __init__(
        self,
        transcript_store: SubAgentTranscriptStorePort | None = None,
        *,
        artifact_reference_verifier: ArtifactReferenceVerifierPort | None = None,
    ) -> None:
        if transcript_store is not None and not isinstance(
            transcript_store,
            SubAgentTranscriptStorePort,
        ):
            raise TypeError("transcript_store must implement SubAgentTranscriptStorePort")
        self._transcript_store = transcript_store
        if artifact_reference_verifier is not None and not isinstance(
            artifact_reference_verifier,
            ArtifactReferenceVerifierPort,
        ):
            raise TypeError(
                "artifact_reference_verifier must implement "
                "ArtifactReferenceVerifierPort"
            )
        self._artifact_reference_verifier = artifact_reference_verifier

    def reduce(
        self,
        plan: ValidatedTaskPlan | Iterable[ValidatedTaskPlan],
        events: Iterable[TaskPlanEvent],
        *,
        results: Iterable[TaskResultRecord] = (),
        patches: Iterable[PlanPatch] = (),
        require_terminal_events: bool = False,
    ) -> TaskPlanProjection:
        plan_history = (plan,) if isinstance(plan, ValidatedTaskPlan) else tuple(plan)
        return self.replay(
            plan_history,
            events,
            results=results,
            patches=patches,
            require_terminal_events=require_terminal_events,
            apply_unterminated_results=False,
        ).projection

    def replay(
        self,
        plans: Iterable[ValidatedTaskPlan],
        events: Iterable[TaskPlanEvent],
        *,
        results: Iterable[TaskResultRecord] = (),
        patches: Iterable[PlanPatch] = (),
        through_sequence: int | None = None,
        require_terminal_events: bool = True,
        apply_unterminated_results: bool = False,
        require_latest_plan: bool = True,
    ) -> TaskPlanReplayReport:
        plan_history = _validated_plan_history(plans)
        plans_by_version = {item.version: item for item in plan_history}
        ordered_events = _validated_event_prefix(
            tuple(events),
            plan_history[0],
            through_sequence=through_sequence,
        )
        _validate_atomic_wave_intents(ordered_events)
        results_by_attempt = _validated_results(results, plan_history)
        patches_by_checksum = _validated_patches(patches, plan_history)
        submissions = submissions_from_events(ordered_events)
        admitted_submissions: dict[str, CandidateSubmission] = {}

        projection: TaskPlanProjection | None = None
        current_plan: ValidatedTaskPlan | None = None
        instances: dict[str, TaskInstance] = {}
        ready_sequences: dict[str, int] = {}
        pending_results: dict[tuple[str, int, int], TaskResultRecord] = {}
        failed_result_checksums: dict[tuple[str, int, int], str] = {}
        retry_counts: dict[str, int] = {}
        aggregate_ref: str | None = None
        aggregate_checksum: str | None = None
        aggregate_output_refs: tuple[str, ...] = ()
        aggregate_output_refs_by_role: dict[str, Any] = {}
        aggregate_branch_refs: tuple[Any, ...] = ()
        aggregate_projection_checksum: str | None = None
        accepted_patch: tuple[TaskPlanEvent, PlanPatch] | None = None
        parallel_groups: dict[str, dict[str, Any]] = {}
        parallel_waves: dict[str, dict[str, Any]] = {}
        parallel_reservations: dict[str, dict[str, Any]] = {}
        parallel_diagnostics: list[dict[str, Any]] = []
        parallel_spawn_operations: dict[str, dict[str, Any]] = {}
        parallel_event_sequence = 0
        ready_batch_budget_checksum: str | None = None
        ready_batch_tasks: list[str] = []

        for event in ordered_events:
            if event.event_type == "TASK_READY" and projection is not None:
                if not ready_batch_tasks:
                    ready_batch_budget_checksum = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget).to_dict()["ledger_checksum"]
                ready_batch_tasks.append(event.task_id)
            elif event.event_type != "TASK_WAVE_ADMITTED":
                ready_batch_budget_checksum = None
                ready_batch_tasks = []
            if accepted_patch is not None and event.event_type != "PLAN_ACCEPTED":
                raise HarnessValidationError(
                    "accepted TaskPlan patch is not followed by its new plan",
                    code="task_plan_replay_patch_mismatch",
                    details={"patch_ref": accepted_patch[1].patch_checksum},
                )
            if event.event_type == "PLAN_ACCEPTED":
                accepted = _accepted_plan_for_event(event, plans_by_version)
                if accepted.version == 1 and submissions:
                    matching = [
                        item for item in admitted_submissions.values()
                        if item.plan_id == accepted.plan_id
                    ]
                    if len(matching) != 1 or (
                        matching[0].candidate_ref != accepted.source_candidate_ref
                        or matching[0].accepted_at != accepted.accepted_at
                    ):
                        raise HarnessValidationError(
                            "accepted plan does not match its candidate submission",
                            code="task_plan_replay_candidate_mismatch",
                        )
                if current_plan is not None and accepted.version != current_plan.version + 1:
                    raise HarnessValidationError(
                        "TaskPlan replay plan versions are not monotonic",
                        code="task_plan_replay_plan_version_gap",
                        details={"previous": current_plan.version, "actual": accepted.version},
                    )
                if accepted_patch is not None:
                    patch_event, patch = accepted_patch
                    if (
                        event.sequence != patch_event.sequence + 1
                        or accepted.parent_plan_id != patch.base_plan_id
                        or accepted.source_candidate_ref != patch.patch_checksum
                        or accepted.version != patch.base_plan_version + 1
                    ):
                        raise HarnessValidationError(
                            "PLAN_ACCEPTED does not materialize the recorded patch",
                            code="task_plan_replay_patch_mismatch",
                            details={
                                "patch_ref": patch.patch_checksum,
                                "plan_version": accepted.version,
                            },
                        )
                    accepted_patch = None
                projection = _projection_for_plan(
                    accepted,
                    sequence=event.sequence,
                    previous=projection,
                )
                current_plan = accepted
                continue

            if event.event_type in {
                "PLAN_CANDIDATE_BUILT",
                "PLAN_CANDIDATE_REJECTED",
                "PLAN_VALIDATION_FAILED",
            }:
                _validate_candidate_event(event)
                if event.event_type == "PLAN_CANDIDATE_BUILT":
                    raw_submission = event.payload.get("submission")
                    if raw_submission is not None:
                        submission = CandidateSubmission.from_dict(raw_submission)
                        admitted_submissions[submission.identity.dedup_key] = submission
            elif event.event_type in {
                "PLAN_PATCH_PROPOSED",
                "PLAN_PATCH_REJECTED",
                "PLAN_PATCH_ACCEPTED",
            }:
                _validate_patch_event(event)
                patch_ref = thaw_mapping(event.payload).get("patch_ref")
                patch = patches_by_checksum.get(patch_ref)
                if patch is None:
                    raise HarnessValidationError(
                        "TaskPlan replay is missing patch evidence",
                        code="task_plan_replay_patch_missing",
                        details={"patch_ref": patch_ref},
                    )
                if (
                    event.plan_id != patch.base_plan_id
                    or event.plan_version != patch.base_plan_version
                    or event.run_id != patch.run_id
                    or event.stage_id != patch.stage_id
                ):
                    raise HarnessValidationError(
                        "TaskPlan patch event identity does not match recorded patch",
                        code="task_plan_replay_patch_mismatch",
                        details={"patch_ref": patch.patch_checksum},
                    )
                base_plan = plans_by_version.get(patch.base_plan_version)
                if base_plan is None or base_plan.plan_id != patch.base_plan_id:
                    raise HarnessValidationError(
                        "TaskPlan patch base plan evidence is unavailable",
                        code="task_plan_replay_patch_mismatch",
                        details={"patch_ref": patch.patch_checksum},
                    )
                if current_plan is None or current_plan.version != patch.base_plan_version:
                    raise HarnessValidationError(
                        "TaskPlan patch event does not target the current plan",
                        code="task_plan_replay_patch_mismatch",
                        details={"patch_ref": patch.patch_checksum},
                    )
                if event.event_type == "PLAN_PATCH_ACCEPTED":
                    if accepted_patch is not None:
                        raise HarnessValidationError(
                            "TaskPlan replay contains an unmaterialized accepted patch",
                            code="task_plan_replay_patch_mismatch",
                        )
                    if current_plan is None or current_plan.version != patch.base_plan_version:
                        raise HarnessValidationError(
                            "TaskPlan accepted patch does not target the current plan",
                            code="task_plan_replay_patch_mismatch",
                        )
                    accepted_patch = (event, patch)
            elif event.event_type in {
                "TASK_READY",
                "TASK_DISPATCHED",
                "TASK_STARTED",
            }:
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                instance = _instance_for_event(event, task_plan)
                _require_projection_task_definition(projection, instance)
                if event.event_type == "TASK_READY":
                    projection = TaskPlanScheduler().reserve_ready_tasks(
                        projection,
                        TaskPlanReadyDecision((instance,)),
                    )
                    ready_sequences[instance.task_instance_id] = event.sequence
                    instances[instance.task_instance_id] = instance
                elif event.event_type == "TASK_DISPATCHED":
                    _require_recorded_instance(instances, instance, event)
                    projection = TaskPlanScheduler.mark_dispatched(projection, instance)
                else:
                    _require_recorded_instance(instances, instance, event)
                    projection = TaskPlanScheduler.mark_started(projection, instance)
            elif event.event_type in _TASK_RESULT_EVENTS:
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                instance = _instance_for_event(event, task_plan)
                _require_recorded_instance(instances, instance, event)
                result = _result_for_event(
                    event,
                    results_by_attempt,
                    task_plan,
                    transcript_store=self._transcript_store,
                    artifact_reference_verifier=self._artifact_reference_verifier,
                )
                pending_results[(instance.task_instance_id, instance.attempt, task_plan.version)] = result
            elif event.event_type in _TASK_TERMINAL_EVENTS:
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                instance = _instance_for_event(event, task_plan)
                _require_recorded_instance(instances, instance, event)
                result = pending_results.pop(
                    (instance.task_instance_id, instance.attempt, task_plan.version),
                    None,
                )
                if result is None:
                    raise HarnessValidationError(
                        "TaskPlan terminal event is missing committed result evidence",
                        code="task_plan_replay_result_missing",
                        details={"task_id": instance.task_id, "event_type": event.event_type},
                    )
                projection = _apply_terminal_result(
                    projection,
                    event,
                    result,
                    plan=task_plan,
                )
                if result.status is TaskLifecycle.FAILED:
                    failed_result_checksums[
                        (instance.task_instance_id, instance.attempt, task_plan.version)
                    ] = result.result_checksum
                ready_sequences.pop(instance.task_instance_id, None)
            elif event.event_type == "TASK_RETRY_SCHEDULED":
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                instance = _instance_for_event(event, task_plan)
                projection = _schedule_retry(
                    projection,
                    event,
                    instance,
                    task_plan,
                    failed_result_checksum=failed_result_checksums.get(
                        (instance.task_instance_id, instance.attempt, task_plan.version)
                    ),
                )
                ready_sequences.pop(instance.task_instance_id, None)
                retry_counts[instance.task_id] = retry_counts.get(instance.task_id, 0) + 1
            elif event.event_type == "TASK_REPLACED":
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                projection = _apply_replacement(
                    projection,
                    event,
                    task_plan,
                    base_plan=plans_by_version.get(task_plan.version - 1),
                )
            elif event.event_type in {
                "TASK_BLOCKED",
                "TASK_BLOCKED_UPSTREAM_FAILURE",
                "TASK_SKIPPED",
            }:
                projection = _require_projection(projection, event)
                task_plan = _task_plan_for_event(event, plans_by_version)
                projection = _apply_non_result_terminal(projection, event, plan=task_plan)
            elif event.event_type == "STAGE_OUTPUT_AGGREGATED":
                projection = _require_projection(projection, event)
                aggregate_projection_checksum = projection.projection_checksum
                if (
                    event.plan_id != projection.plan_id
                    or event.plan_version != projection.plan_version
                ):
                    raise HarnessValidationError(
                        "TaskPlan aggregation event references a stale plan",
                        code="task_plan_replay_aggregate_mismatch",
                    )
                (
                    aggregate_ref,
                    aggregate_checksum,
                    aggregate_output_refs,
                    _aggregate_result_refs,
                    aggregate_branch_refs,
                ) = _aggregate_for_event(event)
                aggregate_output_refs_by_role = thaw_mapping(event.payload["output_refs_by_role"])
            elif event.event_type == "TASK_PLAN_VERIFIED":
                if submissions or any(key in event.payload for key in ("submission_key", "terminal_result", "terminal_result_checksum")):
                    _require_terminal_submission(event, admitted_submissions, plan_history[0])
                    terminal = submission_result_from_event(event)
                    expected_output = {
                        "aggregate_ref": aggregate_ref,
                        "aggregate_checksum": aggregate_checksum,
                        "output_refs_by_role": aggregate_output_refs_by_role,
                        "analysis_branch_refs": list(aggregate_branch_refs),
                    }
                    expected_diagnostics = {
                        "plan_id": event.plan_id,
                        "plan_version": event.plan_version,
                        "projection_checksum": aggregate_projection_checksum,
                    }
                    if (
                        terminal.output != expected_output
                        or terminal.diagnostics != expected_diagnostics
                    ):
                        raise HarnessValidationError(
                            "recorded submission result differs from the gated aggregate",
                            code="task_plan_submission_result_invalid",
                        )
                projection = _require_projection(projection, event)
                if (
                    event.plan_id != projection.plan_id
                    or event.plan_version != projection.plan_version
                    or aggregate_checksum is None
                    or event.input_checksum != aggregate_checksum
                ):
                    raise HarnessValidationError(
                        "TaskPlan verification is missing matching aggregate evidence",
                        code="task_plan_replay_aggregate_mismatch",
                    )
                if tuple(event.output_refs) != aggregate_output_refs:
                    raise HarnessValidationError(
                        "TaskPlan verification output refs do not match aggregation",
                        code="task_plan_replay_aggregate_mismatch",
                    )
            elif event.event_type in _PARALLEL_EVENT_TYPES:
                projection = _require_projection(projection, event)
                if event.event_type == "TASK_WAVE_ADMITTED":
                    before = ready_batch_budget_checksum or TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget).to_dict()["ledger_checksum"]
                    if event.payload.get("budget_before_checksum") != before or not set(ready_batch_tasks).issubset(event.payload.get("wave", {}).get("task_ids", ())):
                        _parallel_error("wave admission budget predecessor differs from history", event)
                _apply_parallel_event(
                    event,
                    projection,
                    parallel_groups,
                    parallel_waves,
                    parallel_reservations,
                    parallel_diagnostics,
                    parallel_spawn_operations,
                )
                parallel_event_sequence = event.sequence
            elif event.event_type == "TASK_PLAN_HALTED":
                if submissions or any(key in event.payload for key in ("submission_key", "terminal_result", "terminal_result_checksum")):
                    _require_terminal_submission(event, admitted_submissions, plan_history[0])
                    submission_result_from_event(event)
            else:
                raise HarnessValidationError(
                    "TaskPlan replay encountered an unsupported event",
                    code="task_plan_replay_unknown_event",
                    details={"event_type": event.event_type},
                )

            if projection is not None:
                projection = replace(projection, last_sequence=event.sequence)

        if projection is None or current_plan is None:
            raise HarnessValidationError(
                "TaskPlan replay history has no accepted plan",
                code="task_plan_replay_plan_missing",
            )
        if require_latest_plan and current_plan.version != plan_history[-1].version:
            raise HarnessValidationError(
                "TaskPlan replay history is missing the latest accepted plan",
                code="task_plan_replay_plan_missing",
                details={"expected_version": plan_history[-1].version, "actual_version": current_plan.version},
            )
        if accepted_patch is not None:
            raise HarnessValidationError(
                "TaskPlan replay ends before the accepted patch plan",
                code="task_plan_replay_patch_mismatch",
            )
        pending_report: tuple[TaskResultRecord, ...] = ()
        if pending_results:
            if require_terminal_events:
                raise HarnessValidationError(
                    "TaskPlan replay history ends before a result terminal event",
                    code="task_plan_replay_terminal_event_missing",
                    details={"task_instance_ids": sorted({key[0] for key in pending_results})},
                )
            if apply_unterminated_results:
                raise HarnessValidationError(
                    "TaskPlan replay requires durable terminal events",
                    code="task_plan_replay_terminal_event_missing",
                    details={"task_instance_ids": sorted({key[0] for key in pending_results})},
                )
            pending_report = tuple(pending_results.values())

        active_states = {
            item.active_instance_id: item
            for item in projection.tasks
            if item.status in _ACTIVE_TASK_STATES and item.active_instance_id is not None
        }
        missing_instances = sorted(set(active_states) - set(instances))
        if missing_instances:
            raise HarnessValidationError(
                "TaskPlan replay projection is missing active attempt evidence",
                code="task_plan_replay_attempt_missing",
                details={"task_instance_ids": missing_instances},
            )
        active_instances = tuple(
            instances[instance_id]
            for instance_id in sorted(
                active_states,
                key=lambda instance_id: (
                    ready_sequences.get(instance_id, 2**63 - 1),
                    active_states[instance_id].task_id,
                    instance_id,
                ),
            )
        )
        ready_order = tuple(item.task_id for item in active_instances)
        accepted_output_refs = tuple(
            sorted(
                item.result.result_ref
                for item in projection.tasks
                if item.status is TaskLifecycle.SUCCEEDED and item.result is not None
            )
        )
        history_checksum = canonical_payload_checksum(
            {"event_checksums": [event.event_checksum for event in ordered_events]}
        )
        return TaskPlanReplayReport(
            projection=projection,
            active_task_instances=active_instances,
            ready_order=ready_order,
            accepted_output_refs=accepted_output_refs,
            pending_terminal_results=pending_report,
            retry_counts=retry_counts,
            replan_count=max(current_plan.version - 1, 0),
            event_history_checksum=history_checksum,
            aggregate_ref=aggregate_ref,
            aggregate_checksum=aggregate_checksum,
            reducer_version=TASK_PLAN_REPLAY_REDUCER_VERSION_V3,
            parallel_groups=parallel_groups,
            parallel_waves=parallel_waves,
            parallel_reservations=parallel_reservations,
            parallel_diagnostics=tuple(parallel_diagnostics),
            parallel_event_sequence=parallel_event_sequence,
        )

    def decision_checksum(self, projection: TaskPlanProjection) -> str:
        return canonical_payload_checksum(projection.checksum_projection())


def _freeze_parallel_projection_mapping(
    value: Mapping[str, Mapping[str, Any]],
    field_name: str,
) -> Mapping[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    normalized: dict[str, Mapping[str, Any]] = {}
    for key, item in value.items():
        normalized[identifier(key, f"{field_name}.key")] = frozen_mapping(
            item,
            f"{field_name}.item",
        )
    if len(normalized) != len(value):
        raise HarnessValidationError(
            "parallel replay projection contains duplicate identifiers",
            code="task_plan_replay_parallel_conflict",
        )
    return frozen_mapping(dict(sorted(normalized.items())), field_name)


def _validate_parallel_report_projection(
    groups: Mapping[str, Mapping[str, Any]],
    waves: Mapping[str, Mapping[str, Any]],
    reservations: Mapping[str, Mapping[str, Any]],
) -> None:
    for group_id, group in groups.items():
        if thaw_mapping(group).get("group_id") != group_id:
            raise HarnessValidationError(
                "parallel group projection key does not match payload",
                code="task_plan_replay_parallel_identity_mismatch",
            )
        if thaw_mapping(group).get("state") not in _PARALLEL_GROUP_STATES:
            raise HarnessValidationError(
                "parallel group projection has invalid state",
                code="task_plan_replay_parallel_state_mismatch",
            )
    for wave_id, wave in waves.items():
        payload = thaw_mapping(wave)
        if payload.get("wave_id") != wave_id or payload.get("group_id") not in groups:
            raise HarnessValidationError(
                "parallel wave projection identity is invalid",
                code="task_plan_replay_parallel_identity_mismatch",
            )
        if payload.get("state") not in _PARALLEL_WAVE_STATES:
            raise HarnessValidationError(
                "parallel wave projection has invalid state",
                code="task_plan_replay_parallel_state_mismatch",
            )
        outcome = payload.get("terminal_outcome")
        if payload.get("state") == DispatchWaveState.TERMINAL.value:
            if outcome not in _PARALLEL_WAVE_TERMINAL_OUTCOMES:
                raise HarnessValidationError(
                    "terminal parallel wave projection is missing typed outcome",
                    code="task_plan_replay_parallel_state_mismatch",
                )
        elif outcome is not None:
            raise HarnessValidationError(
                "non-terminal parallel wave projection has terminal outcome",
                code="task_plan_replay_parallel_state_mismatch",
            )
    for reservation_id, reservation in reservations.items():
        payload = thaw_mapping(reservation)
        if payload.get("reservation_id") != reservation_id or payload.get("wave_id") not in waves:
            raise HarnessValidationError(
                "parallel reservation projection identity is invalid",
                code="task_plan_replay_parallel_identity_mismatch",
            )
        if payload.get("state") not in _PARALLEL_RESERVATION_STATES:
            raise HarnessValidationError(
                "parallel reservation projection has invalid state",
                code="task_plan_replay_parallel_state_mismatch",
            )
        if "reservation_checksum" in payload or "schema_version" in payload:
            _validate_reservation_checksum(payload)
    for wave in waves.values():
        for embedded in thaw_mapping(wave).get("reservations", ()):
            if isinstance(embedded, Mapping) and (
                "reservation_checksum" in embedded or "schema_version" in embedded
            ):
                _validate_reservation_checksum(embedded)


def _validate_atomic_wave_intents(events: tuple[TaskPlanEvent, ...]) -> None:
    admitted_intents: set[int] = set()
    for index, event in enumerate(events):
        if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT" and index not in admitted_intents:
            _parallel_error("spawn intent is outside atomic wave admission", event)
        if event.event_type != "TASK_WAVE_ADMITTED":
            continue
        wave = event.payload.get("wave", {})
        tasks = tuple(wave.get("task_ids", ())) if wave.get("execution_mode") == "SUPERVISED" else ()
        intents = events[index + 1:index + 1 + len(tasks)]
        if len(intents) != len(tasks) or any(
            intent.event_type != "TASK_ATTEMPT_SPAWN_INTENT"
            or intent.payload.get("wave_id") != wave.get("wave_id")
            or intent.payload.get("task_id") != task_id
            for intent, task_id in zip(intents, tasks, strict=True)
        ):
            _parallel_error("wave admission is missing its complete intent batch", event)
        admitted_intents.update(range(index + 1, index + 1 + len(tasks)))


def _apply_parallel_event(
    event: TaskPlanEvent,
    projection: TaskPlanProjection,
    groups: dict[str, dict[str, Any]],
    waves: dict[str, dict[str, Any]],
    reservations: dict[str, dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    spawn_operations: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Reduce orchestration facts without letting them mutate task outcomes.

    Task lifecycle remains owned by the existing result events.  These facts
    supply the durable admission/join/reservation history needed to explain
    and safely recover a parallel dispatch group.
    """
    spawn_operations = spawn_operations if spawn_operations is not None else {}
    payload = thaw_mapping(event.payload)
    group_payload = payload.get("group")
    group_id = _parallel_group_id(payload, group_payload)
    if event.event_type == "TASK_GROUP_ADMITTED":
        if not isinstance(group_payload, Mapping):
            _parallel_error("group admission is missing its snapshot", event)
        group = _normalize_parallel_group(group_payload, projection, event)
        if group["state"] != DispatchGroupState.ADMITTED.value:
            _parallel_error("group admission snapshot is not admitted", event)
        if group_id in groups:
            _parallel_error("parallel group was admitted more than once", event)
        groups[group_id] = group
        return

    group = groups.get(group_id)
    if group is None:
        _parallel_error("parallel event references an unknown group", event)
    if isinstance(group_payload, Mapping):
        snapshot = _normalize_parallel_group(group_payload, projection, event)
        _require_same_parallel_group(group, snapshot, event)

    if event.event_type.startswith("RECOVERY_"):
        _apply_spawn_recovery_audit(event, payload, group, waves, spawn_operations, diagnostics)
        return

    if event.event_type in {
        "TASK_ATTEMPT_SPAWN_INTENT",
        "TASK_ATTEMPT_SPAWN_CONFIRMED",
        "TASK_ATTEMPT_SPAWN_UNKNOWN",
    }:
        wave_id = _parallel_identifier(payload.get("wave_id"), "wave_id", event)
        wave = waves.get(wave_id)
        if wave is None or wave["group_id"] != group_id:
            _parallel_error("spawn event references an unknown wave", event)
        if wave["execution_mode"] != "SUPERVISED":
            _parallel_error("serial wave cannot contain supervisor spawn events", event)
        task_id = _parallel_identifier(payload.get("task_id"), "task_id", event)
        if task_id not in wave["task_ids"]:
            _parallel_error("spawn event task is outside wave scope", event)
        task_instance_id = _parallel_identifier(payload.get("task_instance_id"), "task_instance_id", event)
        attempt = payload.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            _parallel_error("spawn event attempt is invalid", event)
        operation_key = payload.get("operation_key")
        if not isinstance(operation_key, str) or not operation_key.strip():
            _parallel_error("spawn event operation key is missing", event)
        from framework.harness.task_plan.parallel import spawn_operation_key

        if operation_key != spawn_operation_key(group_id, wave_id, task_instance_id, attempt):
            _parallel_error("spawn operation key differs from its attempt identity", event)
        if payload.get("idempotency_key") != operation_key:
            _parallel_error("spawn idempotency key differs from its operation key", event)
        key = f"{wave_id}:{task_instance_id}:{attempt}"
        existing = spawn_operations.get(key)
        identity = {
            "group_id": group_id, "wave_id": wave_id, "task_id": task_id,
            "task_instance_id": task_instance_id, "attempt": attempt,
            "operation_key": operation_key,
        }
        if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT":
            budget_reservation = payload.get("budget_reservation")
            if not isinstance(budget_reservation, Mapping):
                _parallel_error("spawn intent is missing its budget reservation", event)
            if existing is not None:
                if any(existing[name] != value for name, value in identity.items()):
                    _parallel_error("spawn intent conflicts with recorded identity", event)
                if existing.get("budget_reservation") != thaw_mapping(frozen_mapping(budget_reservation, "spawn_budget_reservation")):
                    _parallel_error("spawn intent conflicts with budget reservation", event)
                return
            _require_parallel_state(wave["state"], {DispatchWaveState.ADMITTED.value}, event)
            task = next((item for item in projection.tasks if item.task_id == task_id), None)
            if task is None or task.active_instance_id != task_instance_id or task.attempts != attempt:
                _parallel_error("spawn intent differs from admitted task attempt", event)
            _validate_spawn_budget_reservation(budget_reservation, operation_key, event)
            from framework.harness.task_plan.parallel import child_budget_reservation

            ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
            record = next((record for record in ledger.records.values() if record["instance"]["task_instance_id"] == task_instance_id), None)
            if record is None or record["status"] != "RESERVED":
                _parallel_error("spawn intent has no outstanding ledger reservation", event)
            expected_budget = child_budget_reservation(ledger, TaskInstance.from_dict(record["instance"]), group_id=group_id, wave_id=wave_id)
            if budget_reservation != expected_budget:
                _parallel_error("spawn intent budget differs from attempt ledger", event)
            if any(item["wave_id"] == wave_id and item["task_id"] == task_id for item in spawn_operations.values()):
                _parallel_error("wave task has multiple spawn attempts", event)
            spawn_operations[key] = {
                **identity,
                "budget_reservation": thaw_mapping(frozen_mapping(budget_reservation, "spawn_budget_reservation")),
                "status": "INTENT",
            }
            return
        if existing is None or any(existing[name] != value for name, value in identity.items()):
            _parallel_error("spawn receipt has no matching intent", event)
        status = payload.get("spawn_status")
        expected_status = (
            "SPAWN_CONFIRMED"
            if event.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED"
            else "SPAWN_UNKNOWN"
        )
        if status != expected_status:
            _parallel_error("spawn receipt status does not match event type", event)
        if status == "SPAWN_CONFIRMED":
            _parallel_identifier(payload.get("child_id"), "child_id", event)
        elif payload.get("child_id") is not None:
            _parallel_error("unknown spawn receipt must not invent a child id", event)
        if existing["status"] != "INTENT":
            if existing["status"] == status and existing.get("child_id") == payload.get("child_id"):
                return
            _parallel_error("spawn receipt conflicts with recorded evidence", event)
        existing["status"] = status
        existing["child_id"] = payload.get("child_id")
        return

    if event.event_type == "TASK_WAVE_ADMITTED":
        wave_payload = payload.get("wave")
        if not isinstance(wave_payload, Mapping):
            _parallel_error("wave admission is missing its snapshot", event)
        wave = _normalize_parallel_wave(wave_payload, group, event)
        if wave["state"] != DispatchWaveState.ADMITTED.value:
            _parallel_error("wave admission snapshot is not admitted", event)
        wave_id = wave["wave_id"]
        ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
        if payload.get("budget_after_checksum") != ledger.to_dict()["ledger_checksum"] or group["budget_envelope"] != dict(ledger.parent_allocation):
            _parallel_error("wave admission budget differs from ledger", event)
        states = {state.task_id: state for state in projection.tasks}
        for reservation in wave_payload.get("reservations", ()):
            record = ledger.records.get(reservation["idempotency_key"])
            state = states.get(reservation["task_id"])
            if record is None or record["status"] != "RESERVED" or state is None or state.status is not TaskLifecycle.READY:
                _parallel_error("wave task has no ready ledger reservation", event)
            instance = record["instance"]
            if instance["task_id"] != state.task_id or instance["task_instance_id"] != state.active_instance_id or instance["attempt"] != state.attempts or instance["budget_snapshot"] != reservation["budget"]:
                _parallel_error("wave reservation differs from attempt ledger", event)
        if wave_id in waves:
            _parallel_error("parallel wave was admitted more than once", event)
        if any(
            reservation["group_id"] == group_id
            and reservation["task_id"] in set(wave["task_ids"])
            for reservation in reservations.values()
        ):
            _parallel_error("parallel task received duplicate reservation", event)
        waves[wave_id] = wave
        for reservation in wave_payload.get("reservations", ()):
            reservation_state = _normalize_parallel_reservation(reservation, wave, event)
            reservations[reservation_state["reservation_id"]] = reservation_state
        _transition_parallel_group(group, DispatchGroupState.DISPATCHING, event)
        return

    if event.event_type in {"TASK_WAVE_DISPATCHED", "TASK_WAVE_COMPLETED"}:
        wave_id = _parallel_identifier(payload.get("wave_id"), "wave_id", event)
        wave = waves.get(wave_id)
        if wave is None or wave["group_id"] != group_id:
            _parallel_error("parallel wave does not belong to group", event)
        event_tasks = _parallel_task_ids(payload.get("task_ids"), event)
        if tuple(event_tasks) != tuple(wave["task_ids"]):
            _parallel_error("parallel wave task scope differs from admission", event)
        if event.event_type == "TASK_WAVE_DISPATCHED":
            for task_id in wave["task_ids"] if wave["execution_mode"] == "SUPERVISED" else ():
                matching = [
                    item
                    for item in spawn_operations.values()
                    if item.get("group_id") == group_id
                    and item.get("wave_id") == wave_id
                    and item.get("task_id") == task_id
                    and item.get("status") == "SPAWN_CONFIRMED"
                    and item.get("child_id")
                ]
                if len(matching) != 1:
                    _parallel_error("wave dispatch is missing a confirmed per-task spawn receipt", event)
            _require_parallel_state(wave["state"], {DispatchWaveState.ADMITTED.value}, event)
            _transition_parallel_wave(wave, DispatchWaveState.RUNNING, event)
            _transition_parallel_group(group, DispatchGroupState.RUNNING, event)
            return
        _require_parallel_state(
            wave["state"],
            {DispatchWaveState.ADMITTED.value, DispatchWaveState.RUNNING.value},
            event,
        )
        reservation_state = payload.get("reservation_state")
        reservation_states = payload.get("reservation_states")
        if reservation_states is not None:
            if (
                not isinstance(reservation_states, Mapping)
                or set(reservation_states) != set(wave["task_ids"])
                or any(value not in _PARALLEL_RESERVATION_STATES for value in reservation_states.values())
            ):
                _parallel_error("wave completion has invalid reservation states", event)
        elif reservation_state not in _PARALLEL_RESERVATION_STATES:
            _parallel_error("wave completion has invalid reservation state", event)
        terminal_outcome = payload.get("terminal_outcome")
        if terminal_outcome not in _PARALLEL_WAVE_TERMINAL_OUTCOMES:
            _parallel_error("wave completion is missing typed terminal outcome", event)
        resolved_reservation_states = {
            task_id: (
                reservation_states[task_id]
                if reservation_states is not None
                else reservation_state
            )
            for task_id in wave["task_ids"]
        }
        _validate_parallel_terminal_outcome(
            terminal_outcome,
            wave,
            resolved_reservation_states,
            payload.get("child_states"),
            event,
        )
        _transition_parallel_wave(wave, DispatchWaveState.TERMINAL, event)
        wave["terminal_outcome"] = terminal_outcome
        _transition_parallel_group(group, DispatchGroupState.RUNNING, event)
        _update_parallel_wave_reservations(
            wave,
            reservations,
            resolved_reservation_states,
            event,
        )
        return

    if event.event_type == "TASK_GROUP_JOIN_WAITING":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.JOINING,
            event,
        )
        _transition_parallel_group(group, DispatchGroupState.JOINING, event)
        _record_parallel_observation(payload, group, diagnostics, event)
        return
    if event.event_type == "TASK_GROUP_JOINED":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.SUCCEEDED,
            event,
        )
        _require_parallel_terminal_waves(group_id, waves, event)
        _require_parallel_successful_tasks(group, projection, event)
        if group["state"] is not DispatchGroupState.JOINING.value:
            _transition_parallel_group(group, DispatchGroupState.JOINING, event)
        _transition_parallel_group(
            group,
            DispatchGroupState.SUCCEEDED,
            event,
            allow_same=False,
        )
        _record_parallel_observation(payload, group, diagnostics, event)
        return
    if event.event_type == "TASK_GROUP_FAILED":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.FAILED,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.FAILED,
            event,
            allow_same=False,
        )
    elif event.event_type == "TASK_GROUP_REPLAN_PENDING":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.REPLAN_PENDING,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.REPLAN_PENDING,
            event,
            allow_same=False,
        )
    elif event.event_type == "TASK_GROUP_CANCEL_REQUESTED":
        _require_parallel_state(
            group["state"],
            {
                DispatchGroupState.ADMITTED.value,
                DispatchGroupState.DISPATCHING.value,
                DispatchGroupState.RUNNING.value,
                DispatchGroupState.JOINING.value,
            },
            event,
        )
        diagnostics.append(_parallel_diagnostic(event, group_id, payload))
        return
    elif event.event_type == "TASK_GROUP_CANCELLED":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.CANCELLED,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.CANCELLED,
            event,
            allow_same=False,
        )
        _release_parallel_group_reservations(group_id, waves, reservations, event)
    elif event.event_type == "TASK_GROUP_INDETERMINATE":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.INDETERMINATE,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.INDETERMINATE,
            event,
            allow_same=False,
        )
        # Unknown external outcomes are not evidence that resources are free.
        # Only a recorded per-reservation settlement can release this charge.
    elif event.event_type == "TASK_GROUP_HALTED":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.HALTED,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.HALTED,
            event,
            allow_same=False,
        )
        _release_parallel_group_reservations(group_id, waves, reservations, event)
    elif event.event_type == "TASK_GROUP_SUPERSEDED":
        _require_group_snapshot_target(
            group_payload,
            group,
            DispatchGroupState.SUPERSEDED,
            event,
        )
        _transition_parallel_group(
            group,
            DispatchGroupState.SUPERSEDED,
            event,
            allow_same=False,
        )
        _release_parallel_group_reservations(group_id, waves, reservations, event)
    elif event.event_type == "TASK_GROUP_RECLAIMED":
        task_ids = payload.get("task_ids")
        if task_ids is None:
            # Historical events predate per-attempt reclaim details and were
            # necessarily group-wide. New events are precise so a crash after
            # one reclaimed child cannot release a sibling reservation.
            _release_parallel_group_reservations(group_id, waves, reservations, event)
        else:
            reclaimed_ids = set(_parallel_task_ids(task_ids, event))
            for reservation in reservations.values():
                if (
                    reservation["group_id"] == group_id
                    and reservation["task_id"] in reclaimed_ids
                    and reservation["state"] == "RESERVED"
                ):
                    _set_parallel_reservation_state(
                        reservation,
                        "RELEASED",
                        waves,
                        event,
                    )
    elif event.event_type == "TASK_GROUP_RECOVERY":
        _apply_parallel_recovery(payload, group, projection, event)
        diagnostics.append(_parallel_diagnostic(event, group_id, payload))
        return
    elif event.event_type == "DEGRADED_SERIAL":
        diagnostics.append(_parallel_diagnostic(event, group_id, payload))
        return
    else:
        _parallel_error("unsupported parallel event", event)
    diagnostics.append(_parallel_diagnostic(event, group_id, payload))


def _apply_spawn_recovery_audit(
    event: TaskPlanEvent,
    payload: Mapping[str, Any],
    group: dict[str, Any],
    waves: dict[str, dict[str, Any]],
    operations: dict[str, dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> None:
    wave_id = _parallel_identifier(payload.get("wave_id"), "wave_id", event)
    wave = waves.get(wave_id)
    instance_id = _parallel_identifier(payload.get("task_instance_id"), "task_instance_id", event)
    attempt = payload.get("attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        _parallel_error("recovery audit attempt is invalid", event)
    operation = operations.get(f"{wave_id}:{instance_id}:{attempt}")
    if wave is None or wave["group_id"] != group["group_id"] or operation is None:
        _parallel_error("recovery audit has no admitted spawn intent", event)
    if any(payload.get(name) != operation[name] for name in (
        "group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key",
    )):
        _parallel_error("recovery audit identity differs from spawn intent", event)
    recovery_id = _parallel_identifier(payload.get("recovery_id"), "recovery_id", event)
    suffix = {
        "RECOVERY_STATUS_READ": "status-read",
        "RECOVERY_RECONCILED": "reconciled",
        "RECOVERY_HALTED": "halted",
    }[event.event_type]
    if payload.get("idempotency_key") != f"{recovery_id}:{suffix}":
        _parallel_error("recovery audit idempotency key is invalid", event)
    reads = operation.setdefault("recovery_reads", {})
    prior = reads.get(recovery_id)
    if event.event_type == "RECOVERY_STATUS_READ":
        if payload.get("recovery_outcome") != "status_read":
            _parallel_error("recovery status read outcome is invalid", event)
        if prior is not None:
            _parallel_error("recovery status call was recorded more than once", event)
        if any(recovery_id in item.get("recovery_reads", {}) for item in operations.values()):
            _parallel_error("recovery status identity belongs to another operation", event)
        reads[recovery_id] = "REQUESTED"
        operation["latest_recovery_id"] = recovery_id
        operation["latest_recovery_outcome"] = "REQUESTED"
    else:
        if prior != "REQUESTED":
            _parallel_error("recovery decision has no unmatched status read", event)
        if event.event_type == "RECOVERY_RECONCILED":
            if (
                payload.get("recovery_outcome") != "SPAWN_CONFIRMED"
                or operation["status"] != "SPAWN_CONFIRMED"
                or payload.get("child_id") != operation.get("child_id")
            ):
                _parallel_error("recovery confirmation has no matching child receipt", event)
            reads[recovery_id] = "SPAWN_CONFIRMED"
            if operation.get("latest_recovery_id") == recovery_id:
                operation["latest_recovery_outcome"] = "SPAWN_CONFIRMED"
            confirmed = [item for item in operations.values() if item["wave_id"] == wave_id]
            if (
                group["state"] == DispatchGroupState.INDETERMINATE.value
                and wave["state"] in {DispatchWaveState.ADMITTED.value, DispatchWaveState.RUNNING.value}
                and {item["task_id"] for item in confirmed} == set(wave["task_ids"])
                and all(item["status"] == "SPAWN_CONFIRMED" and
                        item.get("latest_recovery_outcome") == "SPAWN_CONFIRMED" for item in confirmed)
            ):
                group["state"] = (DispatchGroupState.RUNNING.value if wave["state"] == DispatchWaveState.RUNNING.value
                                  else DispatchGroupState.DISPATCHING.value)
        else:
            reason = payload.get("reason_code")
            if reason not in {"SPAWN_UNKNOWN", "SPAWN_IDENTITY_CONFLICT", "CHILD_NOT_TRACKABLE"}:
                _parallel_error("recovery halt reason is invalid", event)
            if reason == "SPAWN_UNKNOWN" and operation["status"] != "SPAWN_UNKNOWN":
                _parallel_error("unknown recovery decision has no unknown receipt", event)
            reads[recovery_id] = reason
            if operation.get("latest_recovery_id") == recovery_id:
                operation["latest_recovery_outcome"] = reason
    diagnostics.append({
        "event_type": event.event_type, "sequence": event.sequence,
        **{name: payload[name] for name in (
            "group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key", "recovery_id",
        )},
        **{name: payload[name] for name in ("child_id", "recovery_outcome", "reason_code") if name in payload},
        "audit_checksum": canonical_payload_checksum(payload),
    })


def _normalize_parallel_group(
    raw: Mapping[str, Any],
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
) -> dict[str, Any]:
    value = thaw_mapping(frozen_mapping(raw, "parallel_group"))
    required = {
        "schema_version", "group_id", "group_checksum", "run_id", "stage_id", "plan_id",
        "plan_version", "task_ids", "required_output_roles", "join_policy",
        "max_waves", "max_parallelism", "budget_envelope", "correlation_id", "state",
    }
    if (
        not required.issubset(value)
        or set(value) - required
        or value.get("schema_version") != "agora.harness-dispatch-group/v1"
    ):
        _parallel_error("parallel group snapshot has unexpected fields", event)
    for field_name, expected in (
        ("run_id", projection.run_id),
        ("stage_id", projection.stage_id),
        ("plan_id", projection.plan_id),
        ("plan_version", projection.plan_version),
    ):
        if value.get(field_name) != expected:
            _parallel_error("parallel group does not match plan identity", event)
    group_id = _parallel_identifier(value.get("group_id"), "group_id", event)
    task_ids = _parallel_task_ids(value.get("task_ids"), event)
    known_task_ids = {item.task_id for item in projection.tasks}
    if not set(task_ids).issubset(known_task_ids):
        _parallel_error("parallel group references unknown task", event)
    if value.get("state") not in _PARALLEL_GROUP_STATES:
        _parallel_error("parallel group has invalid state", event)
    if not isinstance(value.get("group_checksum"), str) or not value["group_checksum"].startswith("sha256:"):
        _parallel_error("parallel group checksum is invalid", event)
    for field_name in ("max_waves", "max_parallelism"):
        if isinstance(value.get(field_name), bool) or not isinstance(value.get(field_name), int) or value[field_name] < 1:
            _parallel_error("parallel group limits are invalid", event)
    if not isinstance(value.get("budget_envelope"), Mapping):
        _parallel_error("parallel group budget is invalid", event)
    group_checksum_payload = {
        field_name: value[field_name]
        for field_name in (
            "schema_version", "run_id", "stage_id", "plan_id", "plan_version",
            "task_ids", "required_output_roles", "join_policy", "max_waves",
            "max_parallelism", "budget_envelope", "correlation_id",
        )
        if field_name in value
    }
    expected_checksum = canonical_payload_checksum(group_checksum_payload)
    if value["group_checksum"] != expected_checksum or group_id != f"dg_{expected_checksum.removeprefix('sha256:')[:32]}":
        _parallel_error("parallel group checksum does not match its snapshot", event)
    return {
        **value,
        "group_id": group_id,
        "task_ids": list(task_ids),
        "state": value["state"],
    }


def _require_same_parallel_group(
    current: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    event: TaskPlanEvent,
) -> None:
    immutable = set(current) - {"state"}
    if any(current.get(field_name) != snapshot.get(field_name) for field_name in immutable):
        _parallel_error("parallel group snapshot changed after admission", event)


def _normalize_parallel_wave(
    raw: Mapping[str, Any],
    group: Mapping[str, Any],
    event: TaskPlanEvent,
) -> dict[str, Any]:
    value = thaw_mapping(frozen_mapping(raw, "parallel_wave"))
    required = {
        "schema_version",
        "wave_id", "group_id", "ordinal", "task_ids", "effective_parallelism",
        "reservations", "state", "terminal_outcome", "execution_mode",
    }
    if (
        not required.issubset(value)
        or set(value) - required
        or value.get("schema_version") != "agora.harness-dispatch-wave/v3"
        or value.get("group_id") != group["group_id"]
    ):
        _parallel_error("parallel wave identity is invalid", event)
    wave_id = _parallel_identifier(value.get("wave_id"), "wave_id", event)
    task_ids = _parallel_task_ids(value.get("task_ids"), event)
    if not set(task_ids).issubset(set(group["task_ids"])):
        _parallel_error("parallel wave exceeds group task scope", event)
    if isinstance(value.get("ordinal"), bool) or not isinstance(value.get("ordinal"), int) or value["ordinal"] < 1:
        _parallel_error("parallel wave ordinal is invalid", event)
    if isinstance(value.get("effective_parallelism"), bool) or not isinstance(value.get("effective_parallelism"), int) or value["effective_parallelism"] < 1:
        _parallel_error("parallel wave capacity is invalid", event)
    if value["effective_parallelism"] > group["max_parallelism"]:
        _parallel_error("parallel wave exceeds group capacity", event)
    if value["execution_mode"] not in {"SUPERVISED", "SERIAL", "INLINE_TEST"}:
        _parallel_error("parallel wave execution mode is invalid", event)
    if value["execution_mode"] == "SERIAL" and (value["effective_parallelism"] != 1 or len(task_ids) != 1):
        _parallel_error("serial wave must have one task and slot", event)
    if value.get("state") not in _PARALLEL_WAVE_STATES or not isinstance(value.get("reservations"), list):
        _parallel_error("parallel wave snapshot is invalid", event)
    wave_outcome = value.get("terminal_outcome")
    wave_outcomes = {
        "SUCCEEDED", "PARTIAL_FAILED", "FAILED", "CANCELLED", "INDETERMINATE",
        "RECLAIMED", "DEADLINE_EXCEEDED",
    }
    if value["state"] == "TERMINAL":
        if wave_outcome not in wave_outcomes:
            _parallel_error("terminal parallel wave is missing typed outcome", event)
    elif wave_outcome is not None:
        _parallel_error("non-terminal parallel wave has a terminal outcome", event)
    if {item.get("task_id") for item in value["reservations"] if isinstance(item, Mapping)} != set(task_ids) or len(value["reservations"]) != len(task_ids):
        _parallel_error("parallel wave reservations do not cover task scope", event)
    expected_wave_id = canonical_payload_checksum(
        {
            "schema_version": value.get("schema_version"),
            "group_id": value["group_id"],
            "ordinal": value["ordinal"],
            "task_ids": value["task_ids"],
            "effective_parallelism": value["effective_parallelism"],
            "execution_mode": value["execution_mode"],
            "reservations": [
                {
                    "task_id": item["task_id"],
                    "idempotency_key": item["idempotency_key"],
                    "budget": item["budget"],
                    **({"capacity_allocations": item["capacity_allocations"]} if "capacity_allocations" in item else {}),
                    **({"capacity_policy_checksums": item["capacity_policy_checksums"]} if "capacity_policy_checksums" in item else {}),
                }
                for item in value["reservations"]
            ],
        }
    )
    if wave_id != f"dw_{expected_wave_id.removeprefix('sha256:')[:32]}":
        _parallel_error("parallel wave id does not match its snapshot", event)
    return {
        **value,
        "wave_id": wave_id,
        "task_ids": list(task_ids),
        "state": value["state"],
        "terminal_outcome": wave_outcome,
    }


def _apply_parallel_recovery(
    payload: Mapping[str, Any],
    group: dict[str, Any],
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
) -> None:
    """Validate a recovery transition without replaying worker side effects."""

    if payload.get("recovery_outcome") != "receipts_reconciled":
        _parallel_error("parallel recovery outcome is invalid", event)
    if group["state"] == DispatchGroupState.INDETERMINATE.value:
        # A recovery event is the sole audited exception to terminal-state
        # monotonicity: it proves a held supervisor receipt was reconciled.
        group_payload_state = thaw_mapping(payload.get("group", {})).get("state")
        if group_payload_state != DispatchGroupState.RUNNING.value:
            _parallel_error("indeterminate recovery must target RUNNING", event)
    else:
        _require_parallel_state(
            group["state"],
            {
                DispatchGroupState.ADMITTED.value,
                DispatchGroupState.DISPATCHING.value,
                DispatchGroupState.RUNNING.value,
                DispatchGroupState.JOINING.value,
            },
            event,
        )
    raw_results = payload.get("recovered_results")
    if not isinstance(raw_results, list):
        _parallel_error("parallel recovery is missing recovered results", event)
    expected_task_ids = set(group["task_ids"])
    task_states = {item.task_id: item for item in projection.tasks}
    seen: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, Mapping):
            _parallel_error("parallel recovered result is invalid", event)
        value = thaw_mapping(frozen_mapping(raw, "parallel_recovered_result"))
        required = {
            "task_id",
            "task_instance_id",
            "attempt",
            "status",
            "result_checksum",
        }
        if set(value) != required:
            _parallel_error("parallel recovered result has unexpected fields", event)
        task_id = _parallel_identifier(value.get("task_id"), "recovered_result.task_id", event)
        _parallel_identifier(
            value.get("task_instance_id"),
            "recovered_result.task_instance_id",
            event,
        )
        if task_id not in expected_task_ids or task_id in seen:
            _parallel_error("parallel recovery task scope is invalid", event)
        seen.add(task_id)
        if value.get("status") != TaskLifecycle.SUCCEEDED.value:
            _parallel_error("parallel recovery must contain successful results", event)
        if isinstance(value.get("attempt"), bool) or not isinstance(value.get("attempt"), int) or value["attempt"] < 1:
            _parallel_error("parallel recovered attempt is invalid", event)
        if not isinstance(value.get("result_checksum"), str) or not value["result_checksum"].startswith("sha256:"):
            _parallel_error("parallel recovered checksum is invalid", event)
        task = task_states.get(task_id)
        if (
            task is None
            or task.status is not TaskLifecycle.SUCCEEDED
            or task.result is None
            or task.result.result_checksum != value["result_checksum"]
        ):
            _parallel_error("parallel recovery does not match task projection", event)
    if group["state"] == DispatchGroupState.INDETERMINATE.value:
        group["state"] = DispatchGroupState.RUNNING.value
    else:
        _transition_parallel_group(group, DispatchGroupState.RUNNING, event)


def _normalize_parallel_reservation(
    raw: Any,
    wave: Mapping[str, Any],
    event: TaskPlanEvent,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _parallel_error("parallel reservation is invalid", event)
    value = thaw_mapping(frozen_mapping(raw, "parallel_reservation"))
    required = {"schema_version", "task_id", "idempotency_key", "budget", "state", "reservation_checksum"}
    allowed = required | {"capacity_allocations", "capacity_policy_checksums"}
    if (
        not required.issubset(value)
        or set(value) - allowed
        or value.get("schema_version") != "agora.harness-task-reservation/v1"
    ):
        _parallel_error("parallel reservation has unexpected fields", event)
    task_id = _parallel_identifier(value.get("task_id"), "reservation.task_id", event)
    if task_id not in wave["task_ids"] or value.get("state") not in _PARALLEL_RESERVATION_STATES:
        _parallel_error("parallel reservation does not match its wave", event)
    if not isinstance(value.get("idempotency_key"), str) or not value["idempotency_key"].strip() or not isinstance(value.get("budget"), Mapping):
        _parallel_error("parallel reservation payload is invalid", event)
    if "capacity_allocations" in value and not isinstance(value["capacity_allocations"], Mapping):
        _parallel_error("parallel capacity allocation payload is invalid", event)
    if "capacity_policy_checksums" in value and not isinstance(value["capacity_policy_checksums"], Mapping):
        _parallel_error("parallel capacity policy checksum payload is invalid", event)
    if "capacity_allocations" in value:
        allocations = value["capacity_allocations"]
        if not allocations:
            _parallel_error("parallel capacity allocations must not be empty", event)
        policy_checksums = value.get("capacity_policy_checksums")
        if not isinstance(policy_checksums, Mapping) or set(policy_checksums) != set(allocations):
            _parallel_error("parallel capacity policy evidence does not match allocations", event)
        for pool_id, quantity in allocations.items():
            try:
                identifier(str(pool_id), "reservation.pool_id")
            except HarnessValidationError:
                _parallel_error("parallel capacity allocation pool id is invalid", event)
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                _parallel_error("parallel capacity allocation quantity is invalid", event)
            try:
                checksum(policy_checksums[pool_id], "reservation.capacity_policy_checksum")
            except (HarnessValidationError, KeyError):
                _parallel_error("parallel capacity policy checksum is invalid", event)
    elif "capacity_policy_checksums" in value:
        _parallel_error("parallel capacity policy evidence has no allocations", event)
    if "reservation_checksum" in value:
        checksum_payload = {
                field_name: value[field_name]
                for field_name in ("schema_version", "task_id", "idempotency_key", "budget", "state")
                if field_name in value
            }
        if "capacity_allocations" in value:
            checksum_payload["capacity_allocations"] = value["capacity_allocations"]
        if "capacity_policy_checksums" in value:
            checksum_payload["capacity_policy_checksums"] = value["capacity_policy_checksums"]
        expected_checksum = canonical_payload_checksum(checksum_payload)
        if value["reservation_checksum"] != expected_checksum:
            _parallel_error("parallel reservation checksum does not match its snapshot", event)
    return {
        **value,
        "reservation_id": f"{wave['wave_id']}:{task_id}",
        "wave_id": wave["wave_id"],
        "group_id": wave["group_id"],
        "task_id": task_id,
    }


def _parallel_group_id(payload: Mapping[str, Any], group_payload: Any) -> str:
    source = group_payload if isinstance(group_payload, Mapping) else payload
    value = source.get("group_id")
    if not isinstance(value, str) or not value.strip():
        raise HarnessValidationError(
            "parallel event is missing group id",
            code="task_plan_replay_parallel_identity_mismatch",
        )
    return identifier(value, "parallel_group_id")


def _parallel_identifier(value: Any, name: str, event: TaskPlanEvent) -> str:
    if not isinstance(value, str) or not value.strip():
        _parallel_error(f"parallel event is missing {name}", event)
    return identifier(value, name)


def _parallel_task_ids(value: Any, event: TaskPlanEvent) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        _parallel_error("parallel task scope is invalid", event)
    task_ids = tuple(_parallel_identifier(item, "parallel_task_id", event) for item in value)
    if len(task_ids) != len(set(task_ids)):
        _parallel_error("parallel task scope contains duplicates", event)
    return tuple(sorted(task_ids))


def _require_parallel_state(current: str, allowed: set[str], event: TaskPlanEvent) -> None:
    if current not in allowed:
        _parallel_error("parallel event has invalid state transition", event)


def _transition_parallel_group(
    group: dict[str, Any],
    target: DispatchGroupState,
    event: TaskPlanEvent,
    *,
    allow_same: bool = True,
) -> None:
    current = group.get("state")
    if not allow_same and current == target.value:
        _parallel_error("parallel group repeated a terminal transition", event)
    try:
        validate_group_transition(current, target)
    except HarnessValidationError:
        _parallel_error("parallel group has invalid state transition", event)
    group["state"] = target.value


def _transition_parallel_wave(
    wave: dict[str, Any],
    target: DispatchWaveState,
    event: TaskPlanEvent,
    *,
    allow_same: bool = True,
) -> None:
    current = wave.get("state")
    if not allow_same and current == target.value:
        _parallel_error("parallel wave repeated a terminal transition", event)
    try:
        validate_wave_transition(current, target)
    except HarnessValidationError:
        _parallel_error("parallel wave has invalid state transition", event)
    wave["state"] = target.value


def _require_group_snapshot_target(
    group_payload: Any,
    group: Mapping[str, Any],
    target: DispatchGroupState,
    event: TaskPlanEvent,
) -> None:
    if not isinstance(group_payload, Mapping):
        _parallel_error("stateful group event is missing its snapshot", event)
    if group_payload.get("state") != target.value:
        _parallel_error("group snapshot state does not match event target", event)


def _require_parallel_successful_tasks(
    group: Mapping[str, Any],
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
) -> None:
    states = {item.task_id: item.status for item in projection.tasks}
    if any(states.get(task_id) is not TaskLifecycle.SUCCEEDED for task_id in group["task_ids"]):
        _parallel_error("parallel group joined successfully with unfinished or failed task", event)


def _validate_parallel_terminal_outcome(
    outcome: str,
    wave: Mapping[str, Any],
    reservation_states: Mapping[str, str],
    child_states: Any,
    event: TaskPlanEvent,
) -> None:
    if child_states is not None:
        if not isinstance(child_states, Mapping) or not set(child_states).issubset(set(wave["task_ids"])):
            _parallel_error("wave completion child states do not match task scope", event)
        if any(value not in {item.value for item in TaskLifecycle} for value in child_states.values()):
            _parallel_error("wave completion contains invalid child state", event)
        succeeded = sum(value == TaskLifecycle.SUCCEEDED.value for value in child_states.values())
        failed = sum(value == TaskLifecycle.FAILED.value for value in child_states.values())
    else:
        succeeded = failed = 0
    released = sum(value == "RELEASED" for value in reservation_states.values())
    consumed = sum(value == "CONSUMED" for value in reservation_states.values())
    if outcome == DispatchWaveTerminalOutcome.SUCCEEDED.value:
        if child_states is not None and succeeded != len(wave["task_ids"]):
            _parallel_error("successful wave outcome disagrees with child states", event)
        if consumed != len(wave["task_ids"]):
            _parallel_error("successful wave outcome has unreconciled reservations", event)
    elif outcome == DispatchWaveTerminalOutcome.PARTIAL_FAILED.value:
        if child_states is not None and not (succeeded and failed):
            _parallel_error("partial-failed outcome disagrees with child states", event)
    elif outcome == DispatchWaveTerminalOutcome.FAILED.value:
        if child_states is not None and (failed == 0 or succeeded):
            _parallel_error("failed outcome disagrees with child states", event)
    elif outcome == DispatchWaveTerminalOutcome.RECLAIMED.value:
        if child_states is not None and failed != len(wave["task_ids"]):
            _parallel_error("reclaimed outcome disagrees with child states", event)
        if released != len(wave["task_ids"]):
            _parallel_error("reclaimed outcome has unreleased reservations", event)
    elif outcome in {
        DispatchWaveTerminalOutcome.CANCELLED.value,
        DispatchWaveTerminalOutcome.DEADLINE_EXCEEDED.value,
    } and released != len(wave["task_ids"]):
        _parallel_error("cancelled wave outcome has unreleased reservations", event)


def _validate_reservation_checksum(payload: Mapping[str, Any]) -> None:
    supplied = payload.get("reservation_checksum")
    if not isinstance(supplied, str):
        raise HarnessValidationError(
            "parallel reservation checksum is missing",
            code="task_plan_replay_parallel_state_mismatch",
        )
    checksum_payload = {
        field_name: payload[field_name]
        for field_name in ("schema_version", "task_id", "idempotency_key", "budget", "state")
        if field_name in payload
    }
    if "capacity_allocations" in payload:
        checksum_payload["capacity_allocations"] = payload["capacity_allocations"]
    if "capacity_policy_checksums" in payload:
        checksum_payload["capacity_policy_checksums"] = payload["capacity_policy_checksums"]
    expected = canonical_payload_checksum(checksum_payload)
    if supplied != expected:
        raise HarnessValidationError(
            "parallel reservation checksum does not match state",
            code="task_plan_replay_parallel_state_mismatch",
        )


def _set_parallel_reservation_state(
    reservation: dict[str, Any],
    state: str,
    waves: Mapping[str, Mapping[str, Any]],
    event: TaskPlanEvent,
) -> None:
    reservation["state"] = state
    checksum_payload = {
        field_name: reservation[field_name]
        for field_name in ("schema_version", "task_id", "idempotency_key", "budget", "state")
    }
    if "capacity_allocations" in reservation:
        checksum_payload["capacity_allocations"] = reservation["capacity_allocations"]
    if "capacity_policy_checksums" in reservation:
        checksum_payload["capacity_policy_checksums"] = reservation["capacity_policy_checksums"]
    reservation["reservation_checksum"] = canonical_payload_checksum(checksum_payload)
    wave_id = reservation.get("wave_id")
    wave = waves.get(wave_id)
    if wave is None:
        _parallel_error("parallel reservation references unknown wave", event)
    for embedded in wave.get("reservations", ()):
        if isinstance(embedded, dict) and embedded.get("task_id") == reservation.get("task_id"):
            embedded["state"] = state
            embedded["reservation_checksum"] = reservation["reservation_checksum"]


def _update_parallel_wave_reservations(
    wave: dict[str, Any],
    reservations: Mapping[str, dict[str, Any]],
    states: Mapping[str, str],
    event: TaskPlanEvent,
) -> None:
    for task_id, state in states.items():
        reservation_id = f"{wave['wave_id']}:{task_id}"
        reservation = reservations.get(reservation_id)
        if reservation is None:
            _parallel_error("wave completion references an unknown reservation", event)
        _set_parallel_reservation_state(reservation, state, {wave["wave_id"]: wave}, event)


def _require_parallel_terminal_waves(
    group_id: str,
    waves: Mapping[str, Mapping[str, Any]],
    event: TaskPlanEvent,
) -> None:
    if any(wave["group_id"] == group_id and wave["state"] != "TERMINAL" for wave in waves.values()):
        _parallel_error("parallel group joined before all waves were terminal", event)


def _release_parallel_group_reservations(
    group_id: str,
    waves: Mapping[str, Mapping[str, Any]],
    reservations: Mapping[str, dict[str, Any]],
    event: TaskPlanEvent,
) -> None:
    for reservation in reservations.values():
        if reservation["group_id"] == group_id and reservation["state"] == "RESERVED":
            _set_parallel_reservation_state(
                reservation,
                "RELEASED",
                waves,
                event,
            )


def _record_parallel_observation(
    payload: Mapping[str, Any],
    group: Mapping[str, Any],
    diagnostics: list[dict[str, Any]],
    event: TaskPlanEvent,
) -> None:
    group_id = group["group_id"]
    observation = payload.get("observation")
    if observation is not None:
        if not isinstance(observation, Mapping) or observation.get("group_id") != group_id:
            _parallel_error("parallel observation does not match group", event)
        diagnostics.append({"event_type": event.event_type, "group_id": group_id, "observation": thaw_mapping(frozen_mapping(observation, "parallel_observation"))})


def _parallel_diagnostic(
    event: TaskPlanEvent,
    group_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    value: dict[str, Any] = {"event_type": event.event_type, "group_id": group_id, "sequence": event.sequence}
    reason = payload.get("reason_code") or event.reason_code
    if isinstance(reason, str) and reason:
        value["reason_code"] = reason
    return value


def _parallel_error(message: str, event: TaskPlanEvent) -> None:
    raise HarnessValidationError(
        message,
        code="task_plan_replay_parallel_mismatch",
        details={"event_type": event.event_type, "sequence": event.sequence},
    )


def _validate_spawn_budget_reservation(
    value: Mapping[str, Any],
    operation_key: str,
    event: TaskPlanEvent,
) -> None:
    required = {
        "schema_version", "ledger_version", "owner_scope", "reservation_key",
        "parent_allocation", "attempt_allocation", "reservation_checksum",
    }
    if not required.issubset(value):
        _parallel_error("spawn budget reservation is incomplete", event)
    if value.get("schema_version") != "agora.harness-budget-reservation/v1":
        _parallel_error("spawn budget reservation schema is invalid", event)
    if value.get("reservation_key") != operation_key:
        _parallel_error("spawn budget reservation key differs from operation key", event)
    if (
        isinstance(value.get("ledger_version"), bool)
        or not isinstance(value.get("ledger_version"), int)
        or value["ledger_version"] < 1
        or not isinstance(value.get("owner_scope"), str)
        or not value["owner_scope"].strip()
        or not isinstance(value.get("parent_allocation"), Mapping)
        or not isinstance(value.get("attempt_allocation"), Mapping)
    ):
        _parallel_error("spawn budget reservation metadata is invalid", event)
    supplied = value.get("reservation_checksum")
    if not isinstance(supplied, str) or not supplied.startswith("sha256:"):
        _parallel_error("spawn budget reservation checksum is invalid", event)
    expected = canonical_payload_checksum(
        {key: item for key, item in value.items() if key != "reservation_checksum"}
    )
    if supplied != expected:
        _parallel_error("spawn budget reservation checksum does not match its snapshot", event)


def _validated_plan_history(
    plans: Iterable[ValidatedTaskPlan],
) -> tuple[ValidatedTaskPlan, ...]:
    history = tuple(plans)
    if not history or any(not isinstance(item, ValidatedTaskPlan) for item in history):
        raise HarnessValidationError(
            "TaskPlan replay requires accepted plan history",
            code="task_plan_replay_plan_missing",
        )
    ordered = tuple(sorted(history, key=lambda item: item.version))
    versions = [item.version for item in ordered]
    if versions != list(range(1, len(ordered) + 1)):
        raise HarnessValidationError(
            "TaskPlan replay plan history is not contiguous",
            code="task_plan_replay_plan_version_gap",
            details={"versions": versions},
        )
    first = ordered[0]
    for index, plan in enumerate(ordered):
        if (
            plan.run_id != first.run_id
            or plan.stage_id != first.stage_id
            or plan.graph_checksum != first.graph_checksum
            or plan.policy_ref != first.policy_ref
            or plan.policy_checksum != first.policy_checksum
            or not _same_graph_replay_identity(plan, first)
        ):
            raise HarnessValidationError(
                "TaskPlan replay plan history has incompatible pinned identity",
                code="task_plan_replay_identity_mismatch",
            )
        expected_parent = None if index == 0 else ordered[index - 1].plan_id
        if plan.parent_plan_id != expected_parent:
            raise HarnessValidationError(
                "TaskPlan replay plan parent history is invalid",
                code="task_plan_replay_plan_parent_mismatch",
                details={"plan_version": plan.version},
            )
    return ordered


def _validated_event_prefix(
    events: tuple[TaskPlanEvent, ...],
    identity: ValidatedTaskPlan,
    *,
    through_sequence: int | None,
) -> tuple[TaskPlanEvent, ...]:
    if through_sequence is not None:
        through_sequence = non_negative_int(through_sequence, "through_sequence")
        if through_sequence == 0:
            raise HarnessValidationError(
                "TaskPlan replay sequence must include an accepted plan",
                code="task_plan_replay_plan_missing",
            )
    ordered = tuple(event for event in events if through_sequence is None or event.sequence <= through_sequence)
    if through_sequence is not None and (not ordered or ordered[-1].sequence != through_sequence):
        raise HarnessValidationError(
            "TaskPlan replay history does not reach the requested sequence",
            code="task_plan_replay_sequence_gap",
            details={"through_sequence": through_sequence},
        )
    for expected_sequence, event in enumerate(ordered, start=1):
        if not isinstance(event, TaskPlanEvent):
            raise TypeError("events must contain TaskPlanEvent values")
        if event.sequence != expected_sequence:
            raise HarnessValidationError(
                "TaskPlan event sequence is not contiguous",
                code="task_plan_replay_sequence_gap",
                details={"expected": expected_sequence, "actual": event.sequence},
            )
        expected_checksum = canonical_payload_checksum(event.to_dict(include_checksum=False))
        if event.event_checksum != expected_checksum:
            raise HarnessValidationError(
                "TaskPlan event checksum does not match recorded content",
                code="task_plan_replay_event_checksum_mismatch",
                details={"sequence": event.sequence},
            )
        if not event.matches_contract_identity(identity):
            raise HarnessValidationError(
                "TaskPlan event identity does not match accepted plan",
                code="task_plan_replay_identity_mismatch",
                details={"sequence": event.sequence},
            )
    return ordered


def _validated_results(
    results: Iterable[TaskResultRecord],
    plans: tuple[ValidatedTaskPlan, ...],
) -> dict[tuple[str, int, int], TaskResultRecord]:
    plans_by_version = {item.version: item for item in plans}
    result_map: dict[tuple[str, int, int], TaskResultRecord] = {}
    for result in results:
        if not isinstance(result, TaskResultRecord):
            raise TypeError("results must contain TaskResultRecord values")
        expected_checksum = canonical_payload_checksum(result.checksum_projection())
        if result.result_checksum != expected_checksum:
            raise HarnessValidationError(
                "TaskPlan result checksum does not match recorded content",
                code="task_plan_replay_result_checksum_mismatch",
                details={"task_id": result.task_id},
            )
        plan = plans_by_version.get(result.plan_version)
        if plan is None or not result.matches_plan_identity(plan):
            raise HarnessValidationError(
                "TaskPlan result identity does not match plan history",
                code="task_plan_replay_result_mismatch",
            )
        key = (result.task_instance_id, result.attempt, result.plan_version)
        existing = result_map.get(key)
        if existing is not None and existing.result_checksum != result.result_checksum:
            raise HarnessValidationError(
                "TaskPlan replay contains conflicting duplicate results",
                code="task_plan_replay_result_conflict",
                details={"task_instance_id": result.task_instance_id},
            )
        result_map[key] = result
    return result_map


def _validated_patches(
    patches: Iterable[PlanPatch],
    plans: tuple[ValidatedTaskPlan, ...],
) -> dict[str, PlanPatch]:
    plans_by_version = {plan.version: plan for plan in plans}
    values: dict[str, PlanPatch] = {}
    for patch in patches:
        if not isinstance(patch, PlanPatch):
            raise TypeError("patches must contain PlanPatch values")
        expected_checksum = canonical_payload_checksum(patch.checksum_projection())
        if patch.patch_checksum != expected_checksum:
            raise HarnessValidationError(
                "TaskPlan patch checksum does not match recorded content",
                code="task_plan_replay_patch_checksum_mismatch",
                details={"patch_id": patch.patch_id},
            )
        base_plan = plans_by_version.get(patch.base_plan_version)
        if base_plan is None or not patch.matches_plan_identity(base_plan):
            raise HarnessValidationError(
                "TaskPlan patch identity does not match plan history",
                code="task_plan_replay_patch_mismatch",
            )
        existing = values.get(patch.patch_checksum)
        if existing is not None and existing != patch:
            raise HarnessValidationError(
                "TaskPlan replay contains conflicting patch evidence",
                code="task_plan_replay_patch_mismatch",
            )
        values[patch.patch_checksum] = patch
    return values


def _accepted_plan_for_event(
    event: TaskPlanEvent,
    plans_by_version: Mapping[int, ValidatedTaskPlan],
) -> ValidatedTaskPlan:
    if event.plan_version is None:
        raise HarnessValidationError(
            "PLAN_ACCEPTED is missing plan version",
            code="task_plan_replay_plan_missing",
        )
    plan = plans_by_version.get(event.plan_version)
    if plan is None:
        raise HarnessValidationError(
            "PLAN_ACCEPTED references unavailable plan evidence",
            code="task_plan_replay_plan_missing",
            details={"plan_version": event.plan_version},
        )
    payload = thaw_mapping(event.payload)
    if (
        event.plan_id != plan.plan_id
        or event.input_checksum != plan.plan_checksum
        or payload.get("plan_ref") != plan.plan_checksum
        or payload.get("policy_ref") != plan.policy_ref
        or (
            payload.get("policy_checksum") is not None
            and payload.get("policy_checksum") != plan.policy_checksum
        )
    ):
        raise HarnessValidationError(
            "PLAN_ACCEPTED evidence does not match recorded plan",
            code="task_plan_replay_plan_checksum_mismatch",
            details={"plan_version": plan.version},
        )
    return plan


def _require_terminal_submission(
    event: TaskPlanEvent,
    submissions: Mapping[str, CandidateSubmission],
    initial_plan: ValidatedTaskPlan,
) -> None:
    key = event.payload.get("submission_key")
    submission = submissions.get(key) if isinstance(key, str) else None
    if submission is None or submission.plan_id != initial_plan.plan_id:
        raise HarnessValidationError(
            "terminal outcome belongs to another candidate submission",
            code="task_plan_submission_result_invalid",
        )


def _validate_candidate_event(event: TaskPlanEvent) -> None:
    payload = thaw_mapping(event.payload)
    candidate_ref = payload.get("candidate_ref")
    if candidate_ref is not None and candidate_ref != event.input_checksum:
        raise HarnessValidationError(
            "TaskPlan candidate event reference does not match checksum",
            code="task_plan_replay_candidate_mismatch",
            details={"sequence": event.sequence},
        )
    raw_submission = payload.get("submission")
    if raw_submission is not None:
        submission = CandidateSubmission.from_dict(raw_submission)
        if (
            submission.identity.run_id != event.run_id
            or submission.identity.stage_id != event.stage_id
            or submission.candidate_ref != candidate_ref
        ):
            raise HarnessValidationError(
                "candidate submission does not match its canonical event",
                code="task_plan_replay_candidate_mismatch",
            )


def _validate_patch_event(event: TaskPlanEvent) -> None:
    payload = thaw_mapping(event.payload)
    patch_ref = payload.get("patch_ref")
    if not isinstance(patch_ref, str) or patch_ref != event.input_checksum:
        raise HarnessValidationError(
            "TaskPlan patch event is missing matching patch evidence",
            code="task_plan_replay_patch_mismatch",
            details={"sequence": event.sequence},
        )


def _require_projection(
    projection: TaskPlanProjection | None,
    event: TaskPlanEvent,
) -> TaskPlanProjection:
    if projection is None:
        raise HarnessValidationError(
            "TaskPlan lifecycle event precedes plan acceptance",
            code="task_plan_replay_plan_missing",
            details={"event_type": event.event_type, "sequence": event.sequence},
        )
    return projection


def _task_plan_for_event(
    event: TaskPlanEvent,
    plans_by_version: Mapping[int, ValidatedTaskPlan],
) -> ValidatedTaskPlan:
    if event.plan_version is None:
        raise HarnessValidationError(
            "TaskPlan task event is missing plan version",
            code="task_plan_replay_identity_mismatch",
            details={"event_type": event.event_type},
        )
    plan = plans_by_version.get(event.plan_version)
    if plan is None or event.plan_id != plan.plan_id:
        raise HarnessValidationError(
            "TaskPlan task event references unavailable plan evidence",
            code="task_plan_replay_plan_missing",
            details={"plan_version": event.plan_version},
        )
    return plan


def _instance_for_event(event: TaskPlanEvent, plan: ValidatedTaskPlan) -> TaskInstance:
    if event.task_id is None or event.task_instance_id is None or event.attempt is None:
        raise HarnessValidationError(
            "TaskPlan lifecycle event is missing attempt identity",
            code="task_plan_replay_identity_mismatch",
            details={"event_type": event.event_type},
        )
    definition = next((item for item in plan.tasks if item.task_id == event.task_id), None)
    if definition is None:
        raise HarnessValidationError(
            "TaskPlan lifecycle event references unknown task",
            code="task_plan_replay_unknown_task",
            details={"task_id": event.task_id, "plan_version": plan.version},
        )
    if event.event_type in {"TASK_READY", "TASK_DISPATCHED", "TASK_STARTED"} and event.input_checksum != definition.task_definition_checksum:
        raise HarnessValidationError(
            "TaskPlan lifecycle event task checksum does not match accepted definition",
            code="task_plan_replay_task_checksum_mismatch",
            details={"task_id": event.task_id},
        )
    return task_instance_for_attempt(
        plan,
        event.task_id,
        event.attempt,
        task_instance_id=event.task_instance_id,
    )


def _require_projection_task_definition(
    projection: TaskPlanProjection,
    instance: TaskInstance,
) -> None:
    state = next((item for item in projection.tasks if item.task_id == instance.task_id), None)
    if state is None or state.task_definition_checksum != instance.task_definition_checksum:
        raise HarnessValidationError(
            "TaskPlan event task definition does not match current projection",
            code="task_plan_replay_task_checksum_mismatch",
            details={"task_id": instance.task_id},
        )


def _require_recorded_instance(
    instances: Mapping[str, TaskInstance],
    instance: TaskInstance,
    event: TaskPlanEvent,
) -> None:
    recorded = instances.get(instance.task_instance_id)
    if recorded is None or recorded.instance_checksum != instance.instance_checksum:
        raise HarnessValidationError(
            "TaskPlan lifecycle event has no matching TASK_READY evidence",
            code="task_plan_replay_attempt_missing",
            details={"task_id": instance.task_id, "event_type": event.event_type},
        )


def _result_for_event(
    event: TaskPlanEvent,
    results: Mapping[tuple[str, int, int], TaskResultRecord],
    plan: ValidatedTaskPlan,
    *,
    transcript_store: SubAgentTranscriptStorePort | None = None,
    artifact_reference_verifier: ArtifactReferenceVerifierPort | None = None,
) -> TaskResultRecord:
    assert event.task_instance_id is not None
    assert event.attempt is not None
    result = results.get((event.task_instance_id, event.attempt, plan.version))
    if result is None:
        raise HarnessValidationError(
            "TaskPlan replay is missing a recorded result",
            code="task_plan_replay_result_missing",
            details={"task_id": event.task_id},
        )
    definition = next(item for item in plan.tasks if item.task_id == result.task_id)
    payload = thaw_mapping(event.payload)
    expected_event = "TASK_RESULT_ACCEPTED" if result.status is TaskLifecycle.SUCCEEDED else "TASK_RESULT_REJECTED"
    if (
        event.event_type != expected_event
        or event.task_id != result.task_id
        or event.plan_id != result.plan_id
        or event.plan_version != result.plan_version
        or event.input_checksum != result.task_checksum
        or result.task_checksum != definition.task_definition_checksum
        or result.worker_ref != definition.worker_ref
        or result.binding_checksum != definition.binding_checksum
        or tuple(event.output_refs) != result.output_refs
        or payload.get("result_ref") != result.result_ref
        or payload.get("result_checksum") != result.result_checksum
        or tuple(payload.get("gate_refs", ())) != result.verified_gate_refs
        or tuple(payload.get("gate_evidence_refs", ())) != result.gate_evidence_refs
        or payload.get("transcript_ref") != result.transcript_ref
        or payload.get("transcript_checksum") != result.transcript_checksum
        or payload.get("subagent_output_ref") != result.subagent_output_ref
        or payload.get("subagent_output_checksum") != result.subagent_output_checksum
    ):
        raise HarnessValidationError(
            "TaskPlan result event does not match recorded result evidence",
            code="task_plan_replay_result_mismatch",
            details={"task_id": result.task_id},
        )
    _verify_replay_subagent_evidence(
        result,
        definition=definition,
        transcript_store=transcript_store,
        artifact_reference_verifier=artifact_reference_verifier,
    )
    return result


def _verify_replay_subagent_evidence(
    result: TaskResultRecord,
    *,
    definition: Any,
    transcript_store: SubAgentTranscriptStorePort | None,
    artifact_reference_verifier: ArtifactReferenceVerifierPort | None,
) -> None:
    if definition.subagent_id is None:
        return
    if (
        result.transcript_ref is None
        or result.transcript_checksum is None
        or result.subagent_output_ref is None
        or result.subagent_output_checksum is None
    ):
        raise HarnessValidationError(
            "subagent result is missing durable transcript evidence",
            code="task_plan_subagent_evidence_required",
            details={"task_id": result.task_id},
        )
    if transcript_store is None:
        raise HarnessValidationError(
            "TaskPlan replay requires a subagent transcript store",
            code="task_plan_subagent_transcript_store_required",
        )
    transcript = transcript_store.read(result.transcript_ref)
    output = transcript_store.read_output(result.subagent_output_ref)
    stored = transcript_store.find_by_identity(transcript.identity)
    if stored is None:
        raise HarnessValidationError(
            "subagent transcript receipt is unavailable during replay",
            code="subagent_transcript_not_found",
        )
    if (
        stored.transcript_ref != result.transcript_ref
        or stored.transcript_checksum != result.transcript_checksum
        or stored.output_ref != result.subagent_output_ref
        or stored.output_checksum != result.subagent_output_checksum
        or output.output_checksum != result.subagent_output_checksum
        or (
            result.status is TaskLifecycle.SUCCEEDED
            and output.artifact_refs != result.output_refs
        )
        or output.identity != transcript.identity
        or transcript.output_ref != output.ref
        or transcript.output_checksum != output.output_checksum
        or transcript.identity.parent_run_id != result.run_id
        or transcript.identity.stage_id != result.stage_id
        or transcript.identity.task_instance_id != result.task_instance_id
        or transcript.identity.attempt != result.attempt
        or transcript.identity.task_id != result.task_id
        or transcript.identity.subagent_id != definition.subagent_id
        or not _subagent_identity_matches_result(transcript.identity, result)
        or (
            result.status is TaskLifecycle.SUCCEEDED
            and output.status != "succeeded"
        )
        or (
            result.status is TaskLifecycle.FAILED
            and output.status == "succeeded"
        )
    ):
        raise HarnessValidationError(
            "TaskPlan replay subagent evidence identity is inconsistent",
            code="task_plan_replay_result_mismatch",
        )
    transcript_store.verify(stored)
    _verify_replay_artifact_references(
        output.artifact_refs,
        expected_run_id=result.run_id,
        verifier=artifact_reference_verifier,
    )


def _verify_replay_artifact_references(
    refs: tuple[str, ...],
    *,
    expected_run_id: str,
    verifier: ArtifactReferenceVerifierPort | None,
) -> None:
    if not refs:
        return
    if verifier is None:
        raise HarnessValidationError(
            "subagent artifact refs require a canonical verifier during replay",
            code="task_plan_subagent_artifact_verifier_required",
        )
    for index, ref in enumerate(refs):
        try:
            verifier.verify_artifact_ref(ref, expected_run_id=expected_run_id)
        except Exception as exc:
            raise HarnessValidationError(
                "subagent artifact ref could not be verified during replay",
                code="task_plan_subagent_artifact_unverified",
                details={"artifact_index": index},
            ) from exc


def _apply_terminal_result(
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
    result: TaskResultRecord,
    *,
    plan: ValidatedTaskPlan,
) -> TaskPlanProjection:
    state = next((item for item in projection.tasks if item.task_id == result.task_id), None)
    if (
        state is None
        or state.active_instance_id != result.task_instance_id
        or state.attempts != result.attempt
        or state.status not in {TaskLifecycle.READY, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}
    ):
        raise HarnessValidationError(
            "TaskPlan terminal result does not match active projection",
            code="task_plan_replay_result_mismatch",
            details={"task_id": result.task_id},
        )
    payload = thaw_mapping(event.payload)
    if event.input_checksum not in {result.task_checksum, result.result_checksum}:
        raise HarnessValidationError(
            "TaskPlan terminal event checksum does not match result evidence",
            code="task_plan_replay_result_checksum_mismatch",
            details={"task_id": result.task_id},
        )
    payload_checksum = payload.get("result_checksum")
    if payload_checksum is not None and payload_checksum != result.result_checksum:
        raise HarnessValidationError(
            "TaskPlan terminal event result checksum is inconsistent",
            code="task_plan_replay_result_checksum_mismatch",
            details={"task_id": result.task_id},
        )
    if tuple(event.output_refs) != result.output_refs:
        raise HarnessValidationError(
            "TaskPlan terminal event output refs do not match result evidence",
            code="task_plan_replay_result_mismatch",
            details={"task_id": result.task_id},
        )
    if tuple(payload.get("gate_refs", ())) != result.verified_gate_refs or tuple(
        payload.get("gate_evidence_refs", ())
    ) != result.gate_evidence_refs:
        raise HarnessValidationError(
            "TaskPlan terminal event gate evidence does not match result evidence",
            code="task_plan_replay_result_mismatch",
            details={"task_id": result.task_id},
        )
    if (
        payload.get("transcript_ref") != result.transcript_ref
        or payload.get("transcript_checksum") != result.transcript_checksum
        or payload.get("subagent_output_ref") != result.subagent_output_ref
        or payload.get("subagent_output_checksum") != result.subagent_output_checksum
    ):
        raise HarnessValidationError(
            "TaskPlan terminal event subagent evidence does not match result",
            code="task_plan_replay_result_mismatch",
            details={"task_id": result.task_id},
        )
    if event.event_type == "TASK_COMPLETED":
        if result.status is not TaskLifecycle.SUCCEEDED or not result.output_roles:
            raise HarnessValidationError(
                "TASK_COMPLETED requires successful result evidence",
                code="task_plan_replay_result_mismatch",
            )
        reference_value = TaskResultReference(
            result_ref=result.result_ref or "task-result:" + result.result_checksum,
            result_checksum=result.result_checksum,
            output_role=result.output_roles[0],
            output_schema_ref=result.output_schema_ref,
        )
        updated = replace(
            state,
            status=TaskLifecycle.SUCCEEDED,
            active_instance_id=None,
            result=reference_value,
            failure_reason_code=None,
        )
    else:
        if result.status is not TaskLifecycle.FAILED:
            raise HarnessValidationError(
                "TASK_FAILED requires failed result evidence",
                code="task_plan_replay_result_mismatch",
            )
        updated = replace(
            state,
            status=TaskLifecycle.FAILED,
            active_instance_id=result.task_instance_id,
            failure_reason_code=result.error_code or event.reason_code or "task_failed",
        )
    definition = next(
        (item for item in plan.tasks if item.task_id == result.task_id),
        None,
    )
    if definition is None:
        raise HarnessValidationError(
            "TaskPlan terminal result definition is unavailable",
            code="task_plan_replay_unknown_task",
            details={"task_id": result.task_id},
        )
    settled_budget = _settle_result_budget(
        projection.consumed_budget,
        definition,
        result,
    )
    return replace(
        projection,
        tasks=tuple(updated if item.task_id == result.task_id else item for item in projection.tasks),
        consumed_budget=settled_budget,
    )


def _schedule_retry(
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
    instance: TaskInstance,
    plan: ValidatedTaskPlan,
    *,
    failed_result_checksum: str | None,
) -> TaskPlanProjection:
    state = next((item for item in projection.tasks if item.task_id == instance.task_id), None)
    if (
        state is None
        or state.status is not TaskLifecycle.FAILED
        or state.active_instance_id != instance.task_instance_id
        or state.attempts != instance.attempt
    ):
        raise HarnessValidationError(
            "TaskPlan retry event does not match failed attempt",
            code="task_plan_replay_retry_mismatch",
            details={"task_id": instance.task_id},
        )
    definition = next((item for item in plan.tasks if item.task_id == instance.task_id), None)
    if definition is None:
        raise HarnessValidationError(
            "TaskPlan retry event references an unknown task",
            code="task_plan_replay_unknown_task",
            details={"task_id": instance.task_id},
        )
    reason_code = event.reason_code
    if (
        reason_code is None
        or reason_code not in definition.normalized_retry_policy.retryable_reason_codes
    ):
        raise HarnessValidationError(
            "TaskPlan retry event is outside the task retry policy",
            code="task_plan_replay_retry_policy_mismatch",
            details={"task_id": instance.task_id, "reason_code": reason_code},
        )
    if instance.attempt >= definition.normalized_retry_policy.max_attempts:
        raise HarnessValidationError(
            "TaskPlan retry event exceeds the task retry policy",
            code="task_plan_replay_retry_exhausted",
            details={"task_id": instance.task_id, "attempt": instance.attempt},
        )
    if (
        failed_result_checksum is None
        or event.input_checksum != failed_result_checksum
    ):
        raise HarnessValidationError(
            "TaskPlan retry event is not bound to the failed result evidence",
            code="task_plan_replay_retry_checksum_mismatch",
            details={
                "task_id": instance.task_id,
                "expected": failed_result_checksum,
                "actual": event.input_checksum,
            },
        )
    updated = replace(
        state,
        status=TaskLifecycle.PENDING,
        active_instance_id=None,
        failure_reason_code=None,
    )
    return replace(
        projection,
        tasks=tuple(updated if item.task_id == instance.task_id else item for item in projection.tasks),
    )


def _apply_replacement(
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
    plan: ValidatedTaskPlan,
    *,
    base_plan: ValidatedTaskPlan | None,
) -> TaskPlanProjection:
    if event.plan_id != projection.plan_id or event.plan_version != projection.plan_version:
        raise HarnessValidationError(
            "TaskPlan replacement event references a stale plan",
            code="task_plan_replay_plan_mismatch",
        )
    payload = thaw_mapping(event.payload)
    replaced_task_id = payload.get("replaced_task_id")
    replacement_task_id = payload.get("replacement_task_id")
    if (
        not isinstance(replaced_task_id, str)
        or not isinstance(replacement_task_id, str)
        or event.task_id != replaced_task_id
        or event.input_checksum != plan.plan_checksum
        or event.reason_code != "plan_patch_replaced"
        or base_plan is None
        or base_plan.plan_id != plan.parent_plan_id
        or not any(item.task_id == replaced_task_id for item in base_plan.tasks)
        or any(item.task_id == replacement_task_id for item in base_plan.tasks)
        or not any(item.task_id == replaced_task_id for item in plan.tasks)
        or not any(item.task_id == replacement_task_id for item in plan.tasks)
    ):
        raise HarnessValidationError(
            "TaskPlan replacement event mapping is invalid",
            code="task_plan_replay_replacement_mismatch",
        )
    state = next((item for item in projection.tasks if item.task_id == replaced_task_id), None)
    replacement_state = next((item for item in projection.tasks if item.task_id == replacement_task_id), None)
    if state is None or replacement_state is None:
        raise HarnessValidationError(
            "TaskPlan replacement event references an unknown task",
            code="task_plan_replay_unknown_task",
        )
    if state.status not in {
        TaskLifecycle.PENDING,
        TaskLifecycle.READY,
        TaskLifecycle.FAILED,
    }:
        raise HarnessValidationError(
            "TaskPlan replacement event targets a terminal or active task",
            code="task_plan_replay_replacement_mismatch",
        )
    base_definitions = {item.task_id: item for item in base_plan.tasks}
    new_definitions = {item.task_id: item for item in plan.tasks}
    old_definition = base_definitions[replaced_task_id]
    replacement = new_definitions[replacement_task_id]
    if replacement.output_role != old_definition.output_role:
        raise HarnessValidationError(
            "TaskPlan replacement changes the target output role",
            code="task_plan_replay_replacement_mismatch",
            details={
                "replaced_task_id": replaced_task_id,
                "replacement_task_id": replacement_task_id,
            },
        )
    if replaced_task_id in replacement.depends_on:
        raise HarnessValidationError(
            "TaskPlan replacement depends on the task it replaces",
            code="task_plan_replay_replacement_mismatch",
            details={
                "replaced_task_id": replaced_task_id,
                "replacement_task_id": replacement_task_id,
            },
        )
    for task_id, definition in new_definitions.items():
        if replaced_task_id in definition.depends_on:
            raise HarnessValidationError(
                "TaskPlan replacement leaves a dependency on the replaced task",
                code="task_plan_replay_replacement_mismatch",
                details={"task_id": task_id, "replaced_task_id": replaced_task_id},
            )
        base_definition = base_definitions.get(task_id)
        if base_definition is None:
            continue
        if replaced_task_id in base_definition.depends_on and replacement_task_id not in definition.depends_on:
            raise HarnessValidationError(
                "TaskPlan replacement did not rewire a dependency",
                code="task_plan_replay_replacement_mismatch",
                details={"task_id": task_id, "replacement_task_id": replacement_task_id},
            )
        for input_ref in definition.task.input_refs:
            producer = task_reference_producer(input_ref, tuple(new_definitions))
            if producer == replaced_task_id:
                raise HarnessValidationError(
                    "TaskPlan replacement leaves an input reference to the replaced task",
                    code="task_plan_replay_replacement_mismatch",
                    details={"task_id": task_id, "input_ref": input_ref},
                )
        base_producers = {
            task_reference_producer(input_ref, tuple(base_definitions))
            for input_ref in base_definition.task.input_refs
        }
        new_producers = {
            task_reference_producer(input_ref, tuple(new_definitions))
            for input_ref in definition.task.input_refs
        }
        if replaced_task_id in base_producers and replacement_task_id not in new_producers:
            raise HarnessValidationError(
                "TaskPlan replacement did not rewire an input reference",
                code="task_plan_replay_replacement_mismatch",
                details={"task_id": task_id, "replacement_task_id": replacement_task_id},
            )
    updated = replace(
        state,
        status=TaskLifecycle.SKIPPED,
        active_instance_id=None,
        failure_reason_code="plan_patch_replaced",
    )
    return replace(
        projection,
        tasks=tuple(updated if item.task_id == replaced_task_id else item for item in projection.tasks),
    )


def _apply_non_result_terminal(
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
    *,
    plan: ValidatedTaskPlan | None = None,
) -> TaskPlanProjection:
    if event.plan_id != projection.plan_id or event.plan_version != projection.plan_version:
        raise HarnessValidationError(
            "TaskPlan terminal event references a stale plan",
            code="task_plan_replay_plan_mismatch",
        )
    if event.task_id is None:
        raise HarnessValidationError(
            "TaskPlan terminal event is missing task id",
            code="task_plan_replay_identity_mismatch",
        )
    state = next((item for item in projection.tasks if item.task_id == event.task_id), None)
    if state is None:
        raise HarnessValidationError(
            "TaskPlan terminal event references unknown task",
            code="task_plan_replay_unknown_task",
            details={"task_id": event.task_id},
        )
    if event.event_type == "TASK_BLOCKED_UPSTREAM_FAILURE":
        if plan is None:
            raise HarnessValidationError(
                "dependency block replay is missing its accepted plan",
                code="task_plan_replay_plan_missing",
            )
        if event.reason_code != TASK_BLOCKED_UPSTREAM_FAILURE:
            raise HarnessValidationError(
                "dependency block replay has an invalid reason code",
                code="task_plan_replay_dependency_block_invalid",
            )
        payload = thaw_mapping(event.payload)
        if payload.get("projection_checksum") != projection.projection_checksum:
            raise HarnessValidationError(
                "dependency block replay has a stale causal projection",
                code="task_plan_replay_dependency_block_causality",
            )
        expected_targets = dependency_blocked_task_ids(plan, projection)
        if event.task_id not in expected_targets or event.task_id != expected_targets[0]:
            raise HarnessValidationError(
                "dependency block replay violates stable closure order",
                code="task_plan_replay_dependency_block_order",
                details={"task_id": event.task_id, "expected": list(expected_targets)},
            )
        expected_predecessors = dependency_blocking_predecessor_ids(plan, projection, event.task_id)
        actual_predecessors = tuple(payload.get("blocking_predecessor_ids", ()))
        if actual_predecessors != expected_predecessors:
            raise HarnessValidationError(
                "dependency block replay has invalid predecessor evidence",
                code="task_plan_replay_dependency_block_cause",
                details={"task_id": event.task_id},
            )
        return block_dependency_task(plan, projection, event.task_id)
    target = TaskLifecycle.BLOCKED if event.event_type == "TASK_BLOCKED" else TaskLifecycle.SKIPPED
    updated = replace(
        state,
        status=target,
        active_instance_id=None,
        failure_reason_code=event.reason_code,
    )
    return replace(
        projection,
        tasks=tuple(updated if item.task_id == event.task_id else item for item in projection.tasks),
    )


def _aggregate_for_event(
    event: TaskPlanEvent,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    payload = thaw_mapping(event.payload)
    aggregate_ref = payload.get("aggregate_ref")
    aggregate_checksum = payload.get("aggregate_checksum") or event.input_checksum
    if not isinstance(aggregate_ref, str) or not isinstance(aggregate_checksum, str):
        raise HarnessValidationError(
            "TaskPlan aggregation event is missing reference evidence",
            code="task_plan_replay_aggregate_mismatch",
        )
    output_refs_by_role = payload.get("output_refs_by_role", {})
    if not isinstance(output_refs_by_role, Mapping):
        raise HarnessValidationError(
            "TaskPlan aggregation output refs must be an object",
            code="task_plan_replay_aggregate_mismatch",
        )
    projected_refs = tuple(
        sorted(output_refs_by_role[key] for key in sorted(output_refs_by_role))
    )
    if projected_refs != tuple(event.output_refs):
        raise HarnessValidationError(
            "TaskPlan aggregation event output refs are inconsistent",
            code="task_plan_replay_aggregate_mismatch",
        )
    result_refs_raw = payload.get("result_refs")
    branch_refs_raw = payload.get("branch_refs")
    if not isinstance(result_refs_raw, (list, tuple)) or not isinstance(branch_refs_raw, (list, tuple)):
        raise HarnessValidationError(
            "TaskPlan aggregation event is missing result and branch evidence",
            code="task_plan_replay_aggregate_mismatch",
        )
    try:
        result_refs = stable_text_tuple(
            result_refs_raw,
            "result_refs",
            allow_empty=False,
            item_kind="reference",
        )
        branch_refs = tuple(
            frozen_mapping(item, "branch_refs.item") for item in branch_refs_raw
        )
    except (TypeError, HarnessValidationError) as exc:
        raise HarnessValidationError(
            "TaskPlan aggregation branch evidence is invalid",
            code="task_plan_replay_aggregate_mismatch",
        ) from exc
    branch_payload = [thaw_mapping(item) for item in branch_refs]
    expected_checksum = canonical_payload_checksum(
        {
            "roles": dict(output_refs_by_role),
            "result_refs": list(result_refs),
            "branch_refs": branch_payload,
        }
    )
    if event.input_checksum != expected_checksum:
        raise HarnessValidationError(
            "TaskPlan aggregation event input checksum does not match branch evidence",
            code="task_plan_replay_aggregate_mismatch",
        )
    if checksum(aggregate_checksum, "aggregate_checksum") != expected_checksum:
        raise HarnessValidationError(
            "TaskPlan aggregation checksum does not match recorded branch evidence",
            code="task_plan_replay_aggregate_mismatch",
        )
    expected_ref = f"task-plan-aggregate:{expected_checksum}"
    if reference(aggregate_ref, "aggregate_ref") != expected_ref:
        raise HarnessValidationError(
            "TaskPlan aggregation ref is not bound to recorded branch evidence",
            code="task_plan_replay_aggregate_mismatch",
        )
    return (
        expected_ref,
        expected_checksum,
        projected_refs,
        result_refs,
        tuple(branch_refs),
    )


def _same_graph_replay_identity(left: Any, right: Any) -> bool:
    return all(
        getattr(left, field_name) == getattr(right, field_name)
        for field_name in _GRAPH_REPLAY_IDENTITY_FIELDS
    )


def _result_matches_projection_identity(
    result: TaskResultRecord,
    projection: TaskPlanProjection,
) -> bool:
    if (
        result.run_id != projection.run_id
        or result.stage_id != projection.stage_id
        or result.plan_id != projection.plan_id
        or result.plan_version != projection.plan_version
    ):
        return False
    return _same_graph_replay_identity(result, projection)


def _subagent_identity_matches_result(identity: Any, result: TaskResultRecord) -> bool:
    return _same_graph_replay_identity(identity, result)


__all__ = [
    "TASK_PLAN_REPLAY_REDUCER_VERSION",
    "TASK_PLAN_REPLAY_REDUCER_VERSION_V3",
    "TASK_PLAN_REPLAY_REDUCER_VERSIONS",
    "TaskPlanReplayReducer",
    "TaskPlanReplayReport",
]
