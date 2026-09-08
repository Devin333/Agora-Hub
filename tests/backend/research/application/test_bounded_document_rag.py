from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier, Lock
from typing import Any, Callable

import pytest

from backend.research.application.bounded_document_rag import BoundedDocumentRAGRuntime
from framework.harness.control_plane.errors import HarnessValidationError
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.document.models import PaperChunk
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection
from framework.harness.rag.models import (
    EvidenceCandidate,
    RAGBudget,
    RAGBudgetSnapshot,
    RAGContextPack,
    RAGSessionSpec,
    RAGSessionStatus,
    RAGTranscript,
    RetrievalGoal,
)
from framework.harness.rag.policy import RAGDecision, RAGDecisionType
from framework.harness.rag.session import RAGSessionResult
from framework.shared.graph_identity import GraphExecutionIdentity


FIXED_TIME = datetime(2026, 7, 19, 8, 30, tzinfo=UTC)


class _ExecutionMemoryCapability:
    def __init__(self, execution_identity: GraphExecutionIdentity) -> None:
        self._execution_identity = execution_identity
        self.validated: list[GraphExecutionIdentity] = []

    @property
    def execution_identity(self) -> GraphExecutionIdentity:
        return self._execution_identity

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        self.validated.append(execution_identity)

    def recall(self, query, *, policy=None):
        raise AssertionError("capture session does not perform memory recall")


class _MemoryChunkStore:
    """Intentionally ignores filters so the scoped wrapper must enforce them."""

    def __init__(self, chunks: list[PaperChunk] | None = None) -> None:
        self._lock = Lock()
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks or []}
        self.indexed_batches: list[list[PaperChunk]] = []
        self.search_filters: list[dict[str, Any]] = []
        self.ensure_calls = 0

    def ensure_collection(self) -> None:
        with self._lock:
            self.ensure_calls += 1

    def index_chunks(self, chunks: list[PaperChunk]) -> None:
        with self._lock:
            copied = list(chunks)
            self.indexed_batches.append(copied)
            self._chunks.update({chunk.chunk_id: chunk for chunk in copied})

    def delete_paper_chunks(self, paper_id: str) -> None:
        raise AssertionError("bounded RAG must not delete another run's paper chunks")

    def search_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
    ) -> list[PaperChunk]:
        del query_text, score_threshold
        with self._lock:
            self.search_filters.append(dict(filters or {}))
            chunks = [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]
        return sorted(chunks, key=lambda chunk: chunk.chunk_id)[:limit]

    def search_with_scores(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 30,
    ) -> list[tuple[PaperChunk, float]]:
        return [
            (chunk, 0.75)
            for chunk in self.search_chunks(
                paper_id,
                query_text,
                filters=filters,
                limit=limit,
            )
        ]

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        with self._lock:
            return self._chunks.get(chunk_id)

    def get_parent_chunk(self, chunk: PaperChunk) -> PaperChunk | None:
        return self.get_chunk(chunk.parent_chunk_id) if chunk.parent_chunk_id else None

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        with self._lock:
            chunks = [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]
        return sorted(chunks, key=lambda chunk: chunk.chunk_id)


class _RelationalChunker:
    def chunk(self, document: ResearchDocument, parse_source: str) -> list[PaperChunk]:
        parent_id = f"chunk-{document.paper_id}-parent"
        child_id = f"chunk-{document.paper_id}-child"
        source_ref = document.sections[0].source_ref
        parent = PaperChunk(
            chunk_id=parent_id,
            paper_id=document.paper_id,
            parse_source=parse_source,
            content="Method parent evidence.",
            section_title="Method",
            section_role=["method"],
            section_index=0,
            references=[child_id],
            metadata={
                "source_ref": source_ref,
                "source_locator": f"paper://{document.paper_id}/sections/method",
                "section_id": "sec-method",
                "nearby_context_chunk_id": child_id,
                "referenced_by_chunks": [
                    {"chunk_id": child_id, "target_chunk_id": parent_id}
                ],
                "formula_context_chunk_ids": [child_id],
                "chunk_lookup": {child_id: {"id": parent_id}},
                "main_span": {"start": 0, "end": 23},
                "content_span_unit": "char_offset",
            },
        )
        child = PaperChunk(
            chunk_id=child_id,
            paper_id=document.paper_id,
            parse_source=parse_source,
            parent_chunk_id=parent_id,
            content="Child evidence with a stable span.",
            section_title="Method",
            section_role=["method"],
            section_index=0,
            references=[parent_id],
            metadata={
                "source_ref": source_ref,
                "source_locator": f"paper://{document.paper_id}/sections/method#paragraph=1",
                "section_id": "sec-method",
                "overlap_origin_chunk_id": parent_id,
                "parent_table_chunk_id": parent_id,
                "nested": [{"source_parent_chunk_id": parent_id}],
                "main_span": {"start": 0, "end": 34},
                "content_span_unit": "char_offset",
            },
        )
        return [parent, child]


class _CaptureFactory:
    def __init__(
        self,
        result_builder: Callable[[RAGSessionSpec], RAGSessionResult],
        *,
        barrier: Barrier | None = None,
    ) -> None:
        self._result_builder = result_builder
        self._barrier = barrier
        self._lock = Lock()
        self.specs: dict[str, RAGSessionSpec] = {}
        self.stores: dict[str, Any] = {}

    def __call__(self, store: Any) -> Any:
        outer = self

        class _Session:
            def run_spec(
                self,
                spec: RAGSessionSpec,
                *,
                current_section_index: int = 0,
            ) -> RAGSessionResult:
                assert current_section_index == 0
                with outer._lock:
                    outer.specs[spec.session_id] = spec
                    outer.stores[spec.session_id] = store
                if outer._barrier is not None:
                    outer._barrier.wait(timeout=15)
                return outer._result_builder(spec)

        return _Session()


