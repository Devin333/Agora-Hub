from __future__ import annotations

import json
import math
import hashlib
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from backend.research.document.models import PaperChunk
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.field_embedding_index import FieldEmbeddingHit
from backend.research.ports.visual_chunk_index import VisualChunkHit
from backend.research.rag.adapters.paper_field_text import FIELD_NAMES, extract_field_texts
from backend.research.rag.evaluation.paper_answer_eval import (
    EvidenceAnswerEvaluator,
    EvidenceAnswerSample,
)
from backend.research.rag.evaluation.paper_evaluation_report import EvidenceRegressionReport
from backend.research.rag.evaluation.paper_evidence_eval import (
    EvidenceGoldenSetBuilder,
    EvidenceQAPair,
    EvidenceRetrievalEvaluator,
    hydrate_evidence_pairs_from_chunks,
    load_evidence_golden_set,
    save_evidence_golden_set,
)


@dataclass(frozen=True)
class EvidenceEvalOptions:
    golden_set: str | Path | None = None
    output_dir: str | Path = ".newsroom/eval/evidence"
    papers_dir: str | Path | None = None
    build_golden_set: bool = False
    max_pairs_per_type: int = 20
    no_negative: bool = False
    domain: str = ""
    live_retrieval: bool = False
    visual: bool = False
    page_visual: bool = False
    no_render_page_visual: bool = False
    vision_descriptions: bool = False
    image_root: str | Path | None = None
    retrieval_policy: str = ""
    lightweight_reranker: bool = False
    thresholds: Mapping[str, float] = field(default_factory=dict)
    deterministic_answer_eval: bool = False
    live_answer_eval: bool = False
    answer_eval_limit: int = 8


