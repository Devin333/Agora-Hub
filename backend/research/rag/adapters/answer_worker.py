from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from threading import Thread
from typing import Any
from uuid import uuid4

from backend.research.document.models import PaperChunk
from backend.research.domain.personal_memory_context import (
    PersonalMemoryContext, PersonalMemoryContextProvider,
)
from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.rag.retrieval.contracts import RetrievalResult
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator, GeneratedAnswer
from backend.research.rag.retrieval.paper_policy import classify_query_intent
from framework.harness.rag.answer_worker import AnswerWorkerError
from framework.harness.rag.models import AnswerClaim, GroundedAnswerCandidate, RAGContextPack
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True)
class _ProjectedContext:
    retrieval: RetrievalResult
    chunk_to_evidence_id: dict[str, str]
    evidence_id_to_span_refs: dict[str, tuple[str, ...]]


class PaperAnswerWorker:
    """Projects verified Paper RAG context packs into grounded answer candidates."""

    def __init__(
        self,
        generator: AnswerGenerator,
        *,
        personal_memory_context: PersonalMemoryContext | None = None,
        personal_memory_context_provider: PersonalMemoryContextProvider | None = None,
    ) -> None:
        if personal_memory_context is not None and not isinstance(
            personal_memory_context, PersonalMemoryContext
        ):
            raise TypeError("personal_memory_context must be PersonalMemoryContext")
        self._generator = generator
        if personal_memory_context_provider is not None and personal_memory_context is None:
            raise ValueError("a memory provider requires the frozen context")
        self._personal_memory_context = personal_memory_context
        self._personal_memory_context_provider = personal_memory_context_provider

    def with_personal_memory_context(
        self,
        context: PersonalMemoryContext,
        *,
        provider: PersonalMemoryContextProvider | None = None,
    ) -> "PaperAnswerWorker":
        return PaperAnswerWorker(
            self._generator,
            personal_memory_context=context,
            personal_memory_context_provider=provider,
        )

    def generate_answer(
        self,
        *,
        question: str,
        pack: RAGContextPack,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> GroundedAnswerCandidate:
        projected = _project_context_pack(pack, question=question)
        if projected is None:
            raise AnswerWorkerError(
                "context_pack_not_projectable",
                "context pack lacks paper_chunk metadata",
            )
        if self._personal_memory_context is not None and not _supports_personal_memory_context(
            self._generator
        ):
            raise AnswerWorkerError(
                "personal_memory_unsupported",
                "answer generator does not support personal memory context",
            )

        generator_kwargs: dict[str, Any] = {}
        if execution_identity is not None:
            generator_kwargs["execution_identity"] = execution_identity
        if self._personal_memory_context is not None:
            generator_kwargs["personal_memory_context"] = self._personal_memory_context
        if self._personal_memory_context_provider is not None:
            generator_kwargs["personal_memory_context_provider"] = self._personal_memory_context_provider
        try:
            generated = _run_async(
                self._generator.generate(
                    question,
                    projected.retrieval,
                    **generator_kwargs,
                )
            )
        except PaperAnswerModelError as exc:
            raise AnswerWorkerError(
                f"model_{exc.code}", str(exc), retryable=exc.retryable, details=exc.details,
                context_chunk_ids=exc.context_chunk_ids,
            ) from exc

        actual_used_personal_memory: list[dict[str, str | int]] = []
        if self._personal_memory_context is not None:
            receipt = _verified_personal_memory_receipt(
                generated,
                expected=self._personal_memory_context,
            )
            if receipt is None:
                raise AnswerWorkerError(
                    "personal_memory_receipt_mismatch",
                    "answer generator did not acknowledge personal memory context",
                )
            actual_used_personal_memory = receipt

        metadata: dict[str, Any] = {
            "worker": "paper_answer_worker",
            "context_pack_id": pack.pack_id,
            "context_chunk_ids": list(generated.context_chunk_ids),
            "chunk_to_evidence_id": dict(projected.chunk_to_evidence_id),
            "evidence_id_to_span_refs": {
                evidence_id: list(span_refs)
                for evidence_id, span_refs in projected.evidence_id_to_span_refs.items()
            },
            "generated_answer_metadata": dict(generated.context_metadata),
            "model": generated.model,
            "model_usage": generated.model_usage.to_dict(),
            **(
                {"used_personal_memory": actual_used_personal_memory}
                if actual_used_personal_memory
                else {}
            ),
        }
        if generated.abstained:
            return GroundedAnswerCandidate(
                answer_id=f"paper-answer-abstain-{uuid4().hex[:12]}",
                question=question,
                answer_text="",
                cited_evidence_ids=(),
                claims=(),
                abstained=True,
                metadata={
                    **metadata,
                    "abstention_source": "model",
                    "abstention_reason": generated.abstention_reason,
                },
            )

        # Citation validity is judged by the answer gate; the worker only reports it.
        unresolved = list(generated.invalid_citation_markers)
        for index, chunk_id in zip(generated.cited_context_indexes, generated.cited_chunk_ids):
            if chunk_id not in projected.chunk_to_evidence_id:
                unresolved.append(f"[{index}]")
        cited_evidence_ids = _map_chunk_ids(
            generated.cited_chunk_ids,
            projected.chunk_to_evidence_id,
        )
        return GroundedAnswerCandidate(
            answer_id=f"paper-answer-{uuid4().hex[:12]}",
            question=question,
            answer_text=generated.answer,
            cited_evidence_ids=tuple(cited_evidence_ids),
            claims=(_conservative_claim(generated, cited_evidence_ids, projected.evidence_id_to_span_refs),),
            metadata={**metadata, "claims_degraded": True},
            unresolved_citations=tuple(unresolved),
        )


def _supports_personal_memory_context(generator: Any) -> bool:
    generate = getattr(generator, "generate", None)
    if not callable(generate):
        return False
    try:
        parameters = inspect.signature(generate).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "personal_memory_context"
            and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        )
        for parameter in parameters
    )


