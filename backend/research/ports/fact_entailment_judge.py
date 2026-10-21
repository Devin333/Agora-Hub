"""Research-owned contract for judging one answer claim against target texts.

A judge receives one atomic answer claim and the texts it must be compared with
(reference claims, or passages the answering model actually saw) and labels
each target as entailing, contradicting, or not addressing the claim. Judges
are only consulted for pairs deterministic fact comparison left pending, and a
single judge never decides alone: consensus across independent providers is a
separate, deterministic step.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
)

JudgedDimension = Literal["numeric_value", "unit", "dataset", "split", "condition"]


class EntailmentLabel(StrEnum):
    ENTAILED = "entailed"
    CONTRADICTED = "contradicted"
    NEUTRAL = "neutral"


class JudgeTargetKind(StrEnum):
    REFERENCE = "reference"
    EVIDENCE = "evidence"


class JudgedDimensionOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_STATED = "not_stated"


class FactJudgeError(RuntimeError):
    """Raised by an implementation when one judgment request cannot be served."""

    retryable: bool = False


class FactJudgeIdentity(ProtocolModel):
    provider: NonEmptyStr
    model_id: NonEmptyStr
    model_revision: NonEmptyStr | None
    prompt_version: ImmutableVersionStr

    @property
    def key(self) -> str:
        revision = self.model_revision or "unpinned"
        return f"{self.provider}/{self.model_id}@{revision}+{self.prompt_version}"


class EntailmentTarget(ProtocolModel):
    target_id: NonEmptyStr
    text: NonEmptyStr
    supporting_text: tuple[NonEmptyStr, ...] = ()


class FactEntailmentRequest(ProtocolModel):
    request_id: NonEmptyStr
    question: NonEmptyStr
    claim_id: NonEmptyStr
    claim_text: NonEmptyStr
    target_kind: JudgeTargetKind
    targets: tuple[EntailmentTarget, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_targets(self) -> "FactEntailmentRequest":
        ids = [item.target_id for item in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("entailment target ids must be unique")
        return self


class DimensionJudgment(ProtocolModel):
    dimension: JudgedDimension
    outcome: JudgedDimensionOutcome
    name: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _condition_named(self) -> "DimensionJudgment":
        if (self.dimension == "condition") != (self.name is not None):
            raise ValueError("condition judgments, and only they, name the condition")
        return self


class TargetJudgment(ProtocolModel):
    target_id: NonEmptyStr
    label: EntailmentLabel
    dimensions: tuple[DimensionJudgment, ...] = ()
    rationale: NonEmptyStr | None = None


class FactEntailmentJudgment(ProtocolModel):
    request_id: NonEmptyStr
    judge: FactJudgeIdentity
    judgments: tuple[TargetJudgment, ...]

    @model_validator(mode="after")
    def _unique_targets(self) -> "FactEntailmentJudgment":
        ids = [item.target_id for item in self.judgments]
        if len(ids) != len(set(ids)):
            raise ValueError("a judgment may label each target only once")
        return self


@runtime_checkable
class FactEntailmentJudgePort(Protocol):
    @property
    def identity(self) -> FactJudgeIdentity:
        ...

    async def judge(self, request: FactEntailmentRequest) -> FactEntailmentJudgment:
        ...


__all__ = [
    "DimensionJudgment",
    "EntailmentLabel",
    "EntailmentTarget",
    "FactEntailmentJudgePort",
    "FactEntailmentJudgment",
    "FactEntailmentRequest",
    "FactJudgeError",
    "FactJudgeIdentity",
    "JudgeTargetKind",
    "JudgedDimension",
    "JudgedDimensionOutcome",
    "TargetJudgment",
]
