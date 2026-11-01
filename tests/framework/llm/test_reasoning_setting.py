from __future__ import annotations

import pytest

from framework.llm.models.reasoning import ReasoningEffort, ReasoningSetting, ReasoningWire


def test_an_unset_tier_is_budgeted_as_the_provider_default() -> None:
    # DeepSeek thinks at high when no effort is sent; the budget must hold that.
    setting = ReasoningSetting(ReasoningWire.DEEPSEEK_THINKING, None, provider_default=ReasoningEffort.HIGH)

    assert setting.effective_effort is ReasoningEffort.HIGH
    assert setting.output_allowance() == 16384
    assert setting.output_tokens(900, ceiling=None) == 900 + 16384
    # The default is only budgeted, never sent: the provider applies it itself.
    assert setting.chat_fields() == {}


def test_an_unknown_default_is_budgeted_as_medium() -> None:
    setting = ReasoningSetting(ReasoningWire.OPENAI_REASONING_EFFORT, None)

    assert setting.effective_effort is None
    assert setting.output_allowance() == 8192


def test_a_chosen_tier_wins_over_the_provider_default() -> None:
    setting = ReasoningSetting(
        ReasoningWire.DEEPSEEK_THINKING, ReasoningEffort.LOW, provider_default=ReasoningEffort.HIGH
    )

    assert setting.effective_effort is ReasoningEffort.LOW
    assert setting.output_allowance() == 2048
    assert setting.chat_fields() == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}


def test_a_provider_that_does_not_think_by_default_reserves_nothing() -> None:
    setting = ReasoningSetting(ReasoningWire.DEEPSEEK_THINKING, None, provider_default=ReasoningEffort.OFF)

    assert setting.active is False
    assert setting.output_allowance() == 0


def test_the_provider_default_must_be_a_tier_the_wire_can_express() -> None:
    with pytest.raises(ValueError):
        ReasoningSetting(ReasoningWire.ANTHROPIC_EFFORT, None, provider_default=ReasoningEffort.MINIMAL)
