from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.capacity import CapacityPool, PoolReservation, TaskCapacityDemand, pack_first_fit, resource_demands_conflict
from framework.harness.task_plan.capacity_policy import TaskCapacityPolicy
from framework.harness.task_plan.parallel import ParallelAgentCoordinator, ParallelEventSink, SerialTaskExecutorAdapter
from framework.harness.task_plan.policy import TaskPlanPolicy
from tests.framework.harness.task_plan.capacity_fixtures import capacity_pool, bind_capacity_policy
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request


def _rule(**overrides):
    value = {"quantities": {"cpu": 1}, "side_effect_class": "READ_ONLY", "resource_conflict_key": None, "global_serial": False, "idempotent_concurrency": False, "idempotency_receipt_checksum": None}
    value.update(overrides)
    return value


def _policy(plan, pool):
    return TaskCapacityPolicy("capacity.test@1", plan.stage_id, {pool.pool_id: pool.policy_checksum}, {task.worker_ref: _rule() for task in plan.tasks})


def test_snapshot_binds_owner_and_revision_without_treating_live_availability_as_policy_drift():
    pool = capacity_pool("cpu", 3, expires_at_ms=100)
    assert CapacityPool.from_dict(pool.to_dict()) == pool
    changed = replace(pool, reserved=1, reservation_version=1)
    assert changed.policy_checksum == pool.policy_checksum
    assert changed.to_dict()["snapshot_checksum"] != pool.to_dict()["snapshot_checksum"]
    raw = pool.to_dict()
    raw["owner_scope"] = "other-service"
    with pytest.raises(HarnessValidationError):
        CapacityPool.from_dict(raw)


@pytest.mark.parametrize("field,value", [("expires_at_ms", True), ("reservation_version", -1), ("owner_scope", ""), ("reservation_key", ""), ("policy_version", "latest")])
def test_snapshot_rejects_incomplete_or_unversioned_policy(field, value):
    with pytest.raises(HarnessValidationError):
        capacity_pool("cpu", 1, **{field: value})


@pytest.mark.parametrize("drift", ["missing", "expired", "owner", "version", "stage", "worker"])
def test_capacity_policy_fails_closed_for_missing_stale_or_foreign_authority(drift):
    plan = _accepted_parallel_plan(("task-1",))
    pool = capacity_pool("cpu", 2, expires_at_ms=100)
    policy = _policy(plan, pool)
    pools = (pool,)
    if drift == "missing":
        pools = ()
    elif drift == "expired":
        pools = (replace(pool, expires_at_ms=10),)
    elif drift == "owner":
        pools = (replace(pool, owner_scope="other-tenant", policy_checksum=""),)
    elif drift == "version":
        pools = (replace(pool, policy_version="2", policy_checksum=""),)
    elif drift == "stage":
        policy = replace(policy, stage_id="other-stage")
    else:
        policy = replace(policy, worker_rules={"other-worker@1": _rule()})
    with pytest.raises(HarnessValidationError):
        policy.resolve(plan, pools, now_ms=10)


def test_pinned_policy_resolves_complete_plan_and_rejects_caller_side_effect_drift():
    plan = _accepted_parallel_plan(("a", "b"))
    pool = capacity_pool("cpu", 2)
    policy = _policy(plan, pool)
    assert TaskCapacityPolicy.from_dict(policy.to_dict()) == policy
    demands = policy.resolve(plan, (pool,), now_ms=1)
    request = replace(_request(plan), capacity_policy=policy, capacity_pools=(pool,), task_capacity_demands=demands)
    with pytest.raises(HarnessValidationError, match="trusted capacity policy"):
        replace(request, task_capacity_demands={**demands, "a": TaskCapacityDemand("a", {"cpu": 1}, "MUTATING_SERIAL", "foreign-resource")})
    assert set(demands) == {"a", "b"}


def test_pool_reservation_roundtrip_release_idempotence_and_stale_settlement():
    pools = {"cpu": capacity_pool("cpu", 2, reservation_version=4), "io": capacity_pool("io", 1, reservation_version=7)}
    demand = TaskCapacityDemand("a", {"cpu": 2, "io": 1})
    packed = pack_first_fit(("a",), {"a": demand}, pools, max_tasks=1, owner_scope="run/stage/plan", reservation_keys={"a": "attempt-a"})
    reservation = packed.reservations[0]
    assert reservation.pool_versions == {"cpu": 4, "io": 7}
    assert PoolReservation.from_dict(reservation.to_dict()) == reservation
    released = reservation.settled("RELEASED", reservation_key="attempt-a", expected_version=1)
    assert released.settled("RELEASED", reservation_key="attempt-a", expected_version=1) is released
    assert released.admission_snapshot() == reservation.to_dict()
    assert not released.outstanding_allocations
    assert not reservation.settled("CONSUMED", reservation_key="attempt-a", expected_version=1).outstanding_allocations
    for state, key, version in (("CONSUMED", "attempt-a", 1), ("RELEASED", "attempt-b", 1), ("RELEASED", "attempt-a", 2)):
        with pytest.raises(HarnessValidationError):
            released.settled(state, reservation_key=key, expected_version=version)


