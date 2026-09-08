"""Composition root for the paper RAG stack.

Wires the four storage/parsing ports + reranker into ready-to-use pipeline,
retriever and session objects so callers (CLI, services, scripts) never repeat
the adapter assembly.

This module lives in interfaces/ because it is the only layer allowed to import
both business and infrastructure (the architecture boundary forbids business → infra).
"""
from __future__ import annotations

import os
from threading import Condition, Lock
from typing import Any, Callable, cast

from framework.harness.control_plane.errors import HarnessValidationError
from framework.memory.recall_port import ExecutionMemoryRecallPort
from infrastructure.external.reranker import CrossEncoderReranker
from infrastructure.storage.postgres.paper_chunk_repository import PaperChunkRepository
from infrastructure.storage.vector.paper_chunk_store import PaperChunkStore
from infrastructure.storage.vector.paper_field_chunk_store import PaperFieldChunkStore
from infrastructure.storage.vector.paper_visual_chunk_store import (
    PaperVisualChunkStore,
    paper_visual_chunk_store_from_env,
)
from infrastructure.storage.vector.qdrant_store import qdrant_store_from_env

from backend.research.document.chunk_storage import (
    PaperChunkRepositoryAdapter,
    PaperChunkStoreAdapter,
)
from backend.research.document.cascade_parser import CascadeArxivDocumentParser
from backend.research.application.chunk_paper_pipeline import ChunkPaperPipeline
from backend.research.application.visual_chunk_describer import build_visual_chunk_describer_from_env
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.application.llm_client import build_unity_llm_call
from backend.research.rag.adapters import (
    LLMResearchRAGPlanCandidateWorker,
    PaperAnswerWorker,
    ResearchRAGMemoryPort,
    RerankerRelevanceScorer,
)
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from backend.research.rag.retrieval.paper_retriever import (
    ResearchRetriever,
    build_retrieval_policy_from_env,
)
from interfaces.services.source_runtime import (
    SourceRuntimeComposition,
)


def _dsn() -> str:
    from infrastructure.storage.postgres.dsn import normalize_dsn
    dsn = os.environ.get("NEWS_DATABASE_DSN", "")
    if not dsn:
        raise RuntimeError("NEWS_DATABASE_DSN is not set")
    return normalize_dsn(dsn)


NEWS_RAG_MEMORY_ENV = "NEWS_RAG_MEMORY"
NEWS_RAG_MEMORY_COLLECTION_ENV = "NEWS_RAG_MEMORY_COLLECTION"
_UNSET = object()


