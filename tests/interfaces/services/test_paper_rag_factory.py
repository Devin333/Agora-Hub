from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import sleep
from types import SimpleNamespace

import pytest

from interfaces.services import paper_rag_factory
from framework.harness.control_plane.errors import HarnessValidationError
from tests.fixtures.admitted_memory import admitted_memory


@pytest.fixture(autouse=True)
def _disable_llm_planner_env(monkeypatch):
    monkeypatch.delenv("NEWS_RAG_LLM_PLANNER", raising=False)
    monkeypatch.delenv("NEWS_RAG_MEMORY", raising=False)
    monkeypatch.delenv("NEWS_RAG_MEMORY_COLLECTION", raising=False)


class _FakeStore:
    def ensure_collection(self) -> None:
        pass


class _FakeRepo:
    pass


class _FakeFetcher:
    pass


class _FakeSourceRuntime:
    def __init__(self) -> None:
        self.research_arxiv_connector = _FakeFetcher()


class _FakeSourceRuntimeProvider:
    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self.get_calls = 0

    def get(self):
        self.get_calls += 1
        return self._runtime


class _FakeParser:
    pass


class _FakeVisualStore:
    def __init__(self) -> None:
        self.ensure_called = False

    def ensure_collection(self) -> None:
        self.ensure_called = True


class _FakePipeline:
    last_args = ()
    last_kwargs = {}

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        _FakePipeline.last_args = args
        _FakePipeline.last_kwargs = kwargs


class _FakeRetriever:
    last_kwargs = {}

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        _FakeRetriever.last_kwargs = kwargs


class _FakeSession:
    last_kwargs = {}

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        _FakeSession.last_kwargs = kwargs


class _SharedVectorStore:
    def ensure_collections(self, _collections):
        return []

    def ensure_payload_indexes(self, _collections, _indexes):
        return []


def _runtime_resources() -> paper_rag_factory.PaperRagRuntimeResources:
    return paper_rag_factory.PaperRagRuntimeResources(
        vector_store_factory=_SharedVectorStore,
        visual_store_factory=paper_rag_factory.build_visual_chunk_store,
        reranker_factory=paper_rag_factory.get_reranker,
        retrieval_policy_factory=paper_rag_factory.build_retrieval_policy_from_env,
    )


def test_build_visual_chunk_store_returns_none_when_visual_embedding_disabled(monkeypatch):
    monkeypatch.setattr(paper_rag_factory, "paper_visual_chunk_store_from_env", lambda: None)

    assert paper_rag_factory.build_visual_chunk_store() is None


def test_build_visual_chunk_store_ensures_configured_store(monkeypatch):
    visual_store = _FakeVisualStore()
    monkeypatch.setattr(
        paper_rag_factory,
        "paper_visual_chunk_store_from_env",
        lambda: visual_store,
    )

    assert paper_rag_factory.build_visual_chunk_store() is visual_store
    assert visual_store.ensure_called is True


def test_chunk_pipeline_receives_visual_chunk_indexer(monkeypatch):
    visual_store = _FakeVisualStore()
    monkeypatch.setattr(paper_rag_factory, "ChunkPaperPipeline", _FakePipeline)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_chunk_repository", lambda: _FakeRepo())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: visual_store)
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_describer_from_env", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "CascadeArxivDocumentParser", _FakeParser)

    source_runtime = _FakeSourceRuntime()
    paper_rag_factory.build_chunk_pipeline(source_runtime=source_runtime)

    assert _FakePipeline.last_kwargs["visual_chunk_indexer"] is visual_store
    assert _FakePipeline.last_args[2] is source_runtime.research_arxiv_connector


def test_chunk_pipeline_receives_visual_chunk_describer(monkeypatch):
    describer = object()
    monkeypatch.setattr(paper_rag_factory, "ChunkPaperPipeline", _FakePipeline)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_chunk_repository", lambda: _FakeRepo())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_describer_from_env", lambda: describer)
    monkeypatch.setattr(paper_rag_factory, "CascadeArxivDocumentParser", _FakeParser)

    paper_rag_factory.build_chunk_pipeline(source_runtime=_FakeSourceRuntime())

    assert _FakePipeline.last_kwargs["visual_chunk_describer"] is describer


