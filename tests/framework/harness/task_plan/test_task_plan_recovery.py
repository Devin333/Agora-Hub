from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.attempt_history import (
    TaskAttemptHistoryRecord,
    TaskAttemptOutcome,
)
from framework.harness.task_plan import (
    InMemoryTaskPlanStore,
    LogicalTaskReadiness,
    PlanBuildBudget,
    PlanCandidate,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskLifecycle,
    TaskOutputContract,
    TaskPlanCheckpoint,
    TaskPlanEvent,
    TASK_PLAN_RESULT_SCHEMA_V3,
    TaskPlanPolicy,
    TaskPlanRecoveryService,
    TaskPlanReplayReducer,
    TaskPlanStageIdentity,
    TaskPlanQueueProjection,
    TASK_PLAN_REPLAY_REDUCER_VERSION_V4,
    TaskPlanValidationContext,
    TaskPlanValidator,
    TaskResultRecord,
    TaskQueueAdmissionEvidence,
    TaskRetryPolicy,
    TaskSpec,
    task_instance_for_attempt,
)
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.checkpoint import (
    TASK_PLAN_CHECKPOINT_SCHEMA_V3,
    TASK_PLAN_CHECKPOINT_SCHEMA_V4,
    JsonlTaskPlanCheckpointStore,
)
from framework.harness.task_plan.models import PlanPatch, PlanPatchOperation, PlanPatchOperationType
from framework.harness.task_plan.recovery import TaskPlanRecovery
from framework.harness.task_plan.replay import TASK_PLAN_REPLAY_REDUCER_VERSION_V3
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.workers.result import HarnessWorkerResult
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _NeverCalledWorker:
    worker_id = "recovery-worker"
    worker_version = "1"
    worker_type = HarnessWorkerType.LLM

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, task):
        self.calls += 1
        return HarnessWorkerResult(status="succeeded", candidate_result_ref="result://unexpected")


class _EmptyQueueReader:
    def read_task_plan_queue(self, *, queue_name, task_instance_ids):
        return ()


def _history_fixture():
    policy = TaskPlanPolicy(
        policy_id="recovery.task-plan",
        version="1",
        stage_id="dynamic_stage",
        allowed_worker_capabilities=("recover",),
        allowed_subagent_ids=(),
        allowed_tool_ids=(),
        allowed_memory_namespaces=(),
        allowed_input_refs=("document",),
        allowed_output_roles=("analysis.result",),
        required_output_roles=("analysis.result",),
        allowed_output_schema_refs=("schema://analysis.result@1",),
        allowed_gate_refs=("ResultGate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={"recover": "recovery-worker@1"},
        required_worker_contract_refs={"recover": "recovery-worker-contract@1"},
        max_tasks=2,
        max_depth=2,
        max_parallelism=1,
        max_replans=1,
        max_task_attempts=2,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(max_turns=1, max_output_tokens=64),
        aggregate_task_budget=TaskBudget(max_turns=2, max_output_tokens=128),
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="recovery-graph",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        input_keys=("document",),
    )
    worker = _NeverCalledWorker()
    registry = TaskCapabilityRegistry(
        (
            TaskCapabilityRegistration(
                capability="recover",
                worker_binding=HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        worker.worker_id,
                        worker.worker_version,
                    ),
                    HarnessWorkerType.LLM,
                    worker,
                ),
                worker_contract_ref="recovery-worker-contract@1",
                input_schema_ref="schema://recovery-input@1",
                output_schema_ref="schema://analysis.result@1",
            ),
        )
    )
    task = TaskSpec(
        task_id="recover-task",
        objective="Recover from recorded evidence",
        worker_capability="recover",
        input_refs=("document",),
        output_contract=TaskOutputContract(
            "schema://analysis.result@1",
            "analysis.result",
        ),
        acceptance_criteria=TaskAcceptanceCriteria(("ResultGate@1",)),
        budget_request=TaskBudget(max_turns=1, max_output_tokens=64),
        retry_policy=TaskRetryPolicy(max_attempts=2),
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=TaskPlanStageIdentity("recovery-run", stage_binding),
        candidate_id="recovery-candidate",
        input_context_refs=("document",),
        tasks=(task,),
        required_output_roles=("analysis.result",),
        generated_by="recovery-planner@1",
        requested_plan_budget=PlanBuildBudget(max_builder_calls=1, max_turns=1),
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=TaskPlanValidationContext(
            run_id=candidate.run_id,
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=policy.allowed_gate_refs,
        ),
        accepted_at="2026-08-01T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    return plan, tuple(store.read_events(plan.run_id, plan.stage_id)), worker


def _lifecycle_event(event_type, sequence, plan, instance, *, input_checksum=None, output_refs=(), payload=None):
    return TaskPlanEvent.for_plan(
        event_type,
        plan,
        sequence=sequence,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=input_checksum or instance.task_definition_checksum,
        output_refs=output_refs,
        payload=payload or {},
    )


def _admission_events(plan, instance, *, sequence: int = 3):
    definition = next(item for item in plan.tasks if item.task_id == instance.task_id)
    readiness = LogicalTaskReadiness(
        task_id=instance.task_id,
        task_definition_checksum=definition.task_definition_checksum,
        logical_ready_order=(instance.task_id,),
    )
    budget_before = TaskPlanBudgetLedger.for_plan(plan)
    budget_after = budget_before.reserve((instance,))
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=budget_before.to_dict()["ledger_checksum"],
        budget_after_checksum=budget_after.to_dict()["ledger_checksum"],
    )
    return (
        TaskPlanEvent.for_plan(
            "TASK_READY",
            plan,
            sequence=sequence,
            task_id=instance.task_id,
            input_checksum=instance.task_definition_checksum,
            payload={"logical_readiness": readiness.to_dict()},
        ),
        _lifecycle_event(
            "TASK_QUEUE_ADMITTED",
            sequence + 1,
            plan,
            instance,
            payload={"queue_admission": admission.to_dict()},
        ),
    )


