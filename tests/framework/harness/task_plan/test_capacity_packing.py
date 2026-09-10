import pytest
from dataclasses import replace

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.capacity import (
    CapacityScopeSnapshot,
    FirstFitPacking,
    TaskCapacityDemand,
    pack_first_fit,
)
from tests.framework.harness.task_plan.capacity_fixtures import capacity_pool as CapacityPool
from tests.framework.harness.task_plan.capacity_fixtures import (
    bind_capacity_policy,
    capacity_scope_snapshot,
)
from framework.harness.task_plan.parallel_lifecycle import SideEffectClass
from framework.harness.task_plan.parallel import ParallelAgentCoordinator, ParallelEventSink
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request, _result
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskBudget
from framework.harness.task_plan.scheduler import task_instance_for_attempt


def test_first_fit_skips_unreservable_task_and_selects_later_task_without_partial_allocation():
    plan = _accepted_parallel_plan(("a", "b"))
    instances = {
        task_id: task_instance_for_attempt(plan, task_id, 1)
        for task_id in ("a", "b")
    }
    pools = {
        "cpu": CapacityPool("cpu", 2),
        "gpu": CapacityPool("gpu", 1),
    }
    demands = {
        "a": TaskCapacityDemand("a", {"cpu": 1, "gpu": 2}),
        "b": TaskCapacityDemand("b", {"cpu": 2}),
    }
    scope = capacity_scope_snapshot(tuple(pools.values()))
    result = pack_first_fit(
        ("a", "b"),
        demands,
        pools,
        max_tasks=2,
        owner_scope="service/test-runtime",
        reservation_keys={
            task_id: instance.idempotency_key
            for task_id, instance in instances.items()
        },
        task_instances=instances,
        budget_snapshot=TaskPlanBudgetLedger.for_plan(plan).snapshot(),
        capacity_snapshot=scope,
    )
    assert result.selected == ("b",)
    assert result.overflow == ("a",)
    assert result.reasons["a"] == "CAPACITY_NOT_AVAILABLE"
    assert result.reservations[0].allocations == {"cpu": 2}
    admitted = TaskPlanBudgetLedger.from_snapshot(result.admitted_budget_snapshot)
    assert tuple(
        record["instance"]["task_id"] for record in admitted.records.values()
    ) == ("b",)
    assert result.capacity_before == scope
    assert result.capacity_after.revision == scope.revision + 1


def test_first_fit_capacity_rejection_preserves_last_budget_for_later_task():
    plan = _accepted_parallel_plan(("capacity-blocked", "selected"))
    instances = {
        task_id: task_instance_for_attempt(plan, task_id, 1)
        for task_id in ("capacity-blocked", "selected")
    }
    pools = {
        "cpu": CapacityPool("cpu", 2),
        "gpu": CapacityPool("gpu", 1),
    }
    demands = {
        "capacity-blocked": TaskCapacityDemand(
            "capacity-blocked",
            {"cpu": 1, "gpu": 2},
        ),
        "selected": TaskCapacityDemand("selected", {"cpu": 2}),
    }
    capacity_before = capacity_scope_snapshot(tuple(pools.values()))
    pool_inputs_before = {
        pool_id: pool.to_dict() for pool_id, pool in pools.items()
    }
    budget_before = TaskPlanBudgetLedger(
        plan.run_id,
        plan.stage_id,
        plan.policy_ref,
        instances["selected"].budget_snapshot.to_dict(),
    )
    budget_input_before = budget_before.to_dict()

    result = pack_first_fit(
        ("capacity-blocked", "selected"),
        demands,
        pools,
        max_tasks=2,
        owner_scope=capacity_before.owner_scope,
        reservation_keys={
            task_id: instance.idempotency_key
            for task_id, instance in instances.items()
        },
        task_instances=instances,
        budget_snapshot=budget_before.snapshot(),
        capacity_snapshot=capacity_before,
    )

    expected_budget_after = budget_before.reserve((instances["selected"],))
    expected_capacity_after = capacity_before.with_reserved_pools(
        (
            replace(
                pools["cpu"],
                reserved=2,
                reservation_version=pools["cpu"].reservation_version + 1,
            ),
            pools["gpu"],
        )
    )
    assert result.ready_order == ("capacity-blocked", "selected")
    assert result.selected == ("selected",)
    assert result.overflow == ("capacity-blocked",)
    assert result.reasons == {
        "capacity-blocked": "CAPACITY_NOT_AVAILABLE",
    }
    assert tuple(item.task_id for item in result.reservations) == ("selected",)
    assert result.reservations[0].allocations == {"cpu": 2}
    assert result.capacity_before == capacity_before
    assert result.capacity_after == expected_capacity_after
    assert TaskPlanBudgetLedger.from_snapshot(
        result.admitted_budget_snapshot
    ) == expected_budget_after
    assert result.budget_before_checksum == budget_before.to_dict()[
        "ledger_checksum"
    ]
    assert result.budget_after_checksum == expected_budget_after.to_dict()[
        "ledger_checksum"
    ]
    assert set(expected_budget_after.records) == {
        instances["selected"].idempotency_key,
    }
    assert budget_before.to_dict() == budget_input_before
    assert budget_before.records == {}
    assert {
        pool_id: pool.to_dict() for pool_id, pool in pools.items()
    } == pool_inputs_before


