"""LLM adapter that judges one answer claim against reference or context texts.

A single judge is never authoritative: its labels only count when independent
judges from other providers agree (``protocol_judge_consensus``). The adapter
owns the prompt and strict output parsing; caching and provider selection are
composition concerns.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.research.ports.fact_entailment_judge import (
    DimensionJudgment,
    EntailmentLabel,
    FactEntailmentJudgment,
    FactEntailmentRequest,
    FactJudgeError,
    FactJudgeIdentity,
    JudgedDimensionOutcome,
    JudgeTargetKind,
    TargetJudgment,
)
from framework.llm.models import LLMClient

from infrastructure.research.errors import ResearchAdapterError
from infrastructure.research.structured_llm_call import (
    StructuredCallOutputFailure,
    StructuredCallProviderFailure,
    call_structured,
)

FACT_JUDGE_PROMPT_VERSION = "fact-entailment-judge/v1"


class LLMFactJudgeError(ResearchAdapterError, FactJudgeError):
    error_code = "research_fact_judge_error"


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _DimensionPayload(_Payload):
    dimension: Literal["numeric_value", "unit", "dataset", "split", "condition"]
    name: str | None
    outcome: Literal["match", "mismatch", "not_stated"]


class _TargetPayload(_Payload):
    target_id: str
    label: Literal["entailed", "contradicted", "neutral"]
    dimensions: list[_DimensionPayload]
    rationale: str


class _JudgmentPayload(_Payload):
    judgments: list[_TargetPayload]


_SYSTEM_PROMPT = """You judge one atomic CLAIM against each TARGET text for an evaluation pipeline.

For every target, label exactly one of:
- entailed: the target states what the claim asserts, including its numbers, units,
  datasets, splits, and experimental conditions (rounding the target's value to the
  claim's stated precision is acceptable).
- contradicted: the target addresses the same thing (same object, statistic, and setting)
  and states something incompatible with the claim.
- neutral: the target does not address what the claim asserts, or does not settle it.

Rules:
1. Judge only from TARGET text and its SUPPORTING_TEXT. Never use outside knowledge.
2. A different dataset, split, or condition is "contradicted" only when the target is clearly
   about the same result; otherwise it is "neutral".
3. Percentage points, relative gains, and absolute values are different quantities.
4. When TARGET_KIND is reference and the label is entailed or contradicted, report each
   dimension the CLAIM states: numeric_value, unit, dataset, split, and one "condition"
   entry per named condition (name = the condition name). outcome is match, mismatch, or
   not_stated (the target does not state it). Use null name for non-condition dimensions.
   Otherwise return an empty dimensions list.
5. Return one judgment for every target id, and only for those ids. Keep rationale short.
Return JSON matching the schema."""


class LLMFactEntailmentJudge:
    """Implements FactEntailmentJudgePort over an LLMClient."""

    def __init__(
        self,
        client: LLMClient,
        *,
        provider: str,
        model_id: str,
        model_revision: str | None = None,
        max_output_tokens: int = 2_000,
        timeout_seconds: float = 120.0,
    ) -> None:
        if max_output_tokens <= 0 or timeout_seconds <= 0:
            raise ValueError("token and timeout limits must be positive")
        self._client = client
        self._identity = FactJudgeIdentity(
            provider=provider,
            model_id=model_id,
            model_revision=model_revision,
            prompt_version=FACT_JUDGE_PROMPT_VERSION,
        )
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def identity(self) -> FactJudgeIdentity:
        return self._identity

    async def judge(self, request: FactEntailmentRequest) -> FactEntailmentJudgment:
        user = json.dumps({
            "QUESTION": request.question,
            "CLAIM": request.claim_text,
            "TARGET_KIND": request.target_kind.value,
            "TARGETS": [
                {"target_id": item.target_id, "text": item.text, "SUPPORTING_TEXT": list(item.supporting_text)}
                for item in request.targets
            ],
        }, ensure_ascii=False)
        try:
            payload = await call_structured(
                self._client,
                system=_SYSTEM_PROMPT,
                user=user,
                schema=_JudgmentPayload,
                schema_name="research_fact_entailment_judgment",
                max_output_tokens=self._max_output_tokens,
                timeout_seconds=self._timeout_seconds,
                metadata={
                    "component": "research_fact_entailment_judge",
                    "request_id": request.request_id,
                    "prompt_version": FACT_JUDGE_PROMPT_VERSION,
                },
            )
        except StructuredCallProviderFailure as exc:
            raise LLMFactJudgeError(str(exc), retryable=exc.retryable) from exc
        except StructuredCallOutputFailure as exc:
            raise LLMFactJudgeError(str(exc)) from exc
        try:
            judgments = tuple(
                _target_judgment(item, request.target_kind) for item in payload.judgments
            )
            return FactEntailmentJudgment(
                request_id=request.request_id,
                judge=self._identity,
                judgments=judgments,
            )
        except ValidationError as exc:
            raise LLMFactJudgeError("judgment violates the entailment contract") from exc


def _target_judgment(item: _TargetPayload, kind: JudgeTargetKind) -> TargetJudgment:
    label = EntailmentLabel(item.label)
    keep = kind is JudgeTargetKind.REFERENCE and label is not EntailmentLabel.NEUTRAL
    dimensions = tuple(
        DimensionJudgment(
            dimension=dim.dimension,
            outcome=JudgedDimensionOutcome(dim.outcome),
            name=(dim.name.strip() or None) if dim.dimension == "condition" and dim.name else None,
        )
        for dim in item.dimensions
    ) if keep else ()
    return TargetJudgment(
        target_id=item.target_id,
        label=label,
        dimensions=dimensions,
        rationale=item.rationale.strip() or None,
    )


__all__ = [
    "FACT_JUDGE_PROMPT_VERSION",
    "LLMFactEntailmentJudge",
    "LLMFactJudgeError",
]
