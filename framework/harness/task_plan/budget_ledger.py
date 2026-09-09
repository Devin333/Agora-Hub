"""Immutable per-attempt TaskBudget accounting, committed by the TaskPlan store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.budget_allocation import USAGE_FIELDS, budget_allocation, budget_counter_keys
from framework.harness.task_plan.budget_settlement import (
    BUDGET_EXCEEDED, BudgetSettlementReadPort, BudgetSettlementReceipt, SETTLEMENT_REASONS,
)
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


TASK_PLAN_BUDGET_LEDGER_SCHEMA = "agora.task-plan-budget-ledger/v2"
TASK_PLAN_BUDGET_RECORD_SCHEMA = "agora.task-plan-budget-record/v2"
_RECORD_KEYS = frozenset({
    "schema_version", "reservation_key", "instance", "status", "consumed", "released",
    "result_checksum", "reason_code", "reserved_revision", "settled_revision", "settlement_receipt",
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
        if len(records) > 4096:
            _fail("budget reservation history exceeds its bound")
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
            if any(set(value) != set(parent) for value in (consumed, released, instance.budget_snapshot.to_dict())):
                _fail("reservation dimensions differ from parent policy", "task_plan_budget_policy_missing")
            reserved_revision = positive_int(record["reserved_revision"], "reserved_revision")
            revisions.append(reserved_revision)
            status = record["status"]
            if status == "RESERVED":
                if any(consumed.values()) or any(released.values()) or any(record[name] is not None for name in ("settled_revision", "result_checksum", "reason_code", "settlement_receipt")):
                    _fail("outstanding budget reservation contains terminal accounting")
                if instance.task_id in outstanding:
                    _fail("retry cannot overlap an outstanding reservation", "task_plan_budget_identity_conflict")
                outstanding.add(instance.task_id)
            elif status in {"SETTLED", "RELEASED", "TERMINATED"}:
                settled_revision = positive_int(record["settled_revision"], "settled_revision")
                if settled_revision <= reserved_revision:
                    _fail("budget settlement revision must follow reservation")
                revisions.append(settled_revision)
                if any(consumed[name] + released[name] != getattr(instance.budget_snapshot, name) for name in parent):
                    _fail("budget settlement does not partition the reservation")
                if status == "SETTLED":
                    checksum(record["result_checksum"], "result_checksum")
                    if record["reason_code"] is not None or record["settlement_receipt"] is not None:
                        _fail("result settlement cannot contain a release reason")
                elif status == "RELEASED":
                    required_text(record["reason_code"], "reason_code")
                    if any(consumed.values()) or record["result_checksum"] is not None or record["settlement_receipt"] is not None:
                        _fail("unstarted release cannot contain consumption or a result")
                else:
                    receipt = BudgetSettlementReceipt.from_dict(record["settlement_receipt"])
                    if record["result_checksum"] is not None or record["reason_code"] not in SETTLEMENT_REASONS:
                        _fail("terminated budget requires attributable lifecycle evidence")
                    _validate_termination_receipt(instance, receipt)
                    if receipt.reason_code != record["reason_code"] or result_budget_usage(receipt.usage, instance.budget_snapshot.to_dict()) != consumed:
                        _fail("termination receipt differs from recorded accounting")
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
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("ledger"), Mapping):
            _fail("budget snapshot requires a ledger")
        parent = _allocation(snapshot["ledger"].get("parent_allocation"))
        data = exact_keys(snapshot, required=budget_counter_keys(parent) | {"ledger"}, model="TaskPlanBudgetSnapshot")
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
        counters = dict.fromkeys(sorted(budget_counter_keys(self.parent_allocation)), 0)
        for record in self.records.values():
            for name in self.parent_allocation:
                counters[f"consumed_{name}"] += record["consumed"][name]
                counters[f"released_{name}"] += record["released"][name]
                if record["status"] == "RESERVED":
                    counters[f"reserved_{name}"] += record["instance"]["budget_snapshot"][name]
        return counters

    def allocated_totals(self) -> dict[str, int]:
        counters = self.counters()
        return {name: sum(counters[f"{prefix}_{name}"] for prefix in ("consumed", "released", "reserved")) for name in self.parent_allocation}

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
                "consumed": dict.fromkeys(self.parent_allocation, 0),
                "released": dict.fromkeys(self.parent_allocation, 0),
                "result_checksum": None, "reason_code": None,
                "reserved_revision": revision, "settled_revision": None,
                "settlement_receipt": None,
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
        released = {name: allocation[name] - consumed[name] for name in allocation}
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

    def settle_terminated(
        self, instance: TaskInstance, *, receipt: BudgetSettlementReceipt,
        authority: BudgetSettlementReadPort,
    ) -> TaskPlanBudgetLedger:
        """Account a Harness-verified cancel/reclaim/recovery receipt once.

        The receipt must match evidence read from the Harness lifecycle owner.
        Uncertain termination retains the entire outstanding charge.
        """
        from framework.harness.task_plan.models import TaskInstance

        if not isinstance(instance, TaskInstance):
            _fail("budget settlement requires a typed attempt", "task_plan_budget_identity_conflict")
        _validate_termination_receipt(instance, receipt)
        if not isinstance(authority, BudgetSettlementReadPort):
            _fail("budget settlement authority is required", "task_plan_budget_authority_missing")
        verified = authority.read_budget_settlement(
            source_receipt_checksum=receipt.source_receipt_checksum,
            reservation_key=receipt.reservation_key,
        )
        if not isinstance(verified, BudgetSettlementReceipt) or verified.to_dict() != receipt.to_dict():
            _fail("budget settlement differs from authenticated evidence", "task_plan_budget_receipt_unverified")
        key, record = self._attempt(instance.task_instance_id, instance.task_id, instance.attempt)
        if thaw_mapping(record["instance"]) != instance.to_dict():
            _fail("lifecycle receipt differs from reserved attempt", "task_plan_budget_identity_conflict")
        consumed = result_budget_usage(receipt.usage, instance.budget_snapshot.to_dict())
        released = {name: amount - consumed[name] for name, amount in instance.budget_snapshot.to_dict().items()}
        updates = {"status": "TERMINATED", "consumed": consumed, "released": released, "reason_code": receipt.reason_code, "settlement_receipt": receipt.to_dict()}
        if record["status"] != "RESERVED":
            if all(record[name] == value for name, value in updates.items()):
                return self
            _fail("conflicting lifecycle budget settlement", "task_plan_budget_settlement_conflict")
        return self._terminal(key, updates)

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
    return budget_allocation(value)


def result_budget_usage(usage: Mapping[str, Any], allocation: Mapping[str, Any]) -> dict[str, int]:
    requested = _allocation(allocation)
    if not isinstance(usage, Mapping):
        _fail("task result usage must be an object", "task_plan_result_usage_invalid")
    actual: dict[str, int] = {}
    for name in requested:
        values = [usage[key] for key in (USAGE_FIELDS[name], name) if key in usage]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values) or (len(values) == 2 and values[0] != values[1]):
            _fail("task result usage must be non-negative integers with consistent aliases", "task_plan_result_usage_invalid")
        # Missing measurements are charged conservatively, never silently freed.
        actual[name] = values[0] if values else requested[name]
        if actual[name] > requested[name]:
            _fail("task result usage exceeds the accepted task budget", BUDGET_EXCEEDED)
    output_measured = any(key in usage for key in ("output_tokens", "max_output_tokens"))
    if "token_limit" in actual and output_measured and actual["max_output_tokens"] > actual["token_limit"]:
        _fail("output token usage exceeds total token usage", "task_plan_result_usage_invalid")
    if "token_limit" in actual and not output_measured:
        # Without separate output metering the total measurement is an upper
        # bound on output usage; do not fabricate more output than total tokens.
        actual["max_output_tokens"] = min(actual["max_output_tokens"], actual["token_limit"])
    return actual


def _validate_termination_receipt(instance: TaskInstance, receipt: BudgetSettlementReceipt) -> None:
    if not isinstance(receipt, BudgetSettlementReceipt) or not receipt.termination_confirmed:
        _fail("budget settlement requires confirmed termination", "task_plan_budget_termination_unconfirmed")
    if receipt.reservation_key != instance.idempotency_key or receipt.instance_checksum != instance.instance_checksum:
        _fail("lifecycle receipt differs from reserved attempt", "task_plan_budget_identity_conflict")


def _fail(message: str, code: str = "task_plan_budget_snapshot_invalid") -> None:
    raise HarnessValidationError(message, code=code)
