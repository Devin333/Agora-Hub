"""Provider-neutral reasoning tiers and how each provider family sends them.

A connection picks one tier for its model; the wire style says which request
fields carry it. A tier the model does not offer is refused when the
connection is configured, so the mapping here only translates.

Wire fields (checked 2026-10-30):
  OpenAI Chat Completions: ``reasoning_effort`` (none/minimal/low/medium/high/xhigh);
    reasoning models take ``max_completion_tokens`` and reject ``temperature``.
  OpenRouter: ``reasoning: {"effort": ...}``; ``"none"`` disables reasoning.
    https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
  DeepSeek: ``thinking: {"type": "enabled"|"disabled"}`` plus
    ``reasoning_effort`` low/high/max (minimal folds into low, medium/xhigh into high).
    https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/
  DashScope (Bailian): ``enable_thinking`` true/false.
    https://help.aliyun.com/zh/model-studio/qwq
  Anthropic Messages: ``output_config: {"effort": ...}``; ``thinking: {"type": "disabled"}``
    switches thinking off on the models that allow it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReasoningEffort(StrEnum):
    OFF = "off"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @classmethod
    def parse(cls, value: Any) -> ReasoningEffort:
        if isinstance(value, ReasoningEffort):
            return value
        if not isinstance(value, str):
            raise ValueError("reasoning effort must be a string")
        try:
            return cls(value.strip().casefold())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown reasoning effort {value!r}; expected one of: {allowed}") from exc


EFFORT_ORDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)


class ReasoningWire(StrEnum):
    """How one provider family accepts the reasoning tier."""

    NONE = "none"
    OPENAI_REASONING_EFFORT = "openai_reasoning_effort"
    OPENROUTER_REASONING = "openrouter_reasoning"
    DEEPSEEK_THINKING = "deepseek_thinking"
    DASHSCOPE_ENABLE_THINKING = "dashscope_enable_thinking"
    ANTHROPIC_EFFORT = "anthropic_effort"

    @classmethod
    def parse(cls, value: Any) -> ReasoningWire:
        if isinstance(value, ReasoningWire):
            return value
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown reasoning wire {value!r}; expected one of: {allowed}") from exc

    def offered_efforts(self) -> tuple[ReasoningEffort, ...]:
        """Every tier this wire style can express at all."""
        if self is ReasoningWire.NONE:
            return ()
        if self is ReasoningWire.DASHSCOPE_ENABLE_THINKING:
            return (ReasoningEffort.OFF, ReasoningEffort.MEDIUM)
        if self is ReasoningWire.ANTHROPIC_EFFORT:
            return tuple(item for item in EFFORT_ORDER if item is not ReasoningEffort.MINIMAL)
        return EFFORT_ORDER


# Visible-answer budgets in this codebase are small (hundreds to a few thousand
# tokens). Providers count reasoning tokens against the same output limit, so
# an active tier adds room for it instead of starving the answer.
_REASONING_OUTPUT_ALLOWANCE: dict[ReasoningEffort, int] = {
    ReasoningEffort.OFF: 0,
    ReasoningEffort.MINIMAL: 1024,
    ReasoningEffort.LOW: 2048,
    ReasoningEffort.MEDIUM: 8192,
    ReasoningEffort.HIGH: 16384,
    ReasoningEffort.XHIGH: 32768,
    ReasoningEffort.MAX: 65536,
}

_DEEPSEEK_EFFORT = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "high",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "high",
    ReasoningEffort.MAX: "max",
}


@dataclass(frozen=True, slots=True)
class ReasoningSetting:
    """One connection's reasoning choice; ``effort=None`` sends the provider default."""

    wire: ReasoningWire = ReasoningWire.NONE
    effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "wire", ReasoningWire.parse(self.wire))
        if self.effort is not None:
            effort = ReasoningEffort.parse(self.effort)
            if effort not in self.wire.offered_efforts():
                raise ValueError(f"{self.wire.value} cannot express reasoning effort {effort.value}")
            object.__setattr__(self, "effort", effort)

    @property
    def active(self) -> bool:
        """Whether the call may spend output tokens on reasoning."""
        if self.wire is ReasoningWire.NONE:
            return False
        return self.effort is not ReasoningEffort.OFF

    def output_allowance(self) -> int:
        """Extra output tokens reserved for reasoning on top of the visible answer."""
        if not self.active:
            return 0
        # The provider default tier is treated as medium for budgeting.
        return _REASONING_OUTPUT_ALLOWANCE[self.effort or ReasoningEffort.MEDIUM]

    def output_tokens(self, requested: int | None, *, ceiling: int | None) -> int | None:
        """The output limit to send for a caller that asked for ``requested`` visible tokens."""
        if requested is None:
            return None
        total = requested + self.output_allowance()
        return min(total, ceiling) if ceiling is not None else total

    def chat_fields(self) -> dict[str, Any]:
        """Chat Completions fields that express this setting; empty when nothing is sent."""
        effort = self.effort
        if effort is None or self.wire is ReasoningWire.NONE:
            return {}
        off = effort is ReasoningEffort.OFF
        if self.wire is ReasoningWire.OPENAI_REASONING_EFFORT:
            return {"reasoning_effort": "none" if off else effort.value}
        if self.wire is ReasoningWire.OPENROUTER_REASONING:
            return {"reasoning": {"effort": "none" if off else effort.value}}
        if self.wire is ReasoningWire.DEEPSEEK_THINKING:
            if off:
                return {"thinking": {"type": "disabled"}}
            return {"thinking": {"type": "enabled"}, "reasoning_effort": _DEEPSEEK_EFFORT[effort]}
        if self.wire is ReasoningWire.DASHSCOPE_ENABLE_THINKING:
            return {"enable_thinking": not off}
        raise ValueError(f"{self.wire.value} is not a Chat Completions reasoning wire")

    def anthropic_fields(self) -> dict[str, Any]:
        """Messages API fields that express this setting; empty when nothing is sent."""
        if self.wire is not ReasoningWire.ANTHROPIC_EFFORT:
            raise ValueError(f"{self.wire.value} is not the Anthropic reasoning wire")
        effort = self.effort
        if effort is None:
            return {}
        if effort is ReasoningEffort.OFF:
            return {"thinking": {"type": "disabled"}}
        return {"output_config": {"effort": effort.value}}

    def apply_to_chat_payload(self, payload: dict[str, Any]) -> None:
        payload.update(self.chat_fields())
        if self.wire is ReasoningWire.OPENAI_REASONING_EFFORT:
            # OpenAI reasoning models accept only max_completion_tokens, and
            # refuse a non-default temperature while they reason.
            if "max_tokens" in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            if self.active:
                payload.pop("temperature", None)


def ordered_efforts(efforts: Iterable[ReasoningEffort | str]) -> tuple[ReasoningEffort, ...]:
    parsed = {ReasoningEffort.parse(item) for item in efforts}
    return tuple(effort for effort in EFFORT_ORDER if effort in parsed)


__all__ = [
    "EFFORT_ORDER",
    "ReasoningEffort",
    "ReasoningSetting",
    "ReasoningWire",
    "ordered_efforts",
]
