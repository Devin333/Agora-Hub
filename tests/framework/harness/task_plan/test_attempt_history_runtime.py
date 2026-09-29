from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord, TaskAttemptOutcome
from framework.harness.task_plan.attempt_history_index import recovery_results_from_history
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.checkpoint import TaskPlanCheckpoint
from framework.harness.task_plan.models import (
    PlanPatch,
    PlanPatchOperation,
    PlanPatchOperationType,
    TaskAdmissionOwner,
    TaskLifecycle,
    TaskRetryPolicy,
)
from framework.harness.task_plan.scheduler import task_instance_for_attempt, TaskPlanScheduler, TaskPlanReadyDecision
from framework.harness.task_plan.patches import TaskPlanPatchValidator
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.store import (
    InMemoryTaskPlanStore,
    LogicalTaskReadiness,
    TaskPlanEvent,
    TaskQueueAdmissionEvidence,
)
from framework.harness.task_plan.verification import (
    TaskPlanGateRegistry,
    TaskPlanResultVerificationRequest,
    TaskPlanResultVerifier,
)
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from tests.fixtures.task_plan import InMemoryTaskPlanGateArtifactWriter
from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime, _request
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore, _EventStore, _store, _accepted_plan, _task, _start, _result, _lifecycle_event,
)


def _gate_artifact_owner(store, *, durable):
    if durable:
        return store
    owner = InMemoryTaskPlanGateArtifactWriter()
    store.bind_gate_evidence_reader(owner)
    return owner


def _verified_result(
    plan,
    instance,
    *,
    gate_artifact_owner,
    worker_succeeds=True,
    gate_passes=True,
):
    task = next(item for item in plan.tasks if item.task_id == instance.task_id)
    worker_result = HarnessWorkerResult(
        status=(
            HarnessWorkerStatus.SUCCEEDED
            if worker_succeeds
            else HarnessWorkerStatus.FAILED
        ),
        artifacts=(f"artifact://{instance.task_id}",) if worker_succeeds else (),
        error=None if worker_succeeds else "worker_failed",
    )
    gates = TaskPlanGateRegistry()
    gates.register(
        "SummaryGate@1",
        lambda _request: gate_passes,
        deterministic=True,
    )
    return TaskPlanResultVerifier(
        gates,
        gate_artifact_writer=gate_artifact_owner,
    ).verify(
        worker_result,
        task=task,
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=task,
            instance=instance,
            worker_result=worker_result,
        ),
    )


def test_recovery_history_selects_only_the_current_active_attempt():
    candidate, plan, _, _ = _accepted_plan((_task("structure"),))
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    first = _start(store, plan, "structure")
    second = task_instance_for_attempt(plan, first.task_id, 2)
    first_result = _result(plan, first, status=TaskLifecycle.SUCCEEDED)
    second_result = _result(plan, second, status=TaskLifecycle.SUCCEEDED)
    history = tuple(TaskAttemptHistoryRecord.for_result(plan, result) for result in (first_result, second_result))
    projection = store.load_projection(plan.run_id, plan.stage_id)
    projection = replace(projection, tasks=(replace(projection.tasks[0], attempts=2,
                                                    active_instance_id=second.task_instance_id),))

    assert projection.tasks[0].admission_owner is TaskAdmissionOwner.QUEUE
    assert recovery_results_from_history(history, projection) == (second_result,)
    group_wave_projection = replace(
        projection,
        tasks=(replace(
            projection.tasks[0],
            admission_owner=TaskAdmissionOwner.GROUP_WAVE,
        ),),
    )
    assert recovery_results_from_history(history, group_wave_projection) == (second_result,)
    for changes in (
        {"plan_id": "other-plan"}, {"plan_version": plan.version + 1},
    ):
        assert recovery_results_from_history(history, replace(projection, **changes)) == ()
    ready = replace(
        projection,
        tasks=(replace(
            projection.tasks[0],
            status=TaskLifecycle.READY,
            active_instance_id=None,
            admission_owner=None,
        ),),
        logical_ready_order=("structure",),
    )
    assert ready.tasks[0].attempts == 2
    assert recovery_results_from_history(history, ready) == ()
    pending = replace(
        projection,
        tasks=(replace(
            projection.tasks[0],
            status=TaskLifecycle.PENDING,
            active_instance_id=None,
            admission_owner=None,
        ),),
        logical_ready_order=(),
    )
    assert recovery_results_from_history(history, pending) == ()
    assert tuple(item.result for item in history) == (first_result, second_result)
    for reason in ("gate_failed", "task_worker_failed"):
        rejected = replace(
            second_result,
            status=TaskLifecycle.FAILED,
            error_code=reason,
            result_ref=None,
            output_refs=(),
            output_roles=(),
            worker_result_proof_ref=(
                "sha256:" + "a" * 64
                if reason == "task_worker_failed"
                else None
            ),
        )
        rejected_history = (history[0], TaskAttemptHistoryRecord.for_result(plan, rejected))
        assert recovery_results_from_history(rejected_history, projection) == (rejected,)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("reason,outcome", [("task_worker_failed", TaskAttemptOutcome.FAILED), ("task_gate_failed", TaskAttemptOutcome.REJECTED)])
