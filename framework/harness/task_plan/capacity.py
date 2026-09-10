"""Deterministic, all-or-nothing capacity packing for task waves."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import time_ns
from typing import Any, Mapping, Sequence

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import checksum, canonical_payload_checksum, exact_keys, frozen_mapping, identifier, thaw_mapping
from framework.harness.task_plan.parallel_lifecycle import ReservationState, SideEffectClass
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskInstance


CAPACITY_POOL_SCHEMA = "agora.task-capacity-pool/v1"
CAPACITY_DEMAND_SCHEMA = "agora.task-capacity-demand/v1"
POOL_RESERVATION_SCHEMA = "agora.task-pool-reservation/v1"
CAPACITY_SCOPE_SNAPSHOT_SCHEMA = "agora.task-capacity-scope-snapshot/v1"
FIRST_FIT_PACKING_SCHEMA = "agora.task-first-fit-packing/v1"


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
class CapacityScopeSnapshot:
    """Authoritative, revision-fenced view of one shared pool scope."""

    owner_scope: str
    pools: tuple[CapacityPool, ...]
    revision: int
    expires_at_ms: int
    schema_version: str = CAPACITY_SCOPE_SNAPSHOT_SCHEMA
    snapshot_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_scope", identifier(self.owner_scope, "owner_scope"))
        pools = tuple(self.pools)
        if (
            not pools
            or any(not isinstance(pool, CapacityPool) for pool in pools)
            or tuple(pool.pool_id for pool in pools)
            != tuple(sorted(pool.pool_id for pool in pools))
            or len({pool.pool_id for pool in pools}) != len(pools)
            or any(pool.owner_scope != self.owner_scope for pool in pools)
        ):
            raise HarnessValidationError(
                "capacity scope must contain one stable ordered pool set",
                code="CAPACITY_POLICY_INVALID",
            )
        object.__setattr__(self, "pools", pools)
        _integer(self.revision, "capacity_scope_revision", minimum=1)
        _integer(self.expires_at_ms, "capacity_scope_expires_at_ms", minimum=1)
        if self.expires_at_ms != min(pool.expires_at_ms for pool in pools):
            raise HarnessValidationError(
                "capacity scope expiry must equal its earliest pool expiry",
                code="CAPACITY_POLICY_INVALID",
            )
        if self.schema_version != CAPACITY_SCOPE_SNAPSHOT_SCHEMA:
            raise HarnessValidationError(
                "unsupported capacity scope snapshot schema",
                code="CAPACITY_POLICY_INVALID",
            )
        object.__setattr__(
            self,
            "snapshot_checksum",
            canonical_payload_checksum(self.to_dict(include_checksum=False)),
        )

    def require_current(self, *, now_ms: int) -> None:
        _integer(now_ms, "now_ms")
        if now_ms >= self.expires_at_ms:
            raise HarnessValidationError(
                "capacity scope snapshot has expired",
                code="CAPACITY_POLICY_STALE",
            )
        for pool in self.pools:
            pool.require_current(now_ms=now_ms)

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "owner_scope": self.owner_scope,
            "revision": self.revision,
            "expires_at_ms": self.expires_at_ms,
            "pools": [pool.to_dict() for pool in self.pools],
        }
        if include_checksum:
            result["snapshot_checksum"] = self.snapshot_checksum
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CapacityScopeSnapshot:
        payload = exact_keys(
            value,
            required=frozenset({
                "schema_version", "owner_scope", "revision", "expires_at_ms",
                "pools", "snapshot_checksum",
            }),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("snapshot_checksum"), "snapshot_checksum")
        raw_pools = payload.get("pools")
        if not isinstance(raw_pools, list):
            raise HarnessValidationError(
                "capacity scope pools must be an array",
                code="CAPACITY_POLICY_INVALID",
            )
        payload["pools"] = tuple(CapacityPool.from_dict(item) for item in raw_pools)
        result = cls(**payload)
        if result.snapshot_checksum != supplied:
            raise HarnessValidationError(
                "capacity scope snapshot checksum mismatch",
                code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
            )
        return result

    def with_reserved_pools(self, pools: Sequence[CapacityPool]) -> CapacityScopeSnapshot:
        """Build the next proposed scope revision; persistence grants authority."""

        proposed = tuple(pools)
        return CapacityScopeSnapshot(
            owner_scope=self.owner_scope,
            pools=proposed,
            revision=self.revision + 1,
            expires_at_ms=min(pool.expires_at_ms for pool in proposed),
        )


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
    ready_order: tuple[str, ...]
    selected: tuple[str, ...]
    overflow: tuple[str, ...]
    reservations: tuple[PoolReservation, ...]
    reasons: Mapping[str, str]
    capacity_before: CapacityScopeSnapshot | None = None
    capacity_after: CapacityScopeSnapshot | None = None
    budget_before_checksum: str | None = None
    budget_after_checksum: str | None = None
    admitted_budget_snapshot: Mapping[str, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    schema_version: str = FIRST_FIT_PACKING_SCHEMA
    packing_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        ready_order = tuple(
            identifier(item, "packing.ready_task_id")
            for item in _strict_tuple(self.ready_order, "ready_order")
        )
        selected = tuple(
            identifier(item, "packing.selected_task_id")
            for item in _strict_tuple(self.selected, "selected")
        )
        overflow = tuple(
            identifier(item, "packing.overflow_task_id")
            for item in _strict_tuple(self.overflow, "overflow")
        )
        reservations = _strict_tuple(self.reservations, "reservations")
        object.__setattr__(self, "ready_order", ready_order)
        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "overflow", overflow)
        object.__setattr__(self, "reservations", reservations)
        if not isinstance(self.reasons, Mapping):
            raise HarnessValidationError(
                "packing reasons must be an object",
                code="CAPACITY_RESERVATION_INVALID",
            )
        if (
            len(set(self.ready_order)) != len(self.ready_order)
            or set(self.selected).intersection(self.overflow)
            or set(self.selected).union(self.overflow) != set(self.ready_order)
            or tuple(item for item in self.ready_order if item in set(self.selected)) != self.selected
            or tuple(item for item in self.ready_order if item in set(self.overflow)) != self.overflow
            or any(not isinstance(item, PoolReservation) for item in self.reservations)
            or len({item.task_id for item in self.reservations}) != len(self.reservations)
            or any(item.task_id not in self.selected for item in self.reservations)
            or set(self.reasons) != set(self.overflow)
            or any(
                not isinstance(reason, str) or not reason
                for reason in self.reasons.values()
            )
        ):
            raise HarnessValidationError("packing reservations and overflow must partition tasks", code="CAPACITY_RESERVATION_INVALID")
        object.__setattr__(self, "reasons", frozen_mapping(dict(self.reasons), "packing.reasons"))
        before = self.capacity_before
        after = self.capacity_after
        if (before is None) is not (after is None):
            raise HarnessValidationError(
                "packing capacity evidence must contain both scope snapshots",
                code="CAPACITY_RESERVATION_INVALID",
            )
        if before is not None and (
            not isinstance(before, CapacityScopeSnapshot)
            or not isinstance(after, CapacityScopeSnapshot)
            or before.owner_scope != after.owner_scope
            or after.revision not in {before.revision, before.revision + 1}
            or tuple(item.pool_id for item in before.pools)
            != tuple(item.pool_id for item in after.pools)
            or (
                after.revision == before.revision
                and after.snapshot_checksum != before.snapshot_checksum
            )
            or (
                self.reservations
                and after.revision != before.revision + 1
            )
        ):
            raise HarnessValidationError(
                "packing capacity scope transition is invalid",
                code="CAPACITY_RESERVATION_INVALID",
            )
        object.__setattr__(self, "capacity_before", before)
        object.__setattr__(self, "capacity_after", after)
        if (self.budget_before_checksum is None) is not (self.budget_after_checksum is None):
            raise HarnessValidationError(
                "packing budget evidence must contain both ledger checksums",
                code="CAPACITY_RESERVATION_INVALID",
            )
        if self.budget_before_checksum is not None:
            object.__setattr__(self, "budget_before_checksum", checksum(self.budget_before_checksum, "budget_before_checksum"))
            object.__setattr__(self, "budget_after_checksum", checksum(self.budget_after_checksum, "budget_after_checksum"))
        if not isinstance(self.admitted_budget_snapshot, Mapping):
            raise HarnessValidationError(
                "packing admitted budget snapshot must be an object",
                code="CAPACITY_RESERVATION_INVALID",
            )
        object.__setattr__(self, "admitted_budget_snapshot", frozen_mapping(
            self.admitted_budget_snapshot,
            "packing.admitted_budget_snapshot",
        ))
        if self.schema_version != FIRST_FIT_PACKING_SCHEMA:
            raise HarnessValidationError(
                "unsupported first-fit packing schema",
                code="CAPACITY_RESERVATION_INVALID",
            )
        object.__setattr__(self, "packing_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "ready_order": list(self.ready_order),
            "selected": list(self.selected),
            "overflow": list(self.overflow),
            "reservations": [item.to_dict() for item in self.reservations],
            "reasons": thaw_mapping(self.reasons),
            "capacity_before": self.capacity_before.to_dict() if self.capacity_before else None,
            "capacity_after": self.capacity_after.to_dict() if self.capacity_after else None,
            "budget_before_checksum": self.budget_before_checksum,
            "budget_after_checksum": self.budget_after_checksum,
        }
        if include_checksum:
            value["packing_checksum"] = self.packing_checksum
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FirstFitPacking:
        payload = exact_keys(
            value,
            required=frozenset({
                "schema_version", "ready_order", "selected", "overflow",
                "reservations", "reasons", "capacity_before", "capacity_after",
                "budget_before_checksum", "budget_after_checksum",
                "packing_checksum",
            }),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("packing_checksum"), "packing_checksum")
        for name in ("ready_order", "selected", "overflow", "reservations"):
            payload[name] = _strict_tuple(payload[name], name)
        payload["reservations"] = tuple(
            PoolReservation.from_dict(item) for item in payload["reservations"]
        )
        payload["capacity_before"] = (
            CapacityScopeSnapshot.from_dict(payload["capacity_before"])
            if payload["capacity_before"] is not None else None
        )
        payload["capacity_after"] = (
            CapacityScopeSnapshot.from_dict(payload["capacity_after"])
            if payload["capacity_after"] is not None else None
        )
        result = cls(**payload)
        if result.packing_checksum != supplied:
            raise HarnessValidationError(
                "first-fit packing checksum mismatch",
                code="CAPACITY_RESERVATION_INVALID",
            )
        return result


def pack_first_fit(
    task_ids: Sequence[str],
    demands: Mapping[str, TaskCapacityDemand],
    pools: Mapping[str, CapacityPool],
    *,
    max_tasks: int,
    occupied_resource_keys: frozenset[str] = frozenset(),
    occupied_resource_demands: Sequence[TaskCapacityDemand] = (),
    owner_scope: str,
    reservation_keys: Mapping[str, str],
    now_ms: int | None = None,
    task_instances: Mapping[str, TaskInstance] | None = None,
    budget_snapshot: Mapping[str, Any] | None = None,
    capacity_snapshot: CapacityScopeSnapshot | None = None,
) -> FirstFitPacking:
    """Select tasks in supplied stable order across capacity and budget.

    Every candidate is evaluated against immutable provisional copies.  A
    failure in either dimension leaves both copies unchanged before the next
    candidate is considered.
    """
    if isinstance(max_tasks, bool) or not isinstance(max_tasks, int) or max_tasks < 0:
        raise HarnessValidationError("max_tasks must be non-negative", code="CAPACITY_POLICY_INVALID")
    if (
        isinstance(task_ids, (str, bytes, bytearray, Mapping, set, frozenset))
        or not isinstance(task_ids, Sequence)
    ):
        raise HarnessValidationError(
            "capacity packing task ids must be an ordered array",
            code="CAPACITY_DEMAND_INVALID",
        )
    if not isinstance(demands, Mapping) or not isinstance(pools, Mapping):
        raise HarnessValidationError(
            "capacity packing demands and pools must be objects",
            code="CAPACITY_POLICY_INVALID",
        )
    if (
        isinstance(occupied_resource_demands, (str, bytes, bytearray, Mapping, set, frozenset))
        or not isinstance(occupied_resource_demands, Sequence)
        or any(
            not isinstance(item, TaskCapacityDemand)
            for item in occupied_resource_demands
        )
    ):
        raise HarnessValidationError(
            "occupied resource demands must be an ordered array",
            code="CAPACITY_DEMAND_INVALID",
        )
    active_resource_demands = tuple(occupied_resource_demands)
    identifier(owner_scope, "owner_scope")
    observed_at = capacity_now_ms() if now_ms is None else _integer(now_ms, "now_ms")
    for pool_id, pool in pools.items():
        if not isinstance(pool, CapacityPool) or pool_id != pool.pool_id:
            raise HarnessValidationError("capacity pools must be keyed by pool identity", code="CAPACITY_POLICY_INVALID")
        pool.require_current(now_ms=observed_at)
    if not isinstance(reservation_keys, Mapping):
        raise HarnessValidationError("task reservation keys are required", code="CAPACITY_RESERVATION_INVALID")
    ordered_pools = tuple(sorted(pools.values(), key=lambda item: item.pool_id))
    if capacity_snapshot is not None:
        if (
            not isinstance(capacity_snapshot, CapacityScopeSnapshot)
            or capacity_snapshot.owner_scope != owner_scope
            or capacity_snapshot.pools != ordered_pools
        ):
            raise HarnessValidationError(
                "capacity scope snapshot differs from packing pools",
                code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
            )
        capacity_snapshot.require_current(now_ms=observed_at)
    elif ordered_pools:
        capacity_snapshot = CapacityScopeSnapshot(
            owner_scope=owner_scope,
            pools=ordered_pools,
            revision=max(1, max(pool.reservation_version for pool in ordered_pools)),
            expires_at_ms=min(pool.expires_at_ms for pool in ordered_pools),
        )
    remaining = {pool_id: pool.available for pool_id, pool in pools.items()}
    if (task_instances is None) is not (budget_snapshot is None):
        raise HarnessValidationError(
            "joint packing requires task instances and a budget snapshot together",
            code="task_plan_budget_identity_conflict",
        )
    ledger = None
    budget_before_checksum = budget_after_checksum = None
    if budget_snapshot is not None:
        ledger = TaskPlanBudgetLedger.from_snapshot(budget_snapshot)
        budget_before_checksum = ledger.to_dict()["ledger_checksum"]
        if not isinstance(task_instances, Mapping):
            raise HarnessValidationError(
                "joint packing task instances must be keyed by task id",
                code="task_plan_budget_identity_conflict",
            )
    ready_order, selected, overflow, reservations, reasons = [], [], [], [], {}
    selected_demands: list[TaskCapacityDemand] = []
    seen = set()
    for raw_task_id in task_ids:
        task_id = identifier(raw_task_id, "task_id")
        if task_id in seen:
            raise HarnessValidationError("duplicate task in capacity packing", code="CAPACITY_DEMAND_INVALID")
        seen.add(task_id)
        ready_order.append(task_id)
        demand = demands.get(task_id)
        reason = None
        if demand is None:
            if pools:
                raise HarnessValidationError(
                    "task requires a capacity demand from the pinned policy",
                    code="CAPACITY_POLICY_MISSING",
                )
        elif not isinstance(demand, TaskCapacityDemand) or demand.task_id != task_id:
            raise HarnessValidationError("capacity demand identity mismatch", code="CAPACITY_DEMAND_INVALID")
        elif any(pool_id not in pools for pool_id in demand.quantities):
            raise HarnessValidationError("task requires an unavailable capacity policy", code="CAPACITY_POLICY_MISSING")
        if len(selected) >= max_tasks:
            reason = "CAPACITY_NOT_AVAILABLE"
        elif demand is not None and any(
            pool_id not in pools or remaining.get(pool_id, 0) < quantity
            for pool_id, quantity in demand.quantities.items()
        ):
            reason = "CAPACITY_NOT_AVAILABLE"
        elif (
            demand is not None
            and demand.resource_conflict_key is not None
            and demand.resource_conflict_key in occupied_resource_keys
        ):
            reason = "RESOURCE_CONFLICT"
        elif demand is not None and any(
            resource_demands_conflict(demand, active)
            for active in active_resource_demands
        ):
            reason = "RESOURCE_CONFLICT"
        elif demand is not None and any(resource_demands_conflict(demand, previous) for previous in selected_demands):
            reason = "RESOURCE_CONFLICT"
        if reason:
            overflow.append(task_id)
            reasons[task_id] = reason
            continue
        allocations = {} if demand is None else dict(demand.quantities)
        reservation_key = identifier(reservation_keys.get(task_id), "reservation_key")
        candidate_ledger = ledger
        if ledger is not None:
            instance = task_instances.get(task_id) if task_instances is not None else None
            if not isinstance(instance, TaskInstance) or instance.task_id != task_id:
                raise HarnessValidationError(
                    "joint packing task instance identity mismatch",
                    code="task_plan_budget_identity_conflict",
                )
            try:
                candidate_ledger = ledger.reserve((instance,))
            except HarnessValidationError as exc:
                if exc.code != "task_plan_budget_exceeded":
                    raise
                overflow.append(task_id)
                reasons[task_id] = "BUDGET_EXCEEDED"
                continue
        for pool_id, quantity in allocations.items():
            remaining[pool_id] -= quantity
        ledger = candidate_ledger
        selected.append(task_id)
        if demand is not None:
            selected_demands.append(demand)
        if allocations:
            reservations.append(PoolReservation(
                task_id, allocations, {pool_id: pools[pool_id].policy_checksum for pool_id in allocations},
                owner_scope, reservation_key,
                {pool_id: pools[pool_id].reservation_version for pool_id in allocations},
                {pool_id: pools[pool_id].reservation_key for pool_id in allocations},
                min(pools[pool_id].expires_at_ms for pool_id in allocations),
            ))
    after_pools = tuple(
        replace(
            pool,
            reserved=pool.capacity - remaining[pool.pool_id],
            reservation_version=(
                pool.reservation_version + 1
                if pool.capacity - remaining[pool.pool_id] != pool.reserved
                else pool.reservation_version
            ),
        )
        for pool in ordered_pools
    )
    capacity_after = capacity_snapshot
    if capacity_snapshot is not None and reservations:
        capacity_after = capacity_snapshot.with_reserved_pools(after_pools)
    admitted_budget_snapshot: Mapping[str, Any] = {}
    if ledger is not None:
        budget_after_checksum = ledger.to_dict()["ledger_checksum"]
        admitted_budget_snapshot = ledger.snapshot()
    return FirstFitPacking(
        tuple(ready_order),
        tuple(selected),
        tuple(overflow),
        tuple(reservations),
        reasons,
        capacity_before=capacity_snapshot,
        capacity_after=capacity_after,
        budget_before_checksum=budget_before_checksum,
        budget_after_checksum=budget_after_checksum,
        admitted_budget_snapshot=admitted_budget_snapshot,
    )


def _strict_tuple(value: Any, name: str) -> tuple[Any, ...]:
    if (
        isinstance(value, (str, bytes, bytearray, Mapping, set, frozenset))
        or not isinstance(value, Sequence)
    ):
        raise HarnessValidationError(
            f"packing {name} must be an array",
            code="CAPACITY_RESERVATION_INVALID",
        )
    return tuple(value)


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
