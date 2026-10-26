"""End-to-end QASPER answer evaluation through the production gated answer path.

Chunk and optional field retrieval vectors come from a frozen offline cache. Rewritten
query texts may be embedded live by ``QueryVectorResolver``. Retrieval components and
the bounded final context are selected by an explicit evaluation profile.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.paper_rag_session import ContextWindowLimits, PaperRAGSession, RunScope
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.document.models import PaperChunk
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.paper_answer_model import PaperAnswerModelPort
from backend.research.ports.qasper_evaluation import QasperArtifactStore
from backend.research.ports.qasper_query_embedding import QueryEmbeddingPort, QueryVectorStorePort
from backend.research.ports.field_embedding_index import FieldEmbeddingSearchPort
from backend.research.ports.reranker import RerankerPort
from backend.research.ports.section_navigator import SectionNavigatorPort
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
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureChunkStore,
    OfflineEmbeddingCache,
)
from backend.research.rag.evaluation.qasper_scoring import evidence_group_coverage
from backend.research.rag.retrieval.answer_evidence_selection import RelevanceEvidenceSelector
from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig
from backend.research.rag.retrieval.paper_answer_generator import (
    ANSWER_PROMPT_VERSION,
    ANSWER_PROTOCOL_VERSION,
    AnswerGenerator,
    context_chars_for_passages,
    render_answer_context,
)
from backend.research.rag.retrieval.policies import RetrievalPolicy
from framework.rag.evaluation.answer_metrics import AnswerOutcome
from framework.rag.generation import partition_citation_markers
from framework.harness.context.assembler import ContextAssembler
from framework.harness.rag.models import RAGBudget, RAGSessionSpec

PROTOCOL = "qasper_end_to_end_answer_v1"


@dataclass(frozen=True)
class QasperAnswerRetrievalProfile:
    """Explicit, serializable retrieval profile for an answer evaluation run."""

    name: str = "chunk_only"
    execution_profile: str = "fixture"
    max_context_items: int = 8
    max_context_tokens: int = 2048
    field_index_enabled: bool = False
    reranker_enabled: bool = False
    field_reranker_enabled: bool = False
    # Candidates the retrieval pack holds for the evidence selector to score; None
    # sends the top ``max_context_items`` retrieved passages straight to the answer.
    evidence_pool_size: int | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("retrieval profile name is required")
        if not self.execution_profile.strip():
            raise ValueError("retrieval execution profile is required")
        for field_name in ("field_index_enabled", "reranker_enabled", "field_reranker_enabled"):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        for field_name in ("max_context_items", "max_context_tokens"):
            value = getattr(self, field_name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.evidence_pool_size is not None and (
            type(self.evidence_pool_size) is not int
            or self.evidence_pool_size < self.max_context_items
        ):
            raise ValueError("evidence_pool_size must be an integer no smaller than max_context_items")

    @property
    def retrieval_context_items(self) -> int:
        """Passages the retrieval context pack admits before answer-side selection."""
        return self.evidence_pool_size or self.max_context_items

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "execution_profile": self.execution_profile,
            "max_context_items": self.max_context_items,
            "max_context_tokens": self.max_context_tokens,
            "field_index_enabled": self.field_index_enabled,
            "reranker_enabled": self.reranker_enabled,
            "field_reranker_enabled": self.field_reranker_enabled,
            # Omitted when unset so profiles without evidence selection keep the
            # fingerprint of runs recorded before the field existed.
            **(
                {"evidence_pool_size": self.evidence_pool_size}
                if self.evidence_pool_size is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class AnswerContextRuntime:
    """Context assembly the production reader Q&A path uses, injected as one unit.

    The compaction-verified assembler is only valid together with the answer model
    window it admits against and the run binding its snapshot store requires.
    """

    assembler_factory: Callable[[RAGSessionSpec], ContextAssembler]
    window: ContextWindowLimits
    run_scope: RunScope
    name: str = "research_context_runtime"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_input_tokens": self.window.max_input_tokens,
            "max_output_tokens": self.window.max_output_tokens,
        }


FieldIndexFactory = Callable[
    [list[PaperChunk], OfflineEmbeddingCache, Callable[[str], Any]], FieldEmbeddingSearchPort
]


@dataclass(frozen=True)
class QasperAnswerEvaluationConfig:
    output_directory: str
    seed: str = "qasper-answer-v1"
    per_type: int = 75
    max_cases: int | None = None
    run_id: str = "qasper-answer-eval"
    # Restrict the run to these sampled cases (e.g. re-running gateway failures); the
    # sample itself, and each case's position in it, stay those of the full run.
    case_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.output_directory.strip():
            raise ValueError("output_directory is required")
        if self.per_type < 1:
            raise ValueError("per_type must be positive")
        if self.max_cases is not None and self.max_cases < 1:
            raise ValueError("max_cases must be positive")
        if self.case_ids is not None:
            if not self.case_ids:
                raise ValueError("case_ids must not be empty")
            if len(set(self.case_ids)) != len(self.case_ids):
                raise ValueError("case_ids must be unique")
            if self.max_cases is not None:
                raise ValueError("case_ids and max_cases are mutually exclusive")


class _TrackedReranker:
    def __init__(self, reranker: RerankerPort | None) -> None:
        self._reranker = reranker
        self.calls = 0
        self.passages = 0

    @property
    def available(self) -> bool:
        return self._reranker is not None

    def score(self, query: str, passages: list[str]) -> list[float]:
        if self._reranker is None:
            raise RuntimeError("reranker is unavailable")
        self.calls += 1
        self.passages += len(passages)
        return self._reranker.score(query, passages)


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
        field_index: FieldEmbeddingSearchPort | None = None,
        field_index_factory: FieldIndexFactory | None = None,
        reranker: RerankerPort | None = None,
        field_reranker: RerankerPort | None = None,
        retrieval_profile: QasperAnswerRetrievalProfile | None = None,
        evidence_selector: RelevanceEvidenceSelector | None = None,
        section_navigator: SectionNavigatorPort | None = None,
        section_navigation: SectionNavigationConfig | None = None,
        context_runtime: AnswerContextRuntime | None = None,
        progress: Callable[[int, int, QasperAnswerCase], None] | None = None,
        case_started: Callable[[int, int, str], None] | None = None,
        recovery_source: Mapping[str, Any] | None = None,
        answer_model_identity: Mapping[str, Any] | None = None,
        judge_model_identity: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if field_index is not None and field_index_factory is not None:
            raise ValueError("field_index and field_index_factory are mutually exclusive")
        self._assets = assets
        self._artifacts = artifacts
        self._embedder = query_embedder
        self._vector_store = query_vector_store
        self._answer_model = answer_model
        self._judge_call = judge_call
        self._policy = retrieval_policy
        self._config = config
        self._field_index = field_index
        self._field_index_factory = field_index_factory
        self._reranker = reranker
        self._field_reranker = field_reranker
        self._retrieval_profile = retrieval_profile or QasperAnswerRetrievalProfile(
            name=(
                "chunk_only" if field_index is None and field_index_factory is None
                and reranker is None and field_reranker is None else "custom"
            ),
            field_index_enabled=field_index is not None or field_index_factory is not None,
            reranker_enabled=reranker is not None,
            field_reranker_enabled=field_reranker is not None,
        )
        if self._retrieval_profile.field_index_enabled and field_index is None and field_index_factory is None:
            raise ValueError("retrieval profile enables field index but no field index was supplied")
        if not self._retrieval_profile.field_index_enabled and (
            field_index is not None or field_index_factory is not None
        ):
            raise ValueError("retrieval profile disables field index but a field index was supplied")
        if self._retrieval_profile.reranker_enabled and reranker is None:
            raise ValueError("retrieval profile enables reranker but no reranker was supplied")
        if not self._retrieval_profile.reranker_enabled and reranker is not None:
            raise ValueError("retrieval profile disables reranker but a reranker was supplied")
        if self._retrieval_profile.field_reranker_enabled and field_reranker is None:
            raise ValueError("retrieval profile enables field reranker but no field reranker was supplied")
        if not self._retrieval_profile.field_reranker_enabled and field_reranker is not None:
            raise ValueError("retrieval profile disables field reranker but a field reranker was supplied")
        pool_size = self._retrieval_profile.evidence_pool_size
        if (pool_size is None) != (evidence_selector is None):
            raise ValueError("retrieval profile evidence_pool_size and evidence_selector must be set together")
        if evidence_selector is not None and evidence_selector.pool_size != pool_size:
            raise ValueError("evidence_selector pool_size must equal the profile evidence_pool_size")
        self._evidence_selector = evidence_selector
        if section_navigation is not None and section_navigator is None:
            raise ValueError("section_navigation config requires a section_navigator")
        self._section_navigator = section_navigator
        self._section_navigation = (
            (section_navigation or SectionNavigationConfig()) if section_navigator is not None else None
        )
        self._context_runtime = context_runtime
        self._progress = progress
        self._case_started = case_started
        self._recovery_source = dict(recovery_source) if recovery_source is not None else None
        self._answer_model_identity = _measurement_identity(
            answer_model_identity or getattr(answer_model, "measurement_identity", None)
        )
        self._judge_model_identity = (
            _measurement_identity(
                judge_model_identity or getattr(judge_call, "measurement_identity", None)
            )
            if judge_call is not None
            else None
        )
        self._clock = clock

    def run(self) -> dict[str, Any]:
        assets = self._assets.read_assets()
        frozen = freeze_qasper_dataset(assets.raw_dataset, assets.documents, assets.chunks)
        frozen.verify_digest()
        frozen_by_id = {case.case_id: case for case in frozen.cases}
        sample = sample_questions(assets.raw_dataset, seed=self._config.seed, per_type=self._config.per_type)
        positioned = list(enumerate(sample.questions))
        if self._config.case_ids is not None:
            wanted = set(self._config.case_ids)
            positioned = [(index, question) for index, question in positioned if question.case_id in wanted]
            missing = wanted - {question.case_id for _, question in positioned}
            if missing:
                raise ValueError(f"{len(missing)} requested case(s) are not in this sample")
        elif self._config.max_cases:
            positioned = positioned[: self._config.max_cases]
        qas_by_id = _qas_by_case(assets.raw_dataset)

        cache = self._assets.embedding_cache()
        identity = self._assets.cache_identity()
        resolver = QueryVectorResolver(
            cache, embedder=self._embedder, store=self._vector_store, model_id=identity.model_id,
        )
        run_id = self._config.run_id
        # Session retrieval filters on run_id, so the corpus is bound to this evaluation run.
        chunks = [
            chunk.model_copy(update={"metadata": {**chunk.metadata, "run_id": run_id}})
            for chunk in assets.chunks
        ]
        store = CachedFixtureChunkStore(chunks, cache, query_vector=resolver)
        field_index = self._field_index
        if self._field_index_factory is not None:
            # Construction is deliberately before the first question: missing field
            # vectors must fail the selected profile rather than silently degrade.
            field_index = self._field_index_factory(chunks, cache, resolver)
        if self._retrieval_profile.field_index_enabled and field_index is None:
            raise ValueError("retrieval profile enabled field index but factory returned None")
        components = {
            "field_index": _component_name(field_index),
            "reranker": _component_name(self._reranker),
            "field_reranker": _component_name(self._field_reranker),
        }
        reranker_tracker = _TrackedReranker(self._reranker)
        field_reranker_tracker = _TrackedReranker(self._field_reranker)
        budget = replace(
            RAGBudget.safe_default(),
            max_context_items=self._retrieval_profile.retrieval_context_items,
            max_context_tokens=self._retrieval_profile.max_context_tokens,
        )
        chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
        session = PaperRAGSession(
            store,
            budget=budget,
            field_index=field_index,
            reranker=reranker_tracker if reranker_tracker.available else None,
            field_reranker=field_reranker_tracker if field_reranker_tracker.available else None,
            retrieval_policy=self._policy,
            section_navigator=self._section_navigator,
            section_navigation=self._section_navigation,
            # The generator renders exactly the passages the context budget admits, each
            # with the default per-passage room, so a larger budget is not truncated away.
            answer_worker=PaperAnswerWorker(AnswerGenerator(
                self._answer_model,
                max_context_chunks=self._retrieval_profile.max_context_items,
                max_context_chars=context_chars_for_passages(self._retrieval_profile.max_context_items),
                evidence_selector=self._evidence_selector,
            )),
            generation_policy={"enabled": True, "max_attempts": 2},
            **(
                {
                    "context_assembler_factory": self._context_runtime.assembler_factory,
                    "context_window": self._context_runtime.window,
                    "run_scope": self._context_runtime.run_scope,
                }
                if self._context_runtime is not None
                else {}
            ),
        )
        ask_use_case = AskPaperUseCase()

        evidence_selection = (
            _measurement_identity(self._evidence_selector.measurement_identity)
            if self._evidence_selector is not None
            else None
        )
        section_navigation = (
            _measurement_identity({
                "config": self._section_navigation.to_dict(),
                "navigator": dict(self._section_navigator.measurement_identity),
            })
            if self._section_navigator is not None and self._section_navigation is not None
            else None
        )
        report_identity = {
            "dataset_sha256": assets.dataset_sha256,
            "mapping_digest": frozen.digest,
            "embedding_cache_sha256": identity.sha256,
            "embedding_model": identity.model_id,
            "embedding_dimension": identity.dimension,
            "code": assets.code_identity.model_dump(mode="json"),
            "retrieval_policy": self._policy.name,
            "retrieval_policy_settings": _policy_identity(self._policy),
            "retrieval_profile": self._retrieval_profile.to_dict(),
            "retrieval_components": components,
            "reranker_activation": _reranker_activation(
                self._policy, reranker_tracker, field_reranker_tracker,
            ),
            "context_budget": budget.to_dict(),
            "evidence_selection": evidence_selection,
            # Omitted when unset so runs without navigation keep their recorded identity.
            **({"section_navigation": section_navigation} if section_navigation is not None else {}),
            "retrieval_profile_fingerprint": _profile_fingerprint(
                self._retrieval_profile, self._policy, components, budget.to_dict(), evidence_selection,
                section_navigation,
            ),
            "fixture_boundary": (
                "offline_cached_chunk_and_field_vectors; "
                "query_vectors_resolved_by_cache_or_configured_embedder; "
                "no_production_qdrant"
            ),
            "answer_prompt_version": ANSWER_PROMPT_VERSION,
            "answer_protocol_version": ANSWER_PROTOCOL_VERSION,
            "answer_model_identity": self._answer_model_identity,
            "answer_models": [],
            "judge_prompt_version": JUDGE_PROMPT_VERSION if self._judge_call is not None else None,
            "judge_model_identity": self._judge_model_identity,
            "session": "generation max_attempts=2",
            "context_runtime": (
                self._context_runtime.to_dict() if self._context_runtime is not None else {"name": "default"}
            ),
        }
        if self._recovery_source is not None:
            from backend.research.application.qasper_answer_merge import require_same_measurement

            source_identity = self._recovery_source.get("identity")
            if not isinstance(source_identity, Mapping):
                raise ValueError("recovery source identity is required")
            source_case_ids = self._recovery_source.get("case_ids")
            if list(self._config.case_ids or ()) != list(source_case_ids or ()):
                raise ValueError("recovery source case ids do not match evaluation selection")
            # This preflight runs after local assets/components are identified but before
            # the first answer or judge provider call.
            require_same_measurement(source_identity, report_identity)

        records: list[QasperAnswerCase] = []
        try:
            for done, (index, question) in enumerate(positioned, start=1):
                frozen_case = frozen_by_id.get(question.case_id)
                if frozen_case is None:
                    raise ValueError(f"sampled case {question.case_id} is absent from the frozen mapping")
                if self._case_started is not None:
                    self._case_started(done, len(positioned), question.case_id)
                record = self._evaluate_case(
                    question, frozen_case, qas_by_id[question.case_id],
                    session=session, ask_use_case=ask_use_case, chunks_by_id=chunks_by_id, index=index,
                )
                records.append(record)
                resolver.flush()
                if self._progress is not None:
                    self._progress(done, len(positioned), record)
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
                **report_identity,
                "reranker_activation": _reranker_activation(
                    self._policy, reranker_tracker, field_reranker_tracker,
                ),
                "answer_models": sorted({case.model for case in records if case.model}),
            },
            "sample": sample.manifest(),
            "evaluated_cases": len(records),
            "case_selection": (
                {"mode": "case_ids", "case_ids": [record.case_id for record in records]}
                if self._config.case_ids is not None
                else {"mode": "max_cases", "max_cases": self._config.max_cases}
                if self._config.max_cases
                else {"mode": "full_sample"}
            ),
            "production_qualification": {
                "eligible": False,
                "reason": "Retrieval scans cached vectors in memory instead of the production Qdrant index.",
            },
            "metrics": summary.to_dict(),
        }
        if self._recovery_source is not None:
            report["recovery_source"] = dict(self._recovery_source)
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
        failure = (result.decision.metadata or {}).get("generation_error") or {}
        # Prefer the passages exactly as sent to the model; the pack order is only a
        # fallback for failures that happened before context selection.
        context_ids = tuple(
            metadata.get("context_chunk_ids")
            or failure.get("context_chunk_ids")
            or _pack_chunk_ids(result.context_pack)
        )
        answer = candidate.answer_text if candidate is not None and outcome is AnswerOutcome.ANSWERED else ""
        markers = partition_citation_markers(answer, context_count=len(context_ids))
        cited_ids = tuple(context_ids[i - 1] for i in markers.valid_indexes)

        references = references_for_question(qa)
        prediction = project_prediction(question.question, outcome, answer)
        f1 = max_answer_f1(prediction, references)

        judge = None
        judge_request = None
        if outcome is AnswerOutcome.ANSWERED:
            judge_request = JudgeRequest(
                question=question.question,
                references=[ref.answer for ref in references],
                answer=answer,
                cited_passages=[
                    (i, render_answer_context(chunks_by_id[context_ids[i - 1]]))
                    for i in markers.valid_indexes
                ],
            )
            if self._judge_call is not None:
                judge = asyncio.run(judge_answer(self._judge_call, judge_request))
        generation_error = failure.get("code", "")
        generation_error_detail = (
            {
                "code": str(failure.get("code") or ""),
                "message": str(failure.get("message") or ""),
                "retryable": bool(failure.get("retryable", False)),
                "details": dict(failure.get("details") or {}),
            }
            if failure else None
        )
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
            generation_error_detail=generation_error_detail,
            judge_request=judge_request,
            evidence_selection=_case_evidence_selection(generated, frozen_case),
            section_navigation=_case_section_navigation(result.transcript.events, frozen_case, context_ids),
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


def _component_name(component: object | None) -> str | None:
    if component is None:
        return None
    return f"{type(component).__module__}.{type(component).__qualname__}"


def _measurement_identity(value: Any) -> dict[str, Any] | str:
    if isinstance(value, Mapping):
        return json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
    return "unknown"


def _reranker_activation(
    policy: RetrievalPolicy,
    reranker: _TrackedReranker,
    field_reranker: _TrackedReranker,
) -> dict[str, Any]:
    return {
        "base": {
            "available": reranker.available,
            "can_run": reranker.available,
            "policy_gate_mode": "all_intents" if not policy.reranking_intents else "allowlist",
            "eligible_intents": list(policy.reranking_intents) or "all",
            "calls": reranker.calls,
            "passages": reranker.passages,
        },
        "field": {
            "available": field_reranker.available,
            "policy_enabled": policy.field_reranking_enabled,
            "can_run": field_reranker.available and policy.field_reranking_enabled,
            "policy_gate_mode": "all_intents" if not policy.field_reranking_intents else "allowlist",
            "eligible_intents": list(policy.field_reranking_intents) or "all",
            "calls": field_reranker.calls,
            "passages": field_reranker.passages,
        },
    }


def _policy_identity(policy: RetrievalPolicy) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(policy), ensure_ascii=False, sort_keys=True))


def _case_evidence_selection(
    generated: Mapping[str, Any], frozen_case: FrozenQasperCase,
) -> dict[str, Any] | None:
    selection = generated.get("evidence_selection")
    if not isinstance(selection, Mapping):
        return None
    # The scored passages are exactly the pool the selector chose from.
    pool_ids = tuple(str(chunk_id) for chunk_id in (selection.get("relevance") or {}))
    return {
        "pool_ids": list(pool_ids),
        "pool_strict_context_coverage": _coverage(frozen_case, pool_ids, strict=True),
        "pool_equivalent_context_coverage": _coverage(frozen_case, pool_ids, strict=False),
        "relevance": dict(selection.get("relevance") or {}),
        "adaptive_k": selection.get("adaptive_k"),
        "pinned_ids": list(selection.get("pinned_ids") or ()),
        "selected_ids": list(selection.get("selected_ids") or ()),
        "condensed_ids": list(selection.get("condensed_ids") or ()),
        "summarizer_diagnostics": dict(selection.get("summarizer_diagnostics") or {}),
        "model": str(selection.get("model") or ""),
        "usage": dict(selection.get("usage") or {}),
    }


def _case_section_navigation(
    events: Sequence[Mapping[str, Any]],
    frozen_case: FrozenQasperCase,
    context_ids: Sequence[str],
) -> dict[str, Any] | None:
    """Navigation reports of the session's retrieval steps, or None when navigation is off."""
    steps = [
        dict(navigation)
        for event in events
        if event.get("event_type") == "rag_step_executed"
        and isinstance(payload := event.get("payload"), Mapping)
        and isinstance(metadata := payload.get("metadata"), Mapping)
        and isinstance(navigation := metadata.get("section_navigation"), Mapping)
    ]
    if not steps:
        return None
    added = tuple(dict.fromkeys(
        str(chunk_id) for step in steps for chunk_id in (step.get("added_chunk_ids") or ())
    ))
    in_context = set(context_ids)
    added_in_context = tuple(chunk_id for chunk_id in added if chunk_id in in_context)
    added_coverage = _coverage(frozen_case, added_in_context, strict=False) if added_in_context else None
    return {
        "steps": steps,
        "added_chunk_ids": list(added),
        "displaced_chunk_ids": list(dict.fromkeys(
            str(chunk_id) for step in steps for chunk_id in (step.get("displaced_chunk_ids") or ())
        )),
        "added_in_context_ids": list(added_in_context),
        "added_equivalent_context_coverage": added_coverage,
        "added_gold_in_context": bool(added_coverage),
    }


def _profile_fingerprint(
    profile: QasperAnswerRetrievalProfile,
    policy: RetrievalPolicy,
    components: dict[str, str | None],
    context_budget: dict[str, int],
    evidence_selection: Mapping[str, Any] | None,
    section_navigation: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "profile": profile.to_dict(),
        "policy": _policy_identity(policy),
        "components": components,
        "context_budget": context_budget,
        **({"evidence_selection": evidence_selection} if evidence_selection is not None else {}),
        **({"section_navigation": section_navigation} if section_navigation is not None else {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


__all__ = [
    "PROTOCOL",
    "FieldIndexFactory",
    "QasperAnswerEvaluationConfig",
    "QasperAnswerEvaluationService",
    "QasperAnswerRetrievalProfile",
]