def _result(plan, instance):
    definition = plan.tasks[0]
    return TaskResultRecord(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        worker_ref=instance.worker_ref,
        task_checksum=instance.task_definition_checksum,
        binding_checksum=definition.binding_checksum,
        status=TaskLifecycle.SUCCEEDED,
        schema_version=TASK_PLAN_RESULT_SCHEMA_V3,
        graph_checksum=instance.graph_checksum,
        graph_id=instance.graph_id,
        graph_version=instance.graph_version,
        graph_ref=instance.graph_ref,
        graph_schema_version=instance.graph_schema_version,
        compiler_version=instance.compiler_version,
        condition_policy_version=instance.condition_policy_version,
        stage_binding_checksum=instance.stage_binding_checksum,
        stage_identity_schema=instance.stage_identity_schema,
        stage_identity_checksum=instance.stage_identity_checksum,
        result_ref="result://recover-task",
        output_refs=("artifact://recover-task",),
        output_roles=("analysis.result",),
        output_schema_ref="schema://analysis.result@1",
        verified_gate_refs=("ResultGate@1",),
        gate_evidence_refs=("evidence://result-gate",),
    )


def _attempt_history_record(
    plan,
    instance,
    outcome: TaskAttemptOutcome,
) -> TaskAttemptHistoryRecord:
    result = None
    if outcome in {TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.QUARANTINED}:
        result = _result(plan, instance)
    elif outcome is TaskAttemptOutcome.REJECTED:
        result = replace(
            _result(plan, instance),
            status=TaskLifecycle.FAILED,
            result_ref=None,
            output_refs=(),
            output_roles=(),
            error_code="verification_rejected",
        )
    return TaskAttemptHistoryRecord(
        instance=instance,
        binding_checksum=plan.tasks[0].binding_checksum,
        outcome=outcome,
        result=result,
        reason_code=(
            None
            if outcome is TaskAttemptOutcome.ACCEPTED
            else outcome.value.lower()
        ),
    )