def test_first_fit_budget_rejection_preserves_pool_for_smaller_later_task():
    plan = _accepted_parallel_plan(("budget-blocked", "selected"))
    instances = {
        task_id: task_instance_for_attempt(plan, task_id, 1)
        for task_id in ("budget-blocked", "selected")
    }
    instances["budget-blocked"] = replace(
        instances["budget-blocked"],
        budget_snapshot=TaskBudget(
            max_turns=2,
            max_tool_calls=2,
            token_limit=8192,
            time_limit_ms=1_800_000,
        ),
    )
    pools = {"cpu": CapacityPool("cpu", 1)}
    demands = {
        task_id: TaskCapacityDemand(task_id, {"cpu": 1})
        for task_id in ("budget-blocked", "selected")
    }
    capacity_before = capacity_scope_snapshot(tuple(pools.values()))
    pool_inputs_before = {
        pool_id: pool.to_dict() for pool_id, pool in pools.items()
    }
    budget_before = TaskPlanBudgetLedger(
        plan.run_id,
        plan.stage_id,
        plan.policy_ref,
        instances["selected"].budget_snapshot.to_dict(),
    )
    budget_input_before = budget_before.to_dict()

    result = pack_first_fit(
        ("budget-blocked", "selected"),
        demands,
        pools,
        max_tasks=2,
        owner_scope=capacity_before.owner_scope,
        reservation_keys={
            task_id: instance.idempotency_key
            for task_id, instance in instances.items()
        },
        task_instances=instances,
        budget_snapshot=budget_before.snapshot(),
        capacity_snapshot=capacity_before,
    )

    expected_budget_after = budget_before.reserve((instances["selected"],))
    expected_capacity_after = capacity_before.with_reserved_pools(
        (
            replace(
                pools["cpu"],
                reserved=1,
                reservation_version=pools["cpu"].reservation_version + 1,
            ),
        )
    )
    assert result.ready_order == ("budget-blocked", "selected")
    assert result.selected == ("selected",)
    assert result.overflow == ("budget-blocked",)
    assert result.reasons == {"budget-blocked": "BUDGET_EXCEEDED"}
    assert tuple(item.task_id for item in result.reservations) == ("selected",)
    assert result.reservations[0].allocations == {"cpu": 1}
    assert result.capacity_before == capacity_before
    assert result.capacity_after == expected_capacity_after
    assert TaskPlanBudgetLedger.from_snapshot(
        result.admitted_budget_snapshot
    ) == expected_budget_after
    assert result.budget_before_checksum == budget_before.to_dict()[
        "ledger_checksum"
    ]
    assert result.budget_after_checksum == expected_budget_after.to_dict()[
        "ledger_checksum"
    ]
    assert set(expected_budget_after.records) == {
        instances["selected"].idempotency_key,
    }
    assert budget_before.to_dict() == budget_input_before
    assert budget_before.records == {}
    assert {
        pool_id: pool.to_dict() for pool_id, pool in pools.items()
    } == pool_inputs_before