def _document(paper_id: str = "2401.00001") -> ResearchDocument:
    root_ref = f"arxiv://{paper_id}/latex"
    return ResearchDocument(
        paper_id=paper_id,
        source_hash=f"sha256:{paper_id}",
        sections=[
            ResearchSection(
                section_id="sec-method",
                title="Method",
                text="The deterministic Harness controls routing and verification.",
                source_ref=f"paper://{paper_id}/sections/method",
                metadata={
                    "source_locator": f"paper://{paper_id}/sections/method",
                },
            )
        ],
        lineage=SourceLineage(
            source_refs=[root_ref],
            source_hash=f"sha256:{paper_id}",
            artifact_refs=[f"artifact://{paper_id}/source"],
            collected_at=FIXED_TIME,
        ),
        metadata={"parse_source": "latex"},
    )


def _spec(
    *,
    paper_id: str = "2401.00001",
    run_id: str = "run-a",
    session_id: str = "session-a",
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
    source_refs: tuple[str, ...] | None = None,
    required_types: tuple[str, ...] = ("method",),
    budget: RAGBudget | None = None,
) -> RAGSessionSpec:
    allowed_refs = source_refs or (f"arxiv://{paper_id}/latex",)
    memory_namespace = f"research:tenant:{tenant_id or 'public'}:user:{user_id}"
    scope_metadata = {
        "paper_id": paper_id,
        "target_sections": ["sec-method"],
        "target_claims": ["claim-routing"],
        "user_id": user_id,
        "memory_namespace": memory_namespace,
    }
    source_policy: dict[str, Any] = {
        "allowed_source_refs": list(allowed_refs),
        "memory_namespace": memory_namespace,
    }
    metadata: dict[str, Any] = {
        "paper_id": paper_id,
        "user_id": user_id,
        "memory_namespace": memory_namespace,
    }
    if tenant_id:
        scope_metadata["tenant_id"] = tenant_id
        source_policy["tenant_id"] = tenant_id
        metadata["tenant_id"] = tenant_id
    return RAGSessionSpec(
        session_id=session_id,
        graph_identity=build_paper_analysis_context_graph_identity(
            run_id=run_id,
            stage_id="run_research_rag",
        ),
        goal=RetrievalGoal(
            goal_id=f"goal-{run_id}",
            question=f"What evidence is available for {paper_id}?",
            required_evidence_types=required_types,
            target_entities=("sec-method", "claim-routing"),
            known_context_refs=allowed_refs,
            constraints={"paper_only": True},
            metadata=scope_metadata,
        ),
        allowed_corpora=("research_papers",),
        allowed_memory_namespaces=(memory_namespace,),
        allowed_tools=("retrieval.search", "retrieval.read_source"),
        source_policy=source_policy,
        budget=budget or RAGBudget.safe_default(),
        context_policy={"projection": "research_rag_context", "marker": run_id},
        generation_policy={"enabled": False, "marker": run_id},
        metadata=metadata,
    )


def _candidate(
    spec: RAGSessionSpec,
    *,
    suffix: str,
    evidence_type: str,
    source_ref: str | None = None,
    paper_id: str | None = None,
    rejection_reason: str = "",
    conflict: bool = False,
) -> EvidenceCandidate:
    resolved_paper_id = paper_id or str(spec.metadata["paper_id"])
    canonical_id = f"chunk-{resolved_paper_id}-{suffix}"
    metadata: dict[str, Any] = {
        "canonical_chunk_id": canonical_id,
        "paper_id": resolved_paper_id,
        "rag_document_id": resolved_paper_id,
        "run_id": spec.run_id,
        "tenant_id": spec.metadata.get("tenant_id", ""),
        "user_id": spec.metadata.get("user_id", ""),
        "section_id": "sec-method",
        "main_span": {"start": 3, "end": 17},
        "content_span_unit": "char_offset",
        "rag_score": 0.83,
        "rag_score_breakdown": {"final_score": 0.83, "field_score": 0.7},
    }
    if rejection_reason:
        metadata["rejection_reason"] = rejection_reason
    if conflict:
        metadata["conflict"] = True
    exact_ref = source_ref or f"paper://{resolved_paper_id}/chunks/{canonical_id}#paragraph=1"
    return EvidenceCandidate(
        evidence_id=f"physical-{spec.run_id}-{suffix}",
        title=f"Evidence {suffix}",
        summary=f"Grounded summary for {suffix}.",
        source_ref=exact_ref,
        span_refs=(exact_ref,),
        evidence_type=evidence_type,
        claim_refs=("claim-routing",),
        confidence=0.83,
        freshness="fresh",
        lineage=(f"paper://{resolved_paper_id}",),
        artifact_refs=(f"artifact://{spec.run_id}/{suffix}",),
        metadata=metadata,
    )