def _verified_personal_memory_receipt(
    generated: GeneratedAnswer,
    *,
    expected: PersonalMemoryContext,
) -> list[dict[str, str | int]] | None:
    raw = generated.context_metadata.get("used_personal_memory")
    if not isinstance(raw, list):
        return None
    normalized: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"memoryId", "revision"}:
            return None
        memory_id = item.get("memoryId")
        revision = item.get("revision")
        if (
            not isinstance(memory_id, str)
            or not memory_id
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or memory_id in seen
        ):
            return None
        seen.add(memory_id)
        normalized.append({"memoryId": memory_id, "revision": revision})
    expected_refs = list(expected.used_refs)
    return normalized if normalized == expected_refs else None


def _project_context_pack(pack: RAGContextPack, *, question: str) -> _ProjectedContext | None:
    chunks: list[PaperChunk] = []
    chunk_to_evidence_id: dict[str, str] = {}
    evidence_id_to_span_refs: dict[str, tuple[str, ...]] = {}
    for evidence in pack.accepted_evidence:
        chunk = _paper_chunk_from_metadata(evidence.metadata.get("paper_chunk"))
        if chunk is None:
            chunk = _paper_chunk_from_evidence(evidence)
        if chunk is None:
            continue
        chunks.append(chunk)
        chunk_to_evidence_id.setdefault(chunk.chunk_id, evidence.evidence_id)
        evidence_id_to_span_refs.setdefault(
            evidence.evidence_id,
            tuple(
                span_ref
                for raw_span_ref in evidence.span_refs
                if (span_ref := str(raw_span_ref).strip())
            ),
        )

    chunks = _dedupe_chunks(chunks)
    if not chunks:
        return None

    retrieval = RetrievalResult(
        parent_chunks=[],
        child_chunks=chunks,
        ref_chunks=[],
        intent=classify_query_intent(question),
        metadata={
            "source": "rag_context_pack",
            "context_pack_id": pack.pack_id,
            "accepted_evidence_ids": [item.evidence_id for item in pack.accepted_evidence],
        },
    )
    return _ProjectedContext(
        retrieval=retrieval,
        chunk_to_evidence_id=chunk_to_evidence_id,
        evidence_id_to_span_refs=evidence_id_to_span_refs,
    )