def test_first_fit_is_stable_and_pool_policy_evidence_is_checksum_bound():
    pools = {"cpu": CapacityPool("cpu", 3, policy_version="2026-09")}
    demands = {task: TaskCapacityDemand(task, {"cpu": 1}) for task in ("a", "b", "c")}
    left = pack_first_fit(("a", "b", "c"), demands, pools, max_tasks=2, owner_scope="service/test-runtime", reservation_keys={task: f"attempt-{task}" for task in demands})
    right = pack_first_fit(("a", "b", "c"), demands, pools, max_tasks=2, owner_scope="service/test-runtime", reservation_keys={task: f"attempt-{task}" for task in demands})
    assert left.to_dict() == right.to_dict()
    assert left.reservations[0].policy_checksums["cpu"] == pools["cpu"].policy_checksum
    assert left.packing_checksum.startswith("sha256:")


def test_first_fit_respects_existing_resource_conflict_without_blocking_independent_task():
    pools = {"io": CapacityPool("io", 2)}
    demands = {
        "same": TaskCapacityDemand("same", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-1"),
        "other": TaskCapacityDemand("other", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-2"),
    }
    result = pack_first_fit(("same", "other"), demands, pools, max_tasks=2, occupied_resource_keys=frozenset({"resource-1"}), owner_scope="service/test-runtime", reservation_keys={task: f"attempt-{task}" for task in demands})
    assert result.selected == ("other",)
    assert result.reasons["same"] == "RESOURCE_CONFLICT"


def test_first_fit_blocks_read_on_an_occupied_exclusive_resource_key():
    pools = {"io": CapacityPool("io", 2)}
    demands = {
        "same": TaskCapacityDemand(
            "same", {"io": 1}, SideEffectClass.READ_ONLY, "resource-1"
        ),
        "other": TaskCapacityDemand(
            "other", {"io": 1}, SideEffectClass.READ_ONLY, "resource-2"
        ),
    }
    result = pack_first_fit(
        tuple(demands),
        demands,
        pools,
        max_tasks=2,
        occupied_resource_keys=frozenset({"resource-1"}),
        owner_scope="service/test-runtime",
        reservation_keys={task_id: f"attempt-{task_id}" for task_id in demands},
    )
    assert result.selected == ("other",)
    assert result.reasons["same"] == "RESOURCE_CONFLICT"


def test_first_fit_serializes_same_key_read_write_but_allows_read_sharing():
    pools = {"io": CapacityPool("io", 3)}
    demands = {
        "read-a": TaskCapacityDemand(
            "read-a", {"io": 1}, SideEffectClass.READ_ONLY, "resource-1"
        ),
        "write": TaskCapacityDemand(
            "write", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-1"
        ),
        "read-b": TaskCapacityDemand(
            "read-b", {"io": 1}, SideEffectClass.READ_ONLY, "resource-1"
        ),
    }
    result = pack_first_fit(
        tuple(demands),
        demands,
        pools,
        max_tasks=3,
        owner_scope="service/test-runtime",
        reservation_keys={task_id: f"attempt-{task_id}" for task_id in demands},
    )
    assert result.selected == ("read-a", "read-b")
    assert result.reasons["write"] == "RESOURCE_CONFLICT"


def test_first_fit_compares_candidates_with_active_resource_demands():
    pools = {"io": CapacityPool("io", 3)}
    active_read = TaskCapacityDemand(
        "active-read", {"io": 1}, SideEffectClass.READ_ONLY, "resource-1"
    )
    demands = {
        "read": TaskCapacityDemand(
            "read", {"io": 1}, SideEffectClass.READ_ONLY, "resource-1"
        ),
        "write": TaskCapacityDemand(
            "write", {"io": 1}, SideEffectClass.MUTATING_SERIAL, "resource-1"
        ),
    }
    result = pack_first_fit(
        tuple(demands),
        demands,
        pools,
        max_tasks=2,
        occupied_resource_demands=(active_read,),
        owner_scope="service/test-runtime",
        reservation_keys={task_id: f"attempt-{task_id}" for task_id in demands},
    )
    assert result.selected == ("read",)
    assert result.reasons["write"] == "RESOURCE_CONFLICT"


def test_first_fit_blocks_two_same_key_mutations_in_one_wave():
    pools = {"io": CapacityPool("io", 2)}
    demands = {
        task_id: TaskCapacityDemand(
            task_id,
            {"io": 1},
            SideEffectClass.MUTATING_SERIAL,
            "resource-1",
        )
        for task_id in ("write-a", "write-b")
    }
    result = pack_first_fit(
        tuple(demands),
        demands,
        pools,
        max_tasks=2,
        owner_scope="service/test-runtime",
        reservation_keys={task_id: f"attempt-{task_id}" for task_id in demands},
    )
    assert result.selected == ("write-a",)
    assert result.reasons["write-b"] == "RESOURCE_CONFLICT"


def test_capacity_policy_checksum_and_missing_demand_fail_closed():
    with pytest.raises(HarnessValidationError, match="checksum"):
        CapacityPool("cpu", 1, policy_checksum="sha256:" + "0" * 64)
    with pytest.raises(HarnessValidationError) as exc_info:
        pack_first_fit(("missing",), {}, {"cpu": CapacityPool("cpu", 1)}, max_tasks=1, owner_scope="service/test-runtime", reservation_keys={"missing": "attempt-missing"})
    assert exc_info.value.code == "CAPACITY_POLICY_MISSING"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ready_order", "task-1"),
        ("selected", {"task-1": True}),
        ("overflow", {"task-1"}),
        ("reservations", None),
    ],
)
def test_first_fit_packing_rejects_non_array_collections(field, value):
    payload = {
        "ready_order": ("task-1",),
        "selected": ("task-1",),
        "overflow": (),
        "reservations": (),
        "reasons": {},
    }
    payload[field] = value
    with pytest.raises(HarnessValidationError) as exc_info:
        FirstFitPacking(**payload)
    assert exc_info.value.code == "CAPACITY_RESERVATION_INVALID"


