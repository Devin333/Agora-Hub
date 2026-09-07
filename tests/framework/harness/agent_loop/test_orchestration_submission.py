from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Thread
from time import monotonic, sleep

import pytest

from framework.agent.models.orchestration import (
    PARENT_OBSERVATION_REJECTED_SCHEMA,
    PARENT_OBSERVATION_SCHEMA,
    AgentOrchestrationResult,
    ParentObservation,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum, thaw_mapping
from framework.harness.task_plan.ports import TaskPlanStageRequest
from framework.harness.task_plan.parallel import DispatchGroup
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskPlanEvent, TaskResultRecord
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.workers.result import HarnessWorkerResult
from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime, _request
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore, _store,
)
from tests.framework.harness.task_plan.test_candidate_submission_store import _ConcurrentEventStore


@pytest.fixture(params=["memory", "durable"])
def store_factory(request):
    if request.param == "memory":
        store = InMemoryTaskPlanStore()
        return lambda: store
    events, artifacts = _ConcurrentEventStore(), _ArtifactStore()
    return lambda: _store(events, artifacts)


def _counting_worker(calls):
    def execute(_binding, task, _identity):
        calls.append(task)
        return HarnessWorkerResult(status="succeeded", output={"summary": "completed"})
    return execute


def _submission_identity(request):
    return CandidateDedupIdentity(
        run_id=request.run_id, stage_id="delegate_stage",
        parent_turn_id=request.parent_turn_id,
        action_correlation_id=request.candidate.correlation_id,
    )


def test_terminal_resubmission_reopens_without_new_events_or_worker_calls(store_factory):
    calls = []
    store = store_factory()
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    request = _request(identity)
    first = runtime.dispatch(request)
    assert first.status == "succeeded"
    assert len(calls) == 2
    before = store.read_events(request.run_id, "delegate_stage")
    plan = store.plan(request.run_id, "delegate_stage")

    recovered_store = store_factory()
    recovered, _ = _runtime(store=recovered_store, worker_executor=_counting_worker(calls))
    second = recovered.dispatch(request)

    assert second.to_dict() == first.to_dict()
    assert len(calls) == 2
    assert recovered_store.read_events(request.run_id, "delegate_stage") == before
    assert recovered_store.plan(request.run_id, "delegate_stage") == plan
    assert recovered._stage_runner.parallel_coordinator._sessions == {}


def test_terminal_resubmission_uses_persisted_candidate_not_current_profiles(store_factory):
    calls = []
    store = store_factory()
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    request = _request(identity)
    first = runtime.dispatch(request)
    assert first.status == "succeeded"
    before = store.read_events(request.run_id, "delegate_stage")
    reopened, _ = _runtime(store=store_factory(), worker_executor=_counting_worker(calls))

    def unexpected_materialization(*_args):
        pytest.fail("resubmission must not materialize a new candidate")

    reopened._materialize_candidate = unexpected_materialization
    assert reopened.dispatch(request).to_dict() == first.to_dict()
    assert len(calls) == 2
    assert store.read_events(request.run_id, "delegate_stage") == before


def test_active_resubmission_cannot_recover_or_halt_original_execution(store_factory):
    release = Event()
    calls, original_results = [], []

    def worker(_binding, task, _identity):
        calls.append(task)
        assert release.wait(15)
        return HarnessWorkerResult(status="succeeded", output={"summary": "done"})

    store = store_factory()
    runtime, identity = _runtime(store=store, worker_executor=worker)
    request = _request(identity)
    thread = Thread(target=lambda: original_results.append(runtime.dispatch(request)))
    thread.start()
    try:
        deadline = monotonic() + 10
        while monotonic() < deadline:
            before = store.read_events(identity.run_id, "delegate_stage")
            if len(calls) == 2 and any(event.event_type == "TASK_WAVE_DISPATCHED" for event in before):
                break
            sleep(0.005)
        else:
            pytest.fail("original dispatch never reached its active wave")
        restarted, _ = _runtime(store=store_factory(), worker_executor=worker)
        repeated = restarted.dispatch(request)
        assert repeated.reason_code == "task_plan_submission_resume_required"
        assert repeated.status == "rejected"
        assert repeated.observation.group_id is None
        assert repeated.submission_receipt is not None
        assert repeated.submission_receipt.wait_status == "pending"
        assert repeated.submission_receipt.dedup_status == "accepted"
        assert repeated.submission_receipt.submission_id.startswith("candidate-submission-")
        assert repeated.submission_receipt.group_id is not None
        assert store.read_events(identity.run_id, "delegate_stage") == before
        assert restarted._stage_runner.parallel_coordinator._sessions == {}
        assert len(calls) == 2
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive()
    assert original_results[0].status == "succeeded"
    final_events = store.read_events(identity.run_id, "delegate_stage")
    assert not any(event.event_type.startswith("RECOVERY_") for event in final_events)
    replayed = restarted.dispatch(request)
    assert replayed.to_dict() == original_results[0].to_dict()
    assert store.read_events(identity.run_id, "delegate_stage") == final_events
    assert len(calls) == 2


