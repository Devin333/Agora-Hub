import pytest
from dataclasses import replace

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.capacity import TaskCapacityDemand, pack_first_fit
from tests.framework.harness.task_plan.capacity_fixtures import capacity_pool as CapacityPool
from tests.framework.harness.task_plan.capacity_fixtures import bind_capacity_policy
from framework.harness.task_plan.parallel_lifecycle import SideEffectClass
from framework.harness.task_plan.parallel import ParallelAgentCoordinator, ParallelEventSink
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request, _result
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.scheduler import task_instance_for_attempt


def test_first_fit_skips_unreservable_task_and_selects_later_task_without_partial_allocation():
    pools = {
        "cpu": CapacityPool("cpu", 2),
        "gpu": CapacityPool("gpu", 1),
    }
    demands = {
        "a": TaskCapacityDemand("a", {"cpu": 1, "gpu": 2}),
        "b": TaskCapacityDemand("b", {"cpu": 2}),
    }
    result = pack_first_fit(("a", "b"), demands, pools, max_tasks=2, owner_scope="run/stage", reservation_keys={"a": "attempt-a", "b": "attempt-b"})
    assert result.selected == ("b",)
    assert result.overflow == ("a",)
    assert result.reasons["a"] == "CAPACITY_NOT_AVAILABLE"
    assert result.reservations[0].allocations == {"cpu": 2}


def test_first_fit_is_stable_and_pool_policy_evidence_is_checksum_bound():
    pools = {"cpu": CapacityPool("cpu", 3, policy_version="2026-09")}
    demands = {task: TaskCapacityDemand(task, {"cpu": 1}) for task in ("a", "b", "c")}
    left = pack_first_fit(("a", "b", "c"), demands, pools, max_tasks=2, owner_scope="run/stage", reservation_keys={task: f"attempt-{task}" for task in demands})
    right = pack_first_fit(("a", "b", "c"), demands, pools, max_tasks=2, owner_scope="run/stage", reservation_keys={task: f"attempt-{task}" for task in demands})
    assert left.to_dict() == right.to_dict()
    assert left.reservations[0].policy_checksums["cpu"] == pools["cpu"].policy_checksum
    assert left.packing_checksum.startswith("sha256:")