def test_capacity_scope_snapshot_requires_authoritative_revision_one_or_later():
    pool = CapacityPool("cpu", 1)
    with pytest.raises(HarnessValidationError) as exc_info:
        CapacityScopeSnapshot(
            owner_scope=pool.owner_scope,
            pools=(pool,),
            revision=0,
            expires_at_ms=pool.expires_at_ms,
        )
    assert exc_info.value.code == "CAPACITY_POLICY_INVALID"


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
        pools = (CapacityPool("cpu", 2),)
        replace(
            _request(plan),
            capacity_pools=pools,
            capacity_snapshot=capacity_scope_snapshot(pools),
            task_capacity_demands={"task-1": TaskCapacityDemand("task-1", {"cpu": 1})},
        )
    assert exc_info.value.code == "CAPACITY_DEMAND_INVALID"
    with pytest.raises(HarnessValidationError) as exc_info:
        pools = (CapacityPool("cpu", 2),)
        replace(
            _request(plan, ),
            capacity_pools=pools,
            capacity_snapshot=capacity_scope_snapshot(pools),
            task_capacity_demands={
                "task-1": TaskCapacityDemand("task-1", {"gpu": 1}),
                "task-2": TaskCapacityDemand("task-2", {"gpu": 1}),
            },
        )
    assert exc_info.value.code == "CAPACITY_POLICY_MISSING"