def _result(
    spec: RAGSessionSpec,
    *,
    accepted: tuple[EvidenceCandidate, ...] = (),
    rejected: tuple[EvidenceCandidate, ...] = (),
    conflicting: tuple[EvidenceCandidate, ...] = (),
    gap_report: dict[str, Any] | None = None,
    with_pack: bool = True,
    status: RAGSessionStatus = RAGSessionStatus.SUCCEEDED,
) -> RAGSessionResult:
    if not with_pack and status == RAGSessionStatus.SUCCEEDED:
        status = RAGSessionStatus.INSUFFICIENT_EVIDENCE
    snapshot = RAGBudgetSnapshot(
        rounds_used=1,
        queries_used=1,
        source_reads_used=len(accepted),
        context_items_used=len(accepted),
        context_tokens_used=37,
        worker_calls_used=1,
    )
    gap = dict(gap_report or {})
    pack = None
    if with_pack:
        pack = RAGContextPack(
            pack_id=f"rag-context://{spec.session_id}",
            query=spec.goal.question,
            context_refs=spec.goal.known_context_refs,
            goal=spec.goal,
            accepted_evidence=accepted,
            rejected_evidence=rejected,
            conflicting_evidence=conflicting,
            memory_context=(
                {
                    "memory_ref": f"memory://{spec.run_id}",
                    "namespace": spec.allowed_memory_namespaces[0],
                    "summary": f"memory for {spec.run_id}",
                },
            ),
            source_refs=tuple(
                candidate.source_ref
                for candidate in (*accepted, *rejected, *conflicting)
            ),
            artifact_refs=(f"artifact://{spec.run_id}/pack",),
            evidence_trace=(
                {
                    "status": "accepted",
                    "run_id": spec.run_id,
                    "evidence_ids": [candidate.evidence_id for candidate in accepted],
                },
            ),
            gap_report=gap,
            budget_snapshot=snapshot,
            metadata={"run_marker": spec.run_id},
        )
    decision_type = (
        RAGDecisionType.RETURN_CONTEXT_PACK
        if status == RAGSessionStatus.SUCCEEDED
        else RAGDecisionType.INSUFFICIENT_EVIDENCE
    )
    return RAGSessionResult(
        status=status,
        context_pack=pack,
        transcript=RAGTranscript(
            transcript_id=f"rag-transcript://{spec.session_id}/fixture",
            session_id=spec.session_id,
            events=(
                {
                    "event_type": "rag_session_started",
                    "payload": {"session": spec.to_dict()},
                },
            ),
            status=status,
            created_at=FIXED_TIME,
        ),
        decision=RAGDecision(
            decision_type,
            "verified result" if status == RAGSessionStatus.SUCCEEDED else "evidence is incomplete",
            budget_snapshot=snapshot,
            metadata={"run_id": spec.run_id},
        ),
        accepted_evidence=accepted,
        rejected_evidence=rejected,
        conflicting_evidence=conflicting,
        gap_report=gap,
        budget_snapshot=snapshot,
    )


def _mutate_result_identity(
    result: RAGSessionResult,
    identity_case: str,
) -> RAGSessionResult:
    pack = result.context_pack
    assert pack is not None
    if identity_case == "transcript_session":
        return replace(
            result,
            transcript=replace(result.transcript, session_id="foreign-session"),
        )
    if identity_case == "transcript_status":
        return replace(
            result,
            transcript=replace(
                result.transcript,
                status=RAGSessionStatus.FAILED,
            ),
        )
    if identity_case == "transcript_ref":
        return replace(
            result,
            transcript=replace(
                result.transcript,
                transcript_id="rag-transcript://foreign-session/fixture",
            ),
        )
    if identity_case == "started_missing":
        return replace(
            result,
            transcript=replace(result.transcript, events=()),
        )
    if identity_case == "started_duplicate":
        return replace(
            result,
            transcript=replace(
                result.transcript,
                events=(result.transcript.events[0], result.transcript.events[0]),
            ),
        )
    if identity_case.startswith("started_"):
        field = identity_case.removeprefix("started_")
        started_event = dict(result.transcript.events[0])
        payload = dict(started_event["payload"])
        session_payload = dict(payload["session"])
        session_payload[field] = f"foreign-{field}"
        payload["session"] = session_payload
        started_event["payload"] = payload
        return replace(
            result,
            transcript=replace(result.transcript, events=(started_event,)),
        )
    if identity_case == "pack_session":
        return replace(
            result,
            context_pack=replace(pack, pack_id="rag-context://foreign-session"),
        )
    if identity_case == "terminal_decision":
        return replace(
            result,
            decision=replace(
                result.decision,
                decision_type=RAGDecisionType.FAILED,
            ),
        )
    if identity_case == "decision_budget":
        return replace(
            result,
            decision=replace(
                result.decision,
                budget_snapshot=RAGBudgetSnapshot(rounds_used=99),
            ),
        )
    if identity_case == "terminal_pack":
        return replace(result, context_pack=None)
    if identity_case == "pack_query":
        return replace(result, context_pack=replace(pack, query="foreign query"))
    if identity_case == "pack_goal":
        assert pack.goal is not None
        return replace(
            result,
            context_pack=replace(
                pack,
                goal=replace(pack.goal, goal_id="foreign-goal"),
            ),
        )
    if identity_case == "pack_context_refs":
        return replace(
            result,
            context_pack=replace(pack, context_refs=("arxiv://9999.99999/latex",)),
        )
    if identity_case == "pack_budget":
        return replace(
            result,
            context_pack=replace(
                pack,
                budget_snapshot=RAGBudgetSnapshot(rounds_used=99),
            ),
        )
    if identity_case == "pack_context_envelope":
        return replace(
            result,
            context_pack=replace(
                pack,
                metadata={
                    **pack.metadata,
                    "context_envelope_id": "context://rag/foreign-session",
                },
            ),
        )
    if identity_case == "pack_context_policy":
        return replace(
            result,
            context_pack=replace(
                pack,
                metadata={**pack.metadata, "context_policy": {"foreign": True}},
            ),
        )
    if identity_case == "pack_metadata":
        return replace(
            result,
            context_pack=replace(
                pack,
                metadata={**pack.metadata, "run_id": "foreign-run"},
            ),
        )
    if identity_case == "pack_trace":
        trace = dict(pack.evidence_trace[0])
        trace["run_id"] = "foreign-run"
        return replace(
            result,
            context_pack=replace(pack, evidence_trace=(trace,)),
        )
    if identity_case == "memory_namespace":
        memory_hit = dict(pack.memory_context[0])
        memory_hit["namespace"] = "research:tenant:foreign:user:foreign"
        return replace(
            result,
            context_pack=replace(pack, memory_context=(memory_hit,)),
        )
    if identity_case == "pack_artifact":
        return replace(
            result,
            context_pack=replace(pack, artifact_refs=("artifact://foreign-run/pack",)),
        )
    raise AssertionError(f"unsupported identity test case: {identity_case}")


