"""Immutable per-attempt TaskBudget accounting, committed by the TaskPlan store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    exact_reference,
    frozen_mapping,
    identifier,
    non_negative_int,
    positive_int,
    required_text,
    thaw_mapping,
)

if TYPE_CHECKING:
    from framework.harness.task_plan.models import TaskInstance, ValidatedTaskPlan
    from framework.harness.task_plan.store import TaskResultRecord


TASK_PLAN_BUDGET_LEDGER_SCHEMA = "agora.task-plan-budget-ledger/v1"
TASK_PLAN_BUDGET_RECORD_SCHEMA = "agora.task-plan-budget-record/v1"
BUDGET_DIMENSIONS = ("max_turns", "max_tool_calls", "max_memory_ops", "max_output_tokens")
_COUNTERS = frozenset(f"{prefix}_{name}" for prefix in ("reserved", "consumed", "released") for name in BUDGET_DIMENSIONS)
_RECORD_KEYS = frozenset({
    "schema_version", "reservation_key", "instance", "status", "consumed", "released",
    "result_checksum", "reason_code", "reserved_revision", "settled_revision",
})
_LEDGER_KEYS = frozenset({
    "schema_version", "run_id", "stage_id", "policy_ref", "parent_allocation",
    "ledger_version", "records", "ledger_checksum",
})


@dataclass(frozen=True, slots=True)
class TaskPlanBudgetLedger:
    run_id: str
    stage_id: str
    policy_ref: str
    parent_allocation: Mapping[str, int]
    records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    ledger_version: int = 0

    def __post_init__(self) -> None:
        from framework.harness.task_plan.models import TaskInstance

        identifier(self.run_id, "run_id")
        identifier(self.stage_id, "stage_id")
        exact_reference(self.policy_ref, "policy_ref")
        non_negative_int(self.ledger_version, "ledger_version")
        parent = _allocation(self.parent_allocation)
        records = frozen_mapping(self.records, "budget_records")
        revisions: list[int] = []
        identities: set[tuple[str, str, int]] = set()
        instance_ids: set[str] = set()
        outstanding: set[str] = set()
        for key, raw in records.items():
            record = exact_keys(raw, required=_RECORD_KEYS, model="TaskPlanBudgetRecord")
            if record["schema_version"] != TASK_PLAN_BUDGET_RECORD_SCHEMA or record["reservation_key"] != key:
                _fail("budget record schema or reservation key is invalid")
            instance = TaskInstance.from_dict(record["instance"])
            if (instance.run_id, instance.stage_id) != (self.run_id, self.stage_id) or instance.idempotency_key != key:
                _fail("budget reservation owner or identity conflicts", "task_plan_budget_identity_conflict")
            identity = (instance.plan_id, instance.task_id, instance.attempt)
            if instance.task_instance_id in instance_ids or identity in identities:
                _fail("budget reservation attempt identity conflicts", "task_plan_budget_identity_conflict")
            instance_ids.add(instance.task_instance_id)
            identities.add(identity)
            consumed, released = _allocation(record["consumed"]), _allocation(record["released"])
            reserved_revision = positive_int(record["reserved_revision"], "reserved_revision")
            revisions.append(reserved_revision)
            status = record["status"]
            if status == "RESERVED":
                if any(consumed.values()) or any(released.values()) or any(record[name] is not None for name in ("settled_revision", "result_checksum", "reason_code")):
                    _fail("outstanding budget reservation contains terminal accounting")
                if instance.task_id in outstanding:
                    _fail("retry cannot overlap an outstanding reservation", "task_plan_budget_identity_conflict")
                outstanding.add(instance.task_id)
            elif status in {"SETTLED", "RELEASED"}:
                settled_revision = positive_int(record["settled_revision"], "settled_revision")
                if settled_revision <= reserved_revision:
                    _fail("budget settlement revision must follow reservation")
                revisions.append(settled_revision)
                if any(consumed[name] + released[name] != getattr(instance.budget_snapshot, name) for name in BUDGET_DIMENSIONS):
                    _fail("budget settlement does not partition the reservation")
                if status == "SETTLED":
                    checksum(record["result_checksum"], "result_checksum")
                    if record["reason_code"] is not None:
                        _fail("result settlement cannot contain a release reason")
                else:
                    required_text(record["reason_code"], "reason_code")
                    if any(consumed.values()) or record["result_checksum"] is not None:
                        _fail("unstarted release cannot contain consumption or a result")
            else:
                _fail("budget reservation status is invalid")
        # Bound the validation work by actual records, not an untrusted revision.
        if len(revisions) != self.ledger_version or sorted(revisions) != list(range(1, len(revisions) + 1)):
            _fail("budget ledger revisions are not contiguous")
        previous_attempts: dict[str, Mapping[str, Any]] = {}
        for record in sorted(records.values(), key=lambda item: item["reserved_revision"]):
            identity = record["instance"]
            previous = previous_attempts.get(identity["task_id"])
            if previous is not None:
                if previous["settled_revision"] is None or previous["settled_revision"] >= record["reserved_revision"]:
                    _fail("retry history overlaps an unsettled reservation", "task_plan_budget_identity_conflict")
                if previous["instance"]["plan_id"] == identity["plan_id"] and previous["instance"]["attempt"] >= identity["attempt"]:
                    _fail("retry history reuses an earlier attempt", "task_plan_budget_identity_conflict")
            previous_attempts[identity["task_id"]] = record
        object.__setattr__(self, "parent_allocation", frozen_mapping(parent, "parent_allocation"))
        object.__setattr__(self, "records", records)
        if any(value > parent[name] for name, value in self.allocated_totals().items()):
            _fail("budget reservations exceed the parent allocation", "task_plan_budget_exceeded")

    @classmethod
    def for_plan(cls, plan: ValidatedTaskPlan) -> TaskPlanBudgetLedger:
        return cls(plan.run_id, plan.stage_id, plan.policy_ref, plan.limits.aggregate_task_budget.to_dict())

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> TaskPlanBudgetLedger:
        data = exact_keys(snapshot, required=_COUNTERS | {"ledger"}, model="TaskPlanBudgetSnapshot")
        raw = exact_keys(data["ledger"], required=_LEDGER_KEYS, model="TaskPlanBudgetLedger")
        supplied = checksum(raw.pop("ledger_checksum"), "ledger_checksum")
        if raw.pop("schema_version") != TASK_PLAN_BUDGET_LEDGER_SCHEMA:
            _fail("budget ledger schema is unsupported")
        expected = canonical_payload_checksum({"schema_version": TASK_PLAN_BUDGET_LEDGER_SCHEMA, **raw})
        if supplied != expected:
            _fail("budget ledger checksum mismatch", "task_plan_budget_checksum_mismatch")
        ledger = cls(**raw)
        for name, value in ledger.counters().items():
            if non_negative_int(data[name], name) != value:
                _fail("budget counters differ from per-attempt ledger", "task_plan_budget_checksum_mismatch")
        return ledger

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": TASK_PLAN_BUDGET_LEDGER_SCHEMA,
            "run_id": self.run_id, "stage_id": self.stage_id, "policy_ref": self.policy_ref,
            "parent_allocation": dict(self.parent_allocation), "ledger_version": self.ledger_version,
            "records": thaw_mapping(self.records),
        }
        return {**value, "ledger_checksum": canonical_payload_checksum(value)}

    def snapshot(self) -> dict[str, Any]:
        return {**self.counters(), "ledger": self.to_dict()}

    def counters(self) -> dict[str, int]:
        counters = dict.fromkeys(sorted(_COUNTERS), 0)
        for record in self.records.values():
            for name in BUDGET_DIMENSIONS:
                counters[f"consumed_{name}"] += record["consumed"][name]
                counters[f"released_{name}"] += record["released"][name]
                if record["status"] == "RESERVED":
                    counters[f"reserved_{name}"] += record["instance"]["budget_snapshot"][name]
        return counters

    def allocated_totals(self) -> dict[str, int]:
        counters = self.counters()
        return {name: sum(counters[f"{prefix}_{name}"] for prefix in ("consumed", "released", "reserved")) for name in BUDGET_DIMENSIONS}

    def reserve(self, instances: Sequence[TaskInstance]) -> TaskPlanBudgetLedger:
        from framework.harness.task_plan.models import TaskInstance

        if isinstance(instances, (str, bytes)) or not isinstance(instances, Sequence) or any(not isinstance(item, TaskInstance) for item in instances):
            _fail("budget reservations require typed TaskInstance values")
        records = thaw_mapping(self.records)
        revision = self.ledger_version
        for instance in instances:
            key, identity = instance.idempotency_key, instance.to_dict()
            if (instance.run_id, instance.stage_id) != (self.run_id, self.stage_id):
                _fail("budget reservation owner identity conflicts", "task_plan_budget_identity_conflict")
            existing = records.get(key)
            if existing is not None:
                if existing["instance"] != identity:
                    _fail("budget reservation identity conflicts", "task_plan_budget_identity_conflict")
                if existing["status"] != "RESERVED":
                    _fail("terminal reservation cannot fund execution", "task_plan_budget_identity_conflict")
                continue
            for record in records.values():
                previous = record["instance"]
                if previous["task_id"] == instance.task_id and (
                    record["status"] == "RESERVED"
                    or (previous["plan_id"] == instance.plan_id and previous["attempt"] >= instance.attempt)
                ):
                    _fail("retry requires a new attempt after terminal settlement", "task_plan_budget_identity_conflict")
            revision += 1
            records[key] = {
                "schema_version": TASK_PLAN_BUDGET_RECORD_SCHEMA,
                "reservation_key": key, "instance": identity, "status": "RESERVED",
                "consumed": dict.fromkeys(BUDGET_DIMENSIONS, 0),
                "released": dict.fromkeys(BUDGET_DIMENSIONS, 0),
                "result_checksum": None, "reason_code": None,
                "reserved_revision": revision, "settled_revision": None,
            }
        return self if revision == self.ledger_version else replace(self, records=records, ledger_version=revision)

    def settle(self, result: TaskResultRecord, *, expected_allocation: Mapping[str, int] | None = None) -> TaskPlanBudgetLedger:
        from framework.harness.task_plan.models import TaskLifecycle
        from framework.harness.task_plan.store import TaskResultRecord

        if not isinstance(result, TaskResultRecord) or result.status not in {TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED}:
            _fail("budget settlement requires a typed terminal TaskResultRecord", "task_plan_result_invalid")
        key, record = self._attempt(result.task_instance_id, result.task_id, result.attempt)
        instance = record["instance"]
        identity_fields = ("run_id", "stage_id", "plan_id", "plan_version", "graph_checksum", "worker_ref", "stage_identity_checksum")
        if any(getattr(result, name) != instance[name] for name in identity_fields) or result.task_checksum != instance["task_definition_checksum"]:
            _fail("result and budget reservation identity conflicts", "task_plan_budget_identity_conflict")
        allocation = instance["budget_snapshot"]
        if expected_allocation is not None and _allocation(expected_allocation) != allocation:
            _fail("accepted allocation and budget reservation identity conflicts", "task_plan_budget_identity_conflict")
        consumed = result_budget_usage(result.usage, allocation)
        released = {name: allocation[name] - consumed[name] for name in BUDGET_DIMENSIONS}
        if record["status"] == "SETTLED" and record["result_checksum"] == result.result_checksum:
            if record["consumed"] != consumed or record["released"] != released:
                _fail("recorded settlement conflicts with result usage", "task_plan_budget_settlement_conflict")
            return self
        if record["status"] != "RESERVED":
            _fail("conflicting duplicate budget settlement", "task_plan_budget_settlement_conflict")
        return self._terminal(key, {
            "status": "SETTLED", "consumed": consumed,
            "released": released,
            "result_checksum": result.result_checksum,
        })

    def release_unstarted(self, task_instance_id: str | None, task_id: str, attempt: int, *, reason_code: str) -> TaskPlanBudgetLedger:
        """Release only after the caller has proved the attempt never started."""
        required_text(reason_code, "reason_code")
        key, record = self._attempt(task_instance_id, task_id, attempt)
        if record["status"] == "RELEASED" and record["reason_code"] == reason_code:
            return self
        if record["status"] != "RESERVED":
            _fail("conflicting duplicate budget release", "task_plan_budget_settlement_conflict")
        return self._terminal(key, {
            "status": "RELEASED", "released": dict(record["instance"]["budget_snapshot"]),
            "reason_code": reason_code,
        })

    def _attempt(self, task_instance_id: str | None, task_id: str, attempt: int) -> tuple[str, Mapping[str, Any]]:
        for key, record in self.records.items():
            identity = record["instance"]
            if (identity["task_instance_id"], identity["task_id"], identity["attempt"]) == (task_instance_id, task_id, attempt):
                return key, record
        _fail("result or release has no matching budget reservation", "task_plan_budget_reservation_missing")

    def _terminal(self, key: str, updates: Mapping[str, Any]) -> TaskPlanBudgetLedger:
        records = thaw_mapping(self.records)
        records[key].update(updates, settled_revision=self.ledger_version + 1)
        return replace(self, records=records, ledger_version=self.ledger_version + 1)


def _allocation(value: Mapping[str, Any]) -> dict[str, int]:
    data = exact_keys(value, required=frozenset(BUDGET_DIMENSIONS), model="TaskBudgetAllocation")
    return {name: non_negative_int(data[name], name) for name in BUDGET_DIMENSIONS}


def result_budget_usage(usage: Mapping[str, Any], allocation: Mapping[str, Any]) -> dict[str, int]:
    requested = _allocation(allocation)
    if not isinstance(usage, Mapping):
        _fail("task result usage must be an object", "task_plan_result_usage_invalid")
    actual: dict[str, int] = {}
    for name in BUDGET_DIMENSIONS:
        values = [usage[key] for key in (name.removeprefix("max_"), name) if key in usage]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values) or (len(values) == 2 and values[0] != values[1]):
            _fail("task result usage must be non-negative integers with consistent aliases", "task_plan_result_usage_invalid")
        # Missing measurements are charged conservatively, never silently freed.
        actual[name] = values[0] if values else requested[name]
        if actual[name] > requested[name]:
            _fail("task result usage exceeds the accepted task budget", "task_plan_result_budget_exceeded")
    return actual


def _fail(message: str, code: str = "task_plan_budget_snapshot_invalid") -> None:
    raise HarnessValidationError(message, code=code)