def test_first_fit_respects_existing_resource_conflict_without_blocking_independent_task():
    pools = {"io": CapacityPool("io", 2)}
    demands = {
        "same": TaskCapacityDemand("same", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-1"),
        "other": TaskCapacityDemand("other", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-2"),
    }
    result = pack_first_fit(("same", "other"), demands, pools, max_tasks=2, occupied_resource_keys=frozenset({"resource-1"}), owner_scope="run/stage", reservation_keys={task: f"attempt-{task}" for task in demands})
    assert result.selected == ("other",)
    assert result.reasons["same"] == "RESOURCE_CONFLICT"


def test_capacity_policy_checksum_and_missing_demand_fail_closed():
    with pytest.raises(HarnessValidationError, match="checksum"):
        CapacityPool("cpu", 1, policy_checksum="sha256:" + "0" * 64)
    result = pack_first_fit(("missing",), {}, {"cpu": CapacityPool("cpu", 1)}, max_tasks=1, owner_scope="run/stage", reservation_keys={"missing": "attempt-missing"})
    assert result.selected == ()
    assert result.reasons["missing"] == "CAPACITY_POLICY_MISSING"


def test_dispatch_request_rejects_demands_without_capacity_pools():
    plan = _accepted_parallel_plan(("task-1",))
    with pytest.raises(HarnessValidationError) as exc_info:
        replace(
            _request(plan),
            task_capacity_demands={"task-1": TaskCapacityDemand("task-1", {"cpu": 1})},
        )
    assert exc_info.value.code == "CAPACITY_POLICY_MISSING"


def test_dispatch_request_rejects_missing_task_demand_or_pool_policy():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    with pytest.raises(HarnessValidationError) as exc_info:
        replace(
            _request(plan),
            capacity_pools=(CapacityPool("cpu", 2),),
            task_capacity_demands={"task-1": TaskCapacityDemand("task-1", {"cpu": 1})},
        )
    assert exc_info.value.code == "CAPACITY_DEMAND_INVALID"
    with pytest.raises(HarnessValidationError) as exc_info:
        replace(
            _request(plan, ),
            capacity_pools=(CapacityPool("cpu", 2),),
            task_capacity_demands={
                "task-1": TaskCapacityDemand("task-1", {"gpu": 1}),
                "task-2": TaskCapacityDemand("task-2", {"gpu": 1}),
            },
        )
    assert exc_info.value.code == "CAPACITY_POLICY_MISSING"


def test_coordinator_uses_pool_packing_for_later_task_selection():
    plan = _accepted_parallel_plan(("a", "b"))
    base = _request(plan)
    request = replace(base, capacity_pools=(CapacityPool("cpu", 2), CapacityPool("gpu", 1)), task_capacity_demands={
        "a": TaskCapacityDemand("a", {"cpu": 1, "gpu": 2}),
        "b": TaskCapacityDemand("b", {"cpu": 1}),
    })
    supervisor = ChildAgentSupervisor(max_children=1)
    events = []
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor, event_sink=ParallelEventSink(events.append, events.extend))
    try:
        result = coordinator.dispatch(bind_capacity_policy(request), lambda instance: _result(plan, instance))
        assert result.results[0].task_id == "b"
        admitted = next(item for item in events if item["event_type"] == "TASK_WAVE_ADMITTED")
        assert admitted["wave"]["reservations"][0]["capacity_allocations"] == {"cpu": 1}
        assert admitted["wave"]["reservations"][0]["capacity_policy_checksums"]["cpu"] == request.capacity_pools[0].policy_checksum
    finally:
        supervisor.shutdown()


def test_pool_policy_version_is_bound_to_wave_identity():
    plan = _accepted_parallel_plan(("task-1",))
    base = _request(plan)
    events_by_policy = []
    for policy_version in ("policy-v1", "policy-v2"):
        supervisor = ChildAgentSupervisor(max_children=1)
        events = []
        coordinator = ParallelAgentCoordinator(
            max_workers=1,
            child_supervisor=supervisor,
            event_sink=ParallelEventSink(events.append, events.extend),
        )
        request = replace(
            base,
            capacity_pools=(CapacityPool("cpu", 1, policy_version=policy_version),),
            task_capacity_demands={"task-1": TaskCapacityDemand("task-1", {"cpu": 1})},
        )
        try:
            coordinator.dispatch(bind_capacity_policy(request), lambda instance: _result(plan, instance))
            admitted = next(item for item in events if item["event_type"] == "TASK_WAVE_ADMITTED")
            events_by_policy.append(admitted["wave"])
        finally:
            supervisor.shutdown()
    assert events_by_policy[0]["wave_id"] != events_by_policy[1]["wave_id"]


def test_pool_reservation_is_released_between_capacity_limited_waves():
    plan = _accepted_parallel_plan(("a", "b"))
    base = _request(plan)
    request = replace(base, requested_parallelism=1, supervisor_capacity=1, capacity_pools=(CapacityPool("cpu", 1),), task_capacity_demands={
        "a": TaskCapacityDemand("a", {"cpu": 1}),
        "b": TaskCapacityDemand("b", {"cpu": 1}),
    })
    supervisor = ChildAgentSupervisor(max_children=1)
    events = []
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor, event_sink=ParallelEventSink(events.append, events.extend))
    try:
        result = coordinator.dispatch(bind_capacity_policy(request), lambda instance: _result(plan, instance))
        assert result.succeeded
        assert [wave.task_ids for wave in result.waves] == [("a",), ("b",)]
    finally:
        supervisor.shutdown()
