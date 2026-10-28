"""Reader Q&A through the production Research context runtime.

Regression for the desktop ask path: with the compaction-verified assembler the
session must (1) run inside a binding of its run-bound artifact port and (2) be
admitted against the answer model's window rather than the RAG item budget.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.paper_rag_session import ContextWindowLimits, PaperRAGSession
from backend.research.document.models import PaperChunk
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.ports.paper_answer_model import PaperAnswerModelResponse
from backend.research.rag.adapters import PaperAnswerWorker
from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureChunkStore, OfflineEmbeddingCache
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from framework.harness.control_plane.harness import InMemoryHarnessEventPort
from infrastructure.research.artifact_port import ArtifactRunBindingError, FilesystemHarnessArtifactPort
from infrastructure.research.context_runtime import build_research_context_assembler

RUN_ID = "reader-ask-run"
PAPER_ID = "paper-1"
MAX_INPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 2048


class _StubAnswerModel:
    async def complete(self, request):
        return PaperAnswerModelResponse(text="The corpus was transcribed by annotators [1].", model="stub")


def _chunks() -> list[PaperChunk]:
    # Long passages: the admitted evidence exceeds the default 2048-token RAG budget
    # while staying far below the model window.
    sentence = "The corpus was transcribed by trained annotators following a written protocol. "
    return [
        PaperChunk(
            chunk_id=f"chunk-{index}",
            paper_id=PAPER_ID,
            parse_source="latex",
            section_title=f"Data collection {index}",
            section_role=["method"],
            content=f"Passage {index}. " + sentence * 18,
            metadata={"run_id": RUN_ID},
        )
        for index in range(8)
    ]


def _store(chunks: list[PaperChunk]) -> CachedFixtureChunkStore:
    rng = np.random.default_rng(7)
    base = rng.normal(size=16)
    vectors = {}
    for chunk in chunks:
        noisy = base + 0.01 * rng.normal(size=16)
        vectors[OfflineEmbeddingCache.key(chunk.content)] = (noisy / np.linalg.norm(noisy)).tolist()
    cache = OfflineEmbeddingCache.from_vectors(vectors, cache_id="reader-ask-fixture")
    query = (base / np.linalg.norm(base)).astype(np.float32)
    return CachedFixtureChunkStore(chunks, cache, query_vector=lambda _text: query)


def _session(artifacts: FilesystemHarnessArtifactPort, *, window: bool, scoped: bool) -> PaperRAGSession:
    def factory(_spec):
        return build_research_context_assembler(
            artifact_port=artifacts,
            event_port=InMemoryHarnessEventPort(),
            provider="openai-compatible",
            model="reader-model",
            max_input_tokens=MAX_INPUT_TOKENS,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

    return PaperRAGSession(
        _store(_chunks()),
        answer_worker=PaperAnswerWorker(AnswerGenerator(_StubAnswerModel())),
        generation_policy={"enabled": True, "max_attempts": 2},
        context_assembler_factory=factory,
        context_window=ContextWindowLimits(MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS) if window else None,
        run_scope=artifacts.bind_run if scoped else None,
    )


def _ask(session: PaperRAGSession):
    identity = build_paper_analysis_context_graph_identity(
        run_id=RUN_ID, stage_id="run_research_rag",
    ).with_physical_activity(
        node_id="run_research_rag", node_instance_id="reader-node", activity_id="reader-activity",
        activity_attempt=1,
    )
    goal = AskPaperUseCase().build_paper_ask_goal(
        paper_id=PAPER_ID, question="Who transcribed the corpus?", goal_id="reader-goal", run_id=RUN_ID,
    )
    return session.run(goal, graph_identity=identity, session_id="reader-session")


def test_reader_ask_is_admitted_and_writes_snapshots_to_its_own_bound_store(tmp_path: Path) -> None:
    reader_root = tmp_path / "rag" / "context"
    research_root = tmp_path / "runs"
    research_root.mkdir()
    artifacts = FilesystemHarnessArtifactPort(reader_root)

    result = _ask(_session(artifacts, window=True, scoped=True))

    assert result.status.value == "answered"
    assert result.context_pack.accepted_evidence
    assert (reader_root / RUN_ID).is_dir()
    assert artifacts.current_run_id is None
    # The terminal Research run store is never written by reader Q&A.
    assert list(research_root.iterdir()) == []


def test_reader_ask_without_run_binding_cannot_persist_context_snapshots(tmp_path: Path) -> None:
    artifacts = FilesystemHarnessArtifactPort(tmp_path / "context")

    with pytest.raises(ArtifactRunBindingError):
        _ask(_session(artifacts, window=True, scoped=False))


def test_reader_ask_against_the_rag_item_budget_is_refused_by_the_runtime(tmp_path: Path) -> None:
    artifacts = FilesystemHarnessArtifactPort(tmp_path / "context")

    result = _ask(_session(artifacts, window=False, scoped=True))

    # The refusal fails this session before any model call instead of raising out of it.
    assert result.status.value == "failed"
    assert result.answer is None
    failure = result.decision.metadata["context_admission_error"]
    assert failure["code"] == "context_admission_rejected"
    assert failure["retryable"] is False
    assert failure["details"]["status"] == "protected_context_exceeds_window"