def test_chunk_pipeline_uses_cascade_parser_by_default(monkeypatch):
    monkeypatch.setattr(paper_rag_factory, "ChunkPaperPipeline", _FakePipeline)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_chunk_repository", lambda: _FakeRepo())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_describer_from_env", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "CascadeArxivDocumentParser", _FakeParser)

    paper_rag_factory.build_chunk_pipeline(source_runtime=_FakeSourceRuntime())

    assert isinstance(_FakePipeline.last_args[3], _FakeParser)


def test_chunk_pipeline_default_source_runtime_comes_from_research_provider(monkeypatch):
    import interfaces.composition.research as research_composition

    source_runtime = _FakeSourceRuntime()
    source_provider = _FakeSourceRuntimeProvider(source_runtime)
    monkeypatch.setattr(
        research_composition,
        "default_research_runtime_provider",
        lambda: SimpleNamespace(source_runtime_provider=source_provider),
    )
    monkeypatch.setattr(paper_rag_factory, "ChunkPaperPipeline", _FakePipeline)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_chunk_repository", lambda: _FakeRepo())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_describer_from_env", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "CascadeArxivDocumentParser", _FakeParser)

    paper_rag_factory.build_chunk_pipeline()

    assert source_provider.get_calls == 1
    assert _FakePipeline.last_args[2] is source_runtime.research_arxiv_connector


def test_retriever_and_session_receive_visual_store(monkeypatch):
    visual_store = _FakeVisualStore()
    monkeypatch.setattr(paper_rag_factory, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: visual_store)

    resources = _runtime_resources()
    paper_rag_factory.build_research_retriever(
        with_reranker=False,
        runtime_resources=resources,
    )
    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=resources,
    )

    assert _FakeRetriever.last_kwargs["visual_store"] is visual_store
    assert _FakeSession.last_kwargs["visual_store"] is visual_store


def test_retriever_and_session_receive_env_retrieval_policy(monkeypatch):
    policy = object()
    monkeypatch.setattr(paper_rag_factory, "ResearchRetriever", _FakeRetriever)
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: policy)

    resources = _runtime_resources()
    paper_rag_factory.build_research_retriever(
        with_reranker=False,
        runtime_resources=resources,
    )
    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=resources,
    )

    assert _FakeRetriever.last_kwargs["policy"] is policy
    assert _FakeSession.last_kwargs["retrieval_policy"] is policy


def test_paper_rag_session_factory_passes_optional_plan_worker(monkeypatch):
    plan_worker = object()
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        plan_worker=plan_worker,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["plan_worker"] is plan_worker


def test_paper_rag_session_factory_leaves_llm_planner_disabled_by_default(monkeypatch):
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["plan_worker"] is None
    assert _FakeSession.last_kwargs["memory"] is None


def test_build_rag_memory_port_is_disabled_by_default() -> None:
    assert paper_rag_factory.build_rag_memory_port() is None


def test_build_rag_memory_port_requires_grant_without_opening_vector_store(monkeypatch):
    monkeypatch.setenv("NEWS_RAG_MEMORY", "1")
    monkeypatch.setattr(paper_rag_factory, "qdrant_store_from_env", lambda: pytest.fail("mutable memory access"))
    with pytest.raises(HarnessValidationError, match="admitted execution-bound"):
        paper_rag_factory.build_rag_memory_port()


def test_build_rag_memory_port_rejects_mutable_collection_selector(tmp_path, monkeypatch):
    fixture = admitted_memory(tmp_path)
    monkeypatch.setenv("NEWS_RAG_MEMORY", "1")
    monkeypatch.setenv("NEWS_RAG_MEMORY_COLLECTION", "rag_memories")
    with pytest.raises(HarnessValidationError, match="collection selection is removed"):
        paper_rag_factory.build_rag_memory_port(memory_recall=fixture.recall)