def test_terminal_redelivery_returns_the_same_submission_receipt(store_factory):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    request = _request(identity)

    first = runtime.dispatch(request)
    repeated = runtime.dispatch(request)

    assert first.status == repeated.status == "succeeded"
    assert first.submission_receipt is not None
    assert repeated.submission_receipt is not None
    assert repeated.submission_receipt.to_dict() == first.submission_receipt.to_dict()
    assert repeated.submission_receipt.wait_status == "terminal"
    assert repeated.submission_receipt.dedup_status == "accepted"

    restored = AgentOrchestrationResult.from_dict(first.to_dict())
    assert restored == first
    tampered = first.to_dict()
    tampered["submission_receipt"]["candidate_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="submission receipt"):
        AgentOrchestrationResult.from_dict(tampered)


def test_rejected_observation_uses_a_new_schema_boundary():
    payload = ParentObservation(
        group_id=None,
        group_status="rejected",
        plan_version=None,
        diagnostics=("CANDIDATE_IDEMPOTENCY_CONFLICT",),
        schema_version=PARENT_OBSERVATION_REJECTED_SCHEMA,
    ).to_dict()

    assert payload["schema_version"] == PARENT_OBSERVATION_REJECTED_SCHEMA
    assert ParentObservation.from_dict(payload).group_id is None
    with pytest.raises(ValueError, match="v1 requires"):
        ParentObservation.from_dict({**payload, "schema_version": PARENT_OBSERVATION_SCHEMA})


def test_racing_first_submissions_start_only_one_execution(store_factory):
    calls = []
    barrier = Barrier(2)
    runtimes = [_runtime(store=store_factory(), worker_executor=_counting_worker(calls)) for _ in range(2)]
    for runtime, _ in runtimes:
        materialize = runtime._materialize_candidate

        def synchronize(request, policy, original=materialize):
            candidate = original(request, policy)
            barrier.wait(timeout=10)
            return candidate

        runtime._materialize_candidate = synchronize
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda pair: pair[0].dispatch(_request(pair[1])), runtimes))
    assert any(result.status == "succeeded" for result in outcomes)
    assert all(result.status == "succeeded" or result.reason_code == "task_plan_submission_resume_required" for result in outcomes)
    assert len(calls) == 2
    events = store_factory().read_events(runtimes[0][1].run_id, "delegate_stage")
    for kind in ("PLAN_CANDIDATE_BUILT", "PLAN_ACCEPTED", "TASK_GROUP_ADMITTED", "TASK_PLAN_VERIFIED"):
        assert sum(event.event_type == kind for event in events) == 1
    assert not any(event.event_type.startswith("RECOVERY_") for event in events)