def test_checkpoint_roundtrip_and_missing_queue_projection_recovery_are_offline():
    plan, base_events, worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    readiness_event, queue_admission_event = _admission_events(
        plan,
        instance,
        sequence=3,
    )
    readiness_events = (*base_events, readiness_event)
    readiness_report = TaskPlanReplayReducer().replay((plan,), readiness_events)
    assert readiness_report.projection.tasks[0].status is TaskLifecycle.READY
    assert readiness_report.active_task_instances == ()
    assert readiness_report.projection.consumed_budget["ledger"]["records"] == {}
    readiness_recovery = TaskPlanRecoveryService(
        queue_reader=_EmptyQueueReader()
    ).recover((plan,), readiness_events)
    assert readiness_recovery.missing_queue_projections == ()
    assert worker.calls == 0

    events = (
        *base_events,
        readiness_event,
        queue_admission_event,
    )
    report = TaskPlanReplayReducer().replay((plan,), events)
    checkpoint = TaskPlanCheckpoint.from_replay(
        "checkpoint-1",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    )
    restored = TaskPlanCheckpoint.from_dict(checkpoint.to_dict())

    assert restored.budget_snapshot == report.projection.consumed_budget
    record = restored.budget_snapshot["ledger"]["records"][instance.idempotency_key]
    assert record["instance"] == instance.to_dict()
    assert record["status"] == "RESERVED"
    assert record["reserved_revision"] == 1
    assert record["settled_revision"] is None
    assert restored.budget_snapshot["reserved_max_turns"] == instance.budget_snapshot.max_turns
    assert report.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V4
    wire_report = TaskPlanReplayReducer().replay(
        (type(plan).from_dict(plan.to_dict()),),
        tuple(TaskPlanEvent.from_dict(event.to_dict()) for event in events),
    )
    assert wire_report.replay_checksum == report.replay_checksum
    assert wire_report.projection == report.projection
    assert checkpoint.schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V4
    assert checkpoint.checkpoint_checksum.startswith("sha256:")
    assert set(checkpoint.to_dict()) == {
        "accepted_output_refs",
        "active_task_instances",
        "aggregate_checksum",
        "aggregate_ref",
        "attempt_history",
        "budget_ledger",
        "budget_snapshot",
            "checkpoint_checksum",
            "checkpoint_id",
            "compiler_version",
            "condition_policy_version",
            "created_at",
            "event_history_checksum",
            "graph_checksum",
            "graph_id",
            "graph_ref",
            "graph_schema_version",
            "graph_version",
        "last_sequence",
        "pending_terminal_results",
        "parallel_diagnostics",
        "parallel_event_sequence",
        "parallel_groups",
        "parallel_reservations",
        "parallel_spawn_operations",
        "parallel_waves",
        "plan_checksum",
        "plan_id",
        "plan_version",
        "policy_ref",
        "projection",
        "logical_ready_order",
        "active_attempt_order",
        "reducer_version",
        "replan_count",
        "replay_checksum",
        "retry_counts",
        "run_id",
            "schema_version",
            "stage_binding_checksum",
            "stage_id",
            "stage_identity_checksum",
            "stage_identity_schema",
        }

    recovery = TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover(
        (plan,),
        events,
        checkpoint=restored,
    )

    assert recovery.checkpoint_verified is True
    assert recovery.recovered_from_sequence == 4
    assert len(recovery.missing_queue_projections) == 1
    queue_task = recovery.missing_queue_projections[0]
    assert queue_task.task_id == instance.task_instance_id
    assert queue_task.payload == {}
    assert TaskPlanQueueProjection.from_task(queue_task).matches_instance(instance)
    assert worker.calls == 0


def test_retired_checkpoint_v3_payload_fails_closed() -> None:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*base_events, *_admission_events(plan, instance, sequence=3)),
    )
    checkpoint = TaskPlanCheckpoint.from_replay(
        "checkpoint-v3",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    )
    with pytest.raises(HarnessValidationError) as reducer_error:
        replace(report, reducer_version=TASK_PLAN_REPLAY_REDUCER_VERSION_V3)
    assert reducer_error.value.code == "unsupported_task_plan_replay_reducer"

    legacy_payload = checkpoint.to_dict()
    legacy_payload["schema_version"] = TASK_PLAN_CHECKPOINT_SCHEMA_V3

    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanCheckpoint.from_dict(legacy_payload)
    assert captured.value.code == "unsupported_task_plan_checkpoint_schema"


