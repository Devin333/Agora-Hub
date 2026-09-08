from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import Condition, Lock
from typing import Any, Callable

from backend.research.application import AskPaperUseCase
from backend.research.domain import stable_research_id
from backend.research.rag.models import ResearchRetrievalGoal
from backend.research.rag.retrieval.paper_retriever import RetrievalRequest
from backend.research.services.tenant_visibility import chunk_visible_to_tenant, public_metrics
from framework.harness.context.models import ContextGraphIdentity
from framework.harness.rag.visibility import evidence_visible_to_tenant
from framework.memory.recall_port import ExecutionMemoryRecallPort
from interfaces.services.paper_rag_factory import PaperRagRuntimeResources
from interfaces.services.paper_rag_transcript_store import SCHEMA_VERSION, PaperRagTranscriptFileStore


LOGGER = logging.getLogger(__name__)


class PaperRagApplicationService:
    """Chunk-based RAG service: retrieve context or produce a gated answer."""

    def __init__(
        self,
        *,
        with_reranker: bool = True,
        retriever: Any | None = None,
        session_factory: Callable[..., Any] | None = None,
        ask_use_case: AskPaperUseCase | None = None,
        transcript_store: Any | None = None,
        runtime_resources: PaperRagRuntimeResources | None = None,
    ) -> None:
        if runtime_resources is not None and not isinstance(
            runtime_resources,
            PaperRagRuntimeResources,
        ):
            raise TypeError("runtime_resources must be PaperRagRuntimeResources")
        needs_runtime_resources = retriever is None or session_factory is None
        actual_resources = runtime_resources
        if actual_resources is None and needs_runtime_resources:
            actual_resources = PaperRagRuntimeResources()
        owned_resources = actual_resources if runtime_resources is None else None
        try:
            self._with_reranker = with_reranker
            if retriever is None:
                if actual_resources is None:
                    raise RuntimeError("Paper RAG retriever resources are unavailable")
                self._retriever = actual_resources.build_research_retriever(
                    with_reranker=with_reranker
                )
            else:
                self._retriever = retriever
            if session_factory is None:
                if actual_resources is None:
                    raise RuntimeError("Paper RAG session resources are unavailable")
                self._session_factory = actual_resources.build_paper_rag_session
            else:
                self._session_factory = session_factory
            self._ask_use_case = ask_use_case or AskPaperUseCase()
            self._transcript_store = transcript_store or PaperRagTranscriptFileStore()
        except BaseException:
            if owned_resources is not None:
                try:
                    owned_resources.close()
                except Exception:
                    pass
            raise
        self._runtime_resources = actual_resources
        self._owned_runtime_resources = owned_resources
        self._lifecycle = Condition(Lock())
        self._active_calls = 0
        self._closing = False
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lifecycle:
            return self._closed

    def get_reranker(self) -> Any:
        with self._operation():
            if self._runtime_resources is None:
                raise RuntimeError("Paper RAG service has no managed reranker")
            return self._runtime_resources.get_reranker()

    def preload_reranker(self) -> None:
        with self._operation():
            if self._runtime_resources is None:
                raise RuntimeError("Paper RAG service has no managed reranker")
            self._runtime_resources.preload_reranker()

    def close(self) -> None:
        with self._lifecycle:
            while self._closing and not self._closed:
                self._lifecycle.wait()
            if self._closed:
                return
            self._closing = True
            while self._active_calls:
                self._lifecycle.wait()
            owned_resources = self._owned_runtime_resources
            self._owned_runtime_resources = None
            self._retriever = None
            self._session_factory = None

        try:
            if owned_resources is not None:
                owned_resources.close()
        finally:
            with self._lifecycle:
                self._closed = True
                self._closing = False
                self._lifecycle.notify_all()

    def rag_ask(
        self,
        paper_id: str,
        question: str,
        *,
        section_index: int = 0,
        limit: int = 5,
        generate: bool = False,
        gated: bool = True,
        tenant_id: str | None = None,
        user_id: str | None = None,
        memory_namespace: str | None = None,
        graph_identity: ContextGraphIdentity | None = None,
        memory_recall: ExecutionMemoryRecallPort | None = None,
    ) -> dict[str, Any]:
        with self._operation():
            return self._rag_ask(
                paper_id,
                question,
                section_index=section_index,
                limit=limit,
                generate=generate,
                gated=gated,
                tenant_id=tenant_id,
                user_id=user_id,
                memory_namespace=memory_namespace,
                graph_identity=graph_identity,
                memory_recall=memory_recall,
            )

    def _rag_ask(
        self,
        paper_id: str,
        question: str,
        *,
        section_index: int,
        limit: int,
        generate: bool,
        gated: bool,
        tenant_id: str | None,
        user_id: str | None,
        memory_namespace: str | None,
        graph_identity: ContextGraphIdentity | None,
        memory_recall: ExecutionMemoryRecallPort | None,
    ) -> dict[str, Any]:
        if generate and not gated:
            raise ValueError(
                "legacy direct paper RAG answer generation has been removed; "
                "use gated Harness generation"
            )
        if generate:
            return self._gated_ask(
                paper_id,
                question,
                section_index=section_index,
                limit=limit,
                tenant_id=tenant_id,
                user_id=user_id,
                memory_namespace=memory_namespace,
                graph_identity=graph_identity,
                memory_recall=memory_recall,
            )
        if memory_recall is not None:
            raise ValueError("execution-bound memory requires gated Harness generation")
        result = self._retriever.retrieve(RetrievalRequest(
            paper_id=paper_id,
            question=question,
            current_section_index=section_index,
            limit=limit,
            filters=_tenant_filters(tenant_id),
        ))
        passages, filtered_count = _passages_from_retrieval(result, tenant_id=tenant_id)
        payload: dict[str, Any] = {
            "paper_id": paper_id,
            "question": question,
            "intent": result.intent,
            "passages": passages,
            "metrics": _retrieval_metrics(
                result,
                tenant_id=tenant_id,
                user_id=user_id,
                memory_namespace=memory_namespace,
                tenant_filtered_passage_count=filtered_count,
            ),
        }
        return payload

    @contextmanager
    def _operation(self):
        with self._lifecycle:
            if self._closing or self._closed:
                raise RuntimeError("Paper RAG service is closed")
            self._active_calls += 1
        try:
            yield
        finally:
            with self._lifecycle:
                self._active_calls -= 1
                if self._active_calls == 0:
                    self._lifecycle.notify_all()

    def _gated_ask(
        self,
        paper_id: str,
        question: str,
        *,
        section_index: int,
        limit: int,
        tenant_id: str | None,
        user_id: str | None,
        memory_namespace: str | None,
        graph_identity: ContextGraphIdentity | None,
        memory_recall: ExecutionMemoryRecallPort | None,
    ) -> dict[str, Any]:
        if not isinstance(graph_identity, ContextGraphIdentity):
            raise ValueError(
                "Graph-bound paper RAG generation requires an exact Graph activity identity"
            )
        if not graph_identity.has_physical_activity:
            raise ValueError(
                "Graph-bound paper RAG generation requires a physical activity identity"
            )
        session_kwargs: dict[str, Any] = {
            "with_reranker": self._with_reranker,
            "with_answer_worker": True,
        }
        if memory_recall is not None:
            if not isinstance(memory_recall, ExecutionMemoryRecallPort):
                raise TypeError("RAG memory requires execution-bound recall")
            memory_recall.validate_execution(graph_identity.to_graph_execution_identity())
            session_kwargs["memory_recall"] = memory_recall
        session = self._session_factory(**session_kwargs)
        goal = self._ask_use_case.build_paper_ask_goal(
            paper_id=paper_id,
            question=question,
            goal_id=stable_research_id(
                "paper_rag_ask",
                graph_identity.run_id,
                graph_identity.activity_id,
                paper_id,
                question,
            ),
            tenant_id=tenant_id,
            user_id=user_id,
            memory_namespace=memory_namespace,
        )
        result = session.run(
            goal,
            graph_identity=graph_identity,
            session_id=stable_research_id(
                "paper_rag_session",
                graph_identity.run_id,
                graph_identity.activity_id,
                paper_id,
                question,
            ),
            current_section_index=section_index,
        )
        transcript_artifact = _persist_transcript_best_effort(
            self._transcript_store,
            result.transcript,
        )
        return _gated_payload(
            paper_id=paper_id,
            question=question,
            limit=limit,
            goal=goal,
            result=result,
            transcript_artifact=transcript_artifact,
        )


