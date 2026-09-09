from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.continuation import (
    PARENT_CONTINUATION_EVENT,
    ParentContinuation,
)
from framework.harness.task_plan.parallel import (
    ParallelAgentCoordinator,
    SerialTaskExecutorAdapter,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskPlanEvent
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _accepted_plan,
    _task,
)
from tests.framework.harness.task_plan.test_parallel_admission_transactions import (
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


def _continuation_event(plan, group, sequence: int, *, version: int = 1, group_id: str | None = None):
    continuation = ParentContinuation(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        parent_turn_id="parent-turn-1",
        observation_id="observation-1",
        observation_version=version,
        group_id=group_id or group.group_id,
        observation_checksum="sha256:" + "a" * 64,
        status="PENDING",
        group_state=group.state.value,
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
