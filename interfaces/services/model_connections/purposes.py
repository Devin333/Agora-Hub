"""What a model call is for; each account assigns one connection per purpose."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from framework.llm.models.reasoning import ReasoningSetting


class ModelPurpose(StrEnum):
    ANSWER = "answer"
    EVIDENCE_SUMMARY = "evidence_summary"
    SECTION_NAVIGATION = "section_navigation"
    # Structured research candidates: analysis stages, task plans, agent
    # children, reader repair and guided-intent interpretation.
    CANDIDATE_GENERATION = "candidate_generation"

    @property
    def label(self) -> str:
        return _LABELS[self]

    @classmethod
    def parse(cls, value: object) -> ModelPurpose:
        try:
            return cls(str(value or "").strip())
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown model purpose {value!r}; expected one of: {allowed}") from exc


_LABELS: dict[ModelPurpose, str] = {
    ModelPurpose.ANSWER: "回答",
    ModelPurpose.EVIDENCE_SUMMARY: "证据摘要",
    ModelPurpose.SECTION_NAVIGATION: "章节导航",
    ModelPurpose.CANDIDATE_GENERATION: "候选生成",
}


@dataclass(frozen=True, slots=True)
class PurposeRequirement:
    """What a connection must offer before it may serve a purpose.

    ``input_tokens`` and ``output_tokens`` are the largest prompt and visible
    answer one call of this purpose sends; a connection's window must hold
    both plus the output it reserves for reasoning, so every call fits
    without narrowing the request at call time.
    """

    input_tokens: int
    output_tokens: int
    structured_output: bool = False

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def required_window(self, reasoning: ReasoningSetting, *, max_output_tokens: int | None) -> int:
        output = reasoning.output_tokens(self.output_tokens, ceiling=max_output_tokens)
        return self.input_tokens + (output if output is not None else self.output_tokens)

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "structuredOutput": self.structured_output,
        }


def purpose_label(value: object) -> str:
    """Chinese label of a purpose, or the raw value when it is not one."""
    try:
        return ModelPurpose.parse(value).label
    except ValueError:
        return str(value or "")


__all__ = ["ModelPurpose", "PurposeRequirement", "purpose_label"]