def test_coordinator_uses_pool_packing_for_later_task_selection():
    plan = _accepted_parallel_plan(("a", "b"))
    base = _request(plan)
    pools = (CapacityPool("cpu", 2), CapacityPool("gpu", 1))
    request = replace(base, capacity_pools=pools, capacity_snapshot=capacity_scope_snapshot(pools), task_capacity_demands={
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
            capacity_pools=(pool := CapacityPool("cpu", 1, policy_version=policy_version),),
            capacity_snapshot=capacity_scope_snapshot((pool,)),
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
    pools = (CapacityPool("cpu", 1),)
    request = replace(base, requested_parallelism=1, supervisor_capacity=1, capacity_pools=pools, capacity_snapshot=capacity_scope_snapshot(pools), task_capacity_demands={
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
        admissions = [
            event for event in events
            if event["event_type"] == "TASK_WAVE_ADMITTED"
        ]
        completions = [
            event for event in events
            if event["event_type"] == "TASK_WAVE_COMPLETED"
        ]
        assert len(admissions) == len(completions) == 2
        assert completions[0]["reservation_states"] == {"a": "CONSUMED"}
        assert completions[0]["capacity_before"] == admissions[0]["wave"]["packing"]["capacity_after"]
        assert completions[0]["capacity_after"] == admissions[1]["wave"]["packing"]["capacity_before"]
        assert completions[0]["capacity_after"]["pools"][0]["reserved"] == 0
        assert completions[0]["capacity_after"]["revision"] == (
            completions[0]["capacity_before"]["revision"] + 1
        )
    finally:
        supervisor.shutdown()


def test_zero_dispatch_slots_emit_stable_capacity_wait_without_empty_wave():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = replace(_request(plan), available_concurrency_reservations=0)
    events = []
    calls = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    try:
        first = coordinator.dispatch(
            request,
            lambda instance: calls.append(instance),
            finalize=False,
        )
        second = coordinator.dispatch(
            request,
            lambda instance: calls.append(instance),
            finalize=False,
        )
    finally:
        supervisor.shutdown()
    waits = [
        event for event in events
        if event["event_type"] == "TASK_GROUP_CAPACITY_WAITING"
    ]
    assert first.waves == second.waves == ()
    assert first.observation.diagnostics == ("CAPACITY_WAITING",)
    assert second.observation.diagnostics == ("CAPACITY_WAITING",)
    assert calls == []
    assert len(waits) == 2
    assert waits[0]["waiting"] == waits[1]["waiting"]
    assert waits[0]["idempotency_key"] == waits[1]["idempotency_key"]
    assert waits[0]["waiting"]["logical_ready_order"] == ["task-1", "task-2"]


def test_capacity_wait_diagnostic_clears_when_the_same_group_admits_work():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    waiting_request = replace(
        _request(plan),
        available_concurrency_reservations=0,
    )
    events = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    try:
        waiting = coordinator.dispatch(
            waiting_request,
            lambda instance: _result(plan, instance),
            finalize=False,
        )
        resumed = coordinator.dispatch(
            replace(waiting_request, available_concurrency_reservations=2),
            lambda instance: _result(plan, instance),
            finalize=False,
        )
    finally:
        supervisor.shutdown()

    assert waiting.observation.diagnostics == ("CAPACITY_WAITING",)
    assert resumed.observation.diagnostics == ("JOIN_WAITING",)
    assert tuple(result.task_id for result in resumed.results) == (
        "task-1",
        "task-2",
    )
    assert len([
        event
        for event in events
        if event["event_type"] == "TASK_GROUP_CAPACITY_WAITING"
    ]) == 1
