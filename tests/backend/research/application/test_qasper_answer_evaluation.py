from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerEvaluationConfig,
    QasperAnswerRetrievalProfile,
    QasperAnswerEvaluationService,
)
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerSummary
from backend.research.rag.evaluation.qasper_retrieval import (
    CachedFixtureFieldIndex,
    OfflineEmbeddingCache,
    QasperOfflineCacheError,
)
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker
from backend.research.rag.retrieval.paper_answer_generator import ABSTENTION_TOKEN
from backend.research.rag.retrieval.policies import RetrievalPolicy, build_retrieval_policy
from backend.research.rag.retrieval.rerank import RerankCascade
from infrastructure.research.qasper_evaluation import FilesystemQasperArtifacts, FilesystemQasperAssets
from tests.fixtures.paper_answer_model import FakePaperAnswerModel

PAPER = "1900.00001"
EVIDENCE = "We train the BERT encoder on the full corpus."
DIM = 4


def _qa(question_id: str, question: str, answer: dict) -> dict:
    return {
        "question_id": question_id,
        "question": question,
        "answers": [{"answer": {"unanswerable": False, "extractive_spans": [], "free_form_answer": "",
                                "yes_no": None, "evidence": [], "highlighted_evidence": [], **answer}}],
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
                _qa("q_extractive", "Which encoder is trained?", {"extractive_spans": ["BERT encoder"], "evidence": [EVIDENCE]}),
                _qa("q_boolean", "Do they train an encoder?", {"yes_no": True, "evidence": [EVIDENCE]}),
                _qa("q_none", "What GPU did they use?", {"unanswerable": True}),
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
    subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", "commit", "-qm", "f"],
                   cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


class _Embedder:
    model_id = "dashscope/text-embedding-v4"
    dimension = DIM

    def __init__(self) -> None:
        self.calls = 0

    def embed_queries(self, texts):
        self.calls += len(texts)
        return [[1.0, 1.0, 0.0, 0.0] for _ in texts]


class _Store:
    def __init__(self) -> None:
        self.snapshot = None

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot


def _service(
    tmp_path: Path,
    repository: Path,
    *,
    judge_call,
    embedder=None,
    store=None,
    output="out",
    field_profile: bool = False,
    retrieval_policy: RetrievalPolicy | None = None,
):
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    cache_path = tmp_path / "cache.npz"
    # Chunk vectors come from the frozen cache; build it from the chunks the service will index.
    chunks = QasperAssetService(
        FilesystemQasperAssets(dataset_path, None, repository_root=repository)
    ).read_assets().chunks
    texts = {
        chunk.content
        for chunk in chunks
    }
    if field_profile:
        texts.update(
            field_text
            for chunk in chunks
            for field_text in extract_field_texts(chunk).non_empty().values()
        )
    texts = sorted(texts)
    np.savez(cache_path, keys=np.asarray([OfflineEmbeddingCache.key(t) for t in texts], dtype=str),
             vectors=np.tile(np.asarray([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (len(texts), 1)))
    assets = QasperAssetService(FilesystemQasperAssets(dataset_path, cache_path, repository_root=repository))

    def reply(request) -> str:
        if "GPU" in request.user_prompt:
            return f"{ABSTENTION_TOKEN}: the paper does not report hardware"
        if "Do they train" in request.user_prompt:
            return "Yes, they train an encoder [1]."
        return "The BERT encoder [1]."

    embedder = embedder or _Embedder()
    store = store or _Store()
    profile = QasperAnswerRetrievalProfile(
        name="field_lightweight" if field_profile else "chunk_only",
        field_index_enabled=field_profile,
        reranker_enabled=field_profile,
        field_reranker_enabled=field_profile,
    )
    service = QasperAnswerEvaluationService(
        assets,
        FilesystemQasperArtifacts(),
        query_embedder=embedder,
        query_vector_store=store,
        answer_model=FakePaperAnswerModel(reply),
        judge_call=judge_call,
        retrieval_policy=retrieval_policy or build_retrieval_policy(None),
        field_index_factory=(
            (lambda fixture_chunks, cache, query_vector: CachedFixtureFieldIndex(
                fixture_chunks, cache, query_vector=query_vector
            ))
            if field_profile
            else None
        ),
        reranker=LightweightLexicalReranker() if field_profile else None,
        field_reranker=LightweightLexicalReranker() if field_profile else None,
        retrieval_profile=profile,
        config=QasperAnswerEvaluationConfig(output_directory=str(tmp_path / output), per_type=5),
    )
    return service, embedder, store


def test_default_report_keeps_chunk_only_profile_and_fixed_context_budget(
    tmp_path: Path, repository: Path
) -> None:
    service, _embedder, _store = _service(tmp_path, repository, judge_call=None)

    report = service.run()

    identity = report["identity"]
    assert identity["retrieval_profile"]["name"] == "chunk_only"
    assert identity["retrieval_components"] == {
        "field_index": None,
        "reranker": None,
        "field_reranker": None,
    }
    assert identity["context_budget"]["max_context_items"] == 8
    assert identity["context_budget"]["max_context_tokens"] == 2048


def test_selected_field_profile_fails_before_first_question_when_cache_is_incomplete(
    tmp_path: Path, repository: Path
) -> None:
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    cache_path = tmp_path / "cache.npz"
    assets = QasperAssetService(
        FilesystemQasperAssets(dataset_path, None, repository_root=repository)
    )
    chunks = assets.read_assets().chunks
    texts = sorted({chunk.content for chunk in chunks})
    np.savez(
        cache_path,
        keys=np.asarray([OfflineEmbeddingCache.key(text) for text in texts], dtype=str),
        vectors=np.tile(np.asarray([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (len(texts), 1)),
    )
    service, _embedder, _store = _service(
        tmp_path, repository, judge_call=None, field_profile=False, output="preflight"
    )
    # Replace the normal chunk-only service with an explicit field factory while
    # retaining the deliberately incomplete cache fixture.
    service = QasperAnswerEvaluationService(
        QasperAssetService(FilesystemQasperAssets(dataset_path, cache_path, repository_root=repository)),
        FilesystemQasperArtifacts(),
        query_embedder=_embedder,
        query_vector_store=_store,
        answer_model=FakePaperAnswerModel(lambda _request: "unused"),
        judge_call=None,
        retrieval_policy=build_retrieval_policy(None),
        field_index_factory=lambda fixture_chunks, cache, query_vector: CachedFixtureFieldIndex(
            fixture_chunks, cache, query_vector=query_vector
        ),
        retrieval_profile=QasperAnswerRetrievalProfile(name="field_lightweight", field_index_enabled=True),
        config=QasperAnswerEvaluationConfig(output_directory=str(tmp_path / "preflight"), per_type=5),
    )

    with pytest.raises(QasperOfflineCacheError):
        service.run()


def test_explicit_field_profile_reports_real_fixture_components(
    tmp_path: Path, repository: Path
) -> None:
    service, _embedder, _store = _service(
        tmp_path, repository, judge_call=None, field_profile=True, output="field"
    )

    report = service.run()

    identity = report["identity"]
    assert identity["retrieval_profile"]["name"] == "field_lightweight"
    assert identity["retrieval_components"]["field_index"].endswith("CachedFixtureFieldIndex")
    assert identity["retrieval_components"]["reranker"].endswith("LightweightLexicalReranker")
    assert identity["retrieval_policy_settings"]["reranking_intents"] == []
    base_activation = identity["reranker_activation"]["base"]
    assert base_activation["available"] is True
    assert base_activation["can_run"] is True
    assert base_activation["policy_gate_mode"] == "all_intents"
    assert base_activation["eligible_intents"] == "all"
    assert base_activation["calls"] > 0
    assert base_activation["passages"] >= base_activation["calls"]
    assert identity["reranker_activation"]["field"]["calls"] > 0
    fingerprint_payload = {
        "profile": identity["retrieval_profile"],
        "policy": identity["retrieval_policy_settings"],
        "components": identity["retrieval_components"],
        "context_budget": identity["context_budget"],
    }
    expected = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    assert identity["retrieval_profile_fingerprint"] == expected


class _CountingReranker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, passages))
        return [0.75 for _ in passages]


def test_reranker_empty_intents_allow_all_but_explicit_allowlist_still_gates() -> None:
    from backend.research.document.models import PaperChunk

    chunk = PaperChunk(
        chunk_id="c1",
        paper_id="p1",
        parse_source="latex",
        section_title="Method",
        section_role=["method"],
        content="The method trains an encoder.",
    )
    reranker = _CountingReranker()
    allow_all = RerankCascade(RetrievalPolicy(), reranker=reranker)

    assert allow_all.base_scores("How is it trained?", [(chunk, 0.2)], intent="concept_method") == [0.75]
    assert len(reranker.calls) == 1
    gated = RerankCascade(
        RetrievalPolicy(reranking_intents=("numerical_result",)),
        reranker=reranker,
    )
    assert gated.base_scores("How is it trained?", [(chunk, 0.2)], intent="concept_method") == [0.2]
    assert len(reranker.calls) == 1


def test_answer_run_reports_injected_reranker_unused_when_intent_is_not_allowed(
    tmp_path: Path, repository: Path
) -> None:
    policy = RetrievalPolicy(
        reranking_intents=("numerical_result",),
        field_reranking_intents=("numerical_result",),
    )
    service, _embedder, _store = _service(
        tmp_path,
        repository,
        judge_call=None,
        field_profile=True,
        retrieval_policy=policy,
        output="gated-reranker",
    )

    activation = service.run()["identity"]["reranker_activation"]

    assert activation["base"]["available"] is True
    assert activation["base"]["policy_gate_mode"] == "allowlist"
    assert activation["base"]["calls"] == 0
    assert activation["field"]["available"] is True
    assert activation["field"]["calls"] == 0

def test_profile_fingerprint_is_stable_and_context_budget_is_fixed(
    tmp_path: Path, repository: Path
) -> None:
    first, _embedder, _store = _service(tmp_path, repository, judge_call=None, output="fingerprint-a")
    second, _embedder, _store = _service(tmp_path, repository, judge_call=None, output="fingerprint-b")

    first_identity = first.run()["identity"]
    second_identity = second.run()["identity"]

    assert first_identity["retrieval_profile_fingerprint"] == second_identity["retrieval_profile_fingerprint"]
    assert first_identity["context_budget"]["max_context_items"] == 8
    assert first_identity["context_budget"]["max_context_tokens"] == 2048


def test_candidate_overfetch_expands_actual_recall_without_expanding_answer_context(
    tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.research.application import qasper_answer_evaluation as evaluation_module
    from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureChunkStore

    evidence = "Method evidence paragraph 29 contains the trained encoder result."
    paragraphs = [
        evidence if index == 29 else f"Method background paragraph {index} discusses encoder training."
        for index in range(30)
    ]
    dataset = {
        PAPER: {
            "title": "Large fixture paper",
            "abstract": "Encoder study.",
            "full_text": [
                {"section_name": f"Method {index}", "paragraphs": [paragraph]}
                for index, paragraph in enumerate(paragraphs)
            ],
            "figures_and_tables": [],
            "qas": [
                _qa(
                    "q_large",
                    "Which encoder result is reported by the method?",
                    {"extractive_spans": ["trained encoder result"], "evidence": [evidence]},
                )
            ],
        }
    }
    dataset_path = tmp_path / "large-dataset.json"
    dataset_path.write_text(json.dumps(dataset), encoding="utf-8")
    chunks = QasperAssetService(
        FilesystemQasperAssets(dataset_path, None, repository_root=repository)
    ).read_assets().chunks
    assert len(chunks) >= 30
    cache_path = tmp_path / "large-cache.npz"
    texts = sorted({chunk.content for chunk in chunks})
    np.savez(
        cache_path,
        keys=np.asarray([OfflineEmbeddingCache.key(text) for text in texts], dtype=str),
        vectors=np.tile(np.asarray([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32), (len(texts), 1)),
    )

    class SpyStore(CachedFixtureChunkStore):
        observations: list[tuple[int, int]] = []

        def search_with_scores(self, paper_id, query_text, *, filters=None, limit=10):
            results = super().search_with_scores(
                paper_id, query_text, filters=filters, limit=limit,
            )
            self.observations.append((limit, len(results)))
            return results

    monkeypatch.setattr(evaluation_module, "CachedFixtureChunkStore", SpyStore)

    def run(overfetch: int, output_name: str) -> tuple[dict, int]:
        SpyStore.observations.clear()
        service = QasperAnswerEvaluationService(
            QasperAssetService(
                FilesystemQasperAssets(dataset_path, cache_path, repository_root=repository)
            ),
            FilesystemQasperArtifacts(),
            query_embedder=_Embedder(),
            query_vector_store=_Store(),
            answer_model=FakePaperAnswerModel(lambda _request: "The trained encoder result is reported [1]."),
            judge_call=None,
            retrieval_policy=RetrievalPolicy(overfetch_multiplier=overfetch),
            retrieval_profile=QasperAnswerRetrievalProfile(name=f"overfetch-{overfetch}"),
            config=QasperAnswerEvaluationConfig(
                output_directory=str(tmp_path / output_name), per_type=1,
            ),
        )
        report = service.run()
        rows = [
            json.loads(line)
            for line in (tmp_path / output_name / "answers.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert rows and all(len(row["context_chunk_ids"]) <= 8 for row in rows)
        return report, max(returned for _limit, returned in SpyStore.observations)

    baseline, baseline_returned = run(3, "overfetch-3")
    expanded, expanded_returned = run(10, "overfetch-10")

    assert expanded_returned > baseline_returned
    assert baseline["identity"]["context_budget"]["max_context_items"] == 8
    assert expanded["identity"]["context_budget"]["max_context_items"] == 8
    assert (
        baseline["identity"]["retrieval_profile_fingerprint"]
        != expanded["identity"]["retrieval_profile_fingerprint"]
    )


def test_end_to_end_run_scores_every_stratum_and_publishes_artifacts(tmp_path: Path, repository: Path) -> None:
    async def judge(prompt: str) -> str:
        return '{"correct": true, "citations": [{"index": 1, "supports": true}], "rationale": "matches"}'

    service, embedder, store = _service(tmp_path, repository, judge_call=judge)

    report = service.run()

    metrics = report["metrics"]
    assert report["evaluated_cases"] == 3
    assert report["sample"]["selected_by_type"] == {"extractive": 1, "abstractive": 0, "boolean": 1, "none": 1}
    assert metrics["system_failure"]["numerator"] == 0
    assert metrics["answer_f1_by_stratum"]["boolean"]["value"] == 1.0
    assert metrics["answer_f1_by_stratum"]["none"]["value"] == 1.0
    assert metrics["correct_abstention"] == {"value": 1.0, "numerator": 1, "denominator": 1}
    assert metrics["judge_correct_on_answered"]["denominator"] == metrics["outcome_counts"].get("answered", 0)
    # Rewritten queries were embedded live once each and persisted for reuse.
    assert embedder.calls == metrics["query_vectors"]["live_calls"] > 0
    assert store.snapshot is not None and len(store.snapshot.vectors) == embedder.calls

    out = tmp_path / "out"
    assert sorted(p.name for p in out.iterdir()) == ["answers.jsonl", "report.json", "sample.json"]
    rows = [json.loads(line) for line in (out / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    disk_report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert {row["stratum"] for row in rows} == {"extractive", "boolean", "none"}
    assert all(len(row["context_chunk_ids"]) <= 8 for row in rows)
    assert disk_report["identity"] == report["identity"]
    none_row = next(row for row in rows if row["stratum"] == "none")
    assert none_row["outcome"] in {"abstained", "insufficient_evidence"}
    assert none_row["prediction"] == "Unanswerable" and none_row["judge"] is None
    assert disk_report["protocol"] == "qasper_end_to_end_answer_v1"


def test_rerun_reuses_persisted_query_vectors(tmp_path: Path, repository: Path) -> None:
    service, embedder, store = _service(tmp_path, repository, judge_call=None)
    service.run()
    first_calls = embedder.calls

    # A fresh service on the same supplementary store must not embed anything again.
    rerun, _, _ = _service(tmp_path, repository, judge_call=None, embedder=embedder, store=store, output="out2")
    report = rerun.run()

    assert first_calls > 0
    assert embedder.calls == first_calls
    assert report["metrics"]["query_vectors"]["live_calls"] == 0
    assert report["metrics"]["query_vectors"]["supplementary_hits"] > 0
    assert report["metrics"]["efficiency"]["judge_calls"] == 0


def test_summary_excludes_judge_errors_and_undefined_coverage_from_denominators() -> None:
    from backend.research.rag.evaluation.qasper_answer_judge import JudgeVerdict
    from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerCase
    from framework.rag.evaluation.answer_metrics import AnswerOutcome

    def case(case_id, outcome, judge, coverage, reference_types=("extractive",)):
        return QasperAnswerCase(
            case_id=case_id, paper_id="p", question="Q?", stratum="extractive", references=("A",),
            reference_types=reference_types, mapping_disposition="valid" if coverage is not None else "needs_review",
            outcome=outcome, answer="A [1]" if outcome is AnswerOutcome.ANSWERED else "", prediction="A",
            answer_f1=1.0, matched_type="extractive", cited_chunk_ids=("c",), context_chunk_ids=("c",),
            strict_citation_coverage=coverage, equivalent_citation_coverage=coverage,
            strict_context_coverage=coverage, equivalent_context_coverage=coverage, judge=judge,
            decision_reason="", abstention_reason="", end_to_end_ms=1.0, generation_ms=None,
            prompt_chars=None, model="m", model_usage=None,
        )

    summary = QasperAnswerSummary((
        case("ok", AnswerOutcome.ANSWERED, JudgeVerdict(True), 1.0),
        case("judge-error", AnswerOutcome.ANSWERED, JudgeVerdict(None, error="judge_output_not_json"), None),
        case("halted", AnswerOutcome.HALTED, None, 1.0),
    )).to_dict()

    assert summary["judge_correct_on_answered"] == {"value": 1.0, "numerator": 1, "denominator": 1}
    assert summary["judge_errors"] == 1
    assert summary["strict_citation_coverage"]["denominator"] == 1
    # A system failure is wrong end to end; an ungraded judge error is excluded.
    assert summary["end_to_end_correct"] == {"value": 0.5, "numerator": 1, "denominator": 2}
    assert summary["system_failure"]["numerator"] == 1
