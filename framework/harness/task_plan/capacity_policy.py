"""Harness-owned capacity rules resolved against exact accepted worker bindings."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum, checksum, exact_keys, exact_reference, frozen_mapping, identifier, thaw_mapping
from framework.harness.task_plan.capacity import CapacityPool, TaskCapacityDemand
from framework.harness.task_plan.models import ValidatedTaskPlan


CAPACITY_POLICY_SCHEMA = "agora.task-capacity-policy/v1"
_RULE_FIELDS = frozenset({"quantities", "side_effect_class", "resource_conflict_key", "global_serial", "idempotent_concurrency", "idempotency_receipt_checksum"})


@dataclass(frozen=True, slots=True)
class TaskCapacityPolicy:
    policy_ref: str
    stage_id: str
    pool_policy_checksums: Mapping[str, str]
    worker_rules: Mapping[str, Mapping[str, Any]]
    schema_version: str = CAPACITY_POLICY_SCHEMA
    policy_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        exact_reference(self.policy_ref, "capacity_policy_ref")
        identifier(self.stage_id, "capacity_stage_id")
        if self.schema_version != CAPACITY_POLICY_SCHEMA:
            raise HarnessValidationError("unsupported capacity policy schema", code="CAPACITY_POLICY_INVALID")
        pools = frozen_mapping(self.pool_policy_checksums, "pool_policy_checksums")
        if not pools or len(pools) > 16:
            raise HarnessValidationError("capacity policy must pin 1..16 pools", code="CAPACITY_POLICY_INVALID")
        for value in pools.values():
            checksum(value, "pool_policy_checksum")
        if not isinstance(self.worker_rules, Mapping) or not self.worker_rules or len(self.worker_rules) > 256:
            raise HarnessValidationError("capacity policy must define bounded worker rules", code="CAPACITY_POLICY_INVALID")
        rules = {}
        for worker_ref, raw in self.worker_rules.items():
            exact_reference(worker_ref, "capacity_worker_ref")
            rule = exact_keys(raw, required=_RULE_FIELDS, model="TaskCapacityRule")
            # Reuse the demand parser for identical field semantics. The rule
            # contains no task identity until resolved against an accepted plan.
            parsed = TaskCapacityDemand(task_id="capacity-rule", **rule).to_dict()
            if set(parsed["quantities"]) - set(pools):
                raise HarnessValidationError("worker rule references an unpinned capacity pool", code="CAPACITY_POLICY_MISSING")
            rules[worker_ref] = {name: parsed[name] for name in _RULE_FIELDS}
        object.__setattr__(self, "pool_policy_checksums", pools)
        object.__setattr__(self, "worker_rules", frozen_mapping(rules, "worker_rules"))
        object.__setattr__(self, "policy_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {"schema_version": self.schema_version, "policy_ref": self.policy_ref, "stage_id": self.stage_id, "pool_policy_checksums": thaw_mapping(self.pool_policy_checksums), "worker_rules": thaw_mapping(self.worker_rules)}
        if include_checksum:
            value["policy_checksum"] = self.policy_checksum
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskCapacityPolicy:
        payload = exact_keys(value, required=frozenset({"schema_version", "policy_ref", "stage_id", "pool_policy_checksums", "worker_rules", "policy_checksum"}), model=cls.__name__)
        supplied = checksum(payload.pop("policy_checksum"), "capacity_policy_checksum")
        result = cls(**payload)
        if supplied != result.policy_checksum:
            raise HarnessValidationError("capacity policy checksum mismatch", code="CAPACITY_POLICY_CHECKSUM_MISMATCH")
        return result

    def resolve(self, plan: ValidatedTaskPlan, pools: tuple[CapacityPool, ...], *, now_ms: int) -> Mapping[str, TaskCapacityDemand]:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("capacity resolution requires an accepted plan")
        if any(not isinstance(pool, CapacityPool) for pool in pools):
            raise HarnessValidationError("invalid capacity snapshot", code="CAPACITY_POLICY_INVALID")
        by_pool = {pool.pool_id: pool for pool in pools}
        if len(by_pool) != len(pools) or set(by_pool) != set(self.pool_policy_checksums):
            raise HarnessValidationError("required capacity pool snapshot is missing or duplicated", code="CAPACITY_POLICY_MISSING")
        for pool_id, pool in by_pool.items():
            pool.require_current(now_ms=now_ms)
            if pool.policy_checksum != self.pool_policy_checksums[pool_id]:
                raise HarnessValidationError("capacity pool policy differs from pinned revision", code="CAPACITY_POLICY_STALE")
        return self.demands_for(plan)

    def demands_for(self, plan: ValidatedTaskPlan) -> Mapping[str, TaskCapacityDemand]:
        if plan.stage_id != self.stage_id:
            raise HarnessValidationError("capacity policy belongs to a different stage", code="CAPACITY_POLICY_SCOPE_MISMATCH")
        demands = {}
        for task in plan.tasks:
            rule = self.worker_rules.get(task.worker_ref)
            if rule is None:
                raise HarnessValidationError("accepted worker has no capacity policy", code="CAPACITY_POLICY_MISSING", details={"worker_ref": task.worker_ref})
            demands[task.task_id] = TaskCapacityDemand(task_id=task.task_id, **rule)
        return MappingProxyType(demands)