def test_uses_supplied_spec_identity_and_remaps_every_chunk_reference() -> None:
    store = _MemoryChunkStore()
    factory = _CaptureFactory(lambda spec: _result(spec, with_pack=False))
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    document = _document()
    spec_a = _spec(run_id="run-a", session_id="session-a")
    spec_b = _spec(run_id="run-b", session_id="session-b")
    supplied_a = spec_a.to_dict()
    supplied_b = spec_b.to_dict()

    runtime.run(session_spec=spec_a, document=document)
    runtime.run(session_spec=spec_b, document=document)
    runtime.run(session_spec=spec_a, document=document)

    assert factory.specs["session-a"] is spec_a
    assert factory.specs["session-b"] is spec_b
    assert spec_a.to_dict() == supplied_a
    assert spec_b.to_dict() == supplied_b
    first = {chunk.metadata["canonical_chunk_id"]: chunk for chunk in store.indexed_batches[0]}
    second = {chunk.metadata["canonical_chunk_id"]: chunk for chunk in store.indexed_batches[1]}
    replayed = {chunk.metadata["canonical_chunk_id"]: chunk for chunk in store.indexed_batches[2]}
    assert first.keys() == second.keys()
    assert all(first[key].chunk_id != second[key].chunk_id for key in first)
    assert {key: chunk.chunk_id for key, chunk in replayed.items()} == {
        key: chunk.chunk_id for key, chunk in first.items()
    }

    parent = first[f"chunk-{document.paper_id}-parent"]
    child = first[f"chunk-{document.paper_id}-child"]
    assert parent.chunk_id != parent.metadata["canonical_chunk_id"]
    assert child.parent_chunk_id == parent.chunk_id
    assert child.references == [parent.chunk_id]
    assert parent.references == [child.chunk_id]
    assert parent.metadata["nearby_context_chunk_id"] == child.chunk_id
    assert parent.metadata["referenced_by_chunks"] == [
        {"chunk_id": child.chunk_id, "target_chunk_id": parent.chunk_id}
    ]
    assert parent.metadata["formula_context_chunk_ids"] == [child.chunk_id]
    assert parent.metadata["chunk_lookup"] == {child.chunk_id: {"id": parent.chunk_id}}
    assert child.metadata["overlap_origin_chunk_id"] == parent.chunk_id
    assert child.metadata["parent_table_chunk_id"] == parent.chunk_id
    assert child.metadata["nested"] == [{"source_parent_chunk_id": parent.chunk_id}]
    assert child.metadata["canonical_chunk_id"] == f"chunk-{document.paper_id}-child"
    assert child.metadata["run_id"] == spec_a.run_id
    assert child.metadata["session_id"] == spec_a.session_id
    assert child.metadata["graph_ref"] == spec_a.graph_identity.graph_ref
    assert child.metadata["stage_id"] == spec_a.stage_id
    assert child.metadata["tenant_id"] == "tenant-a"
    assert child.metadata["source_hash"] == document.source_hash


def test_execution_memory_capability_is_validated_per_call_and_never_cached() -> None:
    store = _MemoryChunkStore()
    base_factory = _CaptureFactory(lambda spec: _result(spec))
    authorized_calls: list[_ExecutionMemoryCapability] = []

    def authorized_factory(scoped_store, capability):
        authorized_calls.append(capability)
        return base_factory(scoped_store)

    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=base_factory,
        authorized_session_factory=authorized_factory,
    )
    unbound = _spec(run_id="memory-capability", session_id="memory-capability")
    identity = unbound.graph_identity.with_physical_activity(
        node_id="run_research_rag",
        node_instance_id="run_research_rag:1",
        activity_id="hga_memory_capability",
        activity_attempt=1,
    )
    spec = replace(unbound, graph_identity=identity)
    capability = _ExecutionMemoryCapability(identity.to_graph_execution_identity())

    runtime.run(
        session_spec=spec,
        document=_document(),
        memory_recall=capability,
    )
    runtime.run(session_spec=spec, document=_document())

    assert authorized_calls == [capability]
    assert capability.validated == [identity.to_graph_execution_identity()]
    assert len(store.indexed_batches) == 2
    assert runtime.last_context_pack is not None
    assert runtime.context_pack_for_run(spec.run_id) is not None

    foreign = _ExecutionMemoryCapability(
        replace(identity.to_graph_execution_identity(), attempt=2)
    )
    with pytest.raises(HarnessValidationError, match="outside the physical Graph execution"):
        runtime.run(
            session_spec=spec,
            document=_document(),
            memory_recall=foreign,
        )
    assert foreign.validated == []
    assert len(store.indexed_batches) == 2
    assert runtime.last_context_pack is None
    assert runtime.context_pack_for_run(spec.run_id) is None


@pytest.mark.parametrize(
    "identity_case",
    [
        "transcript_session",
        "transcript_status",
        "transcript_ref",
        "started_missing",
        "started_duplicate",
        "started_session_id",
        "started_run_id",
        "started_graph_identity",
        "pack_session",
        "terminal_decision",
        "decision_budget",
        "terminal_pack",
        "pack_query",
        "pack_goal",
        "pack_context_refs",
        "pack_budget",
        "pack_context_envelope",
        "pack_context_policy",
        "pack_metadata",
        "pack_trace",
        "memory_namespace",
        "pack_artifact",
    ],
)
def test_result_identity_mismatch_fails_closed_before_projection(
    identity_case: str,
) -> None:
    document = _document()
    spec = _spec()
    result = _mutate_result_identity(_result(spec), identity_case)
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=_CaptureFactory(lambda supplied: result),
    )

    with pytest.raises(ValueError, match="does not match supplied RAGSessionSpec"):
        runtime.run(session_spec=spec, document=document)

    assert runtime.last_context_pack is None


