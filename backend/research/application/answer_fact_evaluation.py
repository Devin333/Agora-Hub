"""Application orchestration for automated answer fact evaluation.

For one answer the pipeline is:

1. decompose the answer into verified atomic claims (citations from its own [n]);
2. extract and verify facts of every reference alternative, every claim, and
   every context passage the model actually saw;
3. score deterministically against each alternative;
4. if the judge panel passed calibration, resolve only the pending pairs by
   multi-provider consensus and score again;
5. keep the best alternative (benchmarks accept any annotator's answer).

Every model call goes through an injected port; every loss (failed extraction,
rejected draft, disagreement) is counted in the case report instead of silently
shrinking a denominator.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from backend.research.ports.answer_fact_extraction import (
    AnswerFactExtractionError,
    ClaimDecompositionPort,
    ClaimDecompositionRequest,
    FactExtractionPort,
    FactExtractionRequest,
    FactExtractionResult,
    FactSourceRole,
    ScopeVocabulary,
)
from backend.research.ports.fact_entailment_judge import FactEntailmentJudgePort
from backend.research.rag.evaluation.protocol_answer_scoring import (
    AbstentionRubric,
    AnswerScoreResult,
    score_answer,
)
from backend.research.rag.evaluation.protocol_claim_decomposition import (
    ClaimDecompositionReport,
    apply_claim_decomposition,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    AUTOMATED_REVIEW_POLICY,
    ReferenceLabel,
    ReviewAcceptancePolicy,
)
from backend.research.rag.evaluation.protocol_fact_extraction import (
    FactVerificationReport,
    alignment_targets_from_reference,
    build_candidate_projections,
    build_evidence_projections,
    build_reference_projections,
    vocabulary_from_reference,
)
from backend.research.rag.evaluation.protocol_fact_projections import (
    EvidenceFactProjection,
    ReferenceFactProjection,
)
from backend.research.rag.evaluation.protocol_answer_fact_report import (
    AlternativeOutcome,
    AnswerFactCaseResult,
    select_best_alternative,
)
from backend.research.rag.evaluation.protocol_judge_calibration import (
    JudgeCalibrationGate,
    JudgeCalibrationReport,
    calibrate_judges,
)
from backend.research.rag.evaluation.protocol_fact_mutation import (
    FactVariant,
    MutationReport,
    generate_variants,
)
from backend.research.rag.evaluation.protocol_judge_consensus import (
    ConsensusReport,
    build_entailment_requests,
    resolve_by_consensus,
)
from framework.harness.rag.models import GroundedAnswerCandidate, RAGContextPack

_T = TypeVar("_T")


@dataclass(frozen=True)
class ReferenceAlternative:
    """One acceptable gold answer (for example one annotator's answer)."""

    annotation_id: str
    reference: ReferenceLabel
    fixed_reference_facts: tuple[ReferenceFactProjection, ...] = ()


@dataclass(frozen=True)
class AnswerFactCase:
    case_id: str
    question: str
    alternatives: tuple[ReferenceAlternative, ...]


@dataclass(frozen=True)
class GeneratedCandidate:
    """The answer under evaluation and exactly the context it was given."""

    candidate: GroundedAnswerCandidate | None
    context: RAGContextPack | None
    citation_index: Mapping[int, str] = field(default_factory=dict)
    runtime_error: str | None = None


@dataclass(frozen=True)
class PreparedAlternative:
    annotation_id: str
    reference: ReferenceLabel
    reference_facts: tuple[ReferenceFactProjection, ...]
    report: FactVerificationReport | None


@dataclass(frozen=True)
class PreparedCase:
    case: AnswerFactCase
    alternatives: tuple[PreparedAlternative, ...]

    @property
    def vocabulary(self) -> ScopeVocabulary:
        facts = [fact for item in self.alternatives for fact in item.reference_facts]
        return vocabulary_from_reference(facts)


@dataclass(frozen=True)
class AnswerFactEvaluationConfig:
    max_concurrency: int = 4
    review_policy: ReviewAcceptancePolicy = AUTOMATED_REVIEW_POLICY
    rubric: AbstentionRubric = AbstentionRubric()
    calibration_gate: JudgeCalibrationGate = JudgeCalibrationGate()

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


class AnswerFactEvaluator:
    """Ports in, auditable per-case results out; holds no I/O of its own."""

    def __init__(
        self,
        *,
        decomposer: ClaimDecompositionPort,
        extractor: FactExtractionPort,
        judges: Sequence[FactEntailmentJudgePort] = (),
        config: AnswerFactEvaluationConfig = AnswerFactEvaluationConfig(),
    ) -> None:
        if len(judges) == 1:
            raise ValueError("judge consensus needs at least two independent judges, or none")
        self._decomposer = decomposer
        self._extractor = extractor
        self._judges = tuple(judges)
        self._config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    async def _bounded(self, call: Callable[[], Awaitable[_T]]) -> _T:
        async with self._semaphore:
            return await call()

    async def _extract(self, request: FactExtractionRequest) -> FactExtractionResult | str:
        try:
            return await self._bounded(lambda: self._extractor.extract(request))
        except AnswerFactExtractionError as exc:
            return f"extraction failed: {exc}"

    async def _extract_all(
        self, requests: Mapping[str, FactExtractionRequest],
    ) -> tuple[dict[str, FactExtractionResult], dict[str, str]]:
        keys = list(requests)
        answers = await asyncio.gather(*(self._extract(requests[key]) for key in keys))
        results: dict[str, FactExtractionResult] = {}
        failures: dict[str, str] = {}
        for key, answer in zip(keys, answers, strict=True):
            if isinstance(answer, str):
                failures[key] = answer
            else:
                results[key] = answer
        return results, failures

    # -- references ---------------------------------------------------------

    async def prepare_cases(self, cases: Sequence[AnswerFactCase]) -> tuple[PreparedCase, ...]:
        """Extract and verify the facts of every answerable reference alternative."""

        return tuple(await asyncio.gather(*(self._prepare_case(case) for case in cases)))

    async def _prepare_case(self, case: AnswerFactCase) -> PreparedCase:
        vocabulary = vocabulary_from_reference(
            [fact for item in case.alternatives for fact in item.fixed_reference_facts]
        )
        prepared: list[PreparedAlternative] = []
        for alternative in case.alternatives:
            reference = alternative.reference
            if reference.answerability != "answerable":
                prepared.append(PreparedAlternative(alternative.annotation_id, reference, (), None))
                continue
            fixed_ids = {item.reference_claim_id for item in alternative.fixed_reference_facts}
            requests: dict[str, FactExtractionRequest] = {}
            for claim in reference.reference_claims:
                if claim.claim_id in fixed_ids:
                    continue
                excerpts = [
                    anchor.source_excerpt for anchor in reference.evidence_anchors
                    if claim.claim_id in anchor.claim_ids
                ]
                source_text = "\n\n".join((claim.claim_text, *excerpts))
                requests[claim.claim_id] = FactExtractionRequest(
                    request_id=f"{case.case_id}#{alternative.annotation_id}#{claim.claim_id}",
                    role=FactSourceRole.REFERENCE,
                    question=case.question,
                    source_text=source_text,
                    source_text_digest=content_digest(source_text),
                    vocabulary=vocabulary,
                )
            results, failures = await self._extract_all(requests)
            facts, report = build_reference_projections(
                reference, results, fixed=alternative.fixed_reference_facts, failures=failures,
            )
            prepared.append(PreparedAlternative(alternative.annotation_id, reference, facts, report))
        return PreparedCase(case=case, alternatives=tuple(prepared))

    # -- calibration --------------------------------------------------------

    async def calibrate(self, prepared: Sequence[PreparedCase]) -> JudgeCalibrationReport | None:
        """Measure the judge panel on errors injected into verified reference facts."""

        if not self._judges:
            return None
        variants: list[FactVariant] = []
        references: dict[str, ReferenceLabel] = {}
        questions: dict[str, str] = {}
        by_kind: dict[str, int] = {}
        skipped = []
        for case in prepared:
            for alternative in case.alternatives:
                if not alternative.reference_facts:
                    continue
                key = f"{case.case.case_id}#{alternative.annotation_id}"
                generated, report = generate_variants(key, alternative.reference, alternative.reference_facts)
                variants.extend(generated)
                references[key] = alternative.reference
                questions[key] = case.case.question
                for kind, count in report.by_kind.items():
                    by_kind[kind] = by_kind.get(kind, 0) + count
                skipped.extend(report.skipped)
        mutation = MutationReport(variants=len(variants), by_kind=by_kind, skipped=tuple(skipped))
        return await calibrate_judges(
            variants,
            mutation,
            references=references,
            questions=questions,
            judges=self._judges,
            gate=self._config.calibration_gate,
            max_concurrency=self._config.max_concurrency,
        )

    # -- one answer ---------------------------------------------------------

    async def evaluate_case(
        self,
        prepared: PreparedCase,
        generated: GeneratedCandidate,
        *,
        calibration: JudgeCalibrationReport | None,
    ) -> AnswerFactCaseResult:
        case = prepared.case
        if generated.candidate is None or generated.runtime_error is not None:
            reason = generated.runtime_error or "no candidate answer was produced"
            outcomes = tuple(
                AlternativeOutcome(
                    annotation_id=item.annotation_id,
                    score=score_answer(
                        case_id=case.case_id,
                        reference=item.reference,
                        candidate=None,
                        actual_context=generated.context,
                        runtime_error=reason,
                        rubric=self._config.rubric,
                        review_policy=self._config.review_policy,
                    ),
                    reference_report=item.report,
                )
                for item in prepared.alternatives
            )
            return AnswerFactCaseResult(
                case_id=case.case_id, question=case.question, status="runtime_error", reason=reason,
                outcomes=outcomes, selected_annotation_id=select_best_alternative(outcomes).annotation_id,
            )

        candidate, decomposition, decomposition_failure = await self._decompose(case, generated)
        evidence_facts, evidence_report = await self._evidence(prepared, generated.context)
        judges_ready = bool(self._judges) and calibration is not None and calibration.passed
        outcomes = tuple(await asyncio.gather(*(
            self._score_alternative(
                case, alternative, candidate, generated.context, evidence_facts, judges_ready=judges_ready,
            )
            for alternative in prepared.alternatives
        )))
        return AnswerFactCaseResult(
            case_id=case.case_id,
            question=case.question,
            status="scored",
            decomposition=decomposition,
            decomposition_failure=decomposition_failure,
            evidence_report=evidence_report,
            judges_applied=judges_ready,
            outcomes=outcomes,
            selected_annotation_id=select_best_alternative(outcomes).annotation_id,
        )

    async def _decompose(
        self, case: AnswerFactCase, generated: GeneratedCandidate,
    ) -> tuple[GroundedAnswerCandidate, ClaimDecompositionReport | None, str | None]:
        candidate = generated.candidate
        assert candidate is not None
        text = candidate.answer_text.strip()
        rubric = self._config.rubric
        if not text or rubric.matches_complete_refusal(text):
            # A complete refusal asserts nothing to decompose.
            return replace(candidate, claims=(), abstained=bool(text)), None, None
        request = ClaimDecompositionRequest(
            request_id=f"{case.case_id}#answer",
            question=case.question,
            answer_text=candidate.answer_text,
            answer_text_digest=content_digest(candidate.answer_text),
        )
        try:
            result = await self._bounded(lambda: self._decomposer.decompose(request))
        except AnswerFactExtractionError as exc:
            return candidate, None, f"decomposition failed: {exc}"
        decomposed, report = apply_claim_decomposition(
            candidate, result, citation_index=generated.citation_index,
        )
        return decomposed, report, None

    async def _evidence(
        self, prepared: PreparedCase, context: RAGContextPack | None,
    ) -> tuple[tuple[EvidenceFactProjection, ...], FactVerificationReport | None]:
        if context is None or not context.accepted_evidence:
            return (), None
        vocabulary = prepared.vocabulary
        requests = {
            item.evidence_id: FactExtractionRequest(
                request_id=f"{prepared.case.case_id}#evidence#{item.evidence_id}",
                role=FactSourceRole.EVIDENCE,
                question=prepared.case.question,
                source_text=item.summary,
                source_text_digest=content_digest(item.summary),
                vocabulary=vocabulary,
            )
            for item in context.accepted_evidence
        }
        results, failures = await self._extract_all(requests)
        facts, report = build_evidence_projections(context, results, failures=failures)
        return facts, report

    async def _score_alternative(
        self,
        case: AnswerFactCase,
        alternative: PreparedAlternative,
        candidate: GroundedAnswerCandidate,
        context: RAGContextPack | None,
        evidence_facts: tuple[EvidenceFactProjection, ...],
        *,
        judges_ready: bool,
    ) -> AlternativeOutcome:
        reference = alternative.reference
        candidate_facts: tuple = ()
        candidate_report: FactVerificationReport | None = None
        if reference.answerability == "answerable" and candidate.claims:
            vocabulary = vocabulary_from_reference(alternative.reference_facts)
            targets = alignment_targets_from_reference(reference)
            requests = {
                claim.claim_id: FactExtractionRequest(
                    request_id=f"{case.case_id}#{alternative.annotation_id}#{claim.claim_id}",
                    role=FactSourceRole.CANDIDATE,
                    question=case.question,
                    source_text=claim.text,
                    source_text_digest=content_digest(claim.text),
                    vocabulary=vocabulary,
                    alignment_targets=targets,
                )
                for claim in candidate.claims
            }
            results, failures = await self._extract_all(requests)
            candidate_facts, candidate_report = build_candidate_projections(
                candidate.claims, results,
                alignment_targets=targets,
                reference_facts=alternative.reference_facts,
                failures=failures,
            )
        common: dict[str, Any] = dict(
            case_id=case.case_id,
            reference=reference,
            candidate=candidate,
            actual_context=context,
            reference_facts=alternative.reference_facts,
            candidate_facts=candidate_facts,
            evidence_facts=evidence_facts if reference.answerability == "answerable" else (),
            rubric=self._config.rubric,
            review_policy=self._config.review_policy,
        )
        score: AnswerScoreResult = score_answer(**common)
        consensus: ConsensusReport | None = None
        pending = score.pending_judgments()
        if judges_ready and pending:
            requests_for_judges = build_entailment_requests(
                pending, candidate=candidate, reference=reference, actual_context=context,
            )
            resolutions, consensus = await resolve_by_consensus(
                requests_for_judges, self._judges, max_concurrency=self._config.max_concurrency,
            )
            if resolutions:
                score = score_answer(**common, resolutions=resolutions)
        return AlternativeOutcome(
            annotation_id=alternative.annotation_id,
            score=score,
            reference_report=alternative.report,
            candidate_report=candidate_report,
            consensus=consensus,
        )


__all__ = [
    "AnswerFactCase",
    "AnswerFactEvaluationConfig",
    "AnswerFactEvaluator",
    "GeneratedCandidate",
    "PreparedAlternative",
    "PreparedCase",
    "ReferenceAlternative",
]