def _passages_from_retrieval(result: Any, *, tenant_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
    visible_chunks = []
    filtered_count = 0
    for chunk in result.child_chunks:
        if chunk_visible_to_tenant(chunk, tenant_id=tenant_id):
            visible_chunks.append(chunk)
        else:
            filtered_count += 1
    passages = [
        {
            "chunk_id": chunk.chunk_id,
            "section_title": chunk.section_title,
            "chunk_type": chunk.chunk_type,
            "content": chunk.content,
        }
        for chunk in visible_chunks
    ]
    return passages, filtered_count


def _retrieval_metrics(
    result: Any,
    *,
    tenant_id: str | None,
    user_id: str | None,
    memory_namespace: str | None,
    tenant_filtered_passage_count: int,
) -> dict[str, Any]:
    metrics = dict(result.metadata)
    if tenant_id:
        metrics["tenant_id"] = tenant_id
    if user_id:
        metrics["user_id"] = user_id
    if memory_namespace:
        metrics["memory_namespace"] = memory_namespace
    if tenant_filtered_passage_count:
        metrics["tenant_filtered_passage_count"] = tenant_filtered_passage_count
    return public_metrics(metrics)


def _tenant_filters(tenant_id: str | None) -> dict[str, Any]:
    tenant = str(tenant_id or "").strip()
    return {"tenant_id": tenant} if tenant else {}


def _gated_payload(
    *,
    paper_id: str,
    question: str,
    limit: int,
    goal: ResearchRetrievalGoal,
    result: Any,
    transcript_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tenant_id = str(goal.metadata.get("tenant_id") or "").strip() or None
    pack = result.context_pack
    visible_accepted = _visible_pack_evidence(pack, tenant_id=tenant_id, field_name="accepted_evidence")
    accepted_ids = {item.evidence_id for item in visible_accepted}
    pack_contains_hidden_accepted = bool(
        pack is not None and len(visible_accepted) != len(pack.accepted_evidence)
    )
    answer = result.answer
    if not _answer_is_visible(
        answer,
        accepted_ids=accepted_ids,
        pack_contains_hidden_accepted=pack_contains_hidden_accepted,
    ):
        answer = None
    return {
        "paper_id": paper_id,
        "question": question,
        "intent": goal.metadata.get("intent", ""),
        "status": result.status.value,
        "generation_mode": "gated_harness",
        "answer": answer.answer_text if answer and not answer.abstained else None,
        "answer_candidate": answer.to_dict() if answer else None,
        "claims": [claim.to_dict() for claim in answer.claims] if answer else [],
        "citations": _citations_from_answer(answer, visible_accepted),
        "passages": _passages_from_pack(visible_accepted, limit=limit),
        "metrics": _gated_metrics(result, pack, accepted_evidence_count=len(visible_accepted)),
        "decision": result.decision.to_dict(),
        "gate_results": list(result.decision.gate_results),
        "transcript_id": result.transcript.transcript_id,
        "transcript_artifact": transcript_artifact,
        "context_pack": _context_pack_summary(pack, tenant_id=tenant_id),
    }


def _persist_transcript_best_effort(store: Any, transcript: Any) -> dict[str, Any]:
    try:
        return store.persist(transcript).to_dict()
    except Exception as exc:
        transcript_id = str(getattr(transcript, "transcript_id", "") or "")
        LOGGER.warning(
            "failed to persist Paper RAG transcript",
            extra={"transcript_id": transcript_id},
            exc_info=True,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "transcript_id": transcript_id,
            "path": None,
            "persisted": False,
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc),
            },
        }