def run_evidence_eval_core(
    options: EvidenceEvalOptions,
    *,
    live_answer_ask: Callable[[EvidenceQAPair], dict[str, Any]] | None = None,
) -> int:
    if options.deterministic_answer_eval and options.live_answer_eval:
        raise ValueError("--deterministic-answer-eval and --live-answer-eval are mutually exclusive")
    pairs = []
    chunks = []
    visual_store = None
    if options.papers_dir:
        papers_dir = Path(options.papers_dir)
        chunks = _load_chunks_from_papers_dir(papers_dir)
        if options.page_visual:
            from backend.research.rag.visual.page_visual_chunks import build_page_visual_chunks

            chunks.extend(build_page_visual_chunks(
                chunks,
                papers_dir=papers_dir,
                render_pages=not options.no_render_page_visual,
            ))
        if options.vision_descriptions:
            chunks = _describe_visual_chunks(
                chunks,
                image_root=Path(options.image_root) if options.image_root else papers_dir,
            )
    if options.build_golden_set:
        if not chunks:
            raise ValueError("--build-golden-set requires --papers-dir")
        pairs = EvidenceGoldenSetBuilder(
            max_pairs_per_type=options.max_pairs_per_type,
            include_negative=not options.no_negative,
        ).build(chunks, domain=options.domain)
        if options.golden_set:
            save_evidence_golden_set(pairs, options.golden_set)
    elif options.golden_set:
        pairs = load_evidence_golden_set(options.golden_set)
    if not pairs:
        raise ValueError("--golden-set is required unless --papers-dir --build-golden-set produces pairs")
    golden_set_hydration: dict[str, Any] = {}
    if pairs and chunks and options.golden_set and not options.build_golden_set:
        pairs, golden_set_hydration = hydrate_evidence_pairs_from_chunks(pairs, chunks)

    answer_eval_mode = (
        "live"
        if options.live_answer_eval
        else "deterministic"
        if options.deterministic_answer_eval
        else "none"
    )
    metadata = {
        "golden_set": str(Path(options.golden_set)) if options.golden_set else "",
        "total_pairs": len(pairs),
        "mode": "live_retrieval" if options.live_retrieval else "summary",
        "answer_eval_mode": answer_eval_mode,
    }
    if options.papers_dir:
        metadata["papers_dir"] = str(Path(options.papers_dir))
        metadata["chunks_total"] = len(chunks)
        metadata["visual_descriptions_enabled"] = bool(options.vision_descriptions)
        metadata["visual_described_chunks"] = _visual_described_count(chunks)
        metadata["page_visual_enabled"] = bool(options.page_visual)
        metadata["page_visual_chunks"] = _page_visual_count(chunks)
    qa_type_counts = Counter(pair.qa_type for pair in pairs)
    behavior_counts = Counter(pair.expected_behavior for pair in pairs)
    metadata["qa_type_counts"] = dict(sorted(qa_type_counts.items()))
    metadata["expected_behavior_counts"] = dict(sorted(behavior_counts.items()))
    if golden_set_hydration:
        metadata["golden_set_hydration"] = golden_set_hydration

    thresholds = dict(options.thresholds)
    retrieval = None
    if options.live_retrieval:
        if not chunks:
            raise ValueError("--live-retrieval requires --papers-dir with parsed research_document.json files")
        retriever, visual_store = _build_live_retriever(
            chunks,
            visual_enabled=options.visual,
            image_root=Path(options.image_root) if options.image_root else None,
            retrieval_policy=options.retrieval_policy,
            lightweight_reranker=options.lightweight_reranker,
        )
        retrieval = EvidenceRetrievalEvaluator(retriever).evaluate(pairs)
        policy = retriever.policy
        metadata["retrieval_policy"] = policy.name
        metadata["retrieval_policy_overfetch_multiplier"] = policy.overfetch_multiplier
        metadata["retrieval_policy_child_score_weights"] = policy.normalized_child_score_weights()
        metadata["retrieval_policy_visual_fusion_weights"] = {
            "text": policy.visual_fusion_text_weight,
            "visual": policy.visual_fusion_visual_weight,
        }
        metadata["visual_fusion_enabled"] = visual_store is not None
        metadata["visual_indexed_chunks"] = _visual_indexed_count(visual_store)
        metadata["lightweight_reranker_enabled"] = _lightweight_reranker_enabled(
            options.retrieval_policy,
            explicit=options.lightweight_reranker,
        )
    answer = None
    answer_adapter = "not_executed"
    if options.deterministic_answer_eval:
        answer_adapter = "deterministic_gold_supported_answer"
        answer = EvidenceAnswerEvaluator().evaluate(_build_deterministic_answer_samples(pairs))
    elif options.live_answer_eval:
        if live_answer_ask is not None:
            answer_adapter = "injected_live_answer_callable"
            ask = live_answer_ask
        else:
            answer_adapter = "paper_rag_session_unity_llm"
            ask = _build_live_answer_ask(
                chunks,
                retrieval_policy=options.retrieval_policy,
                visual_enabled=options.visual,
                image_root=Path(options.image_root) if options.image_root else None,
                lightweight_reranker=options.lightweight_reranker,
                limit=options.answer_eval_limit,
            )
        answer = EvidenceAnswerEvaluator().evaluate(
            _build_live_answer_samples(
                pairs,
                ask=ask,
            )
        )
    metadata.update(_diagnostic_execution_identity(
        options,
        answer_adapter=answer_adapter,
    ))
    report = EvidenceRegressionReport(
        retrieval=retrieval,
        answer=answer,
        metadata=metadata,
        thresholds=thresholds,
    )
    report.write(options.output_dir)
    print(report.to_markdown(), end="")
    return 0 if report.passed() else 1


