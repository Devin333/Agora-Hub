from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerEvaluationConfig,
    QasperAnswerEvaluationService,
)
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.rag.evaluation.qasper_answer_report import QasperAnswerSummary
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache
from backend.research.rag.retrieval.paper_answer_generator import ABSTENTION_TOKEN
from backend.research.rag.retrieval.policies import build_retrieval_policy
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


def _service(tmp_path: Path, repository: Path, *, judge_call, embedder=None, store=None, output="out"):
    dataset_path = tmp_path / "dataset.json"
    dataset_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    cache_path = tmp_path / "cache.npz"
    # Chunk vectors come from the frozen cache; build it from the chunks the service will index.
    chunks = QasperAssetService(FilesystemQasperAssets(dataset_path, None, repository_root=repository)).read_assets().chunks
    texts = sorted({chunk.content for chunk in chunks})
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
    service = QasperAnswerEvaluationService(
        assets,
        FilesystemQasperArtifacts(),
        query_embedder=embedder,
        query_vector_store=store,
        answer_model=FakePaperAnswerModel(reply),
        judge_call=judge_call,
        retrieval_policy=build_retrieval_policy(None),
        config=QasperAnswerEvaluationConfig(output_directory=str(tmp_path / output), per_type=5),
    )
    return service, embedder, store


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
    assert {row["stratum"] for row in rows} == {"extractive", "boolean", "none"}
    none_row = next(row for row in rows if row["stratum"] == "none")
    assert none_row["outcome"] in {"abstained", "insufficient_evidence"}
    assert none_row["prediction"] == "Unanswerable" and none_row["judge"] is None
    assert json.loads((out / "report.json").read_text(encoding="utf-8"))["protocol"] == "qasper_end_to_end_answer_v1"


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
