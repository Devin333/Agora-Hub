"""LLM adapter for answer claim decomposition and fact extraction.

The adapter only proposes drafts. Every draft is re-verified deterministically
by ``backend.research.rag.evaluation`` before it may influence a score, so this
module owns prompts, transport, and strict output parsing, and nothing else.
Caching and provider selection are composition concerns: inject an already
configured (and, for reproducibility, cached) ``LLMClient``.
"""
from __future__ import annotations

import json
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.research.ports.answer_fact_extraction import (
    AnswerFactExtractionError,
    ClaimDecompositionRequest,
    ClaimDecompositionResult,
    ClaimDraft,
    DraftCondition,
    ExtractorIdentity,
    FactDraft,
    FactExtractionRequest,
    FactExtractionResult,
)
from framework.llm.models import LLMClient

from infrastructure.research.errors import ResearchAdapterError
from infrastructure.research.structured_llm_call import (
    StructuredCallOutputFailure,
    StructuredCallProviderFailure,
    call_structured,
)

FACT_EXTRACTION_PROMPT_VERSION = "answer-fact-extraction/v1"

_T = TypeVar("_T", bound=BaseModel)


class FactExtractorError(ResearchAdapterError, AnswerFactExtractionError):
    error_code = "research_fact_extractor_error"


class FactExtractorProviderError(FactExtractorError):
    error_code = "research_fact_extractor_provider_error"


class FactExtractorOutputError(FactExtractorError):
    error_code = "research_fact_extractor_output_error"


# Provider-facing payloads. Every property is required (nullable instead of
# optional) so the schema is valid for strict provider structured output.

class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _ConditionPayload(_Payload):
    name: str
    value: str


class _FactPayload(_Payload):
    statement: str
    value_text: str | None
    numeric_value: float | None
    unit: Literal["fraction", "percent", "percentage_point", "relative_percent", "scalar"] | None
    polarity: bool | None
    categorical_value: str | None
    object_id: str | None
    dataset_id: str | None
    split_id: str | None
    statistic: str | None
    conditions: list[_ConditionPayload]
    modality: Literal["text", "table", "figure", "equation"]
    aligned_target_id: str | None


class _FactsPayload(_Payload):
    facts: list[_FactPayload]


class _ClaimPayload(_Payload):
    claim_text: str
    grounding_span: str
    occurrence: int = Field(ge=1)


class _ClaimsPayload(_Payload):
    claims: list[_ClaimPayload]


_FACT_SYSTEM_PROMPT = """You extract atomic, checkable facts from one text for an evaluation pipeline.
Your output is verified by strict rules; anything that does not follow them is discarded.

Rules:
1. statement: copy one span of SOURCE_TEXT exactly, character for character. Never paraphrase.
2. One fact per assertion. Do not merge two numbers or two claims into one fact.
3. Numbers: value_text is the exact substring of statement holding the number (for example "95.3%"
   or "0.953"); numeric_value is that number as written (95.3, not 0.953); unit is one of
   fraction, percent, percentage_point, relative_percent, scalar. "points"/"pp" gains are
   percentage_point; "relative"/"x% better than" gains are relative_percent.
   A numeric fact must name its statistic (for example "accuracy", "f1", "latency_ms").
4. Scope: fill object_id, dataset_id, split_id, statistic and conditions only when the text or
   the QUESTION states them. Reuse an id from VOCABULARY whenever it denotes the same thing.
   Conditions are named experimental settings such as seed, shots, beam, backbone.
5. polarity: true/false only for explicit yes/no assertions (for example "they do not use
   pretraining" -> polarity false with statistic "uses_pretraining").
6. categorical_value: a named answer that is not a number (a method, dataset, or model name)
   and appears in the statement.
7. aligned_target_id: only when ROLE is candidate and the fact answers exactly one of
   ALIGNMENT_TARGETS; otherwise null.
8. Never add knowledge that is not in SOURCE_TEXT or QUESTION. If the text asserts no checkable
   fact, return an empty list.
Return JSON matching the schema."""

_DECOMPOSITION_SYSTEM_PROMPT = """You split one answer into atomic factual claims for an evaluation pipeline.
Your output is verified by strict rules; anything that does not follow them is discarded.

Rules:
1. Each claim states exactly one checkable assertion and is self-contained: resolve pronouns
   and add the subject from the QUESTION when the answer omits it.
2. grounding_span: copy the exact span of ANSWER that makes this assertion, character for
   character; occurrence says which occurrence of that span is meant (1 for the first).
3. Never write a number in claim_text that the ANSWER or QUESTION does not write.
4. Do not include citation markers such as [1] in claim_text.
5. Skip hedges, restated questions, and refusals; they are not claims.
6. A bare "Yes"/"No" answer becomes one full claim, grounded on that word.
Return JSON matching the schema."""


