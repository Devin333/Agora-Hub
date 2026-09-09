"""Immutable child budget evidence projected from the TaskPlan ledger."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.budget_allocation import (
    USAGE_FIELDS, budget_allocation, canonical_payload_checksum, checksum, exact_keys,
    frozen_mapping, identifier, positive_int, thaw_mapping,
)


BUDGET_RESERVATION_SCHEMA = "agora.harness-budget-reservation/v2"


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    owner_scope: str
    reservation_key: str
    parent_allocation: Mapping[str, int]
    attempt_allocation: Mapping[str, int]
    ledger_version: int
    schema_version: str = BUDGET_RESERVATION_SCHEMA
    reservation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        identifier(self.owner_scope, "owner_scope")
        identifier(self.reservation_key, "reservation_key")
        positive_int(self.ledger_version, "ledger_version")
        if self.schema_version != BUDGET_RESERVATION_SCHEMA:
            raise HarnessValidationError("unsupported budget reservation schema", code="task_plan_budget_snapshot_invalid")
        parent, attempt = budget_allocation(self.parent_allocation), budget_allocation(self.attempt_allocation)
        if "time_limit_ms" not in attempt or attempt["time_limit_ms"] < 1 or set(parent) != set(attempt):
            raise HarnessValidationError("versioned execution reservation requires matching explicit limits", code="task_plan_budget_policy_missing")
        for allocation in (parent, attempt):
            positive_int(allocation["max_turns"], "max_turns")
            if allocation["max_output_tokens"] > allocation["token_limit"]:
                raise HarnessValidationError("output token limit exceeds total token limit", code="task_plan_budget_exceeded")
        if any(attempt[name] > parent[name] for name in attempt):
            raise HarnessValidationError("attempt allocation exceeds parent envelope", code="task_plan_budget_exceeded")
        object.__setattr__(self, "parent_allocation", frozen_mapping(parent, "parent_allocation"))
        object.__setattr__(self, "attempt_allocation", frozen_mapping(attempt, "attempt_allocation"))
        object.__setattr__(self, "reservation_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version, "owner_scope": self.owner_scope,
            "reservation_key": self.reservation_key, "ledger_version": self.ledger_version,
            "parent_allocation": thaw_mapping(self.parent_allocation),
            "attempt_allocation": thaw_mapping(self.attempt_allocation),
            "token_limit": self.attempt_allocation["token_limit"],
            "time_limit_ms": self.attempt_allocation["time_limit_ms"],
            "tool_call_limit": self.attempt_allocation["max_tool_calls"],
        }
        if "cost_limit" in self.attempt_allocation:
            value["cost_limit"] = self.attempt_allocation["cost_limit"]
            value["cost_unit"] = "USD_MICRO"
        for name, amount in self.attempt_allocation.items():
            if amount > 0:
                dimension = USAGE_FIELDS[name]
                value[dimension] = amount
                value[f"remaining_{dimension}"] = self.parent_allocation[name]
        if include_checksum:
            value["reservation_checksum"] = self.reservation_checksum
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BudgetReservation:
        base = {"schema_version", "owner_scope", "reservation_key", "parent_allocation", "attempt_allocation", "ledger_version"}
        projected = {"token_limit", "time_limit_ms", "tool_call_limit", "cost_limit", "cost_unit"}
        projected.update(USAGE_FIELDS.values())
        projected.update(f"remaining_{name}" for name in USAGE_FIELDS.values())
        payload = exact_keys(value, required=frozenset(base | {"reservation_checksum", "token_limit", "time_limit_ms", "tool_call_limit"}), optional=frozenset(projected - {"token_limit", "time_limit_ms", "tool_call_limit"}), model=cls.__name__)
        supplied = checksum(payload["reservation_checksum"], "reservation_checksum")
        result = cls(**{name: payload[name] for name in base})
        actual = canonical_payload_checksum({key: item for key, item in payload.items() if key != "reservation_checksum"})
        if supplied != actual or supplied != result.reservation_checksum or payload != result.to_dict():
            raise HarnessValidationError("budget reservation projection or checksum mismatch", code="task_plan_budget_checksum_mismatch")
        return result