def test_paper_rag_session_factory_never_caches_execution_memory(tmp_path, monkeypatch):
    first = admitted_memory(tmp_path / "first")
    second = admitted_memory(tmp_path / "second", activity_id="second-activity")
    monkeypatch.setenv("NEWS_RAG_MEMORY", "1")
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())
    resources = _runtime_resources()
    session_one = paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=resources,
        memory_recall=first.recall,
    )
    session_two = paper_rag_factory.build_paper_rag_session(
        with_reranker=False, runtime_resources=resources, memory_recall=second.recall,
    )
    memory_one = session_one.kwargs["memory"]
    memory_two = session_two.kwargs["memory"]
    assert memory_one is not memory_two
    assert memory_one.execution_identity == first.root.execution_identity
    assert memory_two.execution_identity == second.root.execution_identity
    with pytest.raises(HarnessValidationError, match="caller differs"):
        memory_one.validate_execution(second.root.execution_identity)
    with pytest.raises(HarnessValidationError, match="admitted execution-bound"):
        resources.build_paper_rag_session(with_reranker=False)
    monkeypatch.setenv("NEWS_RAG_MEMORY", "0")
    disabled = resources.build_paper_rag_session(with_reranker=False)
    assert disabled.kwargs["memory"] is None


def test_session_missing_memory_authority_fails_before_resource_initialization(monkeypatch):
    monkeypatch.setenv("NEWS_RAG_MEMORY", "1")
    resources = paper_rag_factory.PaperRagRuntimeResources(
        vector_store_factory=lambda: pytest.fail("resources initialized before memory admission"),
    )
    with pytest.raises(HarnessValidationError, match="admitted execution-bound"):
        resources.build_paper_rag_session()


def test_paper_rag_session_factory_false_env_leaves_llm_planner_disabled(monkeypatch):
    monkeypatch.setenv("NEWS_RAG_LLM_PLANNER", "0")
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["plan_worker"] is None


def test_paper_rag_session_factory_builds_llm_planner_when_enabled(monkeypatch):
    llm_call = object()
    monkeypatch.setenv("NEWS_RAG_LLM_PLANNER", "true")
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())
    monkeypatch.setattr(
        paper_rag_factory,
        "build_unity_llm_call",
        lambda max_tokens, temperature=None: llm_call,
    )

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=_runtime_resources(),
    )

    plan_worker = _FakeSession.last_kwargs["plan_worker"]
    assert isinstance(plan_worker, paper_rag_factory.LLMResearchRAGPlanCandidateWorker)
    assert plan_worker._llm_call is llm_call


def test_paper_rag_session_factory_explicit_plan_worker_takes_precedence(monkeypatch):
    plan_worker = object()
    monkeypatch.setenv("NEWS_RAG_LLM_PLANNER", "true")
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())

    def fail_build_unity_llm_call(*args, **kwargs):
        raise AssertionError("explicit plan_worker should skip LLM planner construction")

    monkeypatch.setattr(paper_rag_factory, "build_unity_llm_call", fail_build_unity_llm_call)

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        plan_worker=plan_worker,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["plan_worker"] is plan_worker


def test_paper_rag_session_factory_builds_optional_answer_worker(monkeypatch):
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())
    monkeypatch.setattr(paper_rag_factory, "build_unity_llm_call", lambda max_tokens: object())

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        with_answer_worker=True,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["answer_worker"] is not None
    assert _FakeSession.last_kwargs["generation_policy"] == {"enabled": True, "max_attempts": 2}


def test_paper_rag_session_factory_wires_relevance_scorer_from_reranker(monkeypatch):
    reranker = _FakeReranker()
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())
    monkeypatch.setattr(paper_rag_factory, "get_reranker", lambda: reranker)

    paper_rag_factory.build_paper_rag_session(
        with_reranker=True,
        runtime_resources=_runtime_resources(),
    )

    scorer = _FakeSession.last_kwargs["relevance_scorer"]
    assert scorer is not None
    assert scorer.score("question", ["passage"])[0] > 0.5
    assert reranker.calls == [("question", ["passage"])]


