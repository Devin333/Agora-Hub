"""Unified reasoning-effort tiers and their mapping onto each provider's request fields.

Callers pick one tier per model; each provider dialect turns that tier into the
parameters its API understands. A tier a model does not offer is rejected when
the connection is configured, so the mapping here only translates.
"""
from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import Any


class ReasoningEffort(str, Enum):
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
DEFAULT_REASONING_EFFORT = ReasoningEffort.MEDIUM


class ReasoningParameterStyle(str, Enum):
    """How a provider accepts the reasoning tier on the wire."""

    NONE = "none"
    OPENAI_REASONING_EFFORT = "openai_reasoning_effort"
    OPENROUTER_REASONING = "openrouter_reasoning"
    DEEPSEEK_THINKING = "deepseek_thinking"
    DASHSCOPE_THINKING = "dashscope_thinking"
    DASHSCOPE_THINKING_TOGGLE = "dashscope_thinking_toggle"
    ANTHROPIC_EFFORT = "anthropic_effort"

    @classmethod
    def parse(cls, value: Any) -> ReasoningParameterStyle:
        if isinstance(value, ReasoningParameterStyle):
            return value
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown reasoning parameter style {value!r}; expected one of: {allowed}") from exc


def default_effort(allowed: Iterable[ReasoningEffort]) -> ReasoningEffort:
    """Medium when offered, otherwise the closest stronger tier, otherwise the strongest offered."""
    offered = set(allowed)
    if not offered:
        raise ValueError("a model must offer at least one reasoning effort")
    start = EFFORT_ORDER.index(DEFAULT_REASONING_EFFORT)
    for effort in EFFORT_ORDER[start:]:
        if effort in offered:
            return effort
    return max(offered, key=EFFORT_ORDER.index)


def ordered_efforts(efforts: Iterable[ReasoningEffort | str]) -> tuple[ReasoningEffort, ...]:
    parsed = {ReasoningEffort.parse(item) for item in efforts}
    return tuple(effort for effort in EFFORT_ORDER if effort in parsed)


# DeepSeek accepts low/high/max; weaker tiers fold into low and xhigh into high.
_DEEPSEEK_EFFORT = {
    ReasoningEffort.MINIMAL: "low",
    ReasoningEffort.LOW: "low",
    ReasoningEffort.MEDIUM: "high",
    ReasoningEffort.HIGH: "high",
    ReasoningEffort.XHIGH: "high",
    ReasoningEffort.MAX: "max",
}
# DashScope's Qwen/GLM/Kimi families take a thinking token budget; MAX leaves the
# model's own ceiling in place. Models without budget support only toggle thinking.
_DASHSCOPE_THINKING_BUDGET = {
    ReasoningEffort.MINIMAL: 1024,
    ReasoningEffort.LOW: 2048,
    ReasoningEffort.MEDIUM: 8192,
    ReasoningEffort.HIGH: 16384,
    ReasoningEffort.XHIGH: 32768,
}


def reasoning_request_fields(
    style: ReasoningParameterStyle,
    effort: ReasoningEffort | None,
) -> dict[str, Any]:
    """Top-level Chat Completions fields that express ``effort``; empty when nothing is sent.

    Anthropic's thinking modes depend on the model, so the Messages client maps
    ``ANTHROPIC_EFFORT`` itself.
    """
    if effort is None or style is ReasoningParameterStyle.NONE:
        return {}
    if style is ReasoningParameterStyle.OPENAI_REASONING_EFFORT:
        return {"reasoning_effort": "none" if effort is ReasoningEffort.OFF else effort.value}
    if style is ReasoningParameterStyle.OPENROUTER_REASONING:
        return {"reasoning": {"effort": "none" if effort is ReasoningEffort.OFF else effort.value}}
    if style is ReasoningParameterStyle.DEEPSEEK_THINKING:
        if effort is ReasoningEffort.OFF:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled"}, "reasoning_effort": _DEEPSEEK_EFFORT[effort]}
    if style is ReasoningParameterStyle.DASHSCOPE_THINKING:
        if effort is ReasoningEffort.OFF:
            return {"enable_thinking": False}
        fields: dict[str, Any] = {"enable_thinking": True}
        budget = _DASHSCOPE_THINKING_BUDGET.get(effort)
        if budget is not None:
            fields["thinking_budget"] = budget
        return fields
    if style is ReasoningParameterStyle.DASHSCOPE_THINKING_TOGGLE:
        return {"enable_thinking": effort is not ReasoningEffort.OFF}
    if style is ReasoningParameterStyle.ANTHROPIC_EFFORT:
        raise ValueError("anthropic_effort is mapped by the Anthropic Messages client")
    raise ValueError(f"unsupported reasoning parameter style: {style}")


def reasoning_is_active(style: ReasoningParameterStyle, effort: ReasoningEffort | None) -> bool:
    """Whether the request will run with model reasoning switched on."""
    return effort is not None and effort is not ReasoningEffort.OFF and style is not ReasoningParameterStyle.NONE


__all__ = [
    "DEFAULT_REASONING_EFFORT",
    "EFFORT_ORDER",
    "ReasoningEffort",
    "ReasoningParameterStyle",
    "default_effort",
    "ordered_efforts",
    "reasoning_is_active",
    "reasoning_request_fields",
]