def test_scoped_store_rechecks_search_get_list_and_parent_when_backend_ignores_filters() -> None:
    store = _MemoryChunkStore()
    factory = _CaptureFactory(lambda spec: _result(spec, with_pack=False))
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    document = _document()
    spec_a = _spec(run_id="run-a", session_id="session-a")
    spec_b = _spec(run_id="run-b", session_id="session-b")
    runtime.run(session_spec=spec_a, document=document)
    runtime.run(session_spec=spec_b, document=document)

    scoped_a = factory.stores["session-a"]
    run_a_chunks = scoped_a.search_chunks(document.paper_id, "method", limit=20)
    assert run_a_chunks
    assert {chunk.metadata["run_id"] for chunk in run_a_chunks} == {"run-a"}
    assert store.search_filters[-1] == {
        "paper_id": document.paper_id,
        "run_id": "run-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
    }
    assert scoped_a.search_chunks(
        document.paper_id,
        "method",
        filters={"run_id": "run-b"},
    ) == []
    assert scoped_a.search_with_scores(
        document.paper_id,
        "method",
        filters={"tenant_id": "tenant-b"},
    ) == []
    assert scoped_a.search_chunks(
        document.paper_id,
        "method",
        filters={"user_id": "user-b"},
    ) == []

    current_child = next(chunk for chunk in run_a_chunks if chunk.parent_chunk_id)
    current_parent = scoped_a.get_parent_chunk(current_child)
    assert current_parent is not None
    assert current_parent.chunk_id == current_child.parent_chunk_id
    run_b_chunk = store.indexed_batches[1][0]
    assert scoped_a.get_chunk(run_b_chunk.chunk_id) is None
    assert scoped_a.get_parent_chunk(run_b_chunk) is None
    assert {chunk.metadata["run_id"] for chunk in scoped_a.list_chunks(document.paper_id)} == {"run-a"}
    assert scoped_a.list_chunks("another-paper") == []


def test_public_scope_rejects_tenant_owned_chunks_even_when_backend_returns_them() -> None:
    store = _MemoryChunkStore()
    factory = _CaptureFactory(lambda spec: _result(spec, with_pack=False))
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    document = _document()
    public_spec = _spec(
        run_id="run-public",
        session_id="session-public",
        tenant_id="",
    )
    tenant_spec = _spec(
        run_id="run-tenant",
        session_id="session-tenant",
        tenant_id="tenant-a",
    )
    runtime.run(session_spec=public_spec, document=document)
    runtime.run(session_spec=tenant_spec, document=document)

    public_store = factory.stores["session-public"]
    visible = public_store.list_chunks(document.paper_id)
    assert visible
    assert all(not chunk.metadata.get("tenant_id") for chunk in visible)
    assert public_store.search_chunks(
        document.paper_id,
        "method",
        filters={"tenant_id": "tenant-a"},
    ) == []


def test_tenant_scope_replaces_stale_aliases_with_canonical_tenant_id() -> None:
    class _StaleTenantAliasChunker(_RelationalChunker):
        def chunk(
            self,
            document: ResearchDocument,
            parse_source: str,
        ) -> list[PaperChunk]:
            return [
                chunk.model_copy(
                    update={
                        "metadata": {
                            **chunk.metadata,
                            "tenant": "stale-tenant",
                            "workspace_id": "stale-workspace",
                        }
                    }
                )
                for chunk in super().chunk(document, parse_source)
            ]

    store = _MemoryChunkStore()
    factory = _CaptureFactory(lambda spec: _result(spec, with_pack=False))
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_StaleTenantAliasChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    document = _document()
    spec = _spec(tenant_id="tenant-a")

    runtime.run(session_spec=spec, document=document)

    indexed = store.indexed_batches[0]
    assert indexed
    assert all(chunk.metadata["tenant_id"] == "tenant-a" for chunk in indexed)
    assert all("tenant" not in chunk.metadata for chunk in indexed)
    assert all("workspace_id" not in chunk.metadata for chunk in indexed)
    scoped_store = factory.stores[spec.session_id]
    assert len(scoped_store.list_chunks(document.paper_id)) == len(indexed)
    assert scoped_store.search_chunks(
        document.paper_id,
        "method",
        filters={"tenant": "tenant-a"},
    )


def test_projects_typed_evidence_gaps_budget_and_transcript_without_synthetic_evidence() -> None:
    document = _document()
    budget = RAGBudget(
        max_rounds=3,
        max_replans=2,
        max_queries=7,
        max_source_reads=9,
        max_memory_hits=2,
        max_context_items=5,
        max_context_tokens=777,
        max_worker_calls=11,
    )
    spec = _spec(
        budget=budget,
        required_types=("method", "experiment", "limitation", "claim_support"),
    )
    accepted = _candidate(spec, suffix="method", evidence_type="method")
    rejected = _candidate(
        spec,
        suffix="experiment",
        evidence_type="experiment",
        rejection_reason="low_relevance",
    )
    conflicting = _candidate(
        spec,
        suffix="limitation",
        evidence_type="limitation",
        conflict=True,
    )
    foreign = _candidate(
        spec,
        suffix="foreign",
        evidence_type="claim_support",
        paper_id="9999.99999",
    )
    result = _result(
        spec,
        accepted=(accepted, foreign),
        rejected=(rejected,),
        conflicting=(conflicting,),
        gap_report={
            "missing_evidence_types": ["claim_support"],
            "rejection_summary": {"low_relevance": {"count": 1}},
        },
    )
    factory = _CaptureFactory(lambda supplied: result)
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )

    context = runtime.run(session_spec=spec, document=document)

    assert [item.evidence_id for item in context.accepted_evidence] == [
        f"chunk-{document.paper_id}-method"
    ]
    accepted_item = context.accepted_evidence[0]
    assert accepted_item.source_ref.startswith(f"paper://{document.paper_id}/chunks/")
    assert accepted_item.span_refs == [
        accepted.source_ref,
        f"paper://{document.paper_id}/chunks/chunk-{document.paper_id}-method"
        "#span=char_offset:3-17",
    ]
    assert accepted_item.lineage.source_refs == [
        f"arxiv://{document.paper_id}/latex",
        accepted.source_ref,
    ]
    assert accepted_item.metadata["score"] == 0.83
    assert accepted_item.metadata["score_breakdown"] == {
        "final_score": 0.83,
        "field_score": 0.7,
    }
    assert accepted_item.lineage.artifact_refs == [
        f"artifact://{document.paper_id}/source",
        f"artifact://{spec.run_id}/method",
    ]

    rejected_ids = {item.evidence_id for item in context.rejected_evidence}
    assert rejected_ids == {
        f"chunk-{document.paper_id}-experiment",
        "chunk-9999.99999-foreign",
    }
    foreign_item = next(
        item for item in context.rejected_evidence if item.evidence_id == "chunk-9999.99999-foreign"
    )
    assert foreign_item.metadata["rejection_reason"] == "source_scope_violation"
    assert [item.evidence_id for item in context.conflicting_evidence] == [
        f"chunk-{document.paper_id}-limitation"
    ]
    assert set(context.gap_report.missing_information) == {
        "experiment",
        "limitation",
        "claim_support",
    }
    assert "low_relevance" in context.gap_report.rejected_reasons
    assert "source_scope_violation" in context.gap_report.rejected_reasons
    assert context.gap_report.conflicting_evidence == [
        f"chunk-{document.paper_id}-limitation"
    ]
    assert not any(
        item.evidence_type in {"experiment", "limitation", "claim_support"}
        for item in context.accepted_evidence
    )
    assert context.source_refs == [f"arxiv://{document.paper_id}/latex"]
    assert context.goal is not spec.goal
    assert context.goal.target_sections == ["sec-method"]
    assert context.goal.target_claims == ["claim-routing"]
    assert context.metadata["budget"] == budget.to_dict()
    assert context.metadata["budget_snapshot"]["context_tokens_used"] == 37
    assert (
        context.metadata["transcript"]["events"][0]["payload"]["session"]
        ["graph_identity"]
        == spec.graph_identity.to_dict()
    )
    trace_status = {
        row["evidence_id"]: row["status"]
        for row in context.metadata["evidence_trace"]
    }
    assert trace_status == {
        accepted.evidence_id: "accepted",
        foreign.evidence_id: "rejected",
        rejected.evidence_id: "rejected",
        conflicting.evidence_id: "conflicting",
    }
    assert runtime.last_context_pack is not result.context_pack