def test_retry_keeps_failed_attempt_and_only_projects_the_accepted_result(durable, reason, outcome):
    artifacts, event_log = _ArtifactStore(), _EventStore()
    store = _store(event_log, artifacts) if durable else InMemoryTaskPlanStore()
    gate_artifact_owner = _gate_artifact_owner(store, durable=durable)
    task = replace(_task("structure"), retry_policy=TaskRetryPolicy(max_attempts=2, retryable_reason_codes=(reason,)))
    candidate, plan, _, _ = _accepted_plan((task,))
    store.append_candidate(candidate)
    store.accept_plan(plan)
    first = _start(store, plan, "structure")
    failed = _verified_result(
        plan,
        first,
        gate_artifact_owner=gate_artifact_owner,
        worker_succeeds=reason != "task_worker_failed",
        gate_passes=reason != "task_gate_failed",
    )
    assert failed.error_code == reason
    store.append_result(failed)
    projection = store.load_projection(plan.run_id, plan.stage_id)
    retry = TaskPlanEvent.for_plan("TASK_RETRY_SCHEDULED", plan, task_id=first.task_id,
                                  task_instance_id=first.task_instance_id, attempt=1,
                                  input_checksum=failed.result_checksum, reason_code=reason,
                                  sequence=projection.last_sequence + 1)
    projection = replace(projection, tasks=tuple(item.transitioned(
        TaskLifecycle.PENDING,
        active_instance_id=None,
        admission_owner=None,
        failure_reason_code=None,
    ) for item in projection.tasks),
                         last_sequence=retry.sequence)
    store.commit_event(retry, projection)
    second = task_instance_for_attempt(plan, first.task_id, 2)
    scheduler = TaskPlanScheduler()
    projection = scheduler.reserve_ready_tasks(
        projection,
        TaskPlanReadyDecision(logical_ready_task_ids=(second.task_id,)),
    )
    sequence = projection.last_sequence + 1
    readiness = LogicalTaskReadiness(
        task_id=second.task_id,
        task_definition_checksum=second.task_definition_checksum,
        logical_ready_order=projection.logical_ready_order,
    )
    projection = replace(projection, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_READY",
        plan,
        task_id=second.task_id,
        input_checksum=second.task_definition_checksum,
        sequence=sequence,
        payload={"logical_readiness": readiness.to_dict()},
    ), projection)

    before_admission = projection
    projection = scheduler.mark_admitted(
        before_admission,
        second,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=second,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            before_admission.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            projection.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    sequence += 1
    projection = replace(projection, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_QUEUE_ADMITTED",
        plan,
        task_id=second.task_id,
        task_instance_id=second.task_instance_id,
        attempt=second.attempt,
        input_checksum=second.task_definition_checksum,
        sequence=sequence,
        payload={"queue_admission": admission.to_dict()},
    ), projection)
    for event_type, transition in (
        ("TASK_DISPATCHED", lambda state: scheduler.mark_dispatched(state, second)),
        ("TASK_STARTED", lambda state: scheduler.mark_started(state, second)),
    ):
        projection = replace(transition(projection), last_sequence=projection.last_sequence + 1)
        store.commit_event(_lifecycle_event(event_type, projection.last_sequence, plan, second), projection)
    accepted = _verified_result(
        plan,
        second,
        gate_artifact_owner=gate_artifact_owner,
    )
    store.append_result(accepted)
    reopened = _store(event_log, artifacts) if durable else store
    history = reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, 1)
    assert [(item.attempt, item.outcome, item.result) for item in history] == [(1, outcome, failed), (2, TaskAttemptOutcome.ACCEPTED, accepted)]
    assert reopened.results_for(plan.run_id, plan.stage_id, plan.plan_id, 1) == (accepted,)
    report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan,),
        reopened.read_events(plan.run_id, plan.stage_id),
        results=history,
    )
    assert report.attempt_history == history


