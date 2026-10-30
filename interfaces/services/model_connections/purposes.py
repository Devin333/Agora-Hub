"""What a model call is for; each account assigns one connection per purpose."""

from __future__ import annotations

from enum import StrEnum


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


def purpose_label(value: object) -> str:
    """Chinese label of a purpose, or the raw value when it is not one."""
    try:
        return ModelPurpose.parse(value).label
    except ValueError:
        return str(value or "")


__all__ = ["ModelPurpose", "purpose_label"]