def _paper_chunk_from_metadata(raw: Any) -> PaperChunk | None:
    if isinstance(raw, PaperChunk):
        return raw
    if not isinstance(raw, dict):
        return None
    try:
        return PaperChunk(**raw)
    except Exception:
        return None


def _paper_chunk_from_evidence_metadata(metadata: dict[str, Any]) -> PaperChunk | None:
    chunk_id = str(metadata.get("rag_chunk_id") or metadata.get("chunk_id") or "").strip()
    paper_id = str(metadata.get("paper_id") or metadata.get("rag_document_id") or "").strip()
    content = str(metadata.get("content") or metadata.get("text") or "").strip()
    if not chunk_id or not paper_id or not content:
        return None
    try:
        return PaperChunk(
            chunk_id=chunk_id,
            paper_id=paper_id,
            parse_source=str(metadata.get("parse_source") or "pymupdf"),
            structure_detected=bool(metadata.get("structure_detected", True)),
            chunk_type=str(metadata.get("chunk_type") or "paragraph"),
            parent_chunk_id=metadata.get("parent_chunk_id"),
            section_title=str(metadata.get("section_title") or ""),
            section_role=_text_list(metadata.get("section_role")),
            section_index=int(metadata.get("section_index") or 0),
            has_formula=bool(metadata.get("has_formula", False)),
            formula_latex=str(metadata.get("formula_latex") or ""),
            formula_description=str(metadata.get("formula_description") or ""),
            has_figure=bool(metadata.get("has_figure", False)),
            figure_id=str(metadata.get("figure_id") or ""),
            has_table=bool(metadata.get("has_table", False)),
            references=_text_list(metadata.get("references")),
            propositions_generated=bool(metadata.get("propositions_generated", False)),
            proposition_quality=str(metadata.get("proposition_quality") or "unknown"),
            content=content,
            metadata=dict(metadata),
        )
    except Exception:
        return None


def _paper_chunk_from_evidence(evidence: Any) -> PaperChunk | None:
    metadata = dict(getattr(evidence, "metadata", {}) or {})
    if "content" not in metadata and "text" not in metadata:
        metadata["content"] = str(getattr(evidence, "summary", "") or "")
    return _paper_chunk_from_evidence_metadata(metadata)


def _map_chunk_ids(chunk_ids: list[str], mapping: dict[str, str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for chunk_id in chunk_ids:
        evidence_id = mapping.get(chunk_id)
        if not evidence_id or evidence_id in seen:
            continue
        seen.add(evidence_id)
        out.append(evidence_id)
    return out


def _text_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _conservative_claim(
    generated: GeneratedAnswer,
    cited_evidence_ids: list[str],
    evidence_id_to_span_refs: dict[str, tuple[str, ...]],
) -> AnswerClaim:
    return AnswerClaim(
        claim_id="claim-1",
        text=generated.answer.strip(),
        evidence_ids=tuple(cited_evidence_ids),
        span_refs=_claim_span_refs(cited_evidence_ids, evidence_id_to_span_refs),
    )


def _claim_span_refs(
    cited_evidence_ids: list[str],
    evidence_id_to_span_refs: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for evidence_id in cited_evidence_ids:
        for span_ref in evidence_id_to_span_refs.get(evidence_id, ()):
            if span_ref in seen:
                continue
            seen.add(span_ref)
            out.append(span_ref)
    return tuple(out)


def _dedupe_chunks(chunks: list[PaperChunk]) -> list[PaperChunk]:
    seen: set[str] = set()
    out: list[PaperChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        out.append(chunk)
    return out


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised in caller thread
            result["error"] = exc

    thread = Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


__all__ = ["PaperAnswerWorker"]