def test_first_group_must_bind_its_original_submission_before_commit_or_replay(store_factory, monkeypatch):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    parent = _request(identity)
    policy = runtime._policy_registry.resolve(parent.policy_ref, stage_id="delegate_stage")
    request = TaskPlanStageRequest(
        run_id=identity.run_id, stage_binding=runtime._stage_binding,
        context_refs={"document": "document"}, policy=policy,
        accepted_at="2026-09-07T00:00:00Z",
        candidate=runtime._materialize_candidate(parent, policy),
        submission_identity=_submission_identity(parent),
        source_candidate_checksum=canonical_payload_checksum(parent.candidate.to_dict()),
        execution_identity=runtime._task_plan_execution_identity(identity, parent.candidate),
    )
    plan = runtime._stage_runner._ensure_plan(request)
    group = runtime._stage_runner.parallel_coordinator.create_group(
        runtime._stage_runner._parallel_request(request, plan, task_instances=()),
    )
    assert group.correlation_id == request.submission_identity.dedup_key
    assert DispatchGroup.from_dict(group.to_dict()).group_id == group.group_id
    altered = replace(group, correlation_id="another-submission")
    current = store.load_projection(identity.run_id, "delegate_stage")
    before = store.read_events(identity.run_id, "delegate_stage")
    event = TaskPlanEvent.for_plan("TASK_GROUP_ADMITTED", plan,
        sequence=current.last_sequence + 1, payload={
            "event_type": "TASK_GROUP_ADMITTED",
            "parallel_event_idempotency_key": "TASK_GROUP_ADMITTED:" + altered.group_id,
            "idempotency_key": altered.group_id,
            "group": altered.to_dict(), "requested_parallelism": 2,
            "effective_parallelism": 2,
        })
    reopened = store_factory()
    if hasattr(reopened, "_put_projection"):
        monkeypatch.setattr(reopened, "_put_projection", lambda *_: pytest.fail("rejected group must not write artifacts"))
    with pytest.raises(HarnessValidationError) as rejected:
        reopened.commit_event(event, replace(current, last_sequence=event.sequence))
    assert rejected.value.code == "task_plan_submission_binding_conflict"
    assert reopened.read_events(identity.run_id, "delegate_stage") == before
    assert reopened.load_projection(identity.run_id, "delegate_stage") == current
    with pytest.raises(HarnessValidationError) as replayed:
        TaskPlanReplayReducer().replay((plan,), (*before, event), require_terminal_events=False)
    assert replayed.value.code == "task_plan_submission_binding_conflict"
    monkeypatch.undo()
    valid = replace(event, payload={
        **thaw_mapping(event.payload), "group": group.to_dict(),
        "parallel_event_idempotency_key": "TASK_GROUP_ADMITTED:" + group.group_id,
        "idempotency_key": group.group_id,
    })
    reopened.commit_event(valid, replace(current, last_sequence=valid.sequence))
    replay = TaskPlanReplayReducer().replay(
        (plan,), reopened.read_events(identity.run_id, "delegate_stage"), require_terminal_events=False,
    )
    assert group.group_id in replay.parallel_groups


@pytest.mark.parametrize("writer", ("append", "batch", "commit"))
def test_terminal_submission_cannot_be_rewritten_at_a_new_sequence(store_factory, writer, monkeypatch):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    request = _request(identity)
    original = runtime.dispatch(request)
    assert original.status == "succeeded"
    before = store.read_events(identity.run_id, "delegate_stage")
    current = store.load_projection(identity.run_id, "delegate_stage")
    plan = store.plan(identity.run_id, "delegate_stage")
    result = HarnessWorkerResult(status="blocked", error="forged_halt",
        diagnostics={"reason_code": "forged_halt"})
    event = TaskPlanEvent.for_plan("TASK_PLAN_HALTED", plan, sequence=len(before) + 1,
        reason_code="forged_halt", payload={
            "submission_key": _submission_identity(request).dedup_key,
            "terminal_result": result.to_dict(),
            "terminal_result_checksum": result.candidate_result_ref,
        })
    reopened = store_factory()
    if hasattr(reopened, "_put_projection"):
        monkeypatch.setattr(reopened, "_put_projection", lambda *_: pytest.fail("terminal rewrite must not write artifacts"))
    with pytest.raises(HarnessValidationError) as rejected:
        if writer == "append":
            reopened.append_event(event)
        elif writer == "batch":
            reopened.append_events((event,))
        else:
            reopened.commit_event(event, replace(current, last_sequence=event.sequence))
    assert rejected.value.code == "task_plan_submission_result_invalid"
    assert reopened.read_events(identity.run_id, "delegate_stage") == before
    assert reopened.load_projection(identity.run_id, "delegate_stage") == current
    with pytest.raises(HarnessValidationError) as replayed:
        TaskPlanReplayReducer().replay((plan,), (*before, event),
            results=store.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version))
    assert replayed.value.code == "task_plan_submission_result_invalid"