def test_jsonl_checkpoint_store_reloads_checksummed_snapshots(tmp_path) -> None:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*base_events, *_admission_events(plan, instance, sequence=3)),
    )
    checkpoint = TaskPlanCheckpoint.from_replay(
        "checkpoint-jsonl",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    )
    path = tmp_path / "checkpoints.jsonl"
    first = JsonlTaskPlanCheckpointStore(path)
    assert first.save(checkpoint) == checkpoint
    assert first.save(checkpoint) == checkpoint

    restored = JsonlTaskPlanCheckpointStore(path)
    assert restored.is_durable is True
    assert restored.load(checkpoint.checkpoint_id) == checkpoint
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_checkpoint_copies_and_validates_parallel_replay_projection() -> None:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*base_events, *_admission_events(plan, instance, sequence=3)),
    )
    parallel_report = replace(
        report,
        parallel_groups={
            "dispatch-group": {
                "group_id": "dispatch-group",
                "state": "RUNNING",
            }
        },
        parallel_waves={
            "dispatch-wave": {
                "wave_id": "dispatch-wave",
                "group_id": "dispatch-group",
                "state": "RUNNING",
            }
        },
        parallel_reservations={
            "dispatch-wave:recover-task": {
                "reservation_id": "dispatch-wave:recover-task",
                "wave_id": "dispatch-wave",
                "state": "RESERVED",
            }
        },
        parallel_diagnostics=(
            {
                "event_type": "TASK_GROUP_RECOVERY",
                "reason_code": "receipts_reconciled",
            },
        ),
        parallel_event_sequence=3,
        parallel_spawn_operations={
            "spawn-operation": {
                "group_id": "dispatch-group",
                "wave_id": "dispatch-wave",
                "task_id": instance.task_id,
                "task_instance_id": instance.task_instance_id,
                "attempt": instance.attempt,
                "operation_key": "spawn-operation",
                "status": "SPAWN_CONFIRMED",
                "budget_reservation": instance.budget_snapshot.to_dict(),
            }
        },
    )

    checkpoint = TaskPlanCheckpoint.from_replay(
        "checkpoint-parallel",
        plan,
        parallel_report,
        created_at="2026-08-01T00:00:01Z",
    )
    restored = TaskPlanCheckpoint.from_dict(checkpoint.to_dict())

    assert restored.parallel_groups == parallel_report.parallel_groups
    assert restored.parallel_waves == parallel_report.parallel_waves
    assert restored.parallel_reservations == parallel_report.parallel_reservations
    assert restored.parallel_diagnostics == parallel_report.parallel_diagnostics
    assert restored.parallel_event_sequence == 3
    assert (
        restored.parallel_spawn_operations
        == parallel_report.parallel_spawn_operations
    )
    restored.verify_replay(parallel_report)

    tampered = replace(parallel_report, parallel_event_sequence=2)
    with pytest.raises(HarnessValidationError) as exc_info:
        restored.verify_replay(tampered)
    assert exc_info.value.code == "task_plan_checkpoint_replay_mismatch"

    changed_spawn = replace(
        parallel_report,
        parallel_spawn_operations={
            "spawn-operation": {
                **parallel_report.parallel_spawn_operations["spawn-operation"],
                "status": "SPAWN_UNKNOWN",
            }
        },
    )
    with pytest.raises(HarnessValidationError) as spawn_error:
        restored.verify_replay(changed_spawn)
    assert spawn_error.value.code == "task_plan_checkpoint_replay_mismatch"
    assert "parallel_spawn_operations" in spawn_error.value.details["mismatches"]


