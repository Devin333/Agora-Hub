from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.continuation import (
    PARENT_CONTINUATION_EVENT,
    ParentContinuation,
)
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.parallel import (
    ParallelAgentCoordinator,
    SerialTaskExecutorAdapter,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskPlanEvent
from framework.harness.task_plan.submission import CandidateDedupIdentity
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    FIXED_NOW,
    _accepted_plan,
    _store,
    _task,
)
from tests.framework.harness.task_plan.test_parallel_admission_transactions import (
    _durable_admission_setup,
    _parallel_event,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import _request


def _admitted_history():
    candidate, plan, _, _ = _accepted_plan(
        (
            _task("a"),
            _task("b", capability="research.helper", role="analysis.helper"),
        ),
        two_tasks=True,
        explicit_execution_budget=True,
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    group = coordinator.create_group(replace(_request(plan), serial_fallback=True))
    current = store.load_projection(plan.run_id, plan.stage_id)
    admission = _parallel_event(
        plan,
        "TASK_GROUP_ADMITTED",
        current.last_sequence + 1,
        {
            "group": group.to_dict(),
            "requested_parallelism": 2,
            "effective_parallelism": 1,
        },
    )
    store.commit_event(
        admission,
        replace(current, last_sequence=admission.sequence),
    )
    return store, plan, group, tuple(store.read_events(plan.run_id, plan.stage_id))


def _continuation_event(
    plan,
    group,
    sequence: int,
    *,
    version: int = 1,
    group_id: str | None = None,
    run_id: str | None = None,
    stage_id: str | None = None,
    parent_turn_id: str = "parent-turn-1",
    observation_id: str = "observation-1",
    observation_checksum: str | None = None,
    status: str = "PENDING",
    group_state: str | None = None,
    submission_id: str | None = None,
):
    continuation = ParentContinuation(
        run_id=run_id or plan.run_id,
        stage_id=stage_id or plan.stage_id,
        parent_turn_id=parent_turn_id,
        observation_id=observation_id,
        observation_version=version,
        group_id=group_id or group.group_id,
        observation_checksum=observation_checksum or "sha256:" + "a" * 64,
        status=status,
        group_state=group_state or group.state.value,
        submission_id=submission_id,
    )
    return TaskPlanEvent.for_plan(
        PARENT_CONTINUATION_EVENT,
        plan,
        sequence=sequence,
        payload={
            "event_type": PARENT_CONTINUATION_EVENT,
            "parallel_event_idempotency_key": f"continuation:{version}:{sequence}",
            "idempotency_key": f"continuation:{version}",
            "continuation": continuation.to_dict(),
        },
    )


def _continuation_owner_fixture(tmp_path, backend: str):
    if backend == "memory":
        store, plan, group, _history = _admitted_history()
        return store, plan, group, None, None
    store, events, artifacts, plan, group, _initial, admission, projection = (
        _durable_admission_setup(tmp_path, backend="sqlite")
    )
    store.commit_event(admission, projection)
    return store, plan, group, events, artifacts


def test_replay_accepts_exact_duplicate_pending_continuation() -> None:
    _store, plan, group, history = _admitted_history()
    first = _continuation_event(plan, group, len(history) + 1)
    duplicate = _continuation_event(plan, group, len(history) + 2)

    report = TaskPlanReplayReducer().replay(
        (plan,),
        (*history, first, duplicate),
        require_terminal_events=False,
    )

    assert report.continuation is not None
    assert report.continuation["observation_version"] == 1
    assert report.continuation["continuation_checksum"] == first.payload["continuation"]["continuation_checksum"]


def test_replay_rejects_pending_continuation_version_gap() -> None:
    _store, plan, group, history = _admitted_history()
    first = _continuation_event(plan, group, len(history) + 1, version=1)
    gap = _continuation_event(plan, group, len(history) + 2, version=3)

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanReplayReducer().replay(
            (plan,),
            (*history, first, gap),
            require_terminal_events=False,
        )

    assert exc_info.value.code == "parent_continuation_version_conflict"


def test_replay_rejects_pending_continuation_for_unknown_group() -> None:
    _store, plan, group, history = _admitted_history()
    unknown = _continuation_event(
        plan,
        group,
        len(history) + 1,
        group_id="unknown-group",
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanReplayReducer().replay(
            (plan,),
            (*history, unknown),
            require_terminal_events=False,
        )

    assert exc_info.value.code == "parent_continuation_scope_mismatch"


@pytest.mark.parametrize("backend", ("memory", "sqlite"))
@pytest.mark.parametrize(
    ("forgery", "expected_code"),
    (
        ("parent", "parent_continuation_scope_mismatch"),
        ("terminal", "parent_continuation_terminal_mismatch"),
    ),
)
def test_continuation_owner_rejects_self_consistent_parent_or_terminal_forgery(
    tmp_path,
    backend: str,
    forgery: str,
    expected_code: str,
) -> None:
    store, plan, group, event_store, artifacts = _continuation_owner_fixture(
        tmp_path,
        backend,
    )
    current = store.load_projection(plan.run_id, plan.stage_id)
    pending = _continuation_event(plan, group, current.last_sequence + 1)
    pending_projection = replace(current, last_sequence=pending.sequence)
    store.commit_event(pending, pending_projection)
    history = store.read_events(plan.run_id, plan.stage_id)
    projection = store.load_projection(plan.run_id, plan.stage_id)
    results = store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    )
    before_report = TaskPlanReplayReducer().replay(
        (plan,),
        history,
        require_terminal_events=False,
    )
    high_watermark = (
        event_store.get_stream_high_watermark(f"run:{plan.run_id}")
        if event_store is not None
        else None
    )
    immutable_artifacts = dict(artifacts._content) if artifacts is not None else None

    if forgery == "parent":
        forged = _continuation_event(
            plan,
            group,
            projection.last_sequence + 1,
            parent_turn_id="forged-parent-turn",
            observation_id="forged-parent-observation",
            submission_id="forged-submission",
            observation_checksum="sha256:" + "b" * 64,
        )
    else:
        forged = _continuation_event(
            plan,
            group,
            projection.last_sequence + 1,
            version=2,
            observation_id="forged-terminal-observation",
            observation_checksum="sha256:" + "b" * 64,
            status="DELIVERED",
            group_state="SUCCEEDED",
        )

    with pytest.raises(HarnessValidationError) as exc_info:
        store.commit_event(
            forged,
            replace(projection, last_sequence=forged.sequence),
        )

    assert exc_info.value.code == expected_code
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_projection(plan.run_id, plan.stage_id) == projection
    assert store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == results
    after_report = TaskPlanReplayReducer().replay(
        (plan,),
        store.read_events(plan.run_id, plan.stage_id),
        require_terminal_events=False,
    )
    assert after_report.projection == before_report.projection
    assert after_report.continuation == before_report.continuation
    assert after_report.accepted_output_refs == before_report.accepted_output_refs
    if event_store is not None:
        assert event_store.get_stream_high_watermark(
            f"run:{plan.run_id}"
        ) == high_watermark
        assert artifacts._content == immutable_artifacts


def test_continuation_owner_rejects_another_valid_parent_submission() -> None:
    candidate, base_plan, _, _ = _accepted_plan(
        (
            _task("a"),
            _task("b", capability="research.helper", role="analysis.helper"),
        ),
        two_tasks=True,
        explicit_execution_budget=True,
    )
    store = InMemoryTaskPlanStore()
    first_identity = CandidateDedupIdentity(
        run_id=candidate.run_id,
        stage_id=candidate.stage_id,
        parent_turn_id="parent-turn-1",
        action_correlation_id="action-1",
    )
    first = store.admit_candidate_submission(
        candidate,
        first_identity,
        accepted_at="2026-09-05T00:00:00Z",
    )
    second = store.admit_candidate_submission(
        candidate,
        replace(
            first_identity,
            parent_turn_id="parent-turn-2",
            action_correlation_id="action-2",
        ),
        accepted_at="2026-09-05T00:00:00Z",
    )
    plan = replace(
        base_plan,
        plan_id=first.plan_id,
        accepted_at=first.accepted_at,
    )
    store.accept_plan(plan)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    group = coordinator.create_group(
        replace(
            _request(plan),
            serial_fallback=True,
            correlation_id=first.identity.dedup_key,
        )
    )
    current = store.load_projection(plan.run_id, plan.stage_id)
    admission = _parallel_event(
        plan,
        "TASK_GROUP_ADMITTED",
        current.last_sequence + 1,
        {
            "group": group.to_dict(),
            "requested_parallelism": 2,
            "effective_parallelism": 1,
        },
    )
    admitted_projection = replace(current, last_sequence=admission.sequence)
    store.commit_event(admission, admitted_projection)
    pending = _continuation_event(
        plan,
        group,
        admission.sequence + 1,
        parent_turn_id=first.identity.parent_turn_id,
        submission_id=first.submission_id,
    )
    pending_projection = replace(admitted_projection, last_sequence=pending.sequence)
    store.commit_event(pending, pending_projection)
    history = store.read_events(plan.run_id, plan.stage_id)

    forged = _continuation_event(
        plan,
        group,
        pending.sequence + 1,
        parent_turn_id=second.identity.parent_turn_id,
        observation_id="observation-from-other-parent",
        submission_id=second.submission_id,
    )
    with pytest.raises(HarnessValidationError) as exc_info:
        store.commit_event(
            forged,
            replace(pending_projection, last_sequence=forged.sequence),
        )

    assert exc_info.value.code == "parent_continuation_scope_mismatch"
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_projection(plan.run_id, plan.stage_id) == pending_projection


def test_sqlite_pending_to_terminal_continuation_is_idempotent_and_replayable(
    tmp_path,
) -> None:
    store, plan, group, event_store, artifacts = _continuation_owner_fixture(
        tmp_path,
        "sqlite",
    )
    def observation_for(group_snapshot, status):
        observation_payload = {
            "schema_version": "agora.harness-parent-observation/v1",
            "group_id": group_snapshot.group_id,
            "group_status": status,
            "plan_version": plan.version,
            "waves": [],
            "tasks": [],
            "aggregate_ref": None,
            "aggregate_checksum": None,
            "diagnostics": [],
            "result_refs": [],
            "truncated": False,
        }
        return {
            **observation_payload,
            "observation_checksum": canonical_payload_checksum(observation_payload),
        }

    observation = observation_for(group, "JOINING")
    current = store.load_projection(plan.run_id, plan.stage_id)
    pending = _continuation_event(
        plan,
        group,
        current.last_sequence + 1,
        observation_checksum=observation["observation_checksum"],
    )
    pending_projection = replace(current, last_sequence=pending.sequence)
    store.commit_event(pending, pending_projection)

    joining = replace(group, state="JOINING")
    waiting = _parallel_event(
        plan,
        "TASK_GROUP_JOIN_WAITING",
        pending.sequence + 1,
        {"group": joining.to_dict(), "observation": observation},
    )
    waiting_projection = replace(pending_projection, last_sequence=waiting.sequence)
    store.commit_event(waiting, waiting_projection)
    failed_group = replace(joining, state="FAILED")
    failed_observation = observation_for(failed_group, "FAILED")
    failed = _parallel_event(
        plan,
        "TASK_GROUP_FAILED",
        waiting.sequence + 1,
        {
            "group": failed_group.to_dict(),
            "observation": failed_observation,
            "reason_code": "TASK_FAILED",
        },
    )
    failed_projection = replace(waiting_projection, last_sequence=failed.sequence)
    store.commit_event(failed, failed_projection)
    terminal = _continuation_event(
        plan,
        failed_group,
        failed.sequence + 1,
        version=2,
        observation_checksum=failed_observation["observation_checksum"],
        status="DELIVERED",
        group_state="FAILED",
    )
    terminal_projection = replace(failed_projection, last_sequence=terminal.sequence)
    store.commit_event(terminal, terminal_projection)
    history = store.read_events(plan.run_id, plan.stage_id)
    high_watermark = event_store.get_stream_high_watermark(f"run:{plan.run_id}")
    immutable_artifacts = dict(artifacts._content)

    assert store.commit_event(terminal, terminal_projection) == terminal.event_checksum
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert event_store.get_stream_high_watermark(f"run:{plan.run_id}") == high_watermark
    assert artifacts._content == immutable_artifacts

    reopened = _store(
        SQLiteEventStore(event_store.database, clock=lambda: FIXED_NOW),
        artifacts,
    )
    report = TaskPlanReplayReducer().replay(
        (plan,),
        reopened.read_events(plan.run_id, plan.stage_id),
        require_terminal_events=False,
    )

    assert reopened.load_projection(plan.run_id, plan.stage_id) == terminal_projection
    assert report.projection == terminal_projection
    assert report.continuation == terminal.payload["continuation"]
    assert report.accepted_output_refs == ()
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == ()


@pytest.mark.parametrize(
    "scope_change",
    ({"run_id": "another-run"}, {"stage_id": "another-stage"}),
)
def test_replay_rejects_checksum_valid_continuation_outside_event_scope(
    scope_change: dict[str, str],
) -> None:
    _store, plan, group, history = _admitted_history()
    forged = _continuation_event(
        plan,
        group,
        len(history) + 1,
        **scope_change,
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanReplayReducer().replay(
            (plan,),
            (*history, forged),
            require_terminal_events=False,
        )

    assert exc_info.value.code == "parent_continuation_scope_mismatch"