@pytest.mark.parametrize("change", ["objective", "invalid_capability", "parallelism"])
def test_conflicting_candidate_never_receives_old_group_results(store_factory, change):
    calls = []
    store = store_factory()
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    request = _request(identity)
    original = runtime.dispatch(request)
    assert original.status == "succeeded"
    before = store.read_events(request.run_id, "delegate_stage")
    projection = store.load_projection(request.run_id, "delegate_stage")
    candidate = request.candidate
    if change == "parallelism":
        candidate = replace(candidate, parallelism_hint=1)
    else:
        replacement = {"objective": "Changed analysis goal"} if change == "objective" else {"capability_hint": "not-registered"}
        candidate = replace(candidate, tasks=(replace(candidate.tasks[0], **replacement), *candidate.tasks[1:]))
    restarted, _ = _runtime(store=store_factory(), worker_executor=_counting_worker(calls))
    rejected = restarted.dispatch(replace(request, candidate=candidate))

    assert rejected.reason_code == "CANDIDATE_IDEMPOTENCY_CONFLICT"
    assert rejected.status != "succeeded"
    assert rejected.observation.group_id is None
    assert rejected.submission_receipt is not None
    assert rejected.submission_receipt.submission_id == original.submission_receipt.submission_id
    assert rejected.observation.result_refs == ()
    assert rejected.observation.task_summaries == ()
    assert rejected.observation.aggregate_ref is None
    assert len(calls) == 2
    assert store.read_events(request.run_id, "delegate_stage") == before
    assert store.load_projection(request.run_id, "delegate_stage") == projection


@pytest.mark.parametrize("field", ["parent_turn_id", "action_correlation_id"])
def test_new_submission_is_not_silently_mapped_to_the_existing_stage(store_factory, field):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    request = _request(identity)
    first = runtime.dispatch(request)
    assert first.status == "succeeded"
    before = store.read_events(request.run_id, "delegate_stage")
    other = (
        replace(request, parent_turn_id="another-parent-turn") if field == "parent_turn_id"
        else replace(request, candidate=replace(request.candidate, correlation_id="another-action"))
    )
    rejected = runtime.dispatch(other)
    assert rejected.reason_code == "task_plan_submission_scope_unavailable"
    assert rejected.observation.aggregate_ref is None
    assert rejected.observation.result_refs == ()
    assert store.read_events(request.run_id, "delegate_stage") == before


def test_restart_between_candidate_commit_and_plan_acceptance_reuses_original_time(store_factory):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    request = _request(identity)
    policy = runtime._policy_registry.resolve(request.policy_ref, stage_id="delegate_stage")
    candidate = runtime._materialize_candidate(request, policy)
    submission = store.admit_candidate_submission(
        candidate, _submission_identity(request), accepted_at="2026-09-05T01:00:00Z",
        candidate_checksum=canonical_payload_checksum(request.candidate.to_dict()),
    )
    restarted_store = store_factory()
    restarted, _ = _runtime(store=restarted_store)
    before = restarted_store.read_events(request.run_id, "delegate_stage")
    deferred = restarted.dispatch(request)
    assert deferred.reason_code == "task_plan_submission_resume_required"
    assert restarted_store.read_events(request.run_id, "delegate_stage") == before
    result = restarted.recover_submission(request)

    assert result.status == "succeeded"
    plan = restarted_store.plan(request.run_id, "delegate_stage")
    assert plan.plan_id == submission.plan_id
    assert plan.accepted_at == submission.accepted_at
    events = restarted_store.read_events(request.run_id, "delegate_stage")
    assert sum(item.event_type == "PLAN_CANDIDATE_BUILT" for item in events) == 1
    assert sum(item.event_type == "PLAN_ACCEPTED" for item in events) == 1