class _SynchronizedReranker:
    """Serialize lazy model initialization and prediction on a shared reranker."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._lock = Lock()
        self._closed = False

    def score(self, query: str, passages: list[str]) -> list[float]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Paper RAG reranker is closed")
            return self._delegate.score(query, passages)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            delegate = self._delegate
            self._delegate = None
            close = getattr(delegate, "close", None)
            if callable(close):
                close()


class PaperRagRuntimeResources:
    """Own the reusable adapters used by one process-scoped Paper RAG service."""

    def __init__(
        self,
        *,
        vector_store_factory: Callable[[], Any] | None = None,
        visual_store_factory: Callable[[], PaperVisualChunkStore | None] | None = None,
        reranker_factory: Callable[[], CrossEncoderReranker] | None = None,
        retrieval_policy_factory: Callable[[], Any] | None = None,
        answer_worker_factory: Callable[[], Any] | None = None,
        plan_worker_factory: Callable[[], Any | None] | None = None,
    ) -> None:
        self._vector_store_factory = vector_store_factory or qdrant_store_from_env
        self._visual_store_factory = visual_store_factory or paper_visual_chunk_store_from_env
        self._reranker_factory = reranker_factory or CrossEncoderReranker
        self._retrieval_policy_factory = (
            retrieval_policy_factory or build_retrieval_policy_from_env
        )
        self._answer_worker_factory = answer_worker_factory or (
            lambda: PaperAnswerWorker(
                AnswerGenerator(build_unity_llm_call(max_tokens=600))
            )
        )
        self._plan_worker_factory = plan_worker_factory or _build_optional_plan_worker
        self._lock = Condition(Lock())
        self._closing = False
        self._closed = False
        self._vector_store: Any | None = None
        self._chunk_store: PaperChunkStoreAdapter | None = None
        self._field_chunk_store: PaperFieldChunkStore | None = None
        self._visual_chunk_store: PaperVisualChunkStore | None | object = _UNSET
        self._reranker: _SynchronizedReranker | None = None
        self._retrieval_policy: Any = _UNSET
        self._answer_worker: Any = _UNSET
        self._plan_worker: Any = _UNSET
        self._owned_resources: list[Any] = []
        self._owned_resource_ids: set[int] = set()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def get_reranker(self) -> Any:
        with self._lock:
            self._ensure_open_locked()
            return self._reranker_locked()

    def preload_reranker(self) -> None:
        self.get_reranker().score("warmup", ["warmup passage"])

    def build_research_retriever(
        self,
        *,
        with_reranker: bool = True,
    ) -> ResearchRetriever:
        with self._lock:
            self._ensure_open_locked()
            chunk_store = self._chunk_store_locked()
            field_store = self._field_chunk_store_locked()
            visual_store = self._visual_chunk_store_locked()
            reranker = self._reranker_locked() if with_reranker else None
            retrieval_policy = self._retrieval_policy_locked()
        return ResearchRetriever(
            chunk_store,
            policy=retrieval_policy,
            reranker=reranker,
            field_index=field_store,
            field_reranker=reranker,
            visual_store=visual_store,
        )

    def build_paper_rag_session(
        self,
        *,
        with_reranker: bool = True,
        plan_worker: Any | None = None,
        with_answer_worker: bool = False,
        memory_recall: ExecutionMemoryRecallPort | None = None,
    ) -> PaperRAGSession:
        with self._lock:
            self._ensure_open_locked()
            memory = build_rag_memory_port(memory_recall=memory_recall)
            chunk_store = self._chunk_store_locked()
            field_store = self._field_chunk_store_locked()
            visual_store = self._visual_chunk_store_locked()
            reranker = self._reranker_locked() if with_reranker else None
            retrieval_policy = self._retrieval_policy_locked()
            answer_worker = (
                self._answer_worker_locked() if with_answer_worker else None
            )
            resolved_plan_worker = (
                plan_worker if plan_worker is not None else self._plan_worker_locked()
            )
        relevance_scorer = (
            RerankerRelevanceScorer(reranker) if reranker is not None else None
        )
        generation_policy: dict[str, object] = (
            {"enabled": True, "max_attempts": 2} if answer_worker is not None else {}
        )
        return PaperRAGSession(
            chunk_store,
            reranker=reranker,
            field_index=field_store,
            field_reranker=reranker,
            visual_store=visual_store,
            retrieval_policy=retrieval_policy,
            plan_worker=resolved_plan_worker,
            answer_worker=answer_worker,
            generation_policy=generation_policy,
            relevance_scorer=relevance_scorer,
            memory=memory,
        )

    def close(self) -> None:
        with self._lock:
            while self._closing:
                self._lock.wait()
            if self._closed:
                return
            self._closing = True
            self._closed = True
            resources = tuple(reversed(self._owned_resources))
            self._owned_resources.clear()
            self._owned_resource_ids.clear()
            self._vector_store = None
            self._chunk_store = None
            self._field_chunk_store = None
            self._visual_chunk_store = _UNSET
            self._reranker = None
            self._retrieval_policy = _UNSET
            self._answer_worker = _UNSET
            self._plan_worker = _UNSET

        first_error: Exception | None = None
        try:
            for resource in resources:
                try:
                    resource.close()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            with self._lock:
                self._closing = False
                self._lock.notify_all()
        if first_error is not None:
            raise first_error

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Paper RAG runtime resources are closed")

    def _vector_store_locked(self) -> Any:
        if self._vector_store is None:
            self._vector_store = self._vector_store_factory()
            self._register_lifecycle_locked(self._vector_store)
        return self._vector_store

    def _chunk_store_locked(self) -> PaperChunkStoreAdapter:
        if self._chunk_store is None:
            self._chunk_store = PaperChunkStoreAdapter(
                PaperChunkStore(self._vector_store_locked())
            )
            self._chunk_store.ensure_collection()
        return self._chunk_store

    def _field_chunk_store_locked(self) -> PaperFieldChunkStore:
        if self._field_chunk_store is None:
            self._field_chunk_store = PaperFieldChunkStore(
                self._vector_store_locked()
            )
            self._field_chunk_store.ensure_collection()
        return self._field_chunk_store

    def _visual_chunk_store_locked(self) -> PaperVisualChunkStore | None:
        if self._visual_chunk_store is _UNSET:
            visual_store = self._visual_store_factory()
            if visual_store is not None:
                self._register_lifecycle_locked(visual_store)
                visual_store.ensure_collection()
            self._visual_chunk_store = visual_store
        value = self._visual_chunk_store
        return cast(PaperVisualChunkStore | None, value)

    def _reranker_locked(self) -> _SynchronizedReranker:
        if self._reranker is None:
            delegate = self._reranker_factory()
            self._reranker = _SynchronizedReranker(delegate)
            self._register_lifecycle_locked(self._reranker)
        return self._reranker

    def _retrieval_policy_locked(self) -> Any:
        if self._retrieval_policy is _UNSET:
            self._retrieval_policy = self._retrieval_policy_factory()
        return self._retrieval_policy

    def _answer_worker_locked(self) -> Any:
        if self._answer_worker is _UNSET:
            self._answer_worker = self._answer_worker_factory()
            self._register_lifecycle_locked(self._answer_worker)
        return self._answer_worker

    def _plan_worker_locked(self) -> Any | None:
        if self._plan_worker is _UNSET:
            self._plan_worker = self._plan_worker_factory()
            if self._plan_worker is not None:
                self._register_lifecycle_locked(self._plan_worker)
        return self._plan_worker

    def _register_lifecycle_locked(self, resource: Any) -> None:
        nested_store = getattr(resource, "_store", None)
        candidates = (
            resource,
            getattr(resource, "client", None),
            nested_store,
            getattr(nested_store, "client", None),
        )
        candidate = next(
            (
                item
                for item in candidates
                if item is not None and callable(getattr(item, "close", None))
            ),
            None,
        )
        if candidate is None:
            return
        identity = id(candidate)
        if identity in self._owned_resource_ids:
            return
        self._owned_resource_ids.add(identity)
        self._owned_resources.append(candidate)


def get_reranker() -> Any:
    """Return the reranker owned by the default Research composition."""
    from interfaces.composition.research import get_default_research_reranker

    return get_default_research_reranker()


def preload_reranker() -> None:
    """Warm the reranker weights at service startup so the first request is fast."""
    from interfaces.composition.research import preload_default_research_reranker

    preload_default_research_reranker()


def build_chunk_store(*, vector_store: Any | None = None) -> PaperChunkStoreAdapter:
    store = PaperChunkStoreAdapter(
        PaperChunkStore(
            vector_store if vector_store is not None else qdrant_store_from_env()
        )
    )
    store.ensure_collection()
    return store


def build_chunk_repository() -> PaperChunkRepositoryAdapter:
    return PaperChunkRepositoryAdapter(PaperChunkRepository(_dsn()))


def build_field_chunk_store(*, vector_store: Any | None = None) -> PaperFieldChunkStore:
    store = PaperFieldChunkStore(
        vector_store if vector_store is not None else qdrant_store_from_env()
    )
    store.ensure_collection()
    return store


def build_visual_chunk_store() -> PaperVisualChunkStore | None:
    store = paper_visual_chunk_store_from_env()
    if store is not None:
        store.ensure_collection()
    return store


def build_rag_memory_port(
    *,
    memory_recall: ExecutionMemoryRecallPort | None = None,
) -> ResearchRAGMemoryPort | None:
    if not _env_truthy(os.environ.get(NEWS_RAG_MEMORY_ENV)):
        return None
    if os.environ.get(NEWS_RAG_MEMORY_COLLECTION_ENV):
        raise HarnessValidationError("RAG memory collection selection is removed; use an admitted namespace revision", code="REF_UNAUTHORIZED")
    if not isinstance(memory_recall, ExecutionMemoryRecallPort):
        raise HarnessValidationError("RAG memory requires an admitted execution-bound recall capability", code="REF_UNAUTHORIZED")
    memory_recall.validate_execution(memory_recall.execution_identity)
    return ResearchRAGMemoryPort(memory_recall)


def build_chunk_pipeline(
    *,
    with_propositions: bool = False,
    source_runtime: SourceRuntimeComposition | None = None,
) -> ChunkPaperPipeline:
    actual_source_runtime = source_runtime or _default_source_runtime()
    return ChunkPaperPipeline(
        build_chunk_store(),
        build_chunk_repository(),
        actual_source_runtime.research_arxiv_connector,
        CascadeArxivDocumentParser(),
        field_chunk_indexer=build_field_chunk_store(),
        visual_chunk_indexer=build_visual_chunk_store(),
        visual_chunk_describer=build_visual_chunk_describer_from_env(),
        with_propositions=with_propositions,
    )


def build_research_retriever(
    *,
    with_reranker: bool = True,
    runtime_resources: PaperRagRuntimeResources | None = None,
) -> ResearchRetriever:
    resources = runtime_resources or _default_runtime_resources()
    return resources.build_research_retriever(
        with_reranker=with_reranker,
    )


def build_paper_rag_session(
    *,
    with_reranker: bool = True,
    plan_worker=None,
    with_answer_worker: bool = False,
    runtime_resources: PaperRagRuntimeResources | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
) -> PaperRAGSession:
    resources = runtime_resources or _default_runtime_resources()
    return resources.build_paper_rag_session(
        with_reranker=with_reranker,
        plan_worker=plan_worker,
        with_answer_worker=with_answer_worker,
        memory_recall=memory_recall,
    )


def _default_runtime_resources() -> PaperRagRuntimeResources:
    from interfaces.composition.research import (
        get_default_paper_rag_runtime_resources,
    )

    resources = get_default_paper_rag_runtime_resources()
    if not isinstance(resources, PaperRagRuntimeResources):
        raise RuntimeError("default Research composition returned invalid RAG resources")
    return resources


def _default_source_runtime() -> SourceRuntimeComposition:
    from interfaces.composition.research import default_research_runtime_provider

    return default_research_runtime_provider().source_runtime_provider.get()


def _build_optional_plan_worker() -> Any | None:
    if not _env_truthy(os.environ.get("NEWS_RAG_LLM_PLANNER")):
        return None
    return LLMResearchRAGPlanCandidateWorker(
        build_unity_llm_call(max_tokens=900, temperature=0.0)
    )


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "PaperRagRuntimeResources",
    "build_chunk_pipeline",
    "build_chunk_repository",
    "build_chunk_store",
    "build_field_chunk_store",
    "build_rag_memory_port",
    "build_visual_chunk_store",
    "build_paper_rag_session",
    "build_research_retriever",
    "get_reranker",
    "preload_reranker",
]