@pytest.mark.parametrize("durable", [False, True])
def test_replaced_attempts_survive_reopen_and_old_version_queries(durable):
    artifacts, event_log = _ArtifactStore(), _EventStore()
    store = _store(event_log, artifacts) if durable else InMemoryTaskPlanStore()
    gate_artifact_owner = _gate_artifact_owner(store, durable=durable)
    candidate, plan, policy, registry = _accepted_plan((
        _task("structure"), _task("helper", capability="research.helper", role="analysis.helper"),
    ), two_tasks=True)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    accepted = _verified_result(
        plan,
        _start(store, plan, "structure"),
        gate_artifact_owner=gate_artifact_owner,
    )
    failed = _verified_result(
        plan,
        _start(store, plan, "helper"),
        gate_artifact_owner=gate_artifact_owner,
        gate_passes=False,
    )
    store.append_result(accepted)
    store.append_result(failed)
    patch = PlanPatch.for_plan(plan, patch_id="history-replacement", reason_code="replacement",
                               source_candidate_ref="candidate://replacement", operations=(PlanPatchOperation(
                                   PlanPatchOperationType.ADD_REPLACEMENT_TASK, target_task_id="helper",
                                   replacement_task=_task("replacement", capability="research.helper", role="analysis.helper"),
                               ),))
    next_plan = TaskPlanPatchValidator().apply(plan, patch, store.load_projection(plan.run_id, plan.stage_id),
                                              policy, registry, accepted_at="2026-08-02T00:01:00Z",
                                              available_input_refs=("document",))
    store.accept_patched_plan(patch, next_plan)
    replacement = _verified_result(
        next_plan,
        _start(store, next_plan, "replacement"),
        gate_artifact_owner=gate_artifact_owner,
    )
    store.append_result(replacement)
    reopened = _store(event_log, artifacts) if durable else store
    old_history = reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, 1)
    history = reopened.result_history_for(plan.run_id, plan.stage_id, next_plan.plan_id, 2)
    assert [item.result for item in old_history] == [accepted, failed]
    assert [item.result for item in history] == [accepted, failed, replacement]
    assert [item.outcome for item in history] == [TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.ACCEPTED]
    assert {item.result_checksum for item in reopened.results_for(plan.run_id, plan.stage_id, next_plan.plan_id, 2)} == {accepted.result_checksum, replacement.result_checksum}
    report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan, next_plan),
        reopened.read_events(plan.run_id, plan.stage_id),
        results=history,
        patches=(patch,),
    )
    assert report.attempt_history == history