def test_rejected_candidate_resubmission_reuses_pre_plan_outcome(store_factory):
    calls = []
    store = store_factory()
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    parent = _request(identity)
    policy = runtime._policy_registry.resolve(parent.policy_ref, stage_id="delegate_stage")
    candidate = runtime._materialize_candidate(parent, policy)
    invalid = replace(candidate, tasks=(
        replace(candidate.tasks[0], worker_capability="not-registered"), *candidate.tasks[1:],
    ))
    request = TaskPlanStageRequest(
        run_id=identity.run_id, stage_binding=runtime._stage_binding,
        context_refs={"document": "document"}, policy=policy,
        accepted_at="2026-09-07T00:00:00Z", candidate=invalid,
        submission_identity=_submission_identity(parent),
        execution_identity=runtime._task_plan_execution_identity(identity, parent.candidate),
    )
    first = runtime._stage_runner.run(request)
    assert first.status.value == "blocked"
    assert store.plan(identity.run_id, "delegate_stage") is None
    before = store.read_events(identity.run_id, "delegate_stage")
    assert before[-1].event_type == "TASK_PLAN_HALTED"
    assert before[-1].plan_id is None
    reopened, _ = _runtime(store=store_factory(), worker_executor=_counting_worker(calls))

    def unexpected_validation(*_args, **_kwargs):
        pytest.fail("terminal rejection must not revalidate the candidate")

    reopened._stage_runner.validator.validate = unexpected_validation
    repeated = reopened._stage_runner.run(replace(request, candidate=None))
    assert repeated.to_dict() == first.to_dict()
    assert calls == []
    assert reopened._store.read_events(identity.run_id, "delegate_stage") == before


def test_accepted_plan_and_terminal_event_redelivery_are_noops(store_factory):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    assert runtime.dispatch(_request(identity)).status == "succeeded"
    before = store.read_events(identity.run_id, "delegate_stage")
    plan = store.plan(identity.run_id, "delegate_stage")
    reopened = store_factory()
    assert reopened.accept_plan(plan) == plan.plan_checksum
    assert reopened.append_events((before[-1],)) == (before[-1].event_checksum,)
    assert reopened.read_events(identity.run_id, "delegate_stage") == before


def test_restart_with_no_candidate_reads_durable_candidate_without_calling_builder(store_factory):
    store = store_factory()
    runtime, identity = _runtime(store=store)
    parent_request = _request(identity)
    policy = runtime._policy_registry.resolve(parent_request.policy_ref, stage_id="delegate_stage")
    candidate = runtime._materialize_candidate(parent_request, policy)
    submission = store.admit_candidate_submission(
        candidate, _submission_identity(parent_request), accepted_at="2026-09-05T01:00:00Z",
        candidate_checksum=canonical_payload_checksum(parent_request.candidate.to_dict()),
    )
    recovered, _ = _runtime(store=store_factory())
    result = recovered._stage_runner.run(TaskPlanStageRequest(
        run_id=identity.run_id, stage_binding=runtime._stage_binding, context_refs={"document": "document"},
        policy=policy, accepted_at="2026-09-05T02:00:00Z",
        submission_identity=submission.identity,
        execution_identity=runtime._task_plan_execution_identity(identity, parent_request.candidate),
    ))
    assert result.status.value == "succeeded"
    assert recovered._store.plan(identity.run_id, "delegate_stage").accepted_at == submission.accepted_at


class _RejectingVerifier:
    registered_gate_refs = ("gate@1",)

    def verify(self, _result, *, task, request):
        instance = request.instance
        return TaskResultRecord.for_plan(
            request.plan, task_id=instance.task_id,
            task_instance_id=instance.task_instance_id, attempt=instance.attempt,
            status="failed", error_code="gate_failed",
        )


def test_terminal_failure_reuses_original_observation_without_new_attempts(store_factory):
    calls = []
    store = store_factory()
    runtime, identity = _runtime(
        store=store, worker_executor=_counting_worker(calls), result_verifier=_RejectingVerifier(),
    )
    request = _request(identity)
    failed = runtime.dispatch(request)
    assert failed.status == "partial_failure"
    count = len(calls)
    assert count == 2
    before = store.read_events(request.run_id, "delegate_stage")
    recovered, _ = _runtime(store=store_factory(), worker_executor=_counting_worker(calls))
    repeated = recovered.dispatch(request)
    assert repeated.to_dict() == failed.to_dict()
    assert len(calls) == count
    assert store.read_events(request.run_id, "delegate_stage") == before