def test_paper_rag_session_factory_leaves_relevance_scorer_disabled_without_reranker(monkeypatch):
    monkeypatch.setattr(paper_rag_factory, "PaperRAGSession", _FakeSession)
    monkeypatch.setattr(paper_rag_factory, "build_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_field_chunk_store", lambda: _FakeStore())
    monkeypatch.setattr(paper_rag_factory, "build_visual_chunk_store", lambda: None)
    monkeypatch.setattr(paper_rag_factory, "build_retrieval_policy_from_env", lambda: object())

    paper_rag_factory.build_paper_rag_session(
        with_reranker=False,
        runtime_resources=_runtime_resources(),
    )

    assert _FakeSession.last_kwargs["relevance_scorer"] is None


class _FakeReranker:
    def __init__(self) -> None:
        self.calls = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, passages))
        return [1.0 for _ in passages]


class _LifecycleVectorStore:
    def __init__(self) -> None:
        self.close_calls = 0
        self.ensure_calls: list[tuple[str, ...]] = []

    def ensure_collections(self, collections):
        self.ensure_calls.append(tuple(collections))
        return []

    def ensure_payload_indexes(self, _collections, _indexes):
        return []

    def close(self) -> None:
        self.close_calls += 1


class _ConcurrentReranker:
    def __init__(self) -> None:
        self._lock = Lock()
        self.active = 0
        self.max_active = 0

    def score(self, _query: str, passages: list[str]) -> list[float]:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        sleep(0.01)
        with self._lock:
            self.active -= 1
        return [1.0 for _ in passages]


def test_runtime_resources_reuse_and_close_real_retriever_session_graph() -> None:
    vector_store = _LifecycleVectorStore()
    reranker = _ConcurrentReranker()
    vector_factory_calls = 0

    def build_vector_store():
        nonlocal vector_factory_calls
        vector_factory_calls += 1
        return vector_store

    resources = paper_rag_factory.PaperRagRuntimeResources(
        vector_store_factory=build_vector_store,
        visual_store_factory=lambda: None,
        reranker_factory=lambda: reranker,
        retrieval_policy_factory=lambda: object(),
        plan_worker_factory=lambda: None,
    )

    with ThreadPoolExecutor(max_workers=12) as executor:
        retrievers = list(
            executor.map(
                lambda _index: resources.build_research_retriever(),
                range(12),
            )
        )
        sessions = list(
            executor.map(
                lambda _index: resources.build_paper_rag_session(),
                range(12),
            )
        )

    shared_chunk_store = resources._chunk_store
    shared_field_store = resources._field_chunk_store
    shared_reranker = resources.get_reranker()
    assert vector_factory_calls == 1
    assert all(item._store is shared_chunk_store for item in retrievers)
    assert all(item._chunk_store is shared_chunk_store for item in sessions)
    assert all(item._field_index is shared_field_store for item in sessions)
    assert len({id(item) for item in sessions}) == len(sessions)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda _index: shared_reranker.score("question", ["passage"]),
                range(8),
            )
        )
    assert reranker.max_active == 1

    resources.close()
    resources.close()

    assert resources.closed is True
    assert vector_store.close_calls == 1
    with pytest.raises(RuntimeError, match="reranker is closed"):
        shared_reranker.score("question", ["passage"])
    with pytest.raises(RuntimeError, match="resources are closed"):
        resources.build_research_retriever()


def test_public_rag_builders_delegate_to_one_default_runtime_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector_store = _LifecycleVectorStore()
    vector_factory_calls = 0

    def build_vector_store():
        nonlocal vector_factory_calls
        vector_factory_calls += 1
        return vector_store

    resources = paper_rag_factory.PaperRagRuntimeResources(
        vector_store_factory=build_vector_store,
        visual_store_factory=lambda: None,
        retrieval_policy_factory=lambda: object(),
        plan_worker_factory=lambda: None,
    )
    monkeypatch.setattr(
        paper_rag_factory,
        "_default_runtime_resources",
        lambda: resources,
    )

    first = paper_rag_factory.build_research_retriever(with_reranker=False)
    second = paper_rag_factory.build_research_retriever(with_reranker=False)
    session = paper_rag_factory.build_paper_rag_session(with_reranker=False)

    assert vector_factory_calls == 1
    assert first._store is second._store is session._chunk_store

    resources.close()
    assert vector_store.close_calls == 1