def _gated_metrics(
    result: Any,
    pack: Any | None,
    *,
    accepted_evidence_count: int,
) -> dict[str, Any]:
    session_metrics = result.metrics.to_dict() if getattr(result, "metrics", None) is not None else {}
    return public_metrics({
        **session_metrics,
        "context_pack_id": pack.pack_id if pack else session_metrics.get("context_pack_id"),
        "accepted_evidence_count": accepted_evidence_count,
        "decision_type": result.decision.decision_type.value,
    })


def _passages_from_pack(accepted_evidence: tuple[Any, ...], *, limit: int) -> list[dict[str, Any]]:
    passages = []
    for evidence in accepted_evidence[:limit]:
        passages.append({
            "evidence_id": evidence.evidence_id,
            "chunk_id": evidence.metadata.get("rag_chunk_id", evidence.evidence_id),
            "section_title": evidence.metadata.get("section_title", evidence.title),
            "chunk_type": evidence.metadata.get("chunk_type", evidence.evidence_type),
            "content": evidence.summary,
            "source_locator": evidence.metadata.get("source_locator") or evidence.source_ref,
        })
    return passages


def _citations_from_answer(answer: Any | None, accepted_evidence: tuple[Any, ...]) -> list[dict[str, Any]]:
    if answer is None:
        return []
    by_id = {item.evidence_id: item for item in accepted_evidence}
    citations = []
    for evidence_id in answer.cited_evidence_ids:
        evidence = by_id.get(evidence_id)
        citations.append({
            "evidence_id": evidence_id,
            "chunk_id": evidence.metadata.get("rag_chunk_id", evidence_id) if evidence else evidence_id,
            "span_refs": _citation_span_refs(answer, evidence_id, evidence),
            "source_locator": (
                evidence.metadata.get("source_locator")
                or evidence.source_ref
                if evidence
                else ""
            ),
            "title": evidence.title if evidence else "",
        })
    return citations


