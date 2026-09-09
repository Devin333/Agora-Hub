"""Canonical integer budget units shared by plans, ledgers and receipts."""
from __future__ import annotations

from typing import Any, Mapping
from types import MappingProxyType
import re

from framework.events.canonical import checksum_for as canonical_payload_checksum
from framework.harness.control_plane.errors import HarnessValidationError


TASK_BUDGET_FIELDS = ("max_turns", "max_tool_calls", "max_memory_ops", "max_output_tokens")
EXECUTION_BUDGET_FIELDS = ("token_limit", "time_limit_ms", "cost_limit")
USAGE_FIELDS = {
    "max_turns": "turns", "max_tool_calls": "tool_calls",
    "max_memory_ops": "memory_ops", "max_output_tokens": "output_tokens",
    "token_limit": "tokens", "time_limit_ms": "time_ms", "cost_limit": "cost_microusd",
}


def budget_allocation(value: Mapping[str, Any]) -> dict[str, int]:
    """Normalize allocations or counters; zero is legal for any counter."""
    data = exact_keys(value, required=frozenset(TASK_BUDGET_FIELDS), optional=frozenset(EXECUTION_BUDGET_FIELDS), model="TaskBudgetAllocation")
    extended = set(data) & set(EXECUTION_BUDGET_FIELDS)
    if extended and not {"token_limit", "time_limit_ms"}.issubset(extended):
        raise HarnessValidationError("execution budget requires both token and time limits", code="task_plan_budget_policy_missing")
    return {name: non_negative_int(data[name], name) for name in (*TASK_BUDGET_FIELDS, *EXECUTION_BUDGET_FIELDS) if name in data}


def budget_counter_keys(allocation: Mapping[str, int]) -> frozenset[str]:
    return frozenset(f"{prefix}_{name}" for prefix in ("reserved", "consumed", "released") for name in allocation)


def exact_keys(value: Mapping[str, Any], *, required: frozenset[str], optional: frozenset[str] = frozenset(), model: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise HarnessValidationError(f"{model} must be an object", code="task_plan_budget_snapshot_invalid")
    if not required.issubset(value) or set(value) - required - optional:
        raise HarnessValidationError(f"{model} fields do not match the contract", code="task_plan_budget_snapshot_invalid")
    return dict(value)


def non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessValidationError(f"{field_name} must be a non-negative integer", code="task_plan_budget_snapshot_invalid")
    return value


def positive_int(value: Any, field_name: str) -> int:
    value = non_negative_int(value, field_name)
    if value == 0:
        raise HarnessValidationError(f"{field_name} must be positive", code="task_plan_budget_snapshot_invalid")
    return value


def identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or len(value) > 512 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", value) is None:
        raise HarnessValidationError(f"{field_name} must be a stable identifier", code="task_plan_budget_snapshot_invalid")
    return value


def checksum(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise HarnessValidationError(f"{field_name} must be a sha256 checksum", code="task_plan_budget_snapshot_invalid")
    return value


def frozen_mapping(value: Mapping[str, int], field_name: str) -> Mapping[str, int]:
    return MappingProxyType(dict(value))


def thaw_mapping(value: Mapping[str, int]) -> dict[str, int]:
    return dict(value)