def test_candidate_span_lineage_and_artifact_scope_are_all_enforced() -> None:
    document = _document()
    spec = _spec()
    valid = replace(
        _candidate(spec, suffix="valid", evidence_type="method"),
        lineage=(document.paper_id,),
    )
    foreign_span = replace(
        _candidate(spec, suffix="foreign-span", evidence_type="method"),
        span_refs=("paper://9999.99999/sections/method",),
    )
    foreign_lineage = replace(
        _candidate(spec, suffix="foreign-lineage", evidence_type="method"),
        lineage=("9999.99999",),
    )
    foreign_artifact = replace(
        _candidate(spec, suffix="foreign-artifact", evidence_type="method"),
        artifact_refs=("artifact://foreign-run/research-rag-context-pack",),
    )
    missing_run = _candidate(spec, suffix="missing-run", evidence_type="method")
    missing_run = replace(
        missing_run,
        metadata={
            key: value
            for key, value in missing_run.metadata.items()
            if key != "run_id"
        },
    )
    foreign_user = _candidate(spec, suffix="foreign-user", evidence_type="method")
    foreign_user = replace(
        foreign_user,
        metadata={**foreign_user.metadata, "user_id": "user-b"},
    )
    result = _result(
        spec,
        accepted=(
            valid,
            foreign_span,
            foreign_lineage,
            foreign_artifact,
            missing_run,
            foreign_user,
        ),
    )
    assert result.context_pack is not None
    result = replace(
        result,
        context_pack=replace(
            result.context_pack,
            source_refs=("paper://9999.99999/sections/method",),
        ),
    )
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=_CaptureFactory(lambda supplied: result),
    )

    context = runtime.run(session_spec=spec, document=document)

    assert [item.evidence_id for item in context.accepted_evidence] == [
        f"chunk-{document.paper_id}-valid"
    ]
    assert {
        item.evidence_id for item in context.rejected_evidence
    } == {
        f"chunk-{document.paper_id}-foreign-span",
        f"chunk-{document.paper_id}-foreign-lineage",
        f"chunk-{document.paper_id}-foreign-artifact",
        f"chunk-{document.paper_id}-missing-run",
        f"chunk-{document.paper_id}-foreign-user",
    }
    assert all(
        item.metadata["rejection_reason"] == "source_scope_violation"
        for item in context.rejected_evidence
    )
    assert "artifact://foreign-run/research-rag-context-pack" not in (
        context.lineage.artifact_refs
    )
    scoped_pack = runtime.last_context_pack
    assert scoped_pack is not None
    assert [candidate.evidence_id for candidate in scoped_pack.accepted_evidence] == [
        valid.evidence_id
    ]
    assert {
        candidate.evidence_id for candidate in scoped_pack.rejected_evidence
    } == {
        foreign_span.evidence_id,
        foreign_lineage.evidence_id,
        foreign_artifact.evidence_id,
        missing_run.evidence_id,
        foreign_user.evidence_id,
    }
    assert {
        row["evidence_id"]: row["status"] for row in scoped_pack.evidence_trace
    } == {
        valid.evidence_id: "accepted",
        foreign_span.evidence_id: "rejected",
        foreign_lineage.evidence_id: "rejected",
        foreign_artifact.evidence_id: "rejected",
        missing_run.evidence_id: "rejected",
        foreign_user.evidence_id: "rejected",
    }
    assert "artifact://foreign-run/research-rag-context-pack" not in (
        scoped_pack.artifact_refs
    )
    assert all("9999.99999" not in ref for ref in scoped_pack.source_refs)


