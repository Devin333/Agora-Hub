"""End-to-end QASPER answer evaluation through the production gated answer path.

Retrieval reads chunk vectors from the frozen offline cache; only rewritten query
texts are embedded live (see ``QueryVectorResolver``). The session is assembled the
way the desktop Research ask path is: chunk store only, no reranker, no field index,
generation enabled with two attempts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.paper_answer_model import PaperAnswerModelPort
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.ports.qasper_query_embedding import QueryEmbeddingPort, QueryVectorStorePort
from backend.research.rag.adapters import PaperAnswerWorker
from backend.research.rag.evaluation.paper_answer_eval import outcome_for_session_status
from backend.research.rag.evaluation.qasper_answer_judge import (
    JUDGE_PROMPT_VERSION,
    JudgeCall,
    JudgeRequest,
    judge_answer,
)
from backend.research.rag.evaluation.qasper_answer_metrics import (
    max_answer_f1,
    project_prediction,
    references_for_question,
)
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase, QasperAnswerSummary
from backend.research.rag.evaluation.qasper_answer_sampling import sample_questions
from backend.research.rag.evaluation.qasper_mapping import FrozenQasperCase, freeze_qasper_dataset
from backend.research.rag.evaluation.qasper_query_vectors import QueryVectorResolver
from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureChunkStore
from backend.research.rag.evaluation.qasper_scoring import evidence_group_coverage
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator, render_answer_context
from backend.research.rag.retrieval.policies import RetrievalPolicy
from framework.rag.evaluation.answer_metrics import AnswerOutcome
from framework.rag.generation import partition_citation_markers

PROTOCOL = "qasper_end_to_end_answer_v1"


@dataclass(frozen=True)
class QasperAnswerEvaluationConfig:
    output_directory: str
    seed: str = "qasper-answer-v1"
    per_type: int = 75
    max_cases: int | None = None
    run_id: str = "qasper-answer-eval"

    def __post_init__(self) -> None:
        if not self.output_directory.strip():
            raise ValueError("output_directory is required")
        if self.per_type < 1:
            raise ValueError("per_type must be positive")
        if self.max_cases is not None and self.max_cases < 1:
            raise ValueError("max_cases must be positive")


class QasperAnswerEvaluationService:
    def __init__(
        self,
        assets: QasperAssetService,
        artifacts: QasperArtifactStore,
        *,
        query_embedder: QueryEmbeddingPort,
        query_vector_store: QueryVectorStorePort,
        answer_model: PaperAnswerModelPort,
        judge_call: JudgeCall | None,
        retrieval_policy: RetrievalPolicy,
        config: QasperAnswerEvaluationConfig,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._assets = assets
        self._artifacts = artifacts
        self._embedder = query_embedder
        self._vector_store = query_vector_store
        self._answer_model = answer_model
        self._judge_call = judge_call
        self._policy = retrieval_policy
        self._config = config
        self._clock = clock

    def run(self) -> dict[str, Any]:
        assets = self._assets.read_assets()
        frozen = freeze_qasper_dataset(assets.raw_dataset, assets.documents, assets.chunks)
        frozen.verify_digest()
        frozen_by_id = {case.case_id: case for case in frozen.cases}
        sample = sample_questions(assets.raw_dataset, seed=self._config.seed, per_type=self._config.per_type)
        questions = sample.questions[: self._config.max_cases] if self._config.max_cases else sample.questions
        qas_by_id = _qas_by_case(assets.raw_dataset)

        cache = self._assets.embedding_cache()
        identity = self._assets.cache_identity()
        resolver = QueryVectorResolver(
            cache, embedder=self._embedder, store=self._vector_store, model_id=identity.model_id,
        )
        run_id = self._config.run_id
        # Session retrieval filters on run_id, so the corpus is bound to this evaluation run.
        chunks = [chunk.model_copy(update={"metadata": {**chunk.metadata, "run_id": run_id}}) for chunk in assets.chunks]
        store = CachedFixtureChunkStore(chunks, cache, query_vector=resolver)
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        session = PaperRAGSession(
            store,
            retrieval_policy=self._policy,
            answer_worker=PaperAnswerWorker(AnswerGenerator(self._answer_model)),
            generation_policy={"enabled": True, "max_attempts": 2},
        )
        ask_use_case = AskPaperUseCase()

        records: list[QasperAnswerCase] = []
        try:
            for index, question in enumerate(questions):
                frozen_case = frozen_by_id.get(question.case_id)
                if frozen_case is None:
                    raise ValueError(f"sampled case {question.case_id} is absent from the frozen mapping")
                records.append(self._evaluate_case(
                    question, frozen_case, qas_by_id[question.case_id],
                    session=session, ask_use_case=ask_use_case, chunks_by_id=chunks_by_id, index=index,
                ))
                resolver.flush()
        finally:
            resolver.flush()

        summary = QasperAnswerSummary(
            tuple(records),
            extra={
                "query_vectors": {**resolver.stats.to_dict(), "supplementary_size": resolver.supplementary_size},
            },
        )
        report = {
            "protocol": PROTOCOL,
            "identity": {
                "dataset_sha256": assets.dataset_sha256,
                "mapping_digest": frozen.digest,
                "embedding_cache_sha256": identity.sha256,
                "embedding_model": identity.model_id,
                "embedding_dimension": identity.dimension,
                "code": assets.code_identity.model_dump(mode="json"),
                "retrieval_policy": self._policy.name,
                "answer_models": sorted({case.model for case in records if case.model}),
                "judge_prompt_version": JUDGE_PROMPT_VERSION if self._judge_call is not None else None,
                "session": "chunk_store_only; no reranker; no field index; generation max_attempts=2",
            },
            "sample": sample.manifest(),
            "evaluated_cases": len(records),
            "production_qualification": {
                "eligible": False,
                "reason": "Retrieval scans cached vectors in memory instead of the production Qdrant index.",
            },
            "metrics": summary.to_dict(),
        }
        answers = "".join(json.dumps(case.to_dict(), ensure_ascii=False) + "\n" for case in records)
        self._artifacts.publish(self._config.output_directory, {
            "sample.json": _json_bytes(sample.manifest()),
            "answers.jsonl": answers.encode("utf-8"),
            "report.json": _json_bytes(report),
        })
        return report

    def _evaluate_case(
        self,
        question,
        frozen_case: FrozenQasperCase,
        qa: dict[str, Any],
        *,
        session: PaperRAGSession,
        ask_use_case: AskPaperUseCase,
        chunks_by_id: dict,
        index: int,
    ) -> QasperAnswerCase:
        suffix = f"{index:04d}"
        goal = ask_use_case.build_paper_ask_goal(
            paper_id=question.paper_id, question=question.question, goal_id=f"qasper-answer-{suffix}",
        )
        started = self._clock()
        result = session.run(
            goal,
            graph_identity=build_paper_analysis_context_graph_identity(
                run_id=self._config.run_id, stage_id="run_research_rag",
            ),
            session_id=f"qasper-answer-session-{suffix}",
            current_section_index=0,
        )
        elapsed_ms = round((self._clock() - started) * 1000, 1)
        outcome = outcome_for_session_status(result.status.value)
        candidate = result.answer
        metadata = dict(candidate.metadata) if candidate is not None else {}
        generated = dict(metadata.get("generated_answer_metadata") or {})
        context_ids = tuple(metadata.get("context_chunk_ids") or _pack_chunk_ids(result.context_pack))
        answer = candidate.answer_text if candidate is not None and outcome is AnswerOutcome.ANSWERED else ""
        markers = partition_citation_markers(answer, context_count=len(context_ids))
        cited_ids = tuple(context_ids[i - 1] for i in markers.valid_indexes)

        references = references_for_question(qa)
        prediction = project_prediction(question.question, outcome, answer)
        f1 = max_answer_f1(prediction, references)

        judge = None
        if outcome is AnswerOutcome.ANSWERED and self._judge_call is not None:
            judge = asyncio.run(judge_answer(self._judge_call, JudgeRequest(
                question=question.question,
                references=[ref.answer for ref in references],
                answer=answer,
                cited_passages=[
                    (i, render_answer_context(chunks_by_id[context_ids[i - 1]]))
                    for i in markers.valid_indexes
                ],
            )))
        generation_error = ((result.decision.metadata or {}).get("generation_error") or {}).get("code", "")
        return QasperAnswerCase(
            case_id=question.case_id,
            paper_id=question.paper_id,
            question=question.question,
            stratum=question.answer_type,
            references=tuple(ref.answer for ref in references),
            reference_types=tuple(ref.answer_type for ref in references),
            mapping_disposition=frozen_case.mapping_result.disposition,
            outcome=outcome,
            answer=answer,
            prediction=prediction,
            answer_f1=f1.score,
            matched_type=f1.matched_type,
            cited_chunk_ids=cited_ids,
            context_chunk_ids=context_ids,
            strict_citation_coverage=_coverage(frozen_case, cited_ids, strict=True) if answer else None,
            equivalent_citation_coverage=_coverage(frozen_case, cited_ids, strict=False) if answer else None,
            strict_context_coverage=_coverage(frozen_case, context_ids, strict=True),
            equivalent_context_coverage=_coverage(frozen_case, context_ids, strict=False),
            judge=judge,
            decision_reason=str(result.decision.reason or ""),
            abstention_reason=str(metadata.get("abstention_reason") or ""),
            end_to_end_ms=elapsed_ms,
            generation_ms=_number(generated.get("generation_elapsed_ms")),
            prompt_chars=generated.get("prompt_chars") if isinstance(generated.get("prompt_chars"), int) else None,
            model=str(metadata.get("model") or ""),
            model_usage=metadata.get("model_usage") if isinstance(metadata.get("model_usage"), dict) else None,
            evidence_type_gap_accepted=bool(result.metrics and result.metrics.evidence_type_gap_accepted),
            generation_error=str(generation_error),
        )


def _coverage(frozen_case: FrozenQasperCase, chunk_ids, *, strict: bool) -> float | None:
    counts = evidence_group_coverage(frozen_case, list(chunk_ids), strict=strict)
    if counts is None:
        return None
    covered, total = counts
    return covered / total


def _pack_chunk_ids(pack) -> list[str]:
    if pack is None:
        return []
    return [str(item.metadata.get("rag_chunk_id") or item.evidence_id) for item in pack.accepted_evidence]


def _qas_by_case(raw_dataset: dict[str, Any]) -> dict[str, dict[str, Any]]:
    papers = raw_dataset.get("papers", raw_dataset)
    return {
        f"{paper_id}:{qa['question_id']}": qa
        for paper_id, paper in papers.items()
        for qa in paper.get("qas") or ()
    }


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


__all__ = ["PROTOCOL", "QasperAnswerEvaluationConfig", "QasperAnswerEvaluationService"]
