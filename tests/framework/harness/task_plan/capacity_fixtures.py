"""Explicit scoped capacity snapshots for orchestration contract fixtures."""
from framework.harness.task_plan.capacity import CapacityPool, capacity_now_ms
from framework.harness.task_plan.capacity_policy import TaskCapacityPolicy
from dataclasses import replace


def capacity_pool(pool_id, capacity, *, reserved=0, policy_version="1", policy_checksum="", **overrides):
    fields = {
        "owner_scope": "service/test-runtime",
        "reservation_key": f"pool/{pool_id}",
        "reservation_version": 0,
        "expires_at_ms": capacity_now_ms() + 3_600_000,
    }
    fields.update(overrides)
    return CapacityPool(pool_id, capacity, reserved, policy_version, policy_checksum, **fields)


def bind_capacity_policy(request):
    rules = {}
    for task in request.plan.tasks:
        rule = request.task_capacity_demands[task.task_id].to_dict()
        rule.pop("task_id")
        rule.pop("schema_version")
        rules[task.worker_ref] = rule
    policy = TaskCapacityPolicy("capacity.fixture@1", request.plan.stage_id, {pool.pool_id: pool.policy_checksum for pool in request.capacity_pools}, rules)
    return replace(request, capacity_policy=policy)