def _diagnostic_execution_identity(
    options: EvidenceEvalOptions,
    *,
    answer_adapter: str,
) -> dict[str, Any]:
    """Describe what the legacy diagnostic actually assembled without granting qualification."""
    retrieval_executed = bool(options.live_retrieval)
    answer_executed = answer_adapter != "not_executed"
    if options.live_answer_eval and retrieval_executed:
        track = "end_to_end"
        track_reason = "The diagnostic executed retrieval plus an answer candidate path."
    elif answer_adapter == "paper_rag_session_unity_llm":
        track = "end_to_end"
        track_reason = "The in-memory gated answer session retrieved context and generated a candidate."
    elif retrieval_executed:
        track = "retrieval"
        track_reason = "The diagnostic executed the in-memory retrieval evaluator."
    else:
        track = "not_applicable"
        if not answer_executed:
            track_reason = "Summary-only diagnostics do not execute a retrieval or generation track."
        elif options.deterministic_answer_eval:
            track_reason = "The deterministic gold-answer diagnostic is not a generation-model track."
        else:
            track_reason = (
                "The core executed an injected answer callable without assembling or verifying a "
                "retrieval path, so it cannot claim an end-to-end track."
            )

    execution_profile = "fixture" if retrieval_executed or answer_executed else "not_applicable"
    execution_profile_reason = (
        "Legacy diagnostics are fixture-scoped and do not confer production qualification."
        if execution_profile == "fixture"
        else "Summary-only diagnostics do not assemble an execution profile."
    )
    gold_context_used = options.deterministic_answer_eval or answer_adapter == "injected_live_answer_callable"
    if options.deterministic_answer_eval:
        gold_context_scope = "deterministic_answer_construction"
    elif answer_adapter == "injected_live_answer_callable":
        gold_context_scope = "full_evaluation_pair_exposed_to_injected_callable"
    elif retrieval_executed or answer_executed:
        gold_context_scope = "scoring_only"
    else:
        gold_context_scope = "not_applicable"

    retrieval_adapter = "not_executed"
    if retrieval_executed or answer_adapter == "paper_rag_session_unity_llm":
        retrieval_adapter = "in_memory_chunk_store_with_sha256_hash_field_index"
        if options.visual:
            retrieval_adapter += "_and_deterministic_visual_embedding"

    generation_adapter = "not_executed"
    if answer_adapter == "paper_rag_session_unity_llm":
        generation_adapter = "configured_unity_llm_via_gated_paper_rag_session"
    elif answer_adapter == "injected_live_answer_callable":
        generation_adapter = "injected_callable_implementation_not_inspected"
    elif answer_adapter == "deterministic_gold_supported_answer":
        generation_adapter = "not_applicable_no_generation_model_executed"

    return {
        "protocol": "legacy_evidence_regression_diagnostic",
        "track": track,
        "track_reason": track_reason,
        "execution_profile": execution_profile,
        "execution_profile_reason": execution_profile_reason,
        "purpose": "diagnostic_regression",
        "component_identity": {
            "corpus_adapter": (
                "parsed_research_document_directory" if options.papers_dir else "not_executed"
            ),
            "golden_set_adapter": (
                "runtime_generated_from_evaluation_corpus"
                if options.build_golden_set
                else "loaded_evidence_qa_pairs"
            ),
            "retrieval_adapter": retrieval_adapter,
            "answer_adapter": answer_adapter,
            "generation_adapter": generation_adapter,
            "threshold_gate": "legacy_minimum_threshold_issue_calculation",
        },
        "gold_context_used": gold_context_used,
        "gold_context_scope": gold_context_scope,
        "production_qualification": {
            "eligible": False,
            "reason": (
                "A passing legacy diagnostic regression report is not a production qualification."
            ),
        },
    }


def _build_deterministic_answer_samples(pairs: list[EvidenceQAPair]) -> list[EvidenceAnswerSample]:
    return [_deterministic_answer_sample(pair) for pair in pairs]


def _deterministic_answer_sample(pair: EvidenceQAPair) -> EvidenceAnswerSample:
    if pair.expected_behavior == "abstain":
        return EvidenceAnswerSample(
            pair=pair,
            answer="The provided context does not mention evidence that answers this question.",
            metadata={"ci_answer_eval": "deterministic_abstention"},
        )

    context_ids = _unique_texts([*pair.gold_chunk_ids, *pair.equivalent_gold_chunk_ids])
    answer_facts = _unique_texts(pair.answer_facts)
    if answer_facts:
        answer_text = " ".join(answer_facts)
    else:
        answer_text = "The answer is supported by the cited evidence."
    if pair.gold_chunk_ids:
        answer_text = f"{answer_text} " + " ".join(f"[{chunk_id}]" for chunk_id in pair.gold_chunk_ids)
    return EvidenceAnswerSample(
        pair=pair,
        answer=answer_text,
        cited_chunk_ids=list(pair.gold_chunk_ids),
        cited_source_locators=list(pair.gold_source_locators),
        context_chunk_ids=context_ids,
        metadata={
            "ci_answer_eval": "deterministic_gold_supported_answer",
            "retrieved_chunk_ids": context_ids,
            "context_relationships": _context_relationships_from_pair(pair),
            "locator_context": _locator_context_from_pair(pair),
        },
    )


def _build_live_answer_samples(
    pairs: list[EvidenceQAPair],
    *,
    ask: Callable[[EvidenceQAPair], dict[str, Any]],
) -> list[EvidenceAnswerSample]:
    return [_live_answer_sample(pair, ask(pair)) for pair in pairs]