@pytest.mark.parametrize("field", [
    "aggregate_ref", "submission_key", "checksum", "accepted_at", "missing_outcome",
    "late_candidate", "diagnostics",
])
def test_replay_rejects_tampered_submission_outcome_without_live_calls(field):
    store = InMemoryTaskPlanStore()
    calls = []
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    request = _request(identity)
    assert runtime.dispatch(request).status == "succeeded"
    events = list(store.read_events(identity.run_id, "delegate_stage"))
    plan = store.plan(identity.run_id, "delegate_stage")
    if field == "accepted_at":
        plans = (replace(plan, accepted_at="2026-01-01T00:00:00Z"),)
        accepted_index = next(i for i, e in enumerate(events) if e.event_type == "PLAN_ACCEPTED")
        accepted = events[accepted_index]
        events[accepted_index] = replace(accepted, input_checksum=plans[0].plan_checksum,
                                         payload={**thaw_mapping(accepted.payload), "plan_ref": plans[0].plan_checksum})
    elif field == "late_candidate":
        plans = (plan,)
        candidate = events.pop(0)
        assert candidate.event_type == "PLAN_CANDIDATE_BUILT"
        events.insert(1, candidate)
        events = [replace(event, sequence=i + 1) for i, event in enumerate(events)]
    else:
        plans = (plan,)
        event = events[-1]
        assert event.event_type == "TASK_PLAN_VERIFIED"
        payload = thaw_mapping(event.payload)
        if field == "aggregate_ref":
            payload["terminal_result"]["output"]["aggregate_ref"] = "artifact://forged-output"
            payload["terminal_result_checksum"] = HarnessWorkerResult.from_dict(payload["terminal_result"]).candidate_result_ref
        elif field == "submission_key":
            payload["submission_key"] = canonical_payload_checksum({"another": "submission"})
        elif field == "diagnostics":
            payload["terminal_result"]["diagnostics"]["plan_id"] = "another-plan"
            payload["terminal_result_checksum"] = HarnessWorkerResult.from_dict(payload["terminal_result"]).candidate_result_ref
        elif field == "missing_outcome":
            for key in ("submission_key", "terminal_result", "terminal_result_checksum"):
                del payload[key]
        else:
            payload["terminal_result_checksum"] = canonical_payload_checksum({"wrong": "checksum"})
        events[-1] = replace(event, payload=payload)
    with pytest.raises(HarnessValidationError) as rejected:
        TaskPlanReplayReducer().replay(
            plans, events, results=store.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version),
        )
    assert rejected.value.code in {"task_plan_submission_result_invalid", "task_plan_replay_candidate_mismatch"}
    assert len(calls) == 2


def test_stage_rejects_changed_candidate_without_submission_identity():
    store = InMemoryTaskPlanStore()
    runtime, identity = _runtime(store=store)
    request = _request(identity)
    policy = runtime._policy_registry.resolve(request.policy_ref, stage_id="delegate_stage")
    candidate = runtime._materialize_candidate(request, policy)
    stage_request = TaskPlanStageRequest(
        run_id=identity.run_id, stage_binding=runtime._stage_binding,
        context_refs={"document": "document"}, policy=policy,
        accepted_at="2026-09-05T00:00:00Z", candidate=candidate,
        execution_identity=runtime._task_plan_execution_identity(identity, request.candidate),
    )
    assert runtime._stage_runner.run(stage_request).status.value == "succeeded"
    before = store.read_events(identity.run_id, "delegate_stage")
    changed = replace(candidate, tasks=(replace(candidate.tasks[0], objective="Changed goal"), *candidate.tasks[1:]))
    rejected = runtime._stage_runner.run(replace(stage_request, candidate=changed))
    assert rejected.diagnostics["reason_code"] == "task_plan_candidate_conflict"
    assert rejected.output == {}
    assert store.read_events(identity.run_id, "delegate_stage") == before


def test_corrupt_terminal_cache_fails_closed_without_reexecuting_workers():
    store = InMemoryTaskPlanStore()
    calls = []
    runtime, identity = _runtime(store=store, worker_executor=_counting_worker(calls))
    request = _request(identity)
    assert runtime.dispatch(request).status == "succeeded"
    events = store._events[(identity.run_id, "delegate_stage")]
    terminal = events[-1]
    payload = thaw_mapping(terminal.payload)
    del payload["submission_key"]
    events[-1] = replace(terminal, payload=payload)
    before = tuple(events)
    restarted, _ = _runtime(store=store, worker_executor=_counting_worker(calls))
    result = restarted.dispatch(request)
    assert result.reason_code == "task_plan_submission_result_invalid"
    assert result.observation.result_refs == ()
    assert result.observation.aggregate_ref is None
    assert len(calls) == 2
    assert tuple(events) == before
