from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.research.application import qasper_retrieval_replay
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.qasper_retrieval_replay import (
    QasperRetrievalReplayConfig,
    QasperRetrievalReplayProfile,
    QasperRetrievalReplayService,
)
from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.ports.qasper_query_embedding import QueryVectorSnapshot
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.evaluation.qasper_answer_sampling import sample_questions
from backend.research.rag.evaluation.qasper_mapping import freeze_qasper_dataset
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureFieldIndex,
    OfflineEmbeddingCache,
)
from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker
from backend.research.rag.retrieval.policies import RetrievalPolicy
from infrastructure.research.qasper_evaluation import FilesystemQasperAssets

PAPER = "1900.00001"
EVIDENCE = "We train the BERT encoder on the full corpus."
DIM = 4


def _qa(question_id: str, question: str, answer: dict) -> dict:
    return {
        "question_id": question_id,
        "question": question,
        "answers": [{"answer": {
            "unanswerable": False,
            "extractive_spans": [],
            "free_form_answer": "",
            "yes_no": None,
            "evidence": [],
            "highlighted_evidence": [],
            **answer,
        }}],
    }


def _dataset() -> dict:
    return {
        PAPER: {
            "title": "Fixture paper",
            "abstract": "We study encoders.",
            "full_text": [
                {"section_name": "Introduction", "paragraphs": ["Encoders matter for this task."]},
                {"section_name": "Method", "paragraphs": [EVIDENCE]},
            ],
            "figures_and_tables": [],
            "qas": [
                _qa("q_extractive", "Which encoder is trained?", {
                    "extractive_spans": ["BERT encoder"], "evidence": [EVIDENCE],
                }),
                _qa("q_boolean", "Do they train an encoder?", {
                    "yes_no": True, "evidence": [EVIDENCE],
                }),
                # Preserve source whitespace to verify replay preflight matches the
                # deterministic planner's strip/join query normalization.
                _qa("q_none", "What GPU did they use?  ", {"unanswerable": True}),
            ],
        }
    }


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    for name in ("backend", "framework", "infrastructure", "interfaces"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "module.py").write_text(f"VALUE = '{name}'\n", encoding="utf-8")
    for args in (("init", "-q"), ("add", ".")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "-qm", "f"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


class _Artifacts:
    def __init__(self, source: dict[str, bytes]) -> None:
        self.source = source
        self.published: dict[str, bytes] | None = None

    def read(self, directory: str, name: str) -> bytes:
        assert directory == "source"
        return self.source[name]

    def publish(self, directory: str, objects) -> None:
        assert directory == "output"
        assert self.published is None
        self.published = dict(objects)


class _QueryStore:
    def __init__(self, snapshot: QueryVectorSnapshot | None) -> None:
        self.snapshot = snapshot

    def load(self):
        return self.snapshot

    def save(self, _snapshot) -> None:  # pragma: no cover - replay must never write
        raise AssertionError("retrieval replay attempted to write query vectors")


def _fixture(
    tmp_path: Path,
    repository: Path,
    *,
    omit_query: bool = False,
    omit_field: bool = False,
    bad_identity: bool = False,
):
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    uncached_assets = QasperAssetService(
        FilesystemQasperAssets(dataset_path, None, repository_root=repository)
    )
    initial = uncached_assets.read_assets()
    chunk_texts = {
        chunk.content
        for chunk in initial.chunks
    }
    field_texts = {
        text
        for chunk in initial.chunks
        for text in extract_field_texts(chunk).non_empty().values()
    }
    texts = chunk_texts | field_texts
    if omit_field:
        field_only = sorted(field_texts - chunk_texts)
        assert field_only
        texts.remove(field_only[0])
    cache_path = tmp_path / "cache.npz"
    np.savez(
        cache_path,
        keys=np.asarray([OfflineEmbeddingCache.key(text) for text in sorted(texts)], dtype=str),
        vectors=np.tile(
            np.asarray([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32),
            (len(texts), 1),
        ),
    )
    assets = QasperAssetService(
        FilesystemQasperAssets(dataset_path, cache_path, repository_root=repository)
    )
    loaded = assets.read_assets()
    frozen = freeze_qasper_dataset(loaded.raw_dataset, loaded.documents, loaded.chunks)
    sample = sample_questions(loaded.raw_dataset, seed="qasper-answer-v1", per_type=5)
    sample_payload = sample.manifest()
    query_texts = []
    ask = AskPaperUseCase()
    for index, question in enumerate(sample.questions):
        goal = ask.build_paper_ask_goal(
            paper_id=question.paper_id,
            question=question.question,
            goal_id=f"preflight-{index}",
        )
        query_texts.append(" ".join((question.question.strip(), goal.required_evidence_types[0].strip())))
    query_texts = list(dict.fromkeys(query_texts))
    if omit_query:
        query_texts = query_texts[:-1]
    cache_identity = assets.cache_identity()
    snapshot = QueryVectorSnapshot(
        base_cache_sha256=cache_identity.sha256,
        model_id=cache_identity.model_id,
        dimension=DIM,
        vectors={
            OfflineEmbeddingCache.key(query): (1.0, 0.0, 0.0, 0.0)
            for query in query_texts
        },
    )
    answer_rows = [{"case_id": question.case_id} for question in sample.questions]
    report = {
        "evaluated_cases": len(sample.questions),
        "identity": {
            "dataset_sha256": "wrong" if bad_identity else loaded.dataset_sha256,
            "mapping_digest": frozen.digest,
            "embedding_cache_sha256": cache_identity.sha256,
            "embedding_model": cache_identity.model_id,
            "embedding_dimension": cache_identity.dimension,
            "retrieval_policy": "default",
        },
    }
    artifacts = _Artifacts({
        "sample.json": json.dumps(sample_payload).encode(),
        "report.json": json.dumps(report).encode(),
        "answers.jsonl": b"".join(
            (json.dumps(row) + "\n").encode() for row in answer_rows
        ),
    })
    return assets, artifacts, snapshot, tuple(question.case_id for question in sample.questions)


def _service(
    tmp_path: Path,
    repository: Path,
    *,
    omit_query: bool = False,
    omit_field: bool = False,
    bad_identity: bool = False,
):
    assets, artifacts, snapshot, case_ids = _fixture(
        tmp_path,
        repository,
        omit_query=omit_query,
        omit_field=omit_field,
        bad_identity=bad_identity,
    )
    baseline = QasperRetrievalReplayProfile(
        name="baseline_chunk", policy=RetrievalPolicy(overfetch_multiplier=3),
    )
    rerank = QasperRetrievalReplayProfile(
        name="expanded_field_rerank",
        policy=RetrievalPolicy(overfetch_multiplier=10),
        field_index_enabled=True,
        reranker_enabled=True,
        field_reranker_enabled=True,
    )

    def field_factory(chunks, cache, query_vector):
        return CachedFixtureFieldIndex(chunks, cache, query_vector=query_vector)

    service = QasperRetrievalReplayService(
        assets,
        artifacts,
        query_vector_store=_QueryStore(snapshot),
        profiles=(baseline, rerank),
        config=QasperRetrievalReplayConfig(
            source_run_directory="source", output_directory="output",
        ),
        field_index_factory=field_factory,
        rerankers={"expanded_field_rerank": LightweightLexicalReranker()},
    )
    return service, artifacts, case_ids


def test_replay_preserves_fixed_cases_and_records_real_retrieval_context_and_reranker(
    tmp_path: Path, repository: Path,
) -> None:
    service, artifacts, expected_ids = _service(tmp_path, repository)

    report = service.run()

    assert report["status"] == "completed"
    assert report["case_count"] == len(expected_ids)
    assert report["query_vectors"]["live_calls"] == 0
    assert artifacts.published is not None
    assert set(artifacts.published) == {"manifest.json", "report.json", "cases.jsonl"}
    rows = [json.loads(line) for line in artifacts.published["cases.jsonl"].splitlines()]
    for profile in ("baseline_chunk", "expanded_field_rerank"):
        selected = [row for row in rows if row["profile"] == profile]
        assert tuple(row["case_id"] for row in selected) == expected_ids
        assert all(row["context_budget"]["actual_items"] <= 8 for row in selected)
        assert all(row["candidate_provenance"] for row in selected)
    reranked = [row for row in rows if row["profile"] == "expanded_field_rerank"]
    assert any(row["reranker"]["called"] for row in reranked)
    summaries = {profile["name"]: profile for profile in report["profiles"]}
    assert (
        summaries["baseline_chunk"]["context_coverage"]["strict"]
        ["micro_evidence_groups"]["denominator"]
        == summaries["expanded_field_rerank"]["context_coverage"]["strict"]
        ["micro_evidence_groups"]["denominator"]
    )
    assert "not an answer-quality score" in report["limitations"]["comparison"]


def test_replay_blocks_before_quality_report_when_query_vector_is_missing(
    tmp_path: Path, repository: Path,
) -> None:
    service, artifacts, _case_ids = _service(tmp_path, repository, omit_query=True)

    report = service.run()

    assert report["status"] == "blocked"
    assert report["blocked"]["code"] == "query_vectors_missing"
    assert report["profiles"] == []
    assert len(report["missing_query_vectors"]) == 1
    diagnostic = report["missing_query_vectors"][0]
    assert diagnostic["query_text"]
    assert len(diagnostic["query_digest"]) == 64
    assert diagnostic["query_kinds"] == ["initial_gap_focused"]
    assert diagnostic["retrieval_channels"] == ["preflight_initial_query"]
    assert diagnostic["affected_profiles"] == ["baseline_chunk", "expanded_field_rerank"]
    assert len(diagnostic["affected_case_ids"]) == 1
    assert artifacts.published is not None
    assert artifacts.published["cases.jsonl"] == b""
    blocked_manifest = json.loads(artifacts.published["manifest.json"])
    assert blocked_manifest["missing_query_vectors"] == report["missing_query_vectors"]


def test_replay_blocks_on_source_identity_mismatch(
    tmp_path: Path, repository: Path,
) -> None:
    service, artifacts, _case_ids = _service(tmp_path, repository, bad_identity=True)

    report = service.run()

    assert report["status"] == "blocked"
    assert report["blocked"]["code"] == "source_identity_mismatch"
    assert report["profiles"] == []
    assert artifacts.published is not None
    assert artifacts.published["cases.jsonl"] == b""


def test_replay_blocks_before_first_case_when_selected_field_vector_is_missing(
    tmp_path: Path, repository: Path,
) -> None:
    service, artifacts, _case_ids = _service(tmp_path, repository, omit_field=True)

    report = service.run()

    assert report["status"] == "blocked"
    assert report["blocked"]["code"] == "field_vectors_missing"
    assert report["profiles"] == []
    assert artifacts.published is not None
    assert artifacts.published["cases.jsonl"] == b""


def test_runtime_query_miss_reports_text_case_profile_and_channel_without_coverage(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, artifacts, case_ids = _service(tmp_path, repository)

    class _MissingRewriteSession:
        def __init__(self, chunk_store, **_kwargs) -> None:
            self.chunk_store = chunk_store

        def run(self, _goal, **_kwargs):
            self.chunk_store.search_with_scores(
                PAPER, "planner rewrite absent from cache", filters={}, limit=4,
            )
            raise AssertionError("unreachable")

    monkeypatch.setattr(qasper_retrieval_replay, "PaperRAGSession", _MissingRewriteSession)

    report = service.run()

    assert report["status"] == "blocked"
    assert report["blocked"]["code"] == "query_vectors_missing_during_replay"
    assert report["profiles"] == []
    assert artifacts.published is not None
    assert artifacts.published["cases.jsonl"] == b""
    diagnostic = report["missing_query_vectors"][0]
    assert diagnostic["query_text"] == "planner rewrite absent from cache"
    assert diagnostic["query_kinds"] == ["controlled_replan_or_pipeline_rewrite"]
    assert diagnostic["retrieval_channels"] == ["dense_chunk"]
    assert diagnostic["affected_profiles"] == ["baseline_chunk", "expanded_field_rerank"]
    assert diagnostic["affected_case_ids"] == sorted(case_ids)