def _live_answer_sample(pair: EvidenceQAPair, payload: dict[str, Any]) -> EvidenceAnswerSample:
    citations = [dict(item) for item in payload.get("citations") or [] if isinstance(item, dict)]
    passages = [dict(item) for item in payload.get("passages") or [] if isinstance(item, dict)]
    context_chunk_ids = _unique_texts([item.get("chunk_id") for item in passages])
    cited_chunk_ids = _unique_texts([
        item.get("chunk_id") or item.get("evidence_id")
        for item in citations
    ])
    cited_source_locators = _unique_texts([item.get("source_locator") for item in citations])
    return EvidenceAnswerSample(
        pair=pair,
        answer=_answer_text_from_live_payload(payload),
        cited_chunk_ids=cited_chunk_ids,
        cited_source_locators=cited_source_locators,
        context_chunk_ids=context_chunk_ids,
        metadata={
            "answer_eval": "live_gated_harness",
            "status": str(payload.get("status") or ""),
            "generation_mode": str(payload.get("generation_mode") or ""),
            "transcript_id": str(payload.get("transcript_id") or ""),
            "retrieved_chunk_ids": context_chunk_ids,
            "gate_results": list(payload.get("gate_results") or []),
            "decision": dict(payload.get("decision") or {}),
            "answer_candidate": dict(payload.get("answer_candidate") or {}),
        },
    )


def _answer_text_from_live_payload(payload: dict[str, Any]) -> str:
    answer = str(payload.get("answer") or "").strip()
    if answer:
        return answer
    candidate = payload.get("answer_candidate")
    if isinstance(candidate, dict):
        candidate_answer = str(candidate.get("answer_text") or "").strip()
        if candidate_answer:
            return candidate_answer
        if candidate.get("abstained"):
            return "The provided context does not mention evidence that answers this question."
    status = str(payload.get("status") or "").casefold()
    if status and status != "succeeded":
        return "The provided context does not mention evidence that answers this question."
    return ""


def _build_live_answer_ask(
    chunks: list[PaperChunk],
    *,
    retrieval_policy: str | None,
    visual_enabled: bool,
    image_root: Path | None,
    lightweight_reranker: bool,
    limit: int,
) -> Callable[[EvidenceQAPair], dict[str, Any]]:
    if not chunks:
        raise ValueError(
            "--live-answer-eval requires --papers-dir with parsed research_document.json files "
            "or an injected live_answer_ask callable from an outer interface/script layer"
        )
    return _build_in_memory_live_answer_ask(
        chunks,
        retrieval_policy=retrieval_policy,
        visual_enabled=visual_enabled,
        image_root=image_root,
        lightweight_reranker=lightweight_reranker,
        limit=limit,
    )


def _build_in_memory_live_answer_ask(
    chunks: list[PaperChunk],
    *,
    retrieval_policy: str | None,
    visual_enabled: bool,
    image_root: Path | None,
    lightweight_reranker: bool,
    limit: int,
) -> Callable[[EvidenceQAPair], dict[str, Any]]:
    from backend.research.application.ask_paper import AskPaperUseCase
    from backend.research.application.llm_client import build_unity_llm_call
    from backend.research.application.paper_rag_session import PaperRAGSession
    from backend.research.rag.adapters import PaperAnswerWorker
    from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
    from backend.research.rag.retrieval.policies import build_retrieval_policy

    chunk_store = _InMemoryChunkStore()
    chunk_store.ensure_collection()
    chunk_store.index_chunks(chunks)

    field_index = _InMemoryFieldEmbeddingIndex()
    field_index.ensure_collection()
    field_index.index_chunks(chunks)

    visual_store = None
    if visual_enabled:
        visual_store = _InMemoryVisualStore(
            _DeterministicVisualEmbedding(),
            image_root=image_root,
        )
        visual_store.ensure_collection()
        visual_store.index_chunks(chunks)

    field_reranker = None
    if _lightweight_reranker_enabled(retrieval_policy, explicit=lightweight_reranker):
        from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker

        field_reranker = LightweightLexicalReranker()

    policy = build_retrieval_policy(retrieval_policy)
    answer_worker = PaperAnswerWorker(AnswerGenerator(build_unity_llm_call(max_tokens=600)))
    session = PaperRAGSession(
        chunk_store,
        field_index=field_index,
        field_reranker=field_reranker,
        visual_store=visual_store,
        retrieval_policy=policy,
        answer_worker=answer_worker,
        generation_policy={"enabled": True, "max_attempts": 2},
    )
    ask_use_case = AskPaperUseCase()

    def ask(pair: EvidenceQAPair) -> dict[str, Any]:
        run_suffix = uuid4().hex[:12]
        goal = ask_use_case.build_paper_ask_goal(
            paper_id=pair.paper_id,
            question=pair.question,
            goal_id=f"paper-rag-answer-eval-{run_suffix}",
        )
        run_id = f"paper-rag-answer-eval-run-{run_suffix}"
        result = session.run(
            goal,
            graph_identity=build_paper_analysis_context_graph_identity(
                run_id=run_id,
                stage_id="run_research_rag",
            ),
            session_id=f"paper-rag-answer-eval-session-{run_suffix}",
            current_section_index=0,
        )
        return _live_answer_payload_from_result(pair, goal=goal, result=result, limit=limit)

    return ask


