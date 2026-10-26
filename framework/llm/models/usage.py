from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class _TokenTotal(int):
    def __new__(cls, value: int) -> _TokenTotal:
        return int.__new__(cls, value)

    def __call__(self) -> int:
        return int(self)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    estimated_cost_usd: float | None = None

    @property
    def total_tokens(self) -> _TokenTotal:
        return _TokenTotal(self.input_tokens + self.output_tokens + self.reasoning_tokens)

    def to_dict(self) -> dict[str, int | float | None]:
        payload: dict[str, int | float | None] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": int(self.total_tokens),
        }
        if self.estimated_cost_usd is not None:
            payload["estimated_cost_usd"] = self.estimated_cost_usd
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> TokenUsage:
        payload = dict(payload or {})
        return cls(
            input_tokens=int(payload.get("input_tokens", 0) or 0),
            output_tokens=int(payload.get("output_tokens", 0) or 0),
            reasoning_tokens=int(payload.get("reasoning_tokens", 0) or 0),
            cached_input_tokens=int(payload.get("cached_input_tokens", 0) or 0),
            estimated_cost_usd=(
                float(payload["estimated_cost_usd"])
                if payload.get("estimated_cost_usd") is not None
                else None
            ),
        )

    @classmethod
    def sum(cls, usages: Iterable[TokenUsage]) -> TokenUsage:
        """Total of several calls; the cost is known only when every call reports one."""
        items = list(usages)
        costs = [usage.estimated_cost_usd for usage in items]
        return cls(
            input_tokens=sum(usage.input_tokens for usage in items),
            output_tokens=sum(usage.output_tokens for usage in items),
            reasoning_tokens=sum(usage.reasoning_tokens for usage in items),
            cached_input_tokens=sum(usage.cached_input_tokens for usage in items),
            estimated_cost_usd=(
                sum(cost for cost in costs if cost is not None)
                if items and all(cost is not None for cost in costs)
                else None
            ),
        )

    @classmethod
    def from_any(cls, value: Any) -> TokenUsage:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        data = value.to_dict() if hasattr(value, "to_dict") else value
        if not isinstance(data, dict):
            return cls()
        return cls.from_dict(data)