@pytest.mark.parametrize("outcome", tuple(TaskAttemptOutcome))
def test_checkpoint_roundtrips_every_attempt_history_outcome(outcome) -> None:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*base_events, *_admission_events(plan, instance, sequence=3)),
    )
    record = _attempt_history_record(plan, instance, outcome)
    history_report = replace(report, attempt_history=(record,))

    checkpoint = TaskPlanCheckpoint.from_replay(
        f"checkpoint-{outcome.value.lower()}",
        plan,
        history_report,
        created_at="2026-08-01T00:00:01Z",
    )
    restored = TaskPlanCheckpoint.from_dict(checkpoint.to_dict())

    assert restored.attempt_history == (record,)
    assert restored.to_dict()["attempt_history"] == [record.to_dict()]
    restored.verify_replay(history_report)
    recovery = TaskPlanRecovery(
        report=history_report,
        checkpoint_verified=False,
        recovered_from_sequence=history_report.projection.last_sequence,
    )
    assert recovery.attempt_history is history_report.attempt_history


def test_checkpoint_detects_attempt_history_changes() -> None:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*base_events, *_admission_events(plan, instance, sequence=3)),
    )
    indeterminate = _attempt_history_record(
        plan,
        instance,
        TaskAttemptOutcome.INDETERMINATE,
    )
    checkpoint_report = replace(report, attempt_history=(indeterminate,))
    checkpoint = TaskPlanCheckpoint.from_replay(
        "checkpoint-attempt-history",
        plan,
        checkpoint_report,
        created_at="2026-08-01T00:00:01Z",
    )
    quarantined = _attempt_history_record(
        plan,
        instance,
        TaskAttemptOutcome.QUARANTINED,
    )

    changed_payload = checkpoint.to_dict()
    changed_payload["attempt_history"] = [quarantined.to_dict()]
    with pytest.raises(HarnessValidationError) as checksum_error:
        TaskPlanCheckpoint.from_dict(changed_payload)
    assert checksum_error.value.code == "task_plan_attempt_history_index_checksum_mismatch"

    changed_report = replace(report, attempt_history=(quarantined,))
    with pytest.raises(HarnessValidationError) as replay_error:
        checkpoint.verify_replay(changed_report)
    assert replay_error.value.code == "task_plan_checkpoint_replay_mismatch"
    assert "attempt_history" in replay_error.value.details["mismatches"]


def test_legacy_replay_rejects_plan_history_with_changed_graph_checksum():
    with pytest.raises(HarnessValidationError) as captured:
        plan, _, _ = _history_fixture()
        replace(
            plan,
            plan_id="recover-plan-2",
            version=2,
            parent_plan_id=plan.plan_id,
            graph_checksum="sha256:" + "f" * 64,
        )

    assert captured.value.code == "task_plan_stage_identity_checksum_invalid"


def test_recovery_preserves_committed_result_until_terminal_event_without_redispatch():
    plan, base_events, worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    result = _result(plan, instance)
    events_before_terminal = (
        *base_events,
        *_admission_events(plan, instance, sequence=3),
        _lifecycle_event("TASK_DISPATCHED", 5, plan, instance),
        _lifecycle_event("TASK_STARTED", 6, plan, instance),
        _lifecycle_event(
            "TASK_RESULT_ACCEPTED",
            7,
            plan,
            instance,
            output_refs=result.output_refs,
            payload={
                "result_ref": result.result_ref,
                "result_checksum": result.result_checksum,
                "gate_refs": list(result.verified_gate_refs),
                "gate_evidence_refs": list(result.gate_evidence_refs),
            },
        ),
    )

    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanReplayReducer().replay(
            (plan,),
            events_before_terminal,
            results=(result,),
        )
    assert captured.value.code == "task_plan_replay_terminal_event_missing"

    recovery = TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover(
        (plan,),
        events_before_terminal,
        results=(result,),
    )
    assert recovery.pending_terminal_results == (result,)
    assert recovery.missing_queue_projections == ()
    assert recovery.awaiting_reclaim == ()
    assert recovery.report.projection.tasks[0].status is TaskLifecycle.RUNNING
    assert worker.calls == 0

    terminal = _lifecycle_event(
        "TASK_COMPLETED",
        8,
        plan,
        instance,
        input_checksum=result.result_checksum,
        output_refs=result.output_refs,
        payload={
            "result_ref": result.result_ref,
            "result_checksum": result.result_checksum,
            "gate_refs": list(result.verified_gate_refs),
            "gate_evidence_refs": list(result.gate_evidence_refs),
        },
    )
    completed = TaskPlanReplayReducer().replay(
        (plan,),
        (*events_before_terminal, terminal),
        results=(result,),
    )
    assert completed.projection.tasks[0].status is TaskLifecycle.SUCCEEDED
    assert completed.projection.tasks[0].result.result_ref == result.result_ref
    assert completed.pending_terminal_results == ()
    assert worker.calls == 0


