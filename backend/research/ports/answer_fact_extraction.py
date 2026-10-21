"""Research-owned contracts for model-assisted answer fact extraction.

Two capabilities sit behind these ports:

* claim decomposition: split one generated answer into atomic, decontextualized
  claims, each grounded on a verbatim span of the answer;
* fact extraction: project one text (a reference claim, an answer claim, or an
  evidence passage) into structured fact drafts.

Implementations are untrusted proposers. Every draft is re-verified
deterministically against the exact source text before it may become a scoring
projection, so the contracts here carry raw drafts plus the extractor identity
needed to reproduce them, and nothing else.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)

DraftUnit = Literal["fraction", "percent", "percentage_point", "relative_percent", "scalar"]
DraftModality = Literal["text", "table", "figure", "equation"]


class AnswerFactExtractionError(RuntimeError):
    """Raised by an implementation when one extraction request cannot be served.

    Callers record the failure against that request's target and continue; a
    failed target is counted as extraction loss, never silently dropped.
    """

    retryable: bool = False


class FactSourceRole(StrEnum):
    REFERENCE = "reference"
    CANDIDATE = "candidate"
    EVIDENCE = "evidence"


class ExtractorIdentity(ProtocolModel):
    """Reproducible identity of one extraction configuration."""

    provider: NonEmptyStr
    model_id: NonEmptyStr
    model_revision: NonEmptyStr | None
    prompt_version: ImmutableVersionStr

    @property
    def rule_id(self) -> str:
        return f"llm-fact-extraction/{self.provider}/{self.model_id}"

    @property
    def rule_version(self) -> str:
        revision = self.model_revision or "unpinned"
        return f"{self.prompt_version}+{revision}"


class ScopeVocabulary(ProtocolModel):
    """Canonical ids already used by the reference side of one case.

    Supplying the same vocabulary to every extraction of a case lets object,
    dataset, split, statistic, and condition ids line up across reference,
    answer, and evidence facts instead of drifting between synonyms.
    """

    object_ids: tuple[NonEmptyStr, ...] = ()
    dataset_ids: tuple[NonEmptyStr, ...] = ()
    split_ids: tuple[NonEmptyStr, ...] = ()
    statistics: tuple[NonEmptyStr, ...] = ()
    condition_names: tuple[NonEmptyStr, ...] = ()


class AlignmentTarget(ProtocolModel):
    """A reference claim an answer claim may explicitly address."""

    target_id: NonEmptyStr
    text: NonEmptyStr


class DraftCondition(ProtocolModel):
    name: NonEmptyStr
    value: NonEmptyStr


class FactDraft(ProtocolModel):
    """One proposed structured fact; untrusted until verified."""

    statement: NonEmptyStr = Field(
        description="Verbatim span of the source text that asserts this fact.",
    )
    value_text: NonEmptyStr | None = Field(
        default=None,
        description="Verbatim substring of statement that carries the number, e.g. '95.3%'.",
    )
    numeric_value: float | None = None
    unit: DraftUnit | None = None
    polarity: bool | None = None
    categorical_value: NonEmptyStr | None = None
    object_id: NonEmptyStr | None = None
    dataset_id: NonEmptyStr | None = None
    split_id: NonEmptyStr | None = None
    statistic: NonEmptyStr | None = None
    conditions: tuple[DraftCondition, ...] = ()
    modality: DraftModality = "text"
    aligned_target_id: NonEmptyStr | None = None


class FactExtractionRequest(ProtocolModel):
    request_id: NonEmptyStr
    role: FactSourceRole
    question: NonEmptyStr
    source_text: NonEmptyStr
    source_text_digest: Sha256Digest
    vocabulary: ScopeVocabulary = ScopeVocabulary()
    alignment_targets: tuple[AlignmentTarget, ...] = ()

    @model_validator(mode="after")
    def _verify_source(self) -> "FactExtractionRequest":
        if content_digest(self.source_text) != self.source_text_digest:
            raise ValueError("source_text_digest does not match source_text")
        if self.alignment_targets and self.role is not FactSourceRole.CANDIDATE:
            raise ValueError("only candidate extraction may align to reference targets")
        ids = [item.target_id for item in self.alignment_targets]
        if len(ids) != len(set(ids)):
            raise ValueError("alignment target ids must be unique")
        return self


class FactExtractionResult(ProtocolModel):
    request_id: NonEmptyStr
    extractor: ExtractorIdentity
    drafts: tuple[FactDraft, ...]


class ClaimDraft(ProtocolModel):
    """One proposed atomic claim of an answer; untrusted until verified."""

    claim_text: NonEmptyStr = Field(
        description="Self-contained restatement of exactly one factual assertion.",
    )
    grounding_span: NonEmptyStr = Field(
        description="Verbatim span of the answer that makes this assertion.",
    )
    occurrence: int = Field(
        default=1,
        ge=1,
        strict=True,
        description="Which occurrence of grounding_span in the answer is meant (1-based).",
    )


class ClaimDecompositionRequest(ProtocolModel):
    request_id: NonEmptyStr
    question: NonEmptyStr
    answer_text: NonEmptyStr
    answer_text_digest: Sha256Digest

    @model_validator(mode="after")
    def _verify_answer(self) -> "ClaimDecompositionRequest":
        if content_digest(self.answer_text) != self.answer_text_digest:
            raise ValueError("answer_text_digest does not match answer_text")
        return self


class ClaimDecompositionResult(ProtocolModel):
    request_id: NonEmptyStr
    extractor: ExtractorIdentity
    claims: tuple[ClaimDraft, ...]


@runtime_checkable
class ClaimDecompositionPort(Protocol):
    @property
    def identity(self) -> ExtractorIdentity:
        ...

    async def decompose(self, request: ClaimDecompositionRequest) -> ClaimDecompositionResult:
        ...


@runtime_checkable
class FactExtractionPort(Protocol):
    @property
    def identity(self) -> ExtractorIdentity:
        ...

    async def extract(self, request: FactExtractionRequest) -> FactExtractionResult:
        ...


__all__ = [
    "AlignmentTarget",
    "AnswerFactExtractionError",
    "ClaimDecompositionPort",
    "ClaimDecompositionRequest",
    "ClaimDecompositionResult",
    "ClaimDraft",
    "DraftCondition",
    "DraftModality",
    "DraftUnit",
    "ExtractorIdentity",
    "FactDraft",
    "FactExtractionPort",
    "FactExtractionRequest",
    "FactExtractionResult",
    "FactSourceRole",
    "ScopeVocabulary",
]
