"""Deterministic, all-or-nothing capacity packing for task waves."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any, Mapping, Sequence

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import checksum, canonical_payload_checksum, exact_keys, frozen_mapping, identifier, thaw_mapping
from framework.harness.task_plan.parallel_lifecycle import ReservationState, SideEffectClass


CAPACITY_POOL_SCHEMA = "agora.task-capacity-pool/v1"
CAPACITY_DEMAND_SCHEMA = "agora.task-capacity-demand/v1"
POOL_RESERVATION_SCHEMA = "agora.task-pool-reservation/v1"


def capacity_now_ms() -> int:
    return time_ns() // 1_000_000


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HarnessValidationError(f"{name} must be an integer >= {minimum}", code="CAPACITY_POLICY_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class CapacityPool:
    pool_id: str
    capacity: int
    reserved: int = 0
    policy_version: str = "1"
    policy_checksum: str = ""
    owner_scope: str = field(kw_only=True)
    reservation_key: str = field(kw_only=True)
    reservation_version: int = field(kw_only=True)
    expires_at_ms: int = field(kw_only=True)
    schema_version: str = CAPACITY_POOL_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "pool_id", identifier(self.pool_id, "pool_id"))
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.capacity, self.reserved)):
            raise HarnessValidationError("capacity pool values must be non-negative", code="CAPACITY_POLICY_INVALID")
        if self.reserved > self.capacity:
            raise HarnessValidationError("capacity pool is over-reserved", code="CAPACITY_POLICY_INVALID")
        identifier(self.policy_version, "capacity_policy_version")
        if self.policy_version.casefold() in {"current", "latest", "default", "stable"}:
            raise HarnessValidationError("capacity policy must have an exact version", code="CAPACITY_POLICY_INVALID")
        expected = canonical_payload_checksum({"pool_id": self.pool_id, "capacity": self.capacity, "policy_version": self.policy_version, "owner_scope": self.owner_scope, "reservation_key": self.reservation_key})
        if self.policy_checksum and self.policy_checksum != expected:
            raise HarnessValidationError("capacity policy checksum mismatch", code="CAPACITY_POLICY_CHECKSUM_MISMATCH")
        object.__setattr__(self, "policy_checksum", expected)
        if self.schema_version != CAPACITY_POOL_SCHEMA:
            raise HarnessValidationError("unsupported capacity pool schema", code="CAPACITY_POLICY_INVALID")
        for name in ("owner_scope", "reservation_key"):
            identifier(getattr(self, name), name)
        _integer(self.reservation_version, "reservation_version")
        _integer(self.expires_at_ms, "expires_at_ms", minimum=1)

    def require_current(self, *, now_ms: int) -> None:
        _integer(now_ms, "now_ms")
        if not self.owner_scope or not self.reservation_key or not self.expires_at_ms:
            raise HarnessValidationError("capacity owner, reservation identity and expiry are required", code="CAPACITY_POLICY_MISSING")
        if now_ms >= self.expires_at_ms:
            raise HarnessValidationError("capacity policy has expired", code="CAPACITY_POLICY_STALE")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version, "pool_id": self.pool_id,
            "capacity": self.capacity, "reserved": self.reserved,
            "policy_version": self.policy_version, "policy_checksum": self.policy_checksum,
            "owner_scope": self.owner_scope, "reservation_key": self.reservation_key,
            "reservation_version": self.reservation_version, "expires_at_ms": self.expires_at_ms,
        }
        return {**payload, "snapshot_checksum": canonical_payload_checksum(payload)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacityPool:
        payload = exact_keys(value, required=frozenset({
            "schema_version", "pool_id", "capacity", "reserved", "policy_version",
            "policy_checksum", "owner_scope", "reservation_key", "reservation_version",
            "expires_at_ms", "snapshot_checksum",
        }), model=cls.__name__)
        supplied = checksum(payload.pop("snapshot_checksum"), "snapshot_checksum")
        result = cls(**payload)
        if result.to_dict()["snapshot_checksum"] != supplied:
            raise HarnessValidationError("capacity snapshot checksum mismatch", code="CAPACITY_POLICY_CHECKSUM_MISMATCH")
        return result

    @property
    def available(self) -> int:
        return self.capacity - self.reserved


@dataclass(frozen=True, slots=True)
class TaskCapacityDemand:
    task_id: str
    quantities: Mapping[str, int]
    side_effect_class: SideEffectClass | str = SideEffectClass.READ_ONLY
    resource_conflict_key: str | None = None
    global_serial: bool = False
    idempotent_concurrency: bool = False
    idempotency_receipt_checksum: str | None = None
    schema_version: str = CAPACITY_DEMAND_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        if not isinstance(self.quantities, Mapping) or not self.quantities:
            raise HarnessValidationError("task capacity demand must name at least one pool", code="CAPACITY_DEMAND_INVALID")
        normalized = {}
        for pool_id, quantity in self.quantities.items():
            pool = identifier(pool_id, "pool_id")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise HarnessValidationError("task capacity quantity must be positive", code="CAPACITY_DEMAND_INVALID")
            normalized[pool] = quantity
        object.__setattr__(self, "quantities", frozen_mapping(normalized, "capacity_demand.quantities"))
        try:
            object.__setattr__(self, "side_effect_class", SideEffectClass(self.side_effect_class))
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError("invalid side-effect class", code="CAPACITY_DEMAND_INVALID") from exc
        if self.resource_conflict_key is not None and (not isinstance(self.resource_conflict_key, str) or not self.resource_conflict_key.strip()):
            raise HarnessValidationError("resource conflict key must be non-empty", code="CAPACITY_DEMAND_INVALID")
        if self.resource_conflict_key is not None:
            identifier(self.resource_conflict_key, "resource_conflict_key")
        if self.side_effect_class is not SideEffectClass.READ_ONLY and not self.resource_conflict_key:
            raise HarnessValidationError("mutation demand requires a trusted resource conflict key", code="RESOURCE_CONFLICT_KEY_REQUIRED")
        if any(not isinstance(flag, bool) for flag in (self.global_serial, self.idempotent_concurrency)):
            raise HarnessValidationError("capacity concurrency flags must be boolean", code="CAPACITY_DEMAND_INVALID")
        if self.global_serial and self.side_effect_class is SideEffectClass.READ_ONLY:
            raise HarnessValidationError("read-only demand cannot require a mutation fence", code="CAPACITY_DEMAND_INVALID")
        if self.side_effect_class is SideEffectClass.EXTERNAL_IDEMPOTENT:
            if self.idempotency_receipt_checksum is None:
                raise HarnessValidationError("idempotent mutation requires receipt evidence", code="SIDE_EFFECT_RECEIPT_REQUIRED")
            checksum(self.idempotency_receipt_checksum, "idempotency_receipt_checksum")
        elif self.idempotent_concurrency or self.idempotency_receipt_checksum is not None:
            raise HarnessValidationError("idempotency receipt is only valid for idempotent mutation", code="CAPACITY_DEMAND_INVALID")
        if self.schema_version != CAPACITY_DEMAND_SCHEMA:
            raise HarnessValidationError("unsupported task demand schema", code="CAPACITY_DEMAND_INVALID")

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "task_id": self.task_id, "quantities": thaw_mapping(self.quantities), "side_effect_class": self.side_effect_class.value, "resource_conflict_key": self.resource_conflict_key, "global_serial": self.global_serial, "idempotent_concurrency": self.idempotent_concurrency, "idempotency_receipt_checksum": self.idempotency_receipt_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskCapacityDemand:
        return cls(**exact_keys(value, required=frozenset({"schema_version", "task_id", "quantities", "side_effect_class", "resource_conflict_key", "global_serial", "idempotent_concurrency", "idempotency_receipt_checksum"}), model=cls.__name__))


@dataclass(frozen=True, slots=True)
class PoolReservation:
    task_id: str
    allocations: Mapping[str, int]
    policy_checksums: Mapping[str, str]
    owner_scope: str
    reservation_key: str
    pool_versions: Mapping[str, int]
    pool_reservation_keys: Mapping[str, str]
    expires_at_ms: int
    reservation_version: int = 1
    state: ReservationState | str = ReservationState.RESERVED
    schema_version: str = POOL_RESERVATION_SCHEMA
    reservation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", identifier(self.task_id, "task_id"))
        if not isinstance(self.allocations, Mapping) or not self.allocations:
            raise HarnessValidationError("pool reservation allocations must be an object", code="CAPACITY_RESERVATION_INVALID")
        allocations = {identifier(key, "pool_id"): value for key, value in self.allocations.items()}
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in allocations.values()):
            raise HarnessValidationError("pool allocation must be positive", code="CAPACITY_RESERVATION_INVALID")
        object.__setattr__(self, "allocations", frozen_mapping(allocations, "pool_reservation.allocations"))
        if not isinstance(self.policy_checksums, Mapping) or set(self.policy_checksums) != set(allocations):
            raise HarnessValidationError("pool reservation policy evidence must match allocations", code="CAPACITY_RESERVATION_INVALID")
        policy_checksums = {
            identifier(str(key), "pool_id"): checksum(value, "pool_policy_checksum")
            for key, value in self.policy_checksums.items()
        }
        object.__setattr__(self, "policy_checksums", frozen_mapping(policy_checksums, "pool_reservation.policy_checksums"))
        identifier(self.owner_scope, "owner_scope")
        identifier(self.reservation_key, "reservation_key")
        _integer(self.expires_at_ms, "expires_at_ms", minimum=1)
        for name, normalize in (("pool_versions", lambda value: _integer(value, "pool_version")), ("pool_reservation_keys", lambda value: identifier(value, "pool_reservation_key"))):
            values = getattr(self, name)
            if not isinstance(values, Mapping) or set(values) != set(allocations):
                raise HarnessValidationError("pool reservation identity must cover every allocation", code="CAPACITY_RESERVATION_INVALID")
            object.__setattr__(self, name, frozen_mapping({key: normalize(value) for key, value in values.items()}, name))
        try:
            object.__setattr__(self, "state", ReservationState(self.state))
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError("invalid pool reservation state", code="CAPACITY_RESERVATION_INVALID") from exc
        expected_version = 1 if self.state is ReservationState.RESERVED else 2
        if _integer(self.reservation_version, "reservation_version", minimum=1) != expected_version or self.schema_version != POOL_RESERVATION_SCHEMA:
            raise HarnessValidationError("invalid pool reservation revision or schema", code="CAPACITY_RESERVATION_INVALID")
        object.__setattr__(self, "reservation_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "task_id": self.task_id, "allocations": thaw_mapping(self.allocations), "policy_checksums": thaw_mapping(self.policy_checksums), "owner_scope": self.owner_scope, "reservation_key": self.reservation_key, "pool_versions": thaw_mapping(self.pool_versions), "pool_reservation_keys": thaw_mapping(self.pool_reservation_keys), "expires_at_ms": self.expires_at_ms, "reservation_version": self.reservation_version, "state": self.state.value}
        if include_checksum:
            result["reservation_checksum"] = self.reservation_checksum
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PoolReservation:
        payload = exact_keys(value, required=frozenset({"schema_version", "task_id", "allocations", "policy_checksums", "owner_scope", "reservation_key", "pool_versions", "pool_reservation_keys", "expires_at_ms", "reservation_version", "state", "reservation_checksum"}), model=cls.__name__)
        supplied = checksum(payload.pop("reservation_checksum"), "reservation_checksum")
        result = cls(**payload)
        if supplied != result.reservation_checksum:
            raise HarnessValidationError("pool reservation checksum mismatch", code="CAPACITY_RESERVATION_INVALID")
        return result

    def settled(self, state: ReservationState | str, *, reservation_key: str, expected_version: int) -> PoolReservation:
        try:
            target = ReservationState(state)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError("invalid capacity settlement state", code="CAPACITY_RESERVATION_INVALID") from exc
        if reservation_key != self.reservation_key or expected_version != 1 or isinstance(expected_version, bool):
            raise HarnessValidationError("capacity settlement key or version conflict", code="CAPACITY_RESERVATION_CONFLICT")
        if target is ReservationState.RESERVED or (self.state is not ReservationState.RESERVED and self.state is not target):
            raise HarnessValidationError("capacity reservation cannot be settled twice differently", code="CAPACITY_RESERVATION_CONFLICT")
        return self if self.state is target else replace(self, state=target, reservation_version=2)

    def admission_snapshot(self) -> dict[str, Any]:
        return replace(self, state=ReservationState.RESERVED, reservation_version=1).to_dict()

    @property
    def outstanding_allocations(self) -> Mapping[str, int]:
        """CONSUMED records finished use; only RESERVED occupies live slots."""
        return self.allocations if self.state is ReservationState.RESERVED else frozen_mapping({}, "outstanding_allocations")


@dataclass(frozen=True, slots=True)
class FirstFitPacking:
    selected: tuple[str, ...]
    overflow: tuple[str, ...]
    reservations: tuple[PoolReservation, ...]
    reasons: Mapping[str, str]
    packing_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "selected", tuple(self.selected))
        object.__setattr__(self, "overflow", tuple(self.overflow))
        object.__setattr__(self, "reservations", tuple(self.reservations))
        if len(set(self.selected + self.overflow)) != len(self.selected) + len(self.overflow) or tuple(item.task_id for item in self.reservations) != self.selected or set(self.reasons) != set(self.overflow):
            raise HarnessValidationError("packing reservations and overflow must partition tasks", code="CAPACITY_RESERVATION_INVALID")
        object.__setattr__(self, "reasons", frozen_mapping(dict(self.reasons), "packing.reasons"))
        object.__setattr__(self, "packing_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {"selected": list(self.selected), "overflow": list(self.overflow), "reservations": [item.to_dict() for item in self.reservations], "reasons": thaw_mapping(self.reasons)}
        if include_checksum:
            value["packing_checksum"] = self.packing_checksum
        return value


def pack_first_fit(
    task_ids: Sequence[str],
    demands: Mapping[str, TaskCapacityDemand],
    pools: Mapping[str, CapacityPool],
    *,
    max_tasks: int,
    occupied_resource_keys: frozenset[str] = frozenset(),
    owner_scope: str,
    reservation_keys: Mapping[str, str],
    now_ms: int | None = None,
) -> FirstFitPacking:
    """Select tasks in supplied stable order; failed tasks never reserve partially."""
    if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 1:
        raise HarnessValidationError("max_tasks must be positive", code="CAPACITY_POLICY_INVALID")
    identifier(owner_scope, "owner_scope")
    observed_at = capacity_now_ms() if now_ms is None else _integer(now_ms, "now_ms")
    for pool_id, pool in pools.items():
        if not isinstance(pool, CapacityPool) or pool_id != pool.pool_id:
            raise HarnessValidationError("capacity pools must be keyed by pool identity", code="CAPACITY_POLICY_INVALID")
        pool.require_current(now_ms=observed_at)
    if not isinstance(reservation_keys, Mapping):
        raise HarnessValidationError("task reservation keys are required", code="CAPACITY_RESERVATION_INVALID")
    remaining = {pool_id: pool.available for pool_id, pool in pools.items()}
    selected, overflow, reservations, reasons = [], [], [], {}
    selected_demands: list[TaskCapacityDemand] = []
    seen = set()
    for raw_task_id in task_ids:
        task_id = identifier(raw_task_id, "task_id")
        if task_id in seen:
            raise HarnessValidationError("duplicate task in capacity packing", code="CAPACITY_DEMAND_INVALID")
        seen.add(task_id)
        demand = demands.get(task_id)
        reason = None
        if demand is None:
            reason = "CAPACITY_POLICY_MISSING"
        elif not isinstance(demand, TaskCapacityDemand) or demand.task_id != task_id:
            raise HarnessValidationError("capacity demand identity mismatch", code="CAPACITY_DEMAND_INVALID")
        elif any(pool_id not in pools for pool_id in demand.quantities):
            raise HarnessValidationError("task requires an unavailable capacity policy", code="CAPACITY_POLICY_MISSING")
        elif len(selected) >= max_tasks:
            reason = "CAPACITY_NOT_AVAILABLE"
        elif any(pool_id not in pools or remaining.get(pool_id, 0) < quantity for pool_id, quantity in demand.quantities.items()):
            reason = "CAPACITY_NOT_AVAILABLE"
        elif demand.side_effect_class is not SideEffectClass.READ_ONLY and demand.resource_conflict_key in occupied_resource_keys:
            reason = "RESOURCE_CONFLICT"
        elif any(resource_demands_conflict(demand, previous) for previous in selected_demands):
            reason = "RESOURCE_CONFLICT"
        if reason:
            overflow.append(task_id)
            reasons[task_id] = reason
            continue
        allocations = dict(demand.quantities)
        reservation_key = identifier(reservation_keys.get(task_id), "reservation_key")
        for pool_id, quantity in allocations.items():
            remaining[pool_id] -= quantity
        selected.append(task_id)
        selected_demands.append(demand)
        reservations.append(PoolReservation(
            task_id, allocations, {pool_id: pools[pool_id].policy_checksum for pool_id in allocations},
            owner_scope, reservation_key,
            {pool_id: pools[pool_id].reservation_version for pool_id in allocations},
            {pool_id: pools[pool_id].reservation_key for pool_id in allocations},
            min(pools[pool_id].expires_at_ms for pool_id in allocations),
        ))
    return FirstFitPacking(tuple(selected), tuple(overflow), tuple(reservations), reasons)


def resource_demands_conflict(left: TaskCapacityDemand, right: TaskCapacityDemand) -> bool:
    """Symmetric resource eligibility; ordering never changes read/write safety."""
    if left.global_serial or right.global_serial:
        return True
    if left.resource_conflict_key is None or left.resource_conflict_key != right.resource_conflict_key:
        return False
    if left.side_effect_class is right.side_effect_class is SideEffectClass.READ_ONLY:
        return False
    return not (
        left.side_effect_class is right.side_effect_class is SideEffectClass.EXTERNAL_IDEMPOTENT
        and left.idempotent_concurrency and right.idempotent_concurrency
    )