def test_recovery_quarantines_terminal_failure_without_durable_halt() -> None:
    plan, base_events, worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    result = replace(
        _result(plan, instance),
        status=TaskLifecycle.FAILED,
        result_ref=None,
        output_refs=(),
        output_roles=(),
        error_code="terminal_failure",
    )
    result_payload = {
        "result_ref": result.result_ref,
        "result_checksum": result.result_checksum,
        "gate_refs": list(result.verified_gate_refs),
        "gate_evidence_refs": list(result.gate_evidence_refs),
    }
    events = (
        *base_events,
        *_admission_events(plan, instance, sequence=3),
        _lifecycle_event("TASK_DISPATCHED", 5, plan, instance),
        _lifecycle_event("TASK_STARTED", 6, plan, instance),
        _lifecycle_event(
            "TASK_RESULT_REJECTED",
            7,
            plan,
            instance,
            output_refs=result.output_refs,
            payload=result_payload,
        ),
        _lifecycle_event(
            "TASK_FAILED",
            8,
            plan,
            instance,
            input_checksum=result.result_checksum,
            output_refs=result.output_refs,
            payload=result_payload,
        ),
    )

    with pytest.raises(HarnessValidationError) as missing_halt:
        TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover((plan,), events, results=(result,))
    assert missing_halt.value.code == "task_plan_recovery_halt_missing"
    assert worker.calls == 0

    halted = TaskPlanEvent.for_plan(
        "TASK_PLAN_HALTED",
        plan,
        sequence=9,
        reason_code="terminal_failure",
        payload={
            "diagnostic_ref": canonical_payload_checksum(
                {"reason_code": "terminal_failure"}
            )
        },
    )
    recovery = TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover(
        (plan,),
        (*events, halted),
        results=(result,),
    )
    assert recovery.missing_queue_projections == ()
    assert recovery.awaiting_reclaim == ()
    assert worker.calls == 0


def test_replay_fails_closed_for_missing_result_and_tampered_event_checksum():
    plan, base_events, _ = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    result = _result(plan, instance)
    events = (
        *base_events,
        *_admission_events(plan, instance, sequence=3),
        _lifecycle_event("TASK_DISPATCHED", 5, plan, instance),
        _lifecycle_event(
            "TASK_RESULT_ACCEPTED",
            6,
            plan,
            instance,
            output_refs=result.output_refs,
            payload={
                "result_ref": result.result_ref,
                "result_checksum": result.result_checksum,
                "gate_refs": list(result.verified_gate_refs),
                "gate_evidence_refs": list(result.gate_evidence_refs),
            },
        ),
    )
    with pytest.raises(HarnessValidationError) as missing:
        TaskPlanReplayReducer().replay((plan,), events)
    assert missing.value.code == "task_plan_replay_result_missing"

    tampered = replace(events[2])
    object.__setattr__(tampered, "event_checksum", "sha256:" + "0" * 64)
    with pytest.raises(HarnessValidationError) as checksum_error:
        TaskPlanReplayReducer().replay((plan,), (*base_events, tampered))
    assert checksum_error.value.code == "task_plan_replay_event_checksum_mismatch"