def _live_answer_payload_from_result(
    pair: EvidenceQAPair,
    *,
    goal: Any,
    result: Any,
    limit: int,
) -> dict[str, Any]:
    answer = result.answer
    pack = result.context_pack
    return {
        "paper_id": pair.paper_id,
        "question": pair.question,
        "intent": goal.metadata.get("intent", ""),
        "status": result.status.value,
        "generation_mode": "gated_harness",
        "answer": answer.answer_text if answer and not answer.abstained else None,
        "answer_candidate": answer.to_dict() if answer else None,
        "claims": [claim.to_dict() for claim in answer.claims] if answer else [],
        "citations": _citations_from_live_answer(answer, pack),
        "passages": _passages_from_live_pack(pack, limit=limit),
        "metrics": _live_answer_metrics(result, pack),
        "decision": result.decision.to_dict(),
        "gate_results": list(result.decision.gate_results),
        "transcript_id": result.transcript.transcript_id,
        "context_pack": _live_context_pack_summary(pack),
    }


def _live_answer_metrics(result: Any, pack: Any | None) -> dict[str, Any]:
    session_metrics = result.metrics.to_dict() if getattr(result, "metrics", None) is not None else {}
    return {
        **session_metrics,
        "context_pack_id": pack.pack_id if pack else session_metrics.get("context_pack_id"),
        "accepted_evidence_count": len(pack.accepted_evidence)
        if pack
        else session_metrics.get("accepted_evidence_count", 0),
        "decision_type": result.decision.decision_type.value,
    }


def _passages_from_live_pack(pack: Any | None, *, limit: int) -> list[dict[str, Any]]:
    if pack is None:
        return []
    passages = []
    for evidence in pack.accepted_evidence[:limit]:
        passages.append({
            "evidence_id": evidence.evidence_id,
            "chunk_id": evidence.metadata.get("rag_chunk_id", evidence.evidence_id),
            "section_title": evidence.metadata.get("section_title", evidence.title),
            "chunk_type": evidence.metadata.get("chunk_type", evidence.evidence_type),
            "content": evidence.summary,
            "source_locator": evidence.metadata.get("source_locator") or evidence.source_ref,
        })
    return passages


def _citations_from_live_answer(answer: Any | None, pack: Any | None) -> list[dict[str, Any]]:
    if answer is None or pack is None:
        return []
    by_id = {item.evidence_id: item for item in pack.accepted_evidence}
    citations = []
    for evidence_id in answer.cited_evidence_ids:
        evidence = by_id.get(evidence_id)
        citations.append({
            "evidence_id": evidence_id,
            "chunk_id": evidence.metadata.get("rag_chunk_id", evidence_id) if evidence else evidence_id,
            "span_refs": _live_citation_span_refs(answer, evidence_id, evidence),
            "source_locator": (
                evidence.metadata.get("source_locator")
                or evidence.source_ref
                if evidence
                else ""
            ),
            "title": evidence.title if evidence else "",
        })
    return citations


def _live_citation_span_refs(answer: Any, evidence_id: str, evidence: Any | None) -> list[str]:
    available = set(evidence.span_refs) if evidence else None
    span_refs: list[str] = []
    seen: set[str] = set()
    for claim in answer.claims:
        if evidence_id not in claim.evidence_ids:
            continue
        for span_ref in claim.span_refs:
            if available is not None and span_ref not in available:
                continue
            if span_ref in seen:
                continue
            seen.add(span_ref)
            span_refs.append(span_ref)
    return span_refs


