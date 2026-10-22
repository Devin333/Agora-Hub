from __future__ import annotations

from typing import Any

from framework.harness.rag.evidence_typing import EvidenceTypeResolver
from framework.harness.rag.models import EvidenceCandidate
from framework.harness.retrieval.evidence_pack import EvidencePackCollection
from framework.harness.retrieval.request import RetrievalRequest
from framework.rag.core import RAGEvidence
from framework.rag.core import RAGQuery, RAGRetrieverPort


def evidence_candidate_from_rag_evidence(
    evidence: RAGEvidence,
    *,
    evidence_type: str = "rag_evidence",
    evidence_types: tuple[str, ...] = (),
    title: str | None = None,
) -> EvidenceCandidate:
    source_ref = _source_ref(evidence)
    metadata: dict[str, Any] = {
        **dict(evidence.metadata),
        "rag_document_id": evidence.document_id,
        "rag_chunk_id": evidence.chunk_id,
        "rag_score": evidence.score,
        "rag_score_breakdown": evidence.score_breakdown.to_dict(),
    }
    if evidence.source_locator is not None:
        metadata["rag_source_locator"] = evidence.source_locator.to_dict()
    return EvidenceCandidate(
        evidence_id=evidence.evidence_id,
        title=title or str(evidence.metadata.get("title") or evidence.metadata.get("section_title") or evidence.chunk_id),
        summary=evidence.text[:1200],
        source_ref=source_ref,
        span_refs=(source_ref,),
        evidence_type=evidence_type,
        evidence_types=evidence_types,
        confidence=_confidence(evidence.score),
        freshness=str(evidence.metadata.get("freshness") or "unknown"),
        lineage=(evidence.document_id,),
        artifact_refs=_artifact_refs(evidence.metadata),
        metadata=metadata,
    )


class KernelRAGRetrieverHarnessAdapter:
    """Adapts a domain-neutral RAG retriever to the Harness retrieval port."""

    def __init__(
        self,
        retriever: RAGRetrieverPort,
        *,
        default_intent: str = "general",
        default_evidence_type: str = "rag_evidence",
        evidence_type_resolver: EvidenceTypeResolver | None = None,
    ) -> None:
        self._retriever = retriever
        self._default_intent = str(default_intent or "general")
        self._default_evidence_type = str(default_evidence_type or "rag_evidence")
        self._evidence_type_resolver = evidence_type_resolver

    def retrieve(self, request: RetrievalRequest) -> EvidencePackCollection:
        query = _rag_query_from_harness_request(request, default_intent=self._default_intent)
        evidence = tuple(self._retriever.retrieve(query))
        requested_type = str(request.metadata.get("evidence_type") or self._default_evidence_type)
        candidates = tuple(
            self._candidate_from_evidence(item, requested_type=requested_type)
            for item in evidence
        )
        return EvidencePackCollection(
            packs=tuple(candidate.to_evidence_pack() for candidate in candidates),
            request_ref=f"rag-kernel://retrieval/{query.intent}/{_stable_request_suffix(query.query)}",
            metadata={
                "rag_query": query.to_dict(),
                "evidence_count": len(evidence),
                "adapter": "kernel_rag_retriever_harness_adapter",
            },
        )

    def _candidate_from_evidence(self, evidence: RAGEvidence, *, requested_type: str) -> EvidenceCandidate:
        resolved_types = (
            self._evidence_type_resolver.resolve_types(evidence.metadata)
            if self._evidence_type_resolver is not None
            else ()
        )
        resolved_type = resolved_types[0] if resolved_types else None
        candidate = evidence_candidate_from_rag_evidence(
            evidence,
            evidence_type=resolved_type or requested_type,
            evidence_types=resolved_types,
        )
        if resolved_type:
            candidate.metadata["evidence_type_source"] = "content_resolved"
        elif self._evidence_type_resolver is not None:
            candidate.metadata["evidence_type_source"] = "requested_fallback"
        else:
            candidate.metadata["evidence_type_source"] = "requested_default"
        return candidate


def _source_ref(evidence: RAGEvidence) -> str:
    if evidence.source_locator is not None:
        return evidence.source_locator.raw_locator or evidence.source_locator.source_id
    source_locator = str(evidence.metadata.get("source_locator") or "")
    if source_locator:
        return source_locator
    return f"rag://{evidence.document_id}/{evidence.chunk_id}"


def _confidence(score: float) -> float:
    return max(0.0, min(float(score), 1.0))


def _artifact_refs(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("artifact_refs") or metadata.get("artifact_ref") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (tuple, list)):
        return ()
    return tuple(str(ref) for ref in raw if str(ref).strip())


def _rag_query_from_harness_request(
    request: RetrievalRequest,
    *,
    default_intent: str,
) -> RAGQuery:
    required_types = _text_tuple(request.metadata.get("required_chunk_types"))
    preferred_fields = _text_tuple(request.metadata.get("preferred_fields"))
    filters = dict(request.filters)
    if request.context_refs:
        filters.setdefault("context_refs", list(request.context_refs))
    return RAGQuery(
        query=request.query,
        intent=str(request.metadata.get("intent") or default_intent),
        required_chunk_types=required_types,
        preferred_fields=preferred_fields,
        filters=filters,
        limit=request.limit,
        metadata={
            "scope": request.scope,
            **dict(request.metadata),
        },
    )


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _stable_request_suffix(text: str) -> str:
    normalized = "-".join(str(text or "").casefold().split())
    return normalized[:80] or "query"


__all__ = [
    "KernelRAGRetrieverHarnessAdapter",
    "evidence_candidate_from_rag_evidence",
]