@pytest.mark.parametrize("durable", [False, True])
def test_supervised_history_is_replayable_without_live_runtime(durable):
    artifacts, event_log = _ArtifactStore(), _EventStore()
    store = _store(event_log, artifacts) if durable else InMemoryTaskPlanStore()
    gate_artifact_owner = _gate_artifact_owner(store, durable=durable)
    gates = TaskPlanGateRegistry()
    gates.register("gate@1", lambda _request: True, deterministic=True)
    runtime, identity = _runtime(
        store=store,
        result_verifier=TaskPlanResultVerifier(
            gates,
            gate_artifact_writer=gate_artifact_owner,
        ),
        worker_executor=lambda _binding, instance, _identity: HarnessWorkerResult(
            status=HarnessWorkerStatus.SUCCEEDED,
            output={"summary": instance.task_id},
        ),
    )
    try:
        result = runtime.dispatch(_request(identity))
        assert result.status == "succeeded", result.reason_code
    finally:
        runtime._child_supervisor.shutdown()
    reopened = _store(event_log, artifacts) if durable else store
    plan = reopened.plan(identity.run_id, "delegate_stage")
    events = reopened.read_events(plan.run_id, plan.stage_id)
    history = reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version)
    assert len(history) == 2
    assert all(item.outcome is TaskAttemptOutcome.ACCEPTED and item.terminal_receipt is not None for item in history)
    assert len({item.operation_key for item in history}) == 2
    report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan,),
        events,
        results=history,
    )
    assert report.attempt_history == history
    assert {item["operation_key"] for item in report.parallel_spawn_operations.values()} == {item.operation_key for item in history}
    checkpoint = TaskPlanCheckpoint.from_replay("complete-history", plan, report, created_at="2026-09-09T00:00:00Z")
    restored = TaskPlanCheckpoint.from_dict(checkpoint.to_dict())
    restored.verify_replay(report)
    assert restored.attempt_history == history

    # A self-consistent replacement receipt still has to match the original
    # canonical spawn confirmation. Re-signing nested checksums grants no authority.
    record = history[0]
    from framework.harness.task_plan.attempt_history import _child_terminal_receipt
    terminal, _ = _child_terminal_receipt(record.terminal_receipt)
    forged = replace(record, outcome=TaskAttemptOutcome.QUARANTINED, child_id="forged-child",
                     terminal_receipt=replace(terminal, child_id="forged-child"), reason_code="late_result")
    event = TaskPlanEvent.for_plan("TASK_ATTEMPT_RECORDED", plan, sequence=len(events) + 1, payload={
        "event_type": "TASK_ATTEMPT_RECORDED", "group_id": record.group["group_id"],
        "wave_id": record.wave["wave_id"], "task_id": record.task_id,
        "task_instance_id": record.task_instance_id, "attempt": record.attempt,
        "history_record": forged.to_dict(), "idempotency_key": forged.record_checksum,
        "parallel_event_idempotency_key": forged.record_checksum,
    })
    projection = reopened.load_projection(plan.run_id, plan.stage_id)
    with pytest.raises(HarnessValidationError, match="confirmed child"):
        reopened.append_event(event)
    assert reopened.read_events(plan.run_id, plan.stage_id) == events
    assert reopened.load_projection(plan.run_id, plan.stage_id) == projection
    with pytest.raises(HarnessValidationError, match="confirmed child"):
        TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
            (plan,),
            (*events, event),
            results=history,
        )

    quarantined = replace(record, outcome=TaskAttemptOutcome.QUARANTINED, reason_code="late_result")
    late_event = replace(event, payload={**dict(event.payload),
                                        "history_record": quarantined.to_dict(),
                                        "idempotency_key": quarantined.record_checksum,
                                        "parallel_event_idempotency_key": quarantined.record_checksum})
    accepted_results = reopened.results_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version)
    reopened.append_event(late_event)
    all_history = reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version)
    assert all_history == (*history, quarantined)
    assert reopened.results_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version) == accepted_results
    replayed = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan,),
        (*events, late_event),
        results=all_history,
    )
    assert replayed.attempt_history == all_history
    assert replayed.projection.tasks == projection.tasks