def _live_context_pack_summary(pack: Any | None) -> dict[str, Any] | None:
    if pack is None:
        return None
    return {
        "pack_id": pack.pack_id,
        "accepted_evidence_ids": [item.evidence_id for item in pack.accepted_evidence],
        "rejected_evidence_ids": [item.evidence_id for item in pack.rejected_evidence],
        "gap_report": pack.gap_report,
        "assembly_summary": pack.assembly_summary,
    }


def _context_relationships_from_pair(pair: EvidenceQAPair) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    relationships = dict(pair.supporting_evidence_group or {}).get("relationships")
    if isinstance(relationships, list):
        out.extend(dict(item) for item in relationships if isinstance(item, dict))
    for claim_id in pair.gold_claim_ids:
        for chunk_id in pair.gold_chunk_ids:
            out.append({"chunk_id": chunk_id, "claim_id": claim_id, "edge": "gold_claim_support"})
    return out


def _locator_context_from_pair(pair: EvidenceQAPair) -> list[dict[str, Any]]:
    locator_context = dict(pair.supporting_evidence_group or {}).get("locator_context")
    if isinstance(locator_context, list):
        return [dict(item) for item in locator_context if isinstance(item, dict)]
    return []


def _unique_texts(values: Sequence[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _load_chunks_from_papers_dir(papers_dir: Path):
    from backend.research.document.chunker import PaperDocumentChunker
    from backend.research.domain.document import ResearchDocument

    chunker = PaperDocumentChunker()
    chunks = []
    for path in sorted(papers_dir.glob("*/research_document.json")):
        document = ResearchDocument.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        parse_source = str(document.metadata.get("parse_source") or "nougat")
        chunks.extend(chunker.chunk(document, parse_source))  # type: ignore[arg-type]
    return chunks


def _build_live_retriever(
    chunks,
    *,
    visual_enabled: bool,
    image_root: Path | None,
    retrieval_policy: str | None = None,
    lightweight_reranker: bool = False,
):
    from backend.research.rag.retrieval.paper_claim_index import PaperClaimIndex
    from backend.research.rag.retrieval.paper_retriever import ResearchRetriever
    from backend.research.rag.retrieval.policies import build_retrieval_policy

    chunk_store = _InMemoryChunkStore()
    chunk_store.ensure_collection()
    chunk_store.index_chunks(chunks)
    claim_index = PaperClaimIndex.from_chunks(chunks)

    field_index = _InMemoryFieldEmbeddingIndex()
    field_index.ensure_collection()
    field_index.index_chunks(chunks)

    visual_store = None
    if visual_enabled:
        visual_store = _InMemoryVisualStore(
            _DeterministicVisualEmbedding(),
            image_root=image_root,
        )
        visual_store.ensure_collection()
        visual_store.index_chunks(chunks)
    policy = build_retrieval_policy(retrieval_policy)
    field_reranker = None
    if _lightweight_reranker_enabled(retrieval_policy, explicit=lightweight_reranker):
        from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker

        field_reranker = LightweightLexicalReranker()
    return ResearchRetriever(
        chunk_store,
        policy=policy,
        field_index=field_index,
        field_reranker=field_reranker,
        visual_store=visual_store,
        claim_index=claim_index,
    ), visual_store


def _lightweight_reranker_enabled(retrieval_policy: str | None, *, explicit: bool) -> bool:
    if explicit:
        return True
    from backend.research.rag.retrieval.policies import retrieval_policy_enables_lightweight_reranker

    return retrieval_policy_enables_lightweight_reranker(retrieval_policy)


def _describe_visual_chunks(chunks: list[PaperChunk], *, image_root: Path) -> list[PaperChunk]:
    from backend.research.application.visual_chunk_describer import (
        VisualChunkDescriptionConfig,
        OpenAICompatibleVisualChunkDescriber,
    )

    config = replace(VisualChunkDescriptionConfig.from_env(image_root=image_root), enabled=True)
    if not config.base_url:
        raise ValueError("--vision-descriptions requires OPENAI_BASE_URL or NEWS_VISUAL_DESCRIPTION_BASE_URL")
    if not os.environ.get(config.api_key_env):
        raise ValueError(f"--vision-descriptions requires {config.api_key_env}")
    describer = OpenAICompatibleVisualChunkDescriber(config)
    return describer.describe_chunks(chunks)


def _visual_indexed_count(visual_store) -> int:
    if visual_store is None:
        return 0
    return len(getattr(visual_store, "_vectors", {}))


def _visual_described_count(chunks: list[PaperChunk]) -> int:
    return sum(1 for chunk in chunks if chunk.metadata.get("visual_description"))


def _page_visual_count(chunks: list[PaperChunk]) -> int:
    return sum(1 for chunk in chunks if chunk.metadata.get("page_visual"))


class _InMemoryChunkStore:
    def __init__(self) -> None:
        self._chunks: dict[str, PaperChunk] = {}

    def ensure_collection(self) -> None:
        pass

    def index_chunks(self, chunks: list[PaperChunk]) -> None:
        self._chunks.update({chunk.chunk_id: chunk for chunk in chunks})

    def search_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
    ) -> list[PaperChunk]:
        return [
            chunk
            for chunk, score in self.search_with_scores(
                paper_id,
                query_text,
                filters=filters,
                limit=limit,
            )
            if score_threshold is None or score >= score_threshold
        ]

    def search_with_scores(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 30,
    ) -> list[tuple[PaperChunk, float]]:
        query_vector = _embed_text(query_text)
        scored = []
        for chunk in self._chunks.values():
            if chunk.paper_id != paper_id or not _matches_filters(chunk, filters or {}):
                continue
            scored.append((chunk, _cosine_similarity(query_vector, _embed_text(chunk.content))))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self._chunks.get(chunk_id)

    def get_parent_chunk(self, chunk: PaperChunk) -> PaperChunk | None:
        return self.get_chunk(chunk.parent_chunk_id) if chunk.parent_chunk_id else None

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        return [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]


class _InMemoryFieldEmbeddingIndex:
    def __init__(self) -> None:
        self._chunks: dict[str, PaperChunk] = {}
        self._vectors: dict[tuple[str, str], list[float]] = {}
        self._texts: dict[tuple[str, str], str] = {}
        self._payloads: dict[tuple[str, str], dict[str, Any]] = {}

    def ensure_collection(self) -> None:
        pass

    def index_chunks(self, chunks: list[PaperChunk]) -> None:
        for chunk in chunks:
            self._chunks[chunk.chunk_id] = chunk
            field_texts = extract_field_texts(chunk)
            for field_name, field_text in field_texts.non_empty().items():
                normalized = str(field_name).strip().casefold()
                if normalized not in FIELD_NAMES:
                    continue
                key = (chunk.chunk_id, normalized)
                self._texts[key] = field_text
                self._vectors[key] = _embed_text(field_text)
                self._payloads[key] = {
                    "paper_id": chunk.paper_id,
                    "chunk_id": chunk.chunk_id,
                    "field_name": normalized,
                    "field_text": field_text,
                    "field_text_sources": list(field_texts.sources_for(normalized)),
                    "chunk_type": chunk.chunk_type,
                    "section_title": chunk.section_title,
                    "section_role": list(chunk.section_role),
                    "section_index": chunk.section_index,
                    "has_formula": chunk.has_formula,
                    "has_figure": chunk.has_figure,
                    "has_table": chunk.has_table,
                    "figure_id": chunk.figure_id,
                    "table_id": chunk.metadata.get("table_id", ""),
                    "source_locator": chunk.metadata.get("source_locator", ""),
                    "caption_source_locator": chunk.metadata.get("caption_source_locator", ""),
                    "page": chunk.metadata.get("page"),
                    "pdf_rect": chunk.metadata.get("pdf_rect"),
                    "caption_pdf_rect": chunk.metadata.get("caption_pdf_rect"),
                }

    def delete_paper_chunks(self, paper_id: str) -> None:
        chunk_ids = [
            chunk_id
            for chunk_id, chunk in self._chunks.items()
            if chunk.paper_id == paper_id
        ]
        for chunk_id in chunk_ids:
            self._chunks.pop(chunk_id, None)
            for key in list(self._texts):
                if key[0] == chunk_id:
                    self._texts.pop(key, None)
                    self._vectors.pop(key, None)
                    self._payloads.pop(key, None)

    def search_field_vectors(
        self,
        paper_id: str,
        query_text: str,
        *,
        field_names: tuple[str, ...] | None = None,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[FieldEmbeddingHit]:
        query_vector = _embed_text(query_text)
        allowed_fields = set(_normalized_field_names(field_names))
        hits: list[FieldEmbeddingHit] = []
        for key, vector in self._vectors.items():
            chunk_id, field_name = key
            if allowed_fields and field_name not in allowed_fields:
                continue
            chunk = self._chunks.get(chunk_id)
            if chunk is None or chunk.paper_id != paper_id or not _matches_filters(chunk, filters or {}):
                continue
            score = _cosine_similarity(query_vector, vector)
            hits.append(FieldEmbeddingHit(
                chunk_id=chunk_id,
                field_name=field_name,
                score=score,
                field_text=self._texts[key],
                metadata=dict(self._payloads[key]),
            ))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


class _InMemoryVisualStore:
    def __init__(self, visual_model, *, image_root: Path | None) -> None:
        self._visual_model = visual_model
        self._image_root = image_root
        self._chunks: dict[str, PaperChunk] = {}
        self._vectors: dict[str, list[float]] = {}

    def ensure_collection(self) -> None:
        pass

    def index_chunks(self, chunks: list[PaperChunk]) -> None:
        for chunk in chunks:
            if chunk.chunk_type != "figure" or not chunk.metadata.get("image_ref"):
                continue
            image_ref = str(chunk.metadata.get("image_ref") or "")
            image_path = _resolve_image_path(
                image_ref,
                paper_id=chunk.paper_id,
                image_root=self._image_root,
            )
            if not image_path.exists():
                continue
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = self._visual_model.embed_image(str(image_path))

    def delete_paper_chunks(self, paper_id: str) -> None:
        ids = [
            chunk_id
            for chunk_id, chunk in self._chunks.items()
            if chunk.paper_id == paper_id
        ]
        for chunk_id in ids:
            self._chunks.pop(chunk_id, None)
            self._vectors.pop(chunk_id, None)

    def search_visual_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[VisualChunkHit]:
        query_vector = self._visual_model.embed_text(query_text)
        hits = []
        for chunk_id, chunk in self._chunks.items():
            if chunk.paper_id != paper_id or not _matches_filters(chunk, filters or {}):
                continue
            hits.append(VisualChunkHit(
                chunk_id=chunk_id,
                score=_cosine_similarity(query_vector, self._vectors[chunk_id]),
                metadata={
                    "chunk_id": chunk_id,
                    "paper_id": chunk.paper_id,
                    "chunk_type": chunk.chunk_type,
                    "image_ref": chunk.metadata.get("image_ref", ""),
                    "visual_indexed": True,
                },
            ))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]


def _matches_filters(chunk: PaperChunk, filters: dict[str, Any]) -> bool:
    for key, value in filters.items():
        if getattr(chunk, key, None) == value:
            continue
        if chunk.metadata.get(key) == value:
            continue
        if key == "chunk_type" and value in set(chunk.metadata.get("source_evidence_types") or []):
            continue
        return False
    return True


def _resolve_image_path(image_ref: str, *, paper_id: str, image_root: Path | None) -> Path:
    normalized = image_ref.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        return path
    candidates = []
    if image_root is not None:
        candidates.extend([image_root / path, image_root / paper_id / path])
        if path.parts and path.parts[0] == ".newsroom":
            candidates.append(Path.cwd() / path)
        newsroom_index = normalized.find(".newsroom/")
        if newsroom_index > 0:
            candidates.append(Path.cwd() / normalized[newsroom_index:])
    candidates.append(Path.cwd() / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _normalized_field_names(field_names: tuple[str, ...] | None) -> tuple[str, ...]:
    if not field_names:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for field_name in field_names:
        normalized = str(field_name).strip().casefold()
        if normalized in FIELD_NAMES and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return tuple(out)


class _DeterministicVisualEmbedding:
    dimension = 64

    def embed_text(self, text: str) -> list[float]:
        return _embed_text(text, dimension=self.dimension)

    def embed_image(self, image_path: str) -> list[float]:
        return _embed_text(Path(image_path).stem.replace("_", " "), dimension=self.dimension)

    def embed_images(self, image_paths: list[str]) -> list[list[float]]:
        return [self.embed_image(path) for path in image_paths]


def _embed_text(text: str, *, dimension: int = 64) -> list[float]:
    vector = [0.0] * dimension
    tokens = [
        token
        for token in "".join(
            char.lower() if char.isalnum() else " "
            for char in text
        ).split()
        if token
    ]
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)


__all__ = [
    "EvidenceEvalOptions",
    "run_evidence_eval_core",
]