def _citation_span_refs(answer: Any, evidence_id: str, evidence: Any | None) -> list[str]:
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


def _context_pack_summary(pack: Any | None, *, tenant_id: str | None) -> dict[str, Any] | None:
    if pack is None:
        return None
    accepted = _visible_pack_evidence(pack, tenant_id=tenant_id, field_name="accepted_evidence")
    rejected = _visible_pack_evidence(pack, tenant_id=tenant_id, field_name="rejected_evidence")
    conflicting = _visible_pack_evidence(pack, tenant_id=tenant_id, field_name="conflicting_evidence")
    hidden = (
        len(accepted) != len(pack.accepted_evidence)
        or len(rejected) != len(pack.rejected_evidence)
        or len(conflicting) != len(pack.conflicting_evidence)
    )
    return {
        "pack_id": pack.pack_id,
        "accepted_evidence_ids": [item.evidence_id for item in accepted],
        "rejected_evidence_ids": [item.evidence_id for item in rejected],
        "gap_report": pack.gap_report,
        "assembly_summary": "Visibility-filtered context pack." if hidden else pack.assembly_summary,
    }


def _visible_pack_evidence(
    pack: Any | None,
    *,
    tenant_id: str | None,
    field_name: str,
) -> tuple[Any, ...]:
    if pack is None:
        return ()
    return tuple(
        candidate
        for candidate in getattr(pack, field_name, ())
        if evidence_visible_to_tenant(candidate, tenant_id=tenant_id)
    )


def _answer_is_visible(
    answer: Any | None,
    *,
    accepted_ids: set[str],
    pack_contains_hidden_accepted: bool,
) -> bool:
    if answer is None:
        return True
    if pack_contains_hidden_accepted:
        return False
    cited_ids = {str(item) for item in answer.cited_evidence_ids}
    claim_ids = {
        str(evidence_id)
        for claim in answer.claims
        for evidence_id in claim.evidence_ids
    }
    return cited_ids.union(claim_ids).issubset(accepted_ids)


__all__ = ["PaperRagApplicationService"]