@pytest.mark.parametrize("reverse", [False, True])
def test_same_key_read_write_conflict_is_symmetric(reverse):
    demands = (TaskCapacityDemand("r", {"cpu": 1}, "READ_ONLY", "key"), TaskCapacityDemand("w", {"cpu": 1}, "MUTATING_SERIAL", "key"))
    if reverse:
        demands = tuple(reversed(demands))
    packed = pack_first_fit(tuple(d.task_id for d in demands), {d.task_id: d for d in demands}, {"cpu": capacity_pool("cpu", 2)}, max_tasks=2, owner_scope="run/stage", reservation_keys={d.task_id: f"attempt-{d.task_id}" for d in demands})
    assert packed.selected == (demands[0].task_id,)
    assert packed.reasons[demands[1].task_id] == "RESOURCE_CONFLICT"


def test_independent_mutations_and_policy_approved_idempotent_writes_can_share_capacity():
    left = TaskCapacityDemand("a", {"cpu": 1}, "MUTATING_SERIAL", "key-a")
    right = TaskCapacityDemand("b", {"cpu": 1}, "MUTATING_SERIAL", "key-b")
    assert not resource_demands_conflict(left, right)
    assert resource_demands_conflict(replace(left, global_serial=True), right)
    left = replace(left, side_effect_class="EXTERNAL_IDEMPOTENT", resource_conflict_key="same", idempotent_concurrency=True, idempotency_receipt_checksum="sha256:" + "1" * 64)
    right = replace(right, side_effect_class="EXTERNAL_IDEMPOTENT", resource_conflict_key="same", idempotent_concurrency=True, idempotency_receipt_checksum="sha256:" + "2" * 64)
    assert not resource_demands_conflict(left, right)
    assert resource_demands_conflict(replace(left, idempotent_concurrency=False), right)
    with pytest.raises(HarnessValidationError, match="receipt"):
        replace(right, idempotency_receipt_checksum=None)


@pytest.mark.parametrize("failure", ["expired", "fence"])
def test_dispatch_rejects_missing_fence_or_expired_pool_before_worker_or_wave(failure):
    plan = _accepted_parallel_plan(("task-1",))
    pool = capacity_pool("cpu", 1, **({"expires_at_ms": 1} if failure == "expired" else {}))
    demand = TaskCapacityDemand("task-1", {"cpu": 1}, "FENCED_MUTATION" if failure == "fence" else "READ_ONLY", "key")
    request = replace(_request(plan), serial_fallback=True, capacity_pools=(pool,), task_capacity_demands={demand.task_id: demand})
    request = bind_capacity_policy(request)
    events, calls = [], []
    coordinator = ParallelAgentCoordinator(max_workers=1, serial_executor=SerialTaskExecutorAdapter(), event_sink=ParallelEventSink(events.append, events.extend))
    with pytest.raises(HarnessValidationError) as caught:
        coordinator.dispatch(request, lambda item: calls.append(item))
    assert caught.value.code == ("CAPACITY_POLICY_STALE" if failure == "expired" else "SIDE_EFFECT_FENCE_REQUIRED")
    assert calls == []
    assert events == []


def test_task_plan_policy_pins_capacity_rules_in_its_accepted_checksum():
    from tests.framework.harness.task_plan.test_task_plan_runtime import _setup
    _, policy, _ = _setup()
    pool = capacity_pool("cpu", 2)
    capacity_policy = TaskCapacityPolicy("capacity.test@1", policy.stage_id, {"cpu": pool.policy_checksum}, {ref: _rule() for ref in policy.pinned_capability_bindings.values()})
    bound = replace(policy, capacity_policy=capacity_policy)
    assert bound.policy_checksum != policy.policy_checksum
    assert TaskPlanPolicy.from_dict(bound.to_dict()) == bound


def test_stage_resolves_registered_capacity_policy_and_rejects_missing_reader():
    from framework.harness.task_plan import FakePlanCandidateBuilder, InMemoryTaskPlanStore, TaskPlanStageRequest, TaskPlanStageRunner
    from tests.framework.harness.task_plan.test_task_plan_runtime import _setup, _candidate, _task, _AcceptingResultVerifier
    binding, policy, registry = _setup()
    pool = capacity_pool("cpu", 2)
    capacity_policy = TaskCapacityPolicy("capacity.test@1", policy.stage_id, {"cpu": pool.policy_checksum}, {ref: _rule() for ref in policy.pinned_capability_bindings.values()})
    policy = replace(policy, capacity_policy=capacity_policy, serial_fallback=True)
    request = TaskPlanStageRequest(run_id="run", stage_binding=binding, context_refs={"document": "document"}, policy=policy, accepted_at="2026-09-09T00:00:00Z")
    builder = FakePlanCandidateBuilder(_candidate(binding, (_task("task-1"),)))
    store = InMemoryTaskPlanStore()
    runner = TaskPlanStageRunner(candidate_builder=builder, capability_registry=registry, store=store, result_verifier=_AcceptingResultVerifier(), parallel_coordinator=ParallelAgentCoordinator(max_workers=1, serial_executor=SerialTaskExecutorAdapter()))
    plan = runner._ensure_plan(request)
    with pytest.raises(HarnessValidationError) as caught:
        runner._parallel_request(request, plan, task_instances=())
    assert caught.value.code == "CAPACITY_POLICY_MISSING"
    reader_calls = []
    def read_snapshot(selected):
        reader_calls.append(selected)
        return (pool,)
    runner.capacity_snapshot_reader = read_snapshot
    dispatch = runner._parallel_request(request, plan, task_instances=())
    assert reader_calls == [capacity_policy]
    assert dispatch.capacity_policy == capacity_policy
    assert dispatch.task_capacity_demands["task-1"].quantities == {"cpu": 1}
    assert dispatch.capacity_pools == (pool,)
