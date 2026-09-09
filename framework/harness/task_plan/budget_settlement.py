"""Checksum-bound settlement facts; provenance is verified by Harness adapters."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.budget_allocation import USAGE_FIELDS
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum, checksum, exact_keys, frozen_mapping,
    identifier, non_negative_int, thaw_mapping,
)


BUDGET_SETTLEMENT_SCHEMA = "agora.task-plan-budget-settlement/v1"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
SETTLEMENT_REASONS = frozenset({"CANCELLED", "RECLAIMED", "RECOVERED", BUDGET_EXCEEDED})


@dataclass(frozen=True, slots=True)
class BudgetSettlementReceipt:
    reservation_key: str
    instance_checksum: str
    source_receipt_checksum: str
    reason_code: str
    usage: Mapping[str, int]
    termination_confirmed: bool
    schema_version: str = BUDGET_SETTLEMENT_SCHEMA
    receipt_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        identifier(self.reservation_key, "reservation_key")
        checksum(self.instance_checksum, "instance_checksum")
        checksum(self.source_receipt_checksum, "source_receipt_checksum")
        if self.schema_version != BUDGET_SETTLEMENT_SCHEMA or self.reason_code not in SETTLEMENT_REASONS:
            raise HarnessValidationError("unsupported budget settlement contract", code="task_plan_budget_snapshot_invalid")
        if not isinstance(self.termination_confirmed, bool):
            raise HarnessValidationError("termination confirmation must be boolean", code="task_plan_budget_snapshot_invalid")
        usage = frozen_mapping(self.usage, "usage")
        if len(usage) > 14:
            raise HarnessValidationError("settlement usage exceeds its bound", code="task_plan_budget_snapshot_invalid")
        for name, amount in usage.items():
            if name not in USAGE_FIELDS and name not in USAGE_FIELDS.values():
                raise HarnessValidationError("unknown settlement usage dimension", code="task_plan_result_usage_invalid")
            non_negative_int(amount, name)
        object.__setattr__(self, "usage", usage)
        object.__setattr__(self, "receipt_checksum", canonical_payload_checksum(self.to_dict(include_checksum=False)))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version, "reservation_key": self.reservation_key,
            "instance_checksum": self.instance_checksum, "source_receipt_checksum": self.source_receipt_checksum,
            "reason_code": self.reason_code, "usage": thaw_mapping(self.usage),
            "termination_confirmed": self.termination_confirmed,
        }
        if include_checksum:
            payload["receipt_checksum"] = self.receipt_checksum
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BudgetSettlementReceipt:
        payload = exact_keys(value, required=frozenset({
            "schema_version", "reservation_key", "instance_checksum", "source_receipt_checksum",
            "reason_code", "usage", "termination_confirmed", "receipt_checksum",
        }), model=cls.__name__)
        supplied = checksum(payload.pop("receipt_checksum"), "receipt_checksum")
        receipt = cls(**payload)
        if receipt.receipt_checksum != supplied:
            raise HarnessValidationError("budget settlement receipt checksum mismatch", code="task_plan_budget_checksum_mismatch")
        return receipt


@runtime_checkable
class BudgetSettlementReadPort(Protocol):
    """Read verified terminal and meter evidence from its Harness owner.

    Implementations must authenticate the source receipt and its attempt before
    returning measured usage. Candidate/worker metrics are not this authority.
    Offline ledger parsing validates recorded facts without invoking this port.
    """

    def read_budget_settlement(
        self, *, source_receipt_checksum: str, reservation_key: str,
    ) -> BudgetSettlementReceipt | None: ...
