"""Reference-free grounding evaluation of ReportBench cross-source reports.

ReportBench supplies no gold answers, so only grounding is measured: every
atomic claim of a report is checked against the quotes the report cites. The
context is therefore ``cited_quotes``, not the full sources; a claim the full
page supports but no quote does is reported as ungrounded *by its citations*,
which is exactly what a reader of the report can verify. Report citations are
not bound to positions in the text, so claim-level citation metrics are not
applicable rather than counted as zero.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence

from backend.research.benchmark.reportbench import CrossSourceResearchResult
from backend.research.ports.answer_fact_extraction import (
    AnswerFactExtractionError,
    ClaimDecompositionPort,
    ClaimDecompositionRequest,
    FactExtractionPort,
    FactExtractionRequest,
    FactExtractionResult,
    FactSourceRole,
)
from backend.research.ports.fact_entailment_judge import FactEntailmentJudgePort
from backend.research.rag.evaluation.protocol_answer_fact_report import (
    AnswerFactRunReport,
    GroundedCaseResult,
    summarize_grounded_run,
)
from backend.research.rag.evaluation.protocol_answer_scoring import score_grounded_answer
from backend.research.rag.evaluation.protocol_claim_decomposition import apply_claim_decomposition
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_fact_extraction import (
    build_candidate_projections,
    build_evidence_projections,
)
from backend.research.rag.evaluation.protocol_judge_calibration import JudgeCalibrationReport
from backend.research.rag.evaluation.protocol_judge_consensus import (
    ConsensusReport,
    build_entailment_requests,
    resolve_by_consensus,
)
from framework.harness.rag.models import EvidenceCandidate, GroundedAnswerCandidate, RAGContextPack

CITED_QUOTES = "cited_quotes"


def citation_context(result: CrossSourceResearchResult) -> RAGContextPack:
    """One context passage per quoted citation, keyed to its declared source."""

    sources = {item.source_id: item for item in result.sources}
    evidence: list[EvidenceCandidate] = []
    for index, citation in enumerate(result.citations, start=1):
        quote = (citation.quote or "").strip()
        source = sources.get(citation.source_id)
        if not quote or source is None:
            continue
        locator = citation.locator or source.locator or source.url
        evidence.append(EvidenceCandidate(
            evidence_id=f"{citation.source_id}#quote-{index}",
            title=source.title,
            summary=quote,
            source_ref=source.url,
            span_refs=(f"citation-{index}",),
            evidence_type="cited_quote",
            lineage=(source.source_id,),
            metadata={
                "paper_id": source.source_id,
                # A quoted web source has no immutable revision; the quote itself
                # is the frozen content that is checked.
                "document_version": "cited-quote/" + hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16],
                "source_sha256": hashlib.sha256(f"{source.url}\n{quote}".encode("utf-8")).hexdigest(),
                "source_locator": locator,
            },
        ))
    return RAGContextPack(
        pack_id=f"{result.task_id}#cited-quotes",
        query=result.question,
        accepted_evidence=tuple(evidence),
        metadata={"context_kind": CITED_QUOTES},
    )


class ReportBenchGroundingEvaluator:
    def __init__(
        self,
        *,
        decomposer: ClaimDecompositionPort,
        extractor: FactExtractionPort,
        judges: Sequence[FactEntailmentJudgePort] = (),
        max_concurrency: int = 4,
    ) -> None:
        if len(judges) == 1:
            raise ValueError("judge consensus needs at least two independent judges, or none")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._decomposer = decomposer
        self._extractor = extractor
        self._judges = tuple(judges)
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _extract(self, request: FactExtractionRequest) -> FactExtractionResult | str:
        async with self._semaphore:
            try:
                return await self._extractor.extract(request)
            except AnswerFactExtractionError as exc:
                return f"extraction failed: {exc}"

    async def _extract_all(self, requests: dict[str, FactExtractionRequest]):
        keys = list(requests)
        answers = await asyncio.gather(*(self._extract(requests[key]) for key in keys))
        results = {key: item for key, item in zip(keys, answers, strict=True) if not isinstance(item, str)}
        failures = {key: item for key, item in zip(keys, answers, strict=True) if isinstance(item, str)}
        return results, failures

    async def evaluate(
        self,
        result: CrossSourceResearchResult,
        *,
        calibration: JudgeCalibrationReport | None,
    ) -> GroundedCaseResult:
        if result.status != "succeeded" or not (result.report or "").strip():
            return GroundedCaseResult(
                case_id=result.task_id, question=result.question, status="runtime_error",
                reason=result.execution.error_code or "report generation failed or produced no report",
            )
        context = citation_context(result)
        if not context.accepted_evidence:
            return GroundedCaseResult(
                case_id=result.task_id, question=result.question, status="not_scorable",
                reason="the report cites no quote that can be checked",
            )
        report = result.report or ""
        candidate = GroundedAnswerCandidate(
            answer_id=f"{result.task_id}#report", question=result.question,
            answer_text=report, cited_evidence_ids=(), claims=(), metadata={},
        )
        decomposition = decomposition_failure = None
        try:
            async with self._semaphore:
                decomposed = await self._decomposer.decompose(ClaimDecompositionRequest(
                    request_id=f"{result.task_id}#report",
                    question=result.question,
                    answer_text=report,
                    answer_text_digest=content_digest(report),
                ))
            candidate, decomposition = apply_claim_decomposition(candidate, decomposed, citation_index={})
        except AnswerFactExtractionError as exc:
            decomposition_failure = f"decomposition failed: {exc}"
        evidence_results, evidence_failures = await self._extract_all({
            item.evidence_id: FactExtractionRequest(
                request_id=f"{result.task_id}#evidence#{item.evidence_id}",
                role=FactSourceRole.EVIDENCE,
                question=result.question,
                source_text=item.summary,
                source_text_digest=content_digest(item.summary),
            )
            for item in context.accepted_evidence
        })
        evidence_facts, evidence_report = build_evidence_projections(
            context, evidence_results, failures=evidence_failures,
        )
        candidate_results, candidate_failures = await self._extract_all({
            claim.claim_id: FactExtractionRequest(
                request_id=f"{result.task_id}#claim#{claim.claim_id}",
                role=FactSourceRole.CANDIDATE,
                question=result.question,
                source_text=claim.text,
                source_text_digest=content_digest(claim.text),
            )
            for claim in candidate.claims
        })
        candidate_facts, candidate_report = build_candidate_projections(
            candidate.claims, candidate_results, alignment_targets=(), failures=candidate_failures,
        )
        common = dict(
            case_id=result.task_id,
            candidate=candidate,
            actual_context=context,
            context_kind=CITED_QUOTES,
            candidate_facts=candidate_facts,
            evidence_facts=evidence_facts,
            claim_citations_available=False,
        )
        score = score_grounded_answer(**common)
        judges_ready = bool(self._judges) and calibration is not None and calibration.passed
        consensus: ConsensusReport | None = None
        pending = score.pending_judgments()
        if judges_ready and pending:
            requests = build_entailment_requests(
                pending, candidate=candidate, reference=None, actual_context=context,
            )
            resolutions, consensus = await resolve_by_consensus(
                requests, self._judges, max_concurrency=self._max_concurrency,
            )
            if resolutions:
                score = score_grounded_answer(**common, resolutions=resolutions)
        return GroundedCaseResult(
            case_id=result.task_id,
            question=result.question,
            status="scored",
            decomposition=decomposition,
            decomposition_failure=decomposition_failure,
            evidence_report=evidence_report,
            candidate_report=candidate_report,
            consensus=consensus,
            judges_applied=judges_ready,
            score=score,
        )

    async def evaluate_run(
        self,
        results: Sequence[CrossSourceResearchResult],
        *,
        run_id: str,
        dataset_id: str,
        dataset_version: str,
        calibration: JudgeCalibrationReport | None,
    ) -> tuple[tuple[GroundedCaseResult, ...], AnswerFactRunReport]:
        cases = tuple([await self.evaluate(item, calibration=calibration) for item in results])
        report = summarize_grounded_run(
            cases,
            run_id=run_id,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            dataset_digest=content_digest([item.model_dump(mode="json") for item in results]),
            calibration=calibration,
        )
        return cases, report


__all__ = [
    "CITED_QUOTES",
    "ReportBenchGroundingEvaluator",
    "citation_context",
]