def test_replay_requires_patch_document_and_binds_it_to_the_next_plan_version():
    plan, base_events, _ = _history_fixture()
    patch = PlanPatch.for_plan(
        plan,
        patch_id="recovery-patch",
        reason_code="repair",
        source_candidate_ref="candidate://recovery-patch",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.SKIP_PENDING_TASK,
                target_task_id="recover-task",
            ),
        ),
    )
    patched = replace(
        plan,
        plan_id="recovery-plan-v2",
        version=2,
        parent_plan_id=plan.plan_id,
        source_candidate_ref=patch.patch_checksum,
        accepted_at="2026-08-01T00:00:02Z",
    )
    patch_event = TaskPlanEvent.for_plan(
        "PLAN_PATCH_ACCEPTED",
        plan,
        sequence=3,
        input_checksum=patch.patch_checksum,
        reason_code=patch.reason_code,
        payload={"patch_ref": patch.patch_checksum},
    )
    plan_event = TaskPlanEvent.for_plan(
        "PLAN_ACCEPTED",
        patched,
        sequence=4,
        input_checksum=patched.plan_checksum,
        payload={"plan_ref": patched.plan_checksum, "policy_ref": patched.policy_ref},
    )
    events = (*base_events, patch_event, plan_event)
    replay = TaskPlanReplayReducer().replay(
        (plan, patched), events, patches=(patch,)
    )
    assert replay.projection.plan_version == 2
    reduced = TaskPlanReplayReducer().reduce(
        (plan, patched),
        events,
        patches=(patch,),
        require_terminal_events=True,
    )
    recovery = TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover(
        (plan, patched),
        events,
        patches=(patch,),
    )
    assert reduced.projection_checksum == replay.projection.projection_checksum
    assert recovery.report.projection.projection_checksum == reduced.projection_checksum
    assert recovery.report.projection.plan_version == 2

    with pytest.raises(HarnessValidationError) as missing:
        TaskPlanReplayReducer().replay((plan, patched), events)
    assert missing.value.code == "task_plan_replay_patch_missing"

    wrong_parent = replace(
        patched,
        parent_plan_id="different-parent",
        source_candidate_ref=patch.patch_checksum,
    )
    with pytest.raises(HarnessValidationError) as mismatch:
        TaskPlanReplayReducer().replay((plan, wrong_parent), events, patches=(patch,))
    assert mismatch.value.code == "task_plan_replay_plan_parent_mismatch"


def test_replay_binds_aggregate_checksum_to_recorded_branch_evidence():
    plan, base_events, _ = _history_fixture()
    output_refs_by_role = {"analysis.result": "result://recover-task"}
    result_refs = ("result://recover-task",)
    branch_refs = (
        {
            "role": "analysis.result",
            "output_ref": "result://recover-task",
            "producer_node_id": "recover-task",
            "output_key": "result",
        },
    )
    aggregate_checksum = canonical_payload_checksum(
        {
            "roles": output_refs_by_role,
            "result_refs": list(result_refs),
            "branch_refs": list(branch_refs),
        }
    )
    aggregate_ref = f"task-plan-aggregate:{aggregate_checksum}"
    aggregate_event = TaskPlanEvent.for_plan(
        "STAGE_OUTPUT_AGGREGATED",
        plan,
        sequence=3,
        input_checksum=aggregate_checksum,
        output_refs=tuple(output_refs_by_role.values()),
        payload={
            "aggregate_ref": aggregate_ref,
            "aggregate_checksum": aggregate_checksum,
            "output_refs_by_role": output_refs_by_role,
            "result_refs": list(result_refs),
            "branch_refs": list(branch_refs),
        },
    )
    verified_event = TaskPlanEvent.for_plan(
        "TASK_PLAN_VERIFIED",
        plan,
        sequence=4,
        input_checksum=aggregate_checksum,
        output_refs=tuple(output_refs_by_role.values()),
    )
    report = TaskPlanReplayReducer().replay(
        (plan,), (*base_events, aggregate_event, verified_event)
    )
    assert report.aggregate_checksum == aggregate_checksum

    tampered_payload = dict(aggregate_event.payload)
    tampered_payload["branch_refs"] = [
        {**branch_refs[0], "producer_node_id": "tampered-task"}
    ]
    tampered_event = replace(aggregate_event, payload=tampered_payload)
    with pytest.raises(HarnessValidationError) as mismatch:
        TaskPlanReplayReducer().replay(
            (plan,), (*base_events, tampered_event, verified_event)
        )
    assert mismatch.value.code == "task_plan_replay_aggregate_mismatch"