def test_mixed_allowed_source_scope_fails_closed_without_running_session() -> None:
    document = _document()
    spec = _spec(
        source_refs=(
            f"arxiv://{document.paper_id}/latex",
            "arxiv://9999.99999/latex",
        )
    )
    called = False

    def result_builder(supplied: RAGSessionSpec) -> RAGSessionResult:
        nonlocal called
        called = True
        return _result(supplied)

    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=_CaptureFactory(result_builder),
    )

    with pytest.raises(ValueError, match="outside the ResearchDocument"):
        runtime.run(session_spec=spec, document=document)
    assert called is False
    assert runtime.last_context_pack is None

    clean_spec = _spec()
    divergent_goal = replace(
        clean_spec.goal,
        known_context_refs=(
            f"arxiv://{document.paper_id}/latex",
            "arxiv://9999.99999/latex",
        ),
    )
    divergent_spec = replace(clean_spec, goal=divergent_goal)
    with pytest.raises(ValueError, match="outside the ResearchDocument"):
        runtime.run(session_spec=divergent_spec, document=document)
    assert called is False


def test_invalid_parse_source_fails_before_index_or_session() -> None:
    document = _document().model_copy(update={"metadata": {"parse_source": "unknown-parser"}})
    store = _MemoryChunkStore()
    factory = _CaptureFactory(lambda spec: _result(spec))
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )

    with pytest.raises(ValueError, match="unsupported ResearchDocument parse_source"):
        runtime.run(session_spec=_spec(), document=document)
    assert store.indexed_batches == []
    assert factory.specs == {}
    assert runtime.last_context_pack is None


def test_failure_resets_last_context_pack_in_the_same_context() -> None:
    document = _document()
    spec_success = _spec(run_id="run-success", session_id="session-success")
    spec_failure = _spec(run_id="run-failure", session_id="session-failure")
    success_result = _result(
        spec_success,
        accepted=(_candidate(spec_success, suffix="method", evidence_type="method"),),
    )

    def result_builder(spec: RAGSessionSpec) -> RAGSessionResult:
        if spec.run_id == "run-failure":
            raise RuntimeError("session failed")
        return success_result

    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=_CaptureFactory(result_builder),
    )
    runtime.run(session_spec=spec_success, document=document)
    assert runtime.last_context_pack is not None
    assert runtime.last_context_pack is not success_result.context_pack
    assert runtime.last_context_pack.pack_id == f"rag-context://{spec_success.session_id}"

    with pytest.raises(RuntimeError, match="session failed"):
        runtime.run(session_spec=spec_failure, document=document)
    assert runtime.last_context_pack is None


def test_concurrent_runs_do_not_leak_document_goal_budget_trace_or_source_refs() -> None:
    barrier = Barrier(2)

    def result_builder(spec: RAGSessionSpec) -> RAGSessionResult:
        candidate = _candidate(
            spec,
            suffix="method",
            evidence_type=spec.goal.required_evidence_types[0],
        )
        return _result(spec, accepted=(candidate,))

    factory = _CaptureFactory(result_builder, barrier=barrier)
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    document_a = _document("2401.00001")
    document_b = _document("2402.00002")
    budget_a = RAGBudget(
        max_rounds=1,
        max_replans=0,
        max_queries=1,
        max_source_reads=1,
        max_memory_hits=1,
        max_context_items=1,
        max_context_tokens=101,
        max_worker_calls=1,
    )
    budget_b = RAGBudget(
        max_rounds=2,
        max_replans=1,
        max_queries=2,
        max_source_reads=2,
        max_memory_hits=2,
        max_context_items=2,
        max_context_tokens=202,
        max_worker_calls=2,
    )
    spec_a = _spec(
        paper_id=document_a.paper_id,
        run_id="run-a",
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        required_types=("method",),
        budget=budget_a,
    )
    spec_b = _spec(
        paper_id=document_b.paper_id,
        run_id="run-b",
        session_id="session-b",
        tenant_id="tenant-b",
        user_id="user-b",
        required_types=("experiment",),
        budget=budget_b,
    )

    def execute(spec: RAGSessionSpec, document: ResearchDocument) -> tuple[Any, RAGContextPack | None]:
        context = runtime.run(session_spec=spec, document=document)
        return context, runtime.last_context_pack

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(execute, spec_a, document_a)
        future_b = executor.submit(execute, spec_b, document_b)
        context_a, pack_a = future_a.result(timeout=10)
        context_b, pack_b = future_b.result(timeout=10)

    assert pack_a is not None and pack_a.pack_id == "rag-context://session-a"
    assert pack_b is not None and pack_b.pack_id == "rag-context://session-b"
    assert context_a.paper_id == document_a.paper_id
    assert context_b.paper_id == document_b.paper_id
    assert context_a.goal.goal_id == spec_a.goal.goal_id
    assert context_b.goal.goal_id == spec_b.goal.goal_id
    assert context_a.metadata["budget"]["max_context_tokens"] == 101
    assert context_b.metadata["budget"]["max_context_tokens"] == 202
    assert context_a.metadata["transcript"]["session_id"] == "session-a"
    assert context_b.metadata["transcript"]["session_id"] == "session-b"
    assert context_a.source_refs == [f"arxiv://{document_a.paper_id}/latex"]
    assert context_b.source_refs == [f"arxiv://{document_b.paper_id}/latex"]
    assert context_a.memory_context[0]["namespace"] == spec_a.allowed_memory_namespaces[0]
    assert context_b.memory_context[0]["namespace"] == spec_b.allowed_memory_namespaces[0]
    assert runtime.last_context_pack is None