class LLMAnswerFactExtractor:
    """Implements ClaimDecompositionPort and FactExtractionPort over an LLMClient."""

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
        self._identity = ExtractorIdentity(
            provider=provider,
            model_id=model_id,
            model_revision=model_revision,
            prompt_version=FACT_EXTRACTION_PROMPT_VERSION,
        )
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @property
    def identity(self) -> ExtractorIdentity:
        return self._identity

    async def extract(self, request: FactExtractionRequest) -> FactExtractionResult:
        user = json.dumps({
            "ROLE": request.role.value,
            "QUESTION": request.question,
            "SOURCE_TEXT": request.source_text,
            "VOCABULARY": request.vocabulary.model_dump(mode="json"),
            "ALIGNMENT_TARGETS": [item.model_dump(mode="json") for item in request.alignment_targets],
        }, ensure_ascii=False)
        payload = await self._call(
            system=_FACT_SYSTEM_PROMPT,
            user=user,
            schema=_FactsPayload,
            task="fact_extraction",
            request_id=request.request_id,
        )
        drafts = tuple(_fact_draft(item) for item in payload.facts)
        return FactExtractionResult(request_id=request.request_id, extractor=self._identity, drafts=drafts)

    async def decompose(self, request: ClaimDecompositionRequest) -> ClaimDecompositionResult:
        user = json.dumps({"QUESTION": request.question, "ANSWER": request.answer_text}, ensure_ascii=False)
        payload = await self._call(
            system=_DECOMPOSITION_SYSTEM_PROMPT,
            user=user,
            schema=_ClaimsPayload,
            task="claim_decomposition",
            request_id=request.request_id,
        )
        claims = tuple(_claim_draft(item) for item in payload.claims)
        return ClaimDecompositionResult(request_id=request.request_id, extractor=self._identity, claims=claims)

    async def _call(
        self,
        *,
        system: str,
        user: str,
        schema: type[_T],
        task: str,
        request_id: str,
    ) -> _T:
        try:
            return await call_structured(
                self._client,
                system=system,
                user=user,
                schema=schema,
                schema_name=f"research_{task}",
                max_output_tokens=self._max_output_tokens,
                timeout_seconds=self._timeout_seconds,
                metadata={
                    "component": "research_answer_fact_extractor",
                    "task": task,
                    "request_id": request_id,
                    "prompt_version": FACT_EXTRACTION_PROMPT_VERSION,
                },
            )
        except StructuredCallProviderFailure as exc:
            raise FactExtractorProviderError(str(exc), retryable=exc.retryable) from exc
        except StructuredCallOutputFailure as exc:
            raise FactExtractorOutputError(str(exc)) from exc


def _blank_to_none(value: str | None) -> str | None:
    return None if value is None or not value.strip() else value


def _fact_draft(item: _FactPayload) -> FactDraft:
    try:
        return FactDraft(
            statement=item.statement,
            value_text=_blank_to_none(item.value_text),
            numeric_value=item.numeric_value,
            unit=item.unit,
            polarity=item.polarity,
            categorical_value=_blank_to_none(item.categorical_value),
            object_id=_blank_to_none(item.object_id),
            dataset_id=_blank_to_none(item.dataset_id),
            split_id=_blank_to_none(item.split_id),
            statistic=_blank_to_none(item.statistic),
            conditions=tuple(
                DraftCondition(name=cond.name, value=cond.value)
                for cond in item.conditions
                if cond.name.strip() and cond.value.strip()
            ),
            modality=item.modality,
            aligned_target_id=_blank_to_none(item.aligned_target_id),
        )
    except ValidationError as exc:
        raise FactExtractorOutputError("fact draft violates the extraction contract") from exc


def _claim_draft(item: _ClaimPayload) -> ClaimDraft:
    try:
        return ClaimDraft(
            claim_text=item.claim_text,
            grounding_span=item.grounding_span,
            occurrence=item.occurrence,
        )
    except ValidationError as exc:
        raise FactExtractorOutputError("claim draft violates the decomposition contract") from exc


__all__ = [
    "FACT_EXTRACTION_PROMPT_VERSION",
    "FactExtractorError",
    "FactExtractorOutputError",
    "FactExtractorProviderError",
    "LLMAnswerFactExtractor",
]