def test_fifty_concurrent_runs_keep_all_request_scoped_state_isolated() -> None:
    run_count = 50
    barrier = Barrier(run_count)

    def result_builder(spec: RAGSessionSpec) -> RAGSessionResult:
        candidate = _candidate(spec, suffix="method", evidence_type="method")
        return _result(spec, accepted=(candidate,))

    store = _MemoryChunkStore()
    factory = _CaptureFactory(result_builder, barrier=barrier)
    runtime = BoundedDocumentRAGRuntime(
        store,
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    documents = [
        _document(f"24{index:02d}.00001")
        for index in range(run_count)
    ]
    specs = [
        _spec(
            paper_id=document.paper_id,
            run_id=f"run-{index:02d}",
            session_id=f"session-{index:02d}",
            tenant_id=f"tenant-{index:02d}",
            user_id=f"user-{index:02d}",
            budget=RAGBudget(
                max_rounds=1,
                max_replans=0,
                max_queries=1,
                max_source_reads=1,
                max_memory_hits=1,
                max_context_items=1,
                max_context_tokens=100 + index,
                max_worker_calls=1,
            ),
        )
        for index, document in enumerate(documents)
    ]

    def execute(
        spec: RAGSessionSpec,
        document: ResearchDocument,
    ) -> tuple[Any, RAGContextPack | None]:
        context = runtime.run(session_spec=spec, document=document)
        return context, runtime.last_context_pack

    with ThreadPoolExecutor(max_workers=run_count) as executor:
        futures = [
            executor.submit(execute, spec, document)
            for spec, document in zip(specs, documents, strict=True)
        ]
        results = [future.result(timeout=30) for future in futures]

    assert len({context.context_id for context, _ in results}) == run_count
    for spec, document, (context, pack) in zip(
        specs,
        documents,
        results,
        strict=True,
    ):
        assert pack is not None
        assert pack.pack_id == f"rag-context://{spec.session_id}"
        assert context.paper_id == document.paper_id
        assert context.metadata["run_id"] == spec.run_id
        assert context.metadata["session_id"] == spec.session_id
        assert context.metadata["tenant_id"] == spec.metadata["tenant_id"]
        assert context.metadata["user_id"] == spec.metadata["user_id"]
        assert (
            context.metadata["memory_namespace"]
            == spec.metadata["memory_namespace"]
        )
        assert context.goal.goal_id == spec.goal.goal_id
        assert context.goal.metadata["tenant_id"] == spec.metadata["tenant_id"]
        assert context.goal.metadata["user_id"] == spec.metadata["user_id"]
        assert (
            context.goal.metadata["memory_namespace"]
            == spec.metadata["memory_namespace"]
        )
        assert spec.source_policy["tenant_id"] == spec.metadata["tenant_id"]
        assert (
            spec.source_policy["memory_namespace"]
            == spec.metadata["memory_namespace"]
        )
        assert context.metadata["budget"] == spec.budget.to_dict()
        assert context.metadata["transcript"]["session_id"] == spec.session_id
        assert context.memory_context[0]["namespace"] == spec.allowed_memory_namespaces[0]
        assert context.source_refs == [f"arxiv://{document.paper_id}/latex"]
    assert {
        chunk.metadata["run_id"]
        for batch in store.indexed_batches
        for chunk in batch
    } == {spec.run_id for spec in specs}
    assert runtime.last_context_pack is None


def test_default_chunker_maps_section_span_and_source_lineage_into_scoped_chunks() -> None:
    document = ResearchDocument(
        paper_id="2403.00003",
        source_hash="sha256:full-document",
        sections=[
            ResearchSection(
                section_id="sec-abstract",
                title="Abstract",
                text="A bounded Research runtime.",
                source_ref="paper://2403.00003/sections/abstract",
            ),
            ResearchSection(
                section_id="sec-method",
                title="Method",
                text="First method paragraph.\n\nSecond method paragraph.",
                page_start=1,
                page_end=2,
                source_ref="paper://2403.00003/sections/method",
            ),
            ResearchSection(
                section_id="sec-experiment",
                title="Experiments",
                text="Experiments compare deterministic gates.",
                page_start=3,
                page_end=3,
                source_ref="paper://2403.00003/sections/experiments",
            ),
            ResearchSection(
                section_id="sec-conclusion",
                title="Conclusion",
                text="The bounded runtime preserves evidence.",
                page_start=4,
                page_end=4,
                source_ref="paper://2403.00003/sections/conclusion",
            ),
        ],
        lineage=SourceLineage(
            source_refs=["arxiv://2403.00003/latex"],
            source_hash="sha256:full-document",
        ),
        metadata={"parse_source": "latex"},
    )
    spec = _spec(paper_id=document.paper_id)
    store = _MemoryChunkStore()
    runtime = BoundedDocumentRAGRuntime(
        store,
        session_factory=_CaptureFactory(lambda supplied: _result(supplied, with_pack=False)),
    )

    runtime.run(session_spec=spec, document=document)

    chunks = store.indexed_batches[0]
    method_chunks = [chunk for chunk in chunks if chunk.metadata.get("section_id") == "sec-method"]
    assert method_chunks
    assert all(chunk.metadata["canonical_chunk_id"] != chunk.chunk_id for chunk in method_chunks)
    assert all(chunk.metadata["source_hash"] == document.source_hash for chunk in method_chunks)
    assert all(chunk.metadata["page_start"] == 1 for chunk in method_chunks)
    assert all(chunk.metadata["page_end"] == 2 for chunk in method_chunks)
    paragraph_chunks = [chunk for chunk in method_chunks if chunk.metadata.get("is_parent") is False]
    assert paragraph_chunks
    assert all(chunk.metadata["content_span_unit"] == "char_offset" for chunk in paragraph_chunks)
    assert all(chunk.metadata["main_span"]["end"] > 0 for chunk in paragraph_chunks)
    assert all(
        chunk.metadata["document_source_refs"] == ["arxiv://2403.00003/latex"]
        for chunk in method_chunks
    )


def test_default_paper_rag_session_executes_the_supplied_bounded_spec() -> None:
    document = _document()
    spec = _spec(required_types=("method",))
    supplied = spec.to_dict()
    runtime = BoundedDocumentRAGRuntime(
        _MemoryChunkStore(),
        chunker=_RelationalChunker(),  # type: ignore[arg-type]
    )

    context = runtime.run(session_spec=spec, document=document)

    assert spec.to_dict() == supplied
    assert context.paper_id == document.paper_id
    assert context.goal.goal_id == spec.goal.goal_id
    assert context.metadata["budget"] == spec.budget.to_dict()
    assert context.metadata["transcript"]["session_id"] == spec.session_id
    assert context.metadata["session_status"] == RAGSessionStatus.SUCCEEDED.value
    assert context.accepted_evidence
    assert runtime.last_context_pack is not None
