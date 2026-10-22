from __future__ import annotations

from framework.rag.evaluation import AnswerOutcome

import json
from pathlib import Path

import pytest

from backend.research.rag.evaluation.paper_benchmark_suite import (
    BenchmarkSuiteConfig,
    GoldEvidenceAuditItem,
    GoldEvidenceJudgeItem,
    GoldEvidenceJudgeReport,
    _answer_judge_failure_reasons,
    _evidence_preview,
    _evidence_pack_required_context_ids,
    _gold_fix_record,
    _gold_judge_sample,
    _judge_prompt,
    _hydrate_retrieval_with_evidence_pack,
    _spot_check_report_from_annotations,
    _spot_check_schema_errors,
    _write_answer_fix_artifacts,
    audit_question_ambiguity,
    audit_gold_evidence,
    run_benchmark_suite,
    split_paper_ids,
)
from backend.research.rag.evaluation.paper_answer_eval import (
    OVERCONSERVATIVE_ABSTENTION_REASON,
    WRONG_ABSTENTION_REASON,
    EvidenceAnswerEvaluator,
    EvidenceAnswerSample,
)
from backend.research.rag.evaluation.paper_benchmark_matrix import (
    BenchmarkMatrixConfig,
    BenchmarkMatrixDataset,
    load_benchmark_matrix_datasets,
    run_benchmark_matrix,
)
from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
from backend.research.rag.evaluation.paper_generation_eval import (
    GenerationEvalResult,
    GenerationSampleJudgment,
    GenerationScores,
)
from backend.research.rag.cli.run_benchmark_suite import _parse_thresholds, main
from backend.research.rag.cli.run_benchmark_matrix import _datasets_from_args, _build_parser as _build_matrix_parser
from backend.research.rag.retrieval.paper_answer_generator import AnswerContextAssembler, GeneratedAnswer
from backend.research.rag.retrieval.paper_retriever import RetrievalResult
from tests.fixtures.paper_answer_model import FakePaperAnswerModel


class _FakeGoldJudge:
    def judge(self, items):
        judged = tuple(
            GoldEvidenceJudgeItem(
                question=item.question,
                paper_id=item.paper_id,
                qa_type=item.qa_type,
                status="pass",
                reason="supported_by_fake_judge",
                supported=True,
                confidence=0.9,
            )
            for item in items
        )
        return GoldEvidenceJudgeReport(
            mode="fake",
            provider="fake",
            model="fake-gold-judge",
            sample_size=len(judged),
            passed=len(judged),
            warning=0,
            failed=0,
            error=0,
            items=judged,
        )


class _RepairArtifactGoldJudge:
    def judge(self, items):
        statuses = ("warning", "fail", "error")
        judged = tuple(
            GoldEvidenceJudgeItem(
                question=item.question,
                paper_id=item.paper_id,
                qa_type=item.qa_type,
                status=statuses[index % len(statuses)],
                reason=f"{statuses[index % len(statuses)]}_by_fake_judge",
                supported=False,
                confidence=0.2,
                gold_chunk_ids=item.gold_chunk_ids,
                equivalent_gold_chunk_ids=item.equivalent_gold_chunk_ids,
            )
            for index, item in enumerate(items)
        )
        counts = {status: sum(1 for item in judged if item.status == status) for status in statuses}
        return GoldEvidenceJudgeReport(
            mode="fake",
            provider="fake",
            model="fake-repair-artifact-gold-judge",
            sample_size=len(judged),
            passed=0,
            warning=counts["warning"],
            failed=counts["fail"],
            error=counts["error"],
            items=judged,
        )


class _ChunkLookup:
    def __init__(self, chunks: list[PaperChunk]) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self._chunks.get(chunk_id)


def _fake_answer_text(request) -> str:
    return (
        "Figure 1: architecture overview. Table 1: accuracy results. "
        "The table reports accuracy 95, Equation 1 defines the score, "
        "and s = x + y. [1]"
    )


def _fake_answer_model() -> FakePaperAnswerModel:
    return FakePaperAnswerModel(_fake_answer_text)


async def _fake_judge_llm(prompt: str) -> str:
    if "claims" in prompt and "citation_checks" in prompt:
        return json.dumps({
            "claims": [
                {
                    "claim_text": "The answer is supported by the provided context [1].",
                    "verdict": "supported",
                    "support_chunk_ids": ["para-p1"],
                    "reason": "fake judge support",
                }
            ],
            "citation_checks": [
                {
                    "claim_text": "The answer is supported by the provided context [1].",
                    "cited_chunk_ids": ["para-p1"],
                    "support_chunk_ids": ["para-p1"],
                    "citation_supports_claim": True,
                    "wrong_citation": False,
                    "missing_citation": False,
                    "reason": "fake citation support",
                }
            ],
            "answer_relevance": 1.0,
            "context_precision": 1.0,
            "reason": "fake structured answer judge",
        })
    if "Score (0-100)" in prompt:
        return "100"
    if "Verdicts (yes/no per passage)" in prompt:
        return "yes\nyes\nyes"
    return "yes"


async def _fake_bad_answer_judge_llm(prompt: str) -> str:
    if "claims" in prompt and "citation_checks" in prompt:
        return json.dumps({
            "claims": [
                {
                    "claim_text": "The generated answer states an unsupported result.",
                    "verdict": "insufficient",
                    "support_chunk_ids": [],
                    "reason": "No supporting context.",
                }
            ],
            "citation_checks": [
                {
                    "claim_text": "The generated answer states an unsupported result.",
                    "cited_chunk_ids": [],
                    "support_chunk_ids": [],
                    "citation_supports_claim": False,
                    "wrong_citation": False,
                    "missing_citation": True,
                    "reason": "The claim has no citation.",
                }
            ],
            "answer_relevance": 0.4,
            "context_precision": 0.2,
            "reason": "unsupported answer",
        })
    return "0"


def _paper_chunk(
    *,
    chunk_id: str,
    chunk_type: str,
    content: str,
    metadata: dict | None = None,
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type=chunk_type,
        has_figure=chunk_type == "figure",
        has_table=chunk_type == "table",
        content=content,
        metadata=metadata or {},
    )


def _audit_item(paper_id: str, qa_type: str, question: str, *, status: str) -> GoldEvidenceAuditItem:
    return GoldEvidenceAuditItem(
        question=question,
        paper_id=paper_id,
        qa_type=qa_type,
        status=status,
        reason="test",
        gold_chunk_ids=(f"{paper_id}-gold",),
        answer_facts_present=True,
    )


def test_split_paper_ids_is_stable_and_non_empty() -> None:
    paper_ids = [f"p{i}" for i in range(10)]

    first = split_paper_ids(paper_ids, seed="seed")
    second = split_paper_ids(list(reversed(paper_ids)), seed="seed")

    assert first == second
    assert set(first) == {"train", "dev", "test"}
    assert all(first[name] for name in ("train", "dev", "test"))
    assert sorted(first["train"] + first["dev"] + first["test"]) == sorted(paper_ids)


def test_audit_gold_evidence_reports_missing_chunks() -> None:
    pair = EvidenceQAPair(
        question="What does Table 1 show?",
        paper_id="p1",
        qa_type="table_qa",
        gold_chunk_ids=["missing"],
        required_evidence_types=["table"],
    )

    report = audit_gold_evidence([pair], [], sample_size=10, seed="seed")

    assert report.failed == 1
    assert report.items[0].reason == "missing_gold_chunks"


def test_audit_gold_evidence_accepts_latex_table_without_image_ref() -> None:
    table = _paper_chunk(
        chunk_id="tbl1",
        chunk_type="table",
        content="Caption: Performance of architectural variants. Rows: baseline 31.2, large 33.8.",
        metadata={
            "source_locator": "paper://p1/table/1",
            "caption_text": "Performance of architectural variants.",
            "table_rows": [{"model": "baseline", "score": "31.2"}],
        },
    )
    pair = EvidenceQAPair(
        question="What quantitative evidence is reported for the architectural variants?",
        paper_id="p1",
        qa_type="table_qa",
        gold_chunk_ids=["tbl1"],
        required_evidence_types=["table"],
        answer_facts=["baseline 31.2"],
    )

    report = audit_gold_evidence([pair], [table], sample_size=10, seed="seed")

    assert report.passed == 1
    assert report.warning == 0
    assert report.items[0].image_ref_count == 0
    assert report.items[0].reason == "ok"


def test_audit_gold_evidence_accepts_caption_only_figure_without_image_ref() -> None:
    figure = _paper_chunk(
        chunk_id="fig1",
        chunk_type="figure",
        content="Caption: Schematic of the baseline objective and training process.",
        metadata={
            "source_locator": "paper://p1/figure/1",
            "caption_text": "Schematic of the baseline objective and training process.",
        },
    )
    pair = EvidenceQAPair(
        question="What visual evidence explains the baseline objective and training process?",
        paper_id="p1",
        qa_type="figure_qa",
        gold_chunk_ids=["fig1"],
        required_evidence_types=["figure"],
        answer_facts=["baseline objective"],
    )

    report = audit_gold_evidence([pair], [figure], sample_size=10, seed="seed")

    assert report.passed == 1
    assert report.warning == 0
    assert report.items[0].image_ref_count == 0
    assert report.items[0].reason == "ok"


def test_audit_gold_evidence_warns_for_figure_without_image_or_textual_evidence() -> None:
    figure = _paper_chunk(
        chunk_id="fig1",
        chunk_type="figure",
        content="Figure.",
        metadata={"source_locator": "paper://p1/figure/1"},
    )
    pair = EvidenceQAPair(
        question="What visual evidence explains the baseline objective?",
        paper_id="p1",
        qa_type="figure_qa",
        gold_chunk_ids=["fig1"],
        required_evidence_types=["figure"],
        answer_facts=["baseline objective"],
    )

    report = audit_gold_evidence([pair], [figure], sample_size=10, seed="seed")

    assert report.warning == 1
    assert report.items[0].reason == "visual_qa_without_image_ref"


def test_run_benchmark_suite_writes_splits_without_fixed_window_by_default(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        gold_judge_mode="fake",
        gold_judge_sample_size=2,
        gold_evidence_judge=_FakeGoldJudge(),
    ))

    assert result.papers_total == 3
    assert (output_dir / "benchmark_suite_report.json").exists()
    assert (output_dir / "benchmark_suite_report.md").exists()
    assert (output_dir / "test" / "candidate" / "evidence_regression_report.json").exists()
    assert not (output_dir / "test" / "fixed_window").exists()
    assert (output_dir / "train" / "golden_set.json").exists()
    assert (output_dir / "dev" / "golden_set.json").exists()
    assert (output_dir / "test" / "golden_set.json").exists()
    assert result.baseline_test_report is None
    assert result.ab_report is None
    assert result.gold_judge is not None
    assert result.gold_judge.model == "fake-gold-judge"
    payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    assert payload["evaluation_protocol"]["reported_split"] == "test"
    assert payload["baseline_test_report"] is None
    assert payload["ab_report"] is None
    breakdown_summary = payload["candidate_test_report"]["retrieval"]["score_breakdown_summary"]
    assert breakdown_summary["evidence_count"] > 0
    assert "final_score" in breakdown_summary["components"]
    retrieval_payload = payload["candidate_test_report"]["retrieval"]
    assert set(retrieval_payload["by_k"]) >= {"3", "5", "10"}
    assert "evidence_coverage" in retrieval_payload["by_k"]["5"]
    assert "source_locator_coverage" in retrieval_payload["by_k"]["5"]
    assert retrieval_payload["intent_distribution"]
    assert retrieval_payload["route_distribution"]
    assert retrieval_payload["intent_confusion"]
    scorecard_metadata = payload["candidate_test_report"]["rag_evaluation_report"]["scorecard"]["metadata"]
    assert scorecard_metadata["score_breakdown_summary"]["evidence_count"] == breakdown_summary["evidence_count"]
    field_distribution = retrieval_payload["field_embedding_distribution"]
    assert field_distribution["search_hits_by_field"]
    assert field_distribution["matched_evidence_count"] > 0
    markdown = (output_dir / "benchmark_suite_report.md").read_text(encoding="utf-8")
    assert "## Score Breakdown" in markdown
    assert "candidate Hit@3/5/10" in markdown
    assert "candidate evidence coverage@3/5/10" in markdown
    assert "candidate source locator coverage@3/5/10" in markdown
    assert "## Field Embedding Distribution" in markdown
    assert "## Route Distribution" in markdown
    assert "## Intent Confusion" in markdown


def test_run_benchmark_suite_expands_gold_audit_sample_for_judge(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=tmp_path / "suite",
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=1,
        gold_judge_mode="fake",
        gold_judge_sample_size=3,
        gold_evidence_judge=_FakeGoldJudge(),
    ))

    assert result.gold_judge is not None
    assert result.gold_judge.sample_size == 3


def test_run_benchmark_suite_can_write_fixed_window_baseline_when_requested(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        include_fixed_window_baseline=True,
    ))

    assert (output_dir / "test" / "fixed_window" / "evidence_regression_report.json").exists()
    assert result.baseline_test_report is not None
    assert result.ab_report is not None
    assert result.ab_report["baseline_name"] == "fixed_window"
    assert "mrr" in result.ab_report["deltas"]
    assert "relative_improvement" in result.ab_report


def test_run_benchmark_suite_writes_blind_detemplated_protocol(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        gold_judge_mode="fake",
        gold_judge_sample_size=2,
        gold_evidence_judge=_FakeGoldJudge(),
        question_profile="blind_detemplated",
    ))

    payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark_suite_report.md").read_text(encoding="utf-8")
    golden_records = []
    for split in ("train", "dev", "test"):
        golden_records.extend(json.loads((output_dir / split / "golden_set.json").read_text(encoding="utf-8")))
    profiled = [record for record in golden_records if record["metadata"].get("question_profile") == "blind_detemplated"]

    assert result.question_profile == "blind_detemplated"
    assert payload["evaluation_protocol"]["question_profile"] == "blind_detemplated"
    assert payload["evaluation_protocol"]["blind_test"] is True
    assert "question profile: `blind_detemplated`" in markdown
    assert profiled
    assert all("template_question" in record["metadata"] for record in profiled)
    assert not any("Table 1:" in record["question"] for record in profiled)
    assert not any("Figure 1:" in record["question"] for record in profiled)


def test_run_benchmark_suite_writes_blind_semantic_protocol_and_question_audit(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        gold_judge_mode="fake",
        gold_judge_sample_size=2,
        gold_evidence_judge=_FakeGoldJudge(),
        question_profile="blind_semantic",
    ))

    payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark_suite_report.md").read_text(encoding="utf-8")
    golden_records = []
    for split in ("train", "dev", "test"):
        golden_records.extend(json.loads((output_dir / split / "golden_set.json").read_text(encoding="utf-8")))
    profiled = [record for record in golden_records if record["metadata"].get("question_profile") == "blind_semantic"]

    assert result.question_profile == "blind_semantic"
    assert payload["evaluation_protocol"]["question_profile"] == "blind_semantic"
    assert payload["evaluation_protocol"]["blind_test"] is True
    assert payload["evaluation_protocol"]["detemplate_policy"] == "semantic_anchors_no_labels_v1"
    assert payload["question_audit"]["total"] == payload["pairs_total"]
    assert "## Question Ambiguity Audit" in markdown
    assert "question profile: `blind_semantic`" in markdown
    assert profiled
    assert all("template_question" in record["metadata"] for record in profiled)
    assert any(record["metadata"].get("semantic_anchors") for record in profiled)
    assert not any("Table 1:" in record["question"] for record in profiled)
    assert not any("Figure 1:" in record["question"] for record in profiled)
    assert not any("Equation 1" in record["question"] for record in profiled)


def test_run_benchmark_suite_can_report_policy_promotion_checklist(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        question_profile="blind_semantic",
        retrieval_policy="paper_blind_semantic_rag_v1",
    ))

    payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark_suite_report.md").read_text(encoding="utf-8")
    checklist = payload["policy_promotion_checklist"]
    distribution = payload["candidate_test_report"]["retrieval"]["rerank_distribution"]

    assert result.policy_promotion_checklist.policy_name == "paper_blind_semantic_rag_v1"
    assert (output_dir / "policy_promotion_checklist.json").exists()
    assert (output_dir / "policy_promotion_checklist.md").exists()
    assert checklist["policy_name"] == "paper_blind_semantic_rag_v1"
    assert checklist["ready_for_promotion"] is False
    assert any(check["check_id"] == "answer_success" and check["status"] == "fail" for check in checklist["checks"])
    assert any(check["check_id"] == "overall_hit_at_3" for check in checklist["checks"])
    assert any(check["check_id"] == "overall_hit_at_5" for check in checklist["checks"])
    assert any(check["check_id"] == "overall_evidence_coverage_at_5" for check in checklist["checks"])
    assert any(check["check_id"] == "overall_source_locator_coverage_at_5" for check in checklist["checks"])
    assert any(check["check_id"] == "top_k_retrieval_metrics" for check in checklist["checks"])
    assert any(check["check_id"] == "strict_equivalent_hit_at_10_gap" for check in checklist["checks"])
    assert any(check["check_id"] == "answer_diagnostics" for check in checklist["checks"])
    assert any(check["check_id"] == "true_missing_gold_rate" for check in checklist["checks"])
    assert any(check["check_id"] == "claim_support_coverage" for check in checklist["checks"])
    assert "## Policy Promotion Checklist" in markdown
    assert result.candidate_test_report["metadata"]["lightweight_reranker_enabled"] is True
    assert result.candidate_test_report["metadata"]["retrieval_policy"] == "paper_blind_semantic_rag_v1"
    assert distribution["reranker_enabled_sample_count"] > 0
    assert distribution["reranked_evidence_count"] > 0
    assert distribution["max_score"] > 0.0
    assert "## Rerank Distribution" in markdown


def test_question_ambiguity_audit_flags_duplicate_ambiguous_and_label_leakage() -> None:
    pairs = [
        EvidenceQAPair(
            question="What quantitative evidence reports accuracy?",
            paper_id="p1",
            qa_type="table_qa",
            gold_chunk_ids=["tbl1"],
            required_evidence_types=["table"],
            metadata={"question_profile": "blind_semantic", "semantic_anchors": ["accuracy", "benchmark"]},
        ),
        EvidenceQAPair(
            question="What quantitative evidence reports accuracy?",
            paper_id="p1",
            qa_type="table_qa",
            gold_chunk_ids=["tbl2"],
            required_evidence_types=["table"],
            metadata={"question_profile": "blind_semantic", "semantic_anchors": ["accuracy", "benchmark"]},
        ),
        EvidenceQAPair(
            question="What does Table 1 show?",
            paper_id="p1",
            qa_type="table_qa",
            gold_chunk_ids=["tbl1"],
            required_evidence_types=["table"],
            metadata={"question_profile": "blind_semantic", "semantic_anchors": ["accuracy"]},
        ),
    ]

    report = audit_question_ambiguity(pairs, [])

    assert report.total == 3
    assert report.duplicate_questions == 2
    assert report.ambiguous_questions == 2
    assert report.missing_semantic_anchor == 1
    assert report.label_leakage == 1
    assert {reason for item in report.items for reason in item.reasons} >= {
        "duplicate_question",
        "ambiguous_question",
        "missing_semantic_anchor",
        "label_leakage",
    }


def test_run_benchmark_suite_writes_answer_eval_judge_and_spot_check(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))
    annotations_path = tmp_path / "spot_annotations.jsonl"
    annotations_path.write_text(
        "\n".join([
            json.dumps({
                "paper_id": "p1",
                "qa_type": "figure_qa",
                "question": "What visual evidence is shown?",
                "gold_evidence_ok": True,
                "answer_ok": True,
                "citation_ok": True,
                "label": "pass",
                "reason": "gold and answer are supported",
                "annotator": "tester",
                "reviewed_at": "2026-06-30",
            }),
            json.dumps({
                "paper_id": "p2",
                "qa_type": "table_qa",
                "question": "What quantitative evidence is shown?",
                "gold_evidence_ok": True,
                "answer_ok": False,
                "citation_ok": True,
                "label": "needs_fix",
                "reason": "answer missed the table conclusion",
                "annotator": "tester",
                "reviewed_at": "2026-06-30",
            }),
        ]),
        encoding="utf-8",
    )
    output_dir = tmp_path / "suite"

    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        answer_eval_enabled=True,
        answer_eval_sample_size=2,
        answer_model=_fake_answer_model(),
        answer_judge_mode="llm",
        answer_judge_sample_size=1,
        answer_judge_llm_call=_fake_judge_llm,
        spot_check_sample_size=2,
        spot_check_annotations_path=annotations_path,
        quality_thresholds={"answer.success_rate": 1.1},
    ))

    candidate_dir = output_dir / "test" / "candidate"
    candidate = result.candidate_test_report
    report_payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark_suite_report.md").read_text(encoding="utf-8")
    answer_samples = (candidate_dir / "answer_samples.jsonl").read_text(encoding="utf-8").splitlines()
    answer_judge_report = json.loads((candidate_dir / "answer_judge_report.json").read_text(encoding="utf-8"))
    answer_judge_samples = (candidate_dir / "answer_judge_samples.jsonl").read_text(encoding="utf-8").splitlines()
    answer_fix_manifest = json.loads((candidate_dir / "answer_fix_manifest.json").read_text(encoding="utf-8"))
    spot_samples = (candidate_dir / "spot_check_samples.jsonl").read_text(encoding="utf-8").splitlines()
    answer_sample_records = [json.loads(line) for line in answer_samples]

    assert candidate["answer"]["total"] == 2
    assert candidate["generation"]["total"] == 1
    assert candidate["generation"]["claim_support_rate"] == 1.0
    assert candidate["generation"]["citation_claim_support_rate"] == 1.0
    assert candidate["spot_check"]["annotated_count"] == 2
    assert report_payload["spot_check"]["label_counts"] == {"needs_fix": 1, "pass": 1}
    assert report_payload["spot_check"]["pass_rate"] == 0.5
    assert report_payload["spot_check"]["human_answer_ok_rate"] == 0.5
    assert report_payload["spot_check"]["human_citation_ok_rate"] == 1.0
    assert report_payload["spot_check"]["fail_count"] == 1
    assert report_payload["spot_check"]["schema_error_count"] == 0
    assert report_payload["spot_check"]["by_qa_type"]["figure_qa"]["pass"] == 1
    assert any(
        check["check_id"] == "human_spot_check_quality" and check["status"] == "fail"
        for check in report_payload["policy_promotion_checklist"]["checks"]
    )
    assert len(answer_samples) == 2
    assert answer_judge_report["total"] == 1
    assert answer_judge_report["claim_support_rate"] == 1.0
    assert len(answer_judge_samples) == 1
    assert answer_fix_manifest["total"] == 0
    assert all("deterministic_scores" in record for record in answer_sample_records)
    assert all("context_score_breakdowns" in record for record in answer_sample_records)
    assert any(record["context_score_breakdowns"] for record in answer_sample_records)
    assert all("context_role_buckets" in record["metadata"] for record in answer_sample_records)
    assert any(record["metadata"]["primary_evidence_ids"] for record in answer_sample_records)
    assert all("locator_context" in record["metadata"] for record in answer_sample_records)
    assert all(
        "retrieval_context_coverage" in record["deterministic_scores"]
        for record in answer_sample_records
    )
    assert all("diagnostic_tags" in record["deterministic_scores"] for record in answer_sample_records)
    assert "diagnostic_tag_counts" in candidate["answer"]
    assert "true_missing_gold_rate" in candidate["answer"]
    assert "claim_support_coverage" in candidate["answer"]
    assert len(spot_samples) == 2
    assert "candidate_quality_gate_failed" in result.warnings
    assert any(warning.startswith("candidate_quality_issue:answer.success_rate=") for warning in result.warnings)
    assert "## Answer Metrics" in markdown
    assert "## Generation Judge" in markdown
    assert "## Spot Check" in markdown


def test_run_benchmark_suite_writes_answer_fix_manifest_for_answer_judge_failures(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))
    output_dir = tmp_path / "suite"

    run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        answer_eval_enabled=True,
        answer_eval_sample_size=1,
        answer_model=_fake_answer_model(),
        answer_judge_mode="llm",
        answer_judge_sample_size=1,
        answer_judge_llm_call=_fake_bad_answer_judge_llm,
    ))

    candidate_dir = output_dir / "test" / "candidate"
    failures = [
        json.loads(line)
        for line in (candidate_dir / "answer_judge_failures.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads((candidate_dir / "answer_fix_manifest.json").read_text(encoding="utf-8"))

    assert failures
    assert manifest["total"] == 1
    assert "unsupported_claim" in manifest["reason_counts"]
    assert "missing_citation" in manifest["reason_counts"]
    assert manifest["items"][0]["suggested_action"] in {"fix_answer_prompt", "fix_citation_mapping"}


def test_answer_failure_taxonomy_splits_abstention_direction() -> None:
    passing_judge_scores = {
        "claim_support_rate": 1.0,
        "contradiction_rate": 0.0,
        "wrong_citation_rate": 0.0,
        "missing_citation_rate": 0.0,
        "citation_claim_support_rate": 1.0,
    }

    answerable_abstention = {
        "expected_behavior": "answer",
        "outcome": "abstained",
        "answer": "",
        "deterministic_scores": {"failure_reason": "abstention_expected"},
        "llm_judge": {"scores": passing_judge_scores},
    }
    expected_abstain_answered = {
        "expected_behavior": "abstain",
        "outcome": "answered",
        "answer": "The method improves accuracy by 7%.",
        "deterministic_scores": {"failure_reason": "abstention_expected"},
        "llm_judge": {"scores": passing_judge_scores},
    }

    over_conservative_reasons = _answer_judge_failure_reasons(answerable_abstention)
    wrong_answer_reasons = _answer_judge_failure_reasons(expected_abstain_answered)

    assert OVERCONSERVATIVE_ABSTENTION_REASON in over_conservative_reasons
    assert WRONG_ABSTENTION_REASON not in over_conservative_reasons
    assert "abstention_expected" not in over_conservative_reasons
    assert WRONG_ABSTENTION_REASON in wrong_answer_reasons
    assert OVERCONSERVATIVE_ABSTENTION_REASON not in wrong_answer_reasons
    assert "abstention_expected" not in wrong_answer_reasons


def test_answer_eval_failure_counts_preserve_abstention_direction() -> None:
    answerable_abstention = EvidenceAnswerSample(
        pair=EvidenceQAPair(
            question="What did the method improve?",
            paper_id="p1",
            qa_type="table_qa",
            gold_chunk_ids=["gold-1"],
            answer_facts=["accuracy improved"],
            expected_behavior="answer",
        ),
        answer="",
        outcome=AnswerOutcome.ABSTAINED,
        context_chunk_ids=["gold-1"],
    )
    expected_abstain_answered = EvidenceAnswerSample(
        pair=EvidenceQAPair(
            question="What private dataset was used?",
            paper_id="p1",
            qa_type="table_qa",
            expected_behavior="abstain",
        ),
        answer="The private dataset was AlphaBench.",
        outcome=AnswerOutcome.ANSWERED,
    )

    result = EvidenceAnswerEvaluator().evaluate([answerable_abstention, expected_abstain_answered])

    assert result.failure_reason_counts() == {
        OVERCONSERVATIVE_ABSTENTION_REASON: 1,
        WRONG_ABSTENTION_REASON: 1,
    }


def test_answer_fix_manifest_counts_over_conservative_abstention(tmp_path: Path) -> None:
    answer = "Insufficient evidence to answer from the provided context."
    sample = EvidenceAnswerSample(
        pair=EvidenceQAPair(
            question="What changed?",
            paper_id="p1",
            qa_type="table_qa",
            gold_chunk_ids=["gold-1"],
            answer_facts=["accuracy improved"],
            expected_behavior="answer",
        ),
        answer="",
        outcome=AnswerOutcome.ABSTAINED,
        context_chunk_ids=["gold-1"],
    )
    generated = GeneratedAnswer(
        question=sample.pair.question,
        answer=answer,
        context_chunk_ids=["gold-1"],
        contexts=["The table reports an accuracy improvement."],
    )
    judgment = GenerationSampleJudgment(
        question=sample.pair.question,
        answer=answer,
        scores=GenerationScores(
            faithfulness=1.0,
            answer_relevancy=1.0,
            context_precision=1.0,
            claim_support_rate=1.0,
            contradiction_rate=0.0,
            unsupported_claim_rate=0.0,
            citation_claim_support_rate=1.0,
            wrong_citation_rate=0.0,
            missing_citation_rate=0.0,
        ),
        status="pass",
    )

    _write_answer_fix_artifacts(
        tmp_path,
        answer_judge_records=[(sample, generated)],
        generation_result=GenerationEvalResult(
            per_sample=[judgment.scores],
            sample_judgments=[judgment],
        ),
        spot_check=None,
    )

    manifest = json.loads((tmp_path / "answer_fix_manifest.json").read_text(encoding="utf-8"))

    assert manifest["reason_counts"] == {OVERCONSERVATIVE_ABSTENTION_REASON: 1}
    assert manifest["items"][0]["failure_reasons"] == [OVERCONSERVATIVE_ABSTENTION_REASON]
    assert manifest["items"][0]["suggested_action"] == "fix_answer_prompt"


def test_spot_check_schema_validates_extended_boolean_fields() -> None:
    errors = _spot_check_schema_errors({
        "paper_id": "p1",
        "qa_type": "table_qa",
        "question": "What changed?",
        "label": "pass",
        "reason": "ok",
        "retrieval_ok": "yes",
        "context_ok": True,
        "faithfulness_ok": False,
        "correct_support_chunk_ids": "para-1",
    })

    assert "invalid_retrieval_ok" in errors
    assert "invalid_correct_support_chunk_ids" in errors


def test_spot_check_report_calibrates_human_annotations_against_answer_judge(tmp_path: Path) -> None:
    annotations_path = tmp_path / "annotations.jsonl"
    annotations_path.write_text(
        json.dumps({
            "paper_id": "p1",
            "qa_type": "table_qa",
            "question": "What changed?",
            "answer": "It improved.",
            "gold_evidence_ok": True,
            "retrieval_ok": True,
            "context_ok": True,
            "answer_ok": True,
            "faithfulness_ok": True,
            "citation_ok": True,
            "label": "pass",
            "reason": "human says pass",
            "correct_support_chunk_ids": ["tbl-1"],
        }),
        encoding="utf-8",
    )
    judgment = GenerationSampleJudgment(
        question="What changed?",
        answer="It improved.",
        scores=GenerationScores(
            faithfulness=0.0,
            answer_relevancy=1.0,
            context_precision=0.0,
            claim_support_rate=0.0,
            unsupported_claim_rate=1.0,
            citation_claim_support_rate=0.0,
        ),
        status="fail",
    )

    report = _spot_check_report_from_annotations(
        BenchmarkSuiteConfig(papers_dir=tmp_path, output_dir=tmp_path, spot_check_annotations_path=annotations_path),
        output_dir=tmp_path,
        sample_size=1,
        answer_judge_by_key={("p1", "table_qa", "What changed?"): judgment},
    )

    assert report is not None
    payload = report.to_dict()
    calibration = payload["judge_human_calibration"]
    assert payload["human_answer_ok_rate"] == 1.0
    assert calibration["compared_count"] == 1
    assert calibration["judge_human_agreement"] == 0.0
    assert calibration["false_negative"] == 1
    assert payload["conflict_count"] == 1
    assert (tmp_path / "human_spot_check_conflicts.jsonl").exists()


def test_run_benchmark_matrix_writes_held_out_dataset_summary(tmp_path: Path) -> None:
    historical_dir = tmp_path / "historical"
    new50_dir = tmp_path / "new50"
    _write_research_document_fixtures(historical_dir, ("h1", "h2", "h3"))
    _write_research_document_fixtures(new50_dir, ("n1", "n2", "n3"))

    output_dir = tmp_path / "matrix"
    result = run_benchmark_matrix(BenchmarkMatrixConfig(
        datasets=(
            BenchmarkMatrixDataset(name="historical_38", papers_dir=historical_dir),
            BenchmarkMatrixDataset(name="new50_20260629", papers_dir=new50_dir),
        ),
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        answer_eval_enabled=True,
        answer_eval_sample_size=1,
        answer_model=_fake_answer_model(),
        answer_judge_mode="llm",
        answer_judge_sample_size=1,
        answer_judge_llm_call=_fake_judge_llm,
    ))

    payload = json.loads((output_dir / "benchmark_matrix_report.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "benchmark_matrix_report.md").read_text(encoding="utf-8")

    assert set(result.dataset_results) == {"historical_38", "new50_20260629"}
    assert set(payload["datasets"]) == {"historical_38", "new50_20260629"}
    assert payload["datasets"]["historical_38"]["papers_total"] == 3
    assert payload["datasets"]["historical_38"]["claim_support_rate"] == 1.0
    assert payload["datasets"]["historical_38"]["citation_claim_support_rate"] == 1.0
    assert "Eq Hit@10" in markdown
    assert "Claim support" in markdown


def test_run_benchmark_matrix_passes_gold_judge_and_summarizes_quality(tmp_path: Path) -> None:
    historical_dir = tmp_path / "historical"
    new50_dir = tmp_path / "new50"
    _write_research_document_fixtures(historical_dir, ("h1", "h2", "h3"))
    _write_research_document_fixtures(new50_dir, ("n1", "n2", "n3"))

    output_dir = tmp_path / "matrix"
    run_benchmark_matrix(BenchmarkMatrixConfig(
        datasets=(
            BenchmarkMatrixDataset(name="historical_38", papers_dir=historical_dir),
            BenchmarkMatrixDataset(name="new50_20260629", papers_dir=new50_dir),
        ),
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=6,
        gold_judge_mode="fake",
        gold_judge_sample_size=3,
        gold_evidence_judge=_FakeGoldJudge(),
    ))

    payload = json.loads((output_dir / "benchmark_matrix_report.json").read_text(encoding="utf-8"))
    historical = payload["datasets"]["historical_38"]

    assert historical["gold_judge_sample_size"] == 3
    assert historical["gold_judge_pass_rate"] == 1.0
    assert historical["gold_quality"]["judge_enabled"] is True
    assert historical["gold_quality"]["judge_audited"] is True
    assert "blind_semantic_without_gold_judge" not in historical["warnings"]
    assert (output_dir / "historical_38" / "gold_fix_manifest.json").exists()


def test_run_benchmark_suite_writes_gold_fix_artifacts_for_judge_findings(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))
    output_dir = tmp_path / "suite"

    run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=6,
        gold_judge_mode="fake",
        gold_judge_sample_size=3,
        gold_evidence_judge=_RepairArtifactGoldJudge(),
        question_profile="blind_semantic",
    ))

    failure_records = [
        json.loads(line)
        for line in (output_dir / "gold_judge_failures.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    warning_records = [
        json.loads(line)
        for line in (output_dir / "gold_judge_warnings.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    manifest = json.loads((output_dir / "gold_fix_manifest.json").read_text(encoding="utf-8"))
    report_payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    candidate_report = report_payload["candidate_test_report"]

    assert {record["status"] for record in failure_records} == {"fail", "error"}
    assert [record["status"] for record in warning_records] == ["warning"]
    assert all(record["suggested_action"] for record in [*failure_records, *warning_records])
    assert manifest["total"] == 3
    assert manifest["failure_count"] == 2
    assert manifest["warning_count"] == 1
    assert manifest["action_counts"]
    assert report_payload["gold_quality"]["judge_failed"] == 1
    assert report_payload["gold_quality"]["judge_error"] == 1
    assert (output_dir / "test" / "candidate" / "formula_retrieval_diagnostics.json").exists()
    assert (output_dir / "test" / "candidate" / "formula_retrieval_failures.jsonl").exists()
    assert "formula_retrieval_diagnostics" in candidate_report
    assert any(
        check["check_id"] == "gold_judge_quality" and check["status"] == "fail"
        for check in report_payload["policy_promotion_checklist"]["checks"]
    )


def test_gold_fix_record_preserves_formula_gold_judge_reason() -> None:
    item = GoldEvidenceJudgeItem(
        question="How is Equation 2 explained?",
        paper_id="p1",
        qa_type="formula_explanation_qa",
        status="fail",
        reason="missing formula explanation context",
        supported=False,
        confidence=0.2,
        question_clear=True,
        gold_evidence_complete=False,
        equivalent_gold_needed=True,
        bad_gold_reason="formula_context_missing",
        gold_chunk_ids=("eq-2",),
        equivalent_gold_chunk_ids=("eq-2", "para-explain"),
    )

    record = _gold_fix_record(item)

    assert record["question_clear"] is True
    assert record["gold_evidence_complete"] is False
    assert record["equivalent_gold_needed"] is True
    assert record["bad_gold_reason"] == "formula_context_missing"
    assert record["suggested_action"] == "add_context_gold"


def test_benchmark_matrix_loads_manifest_and_requires_real_dataset_dirs(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))
    missing_dir = tmp_path / "missing-future"
    manifest = tmp_path / "datasets.json"
    manifest.write_text(
        json.dumps({
            "datasets": [
                {"name": "historical_38", "papers_dir": str(papers_dir)},
                {"name": "new50_future_blind", "papers_dir": str(missing_dir)},
            ]
        }),
        encoding="utf-8",
    )

    datasets = load_benchmark_matrix_datasets(manifest)

    assert [dataset.name for dataset in datasets] == ["historical_38", "new50_future_blind"]
    assert datasets[0].papers_dir == papers_dir
    with pytest.raises(FileNotFoundError, match="new50_future_blind"):
        run_benchmark_matrix(BenchmarkMatrixConfig(
            datasets=datasets,
            output_dir=tmp_path / "matrix",
            min_papers=1,
            target_min_per_type=1,
            render_page_visual=False,
        ))


def test_benchmark_matrix_cli_accepts_dataset_manifest(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1",))
    manifest = tmp_path / "datasets.json"
    manifest.write_text(
        json.dumps({"datasets": [{"name": "historical_38", "papers_dir": str(papers_dir)}]}),
        encoding="utf-8",
    )

    args = _build_matrix_parser().parse_args([
        "--dataset-manifest",
        str(manifest),
        "--output-dir",
        str(tmp_path / "matrix"),
        "--gold-judge",
        "llm",
        "--gold-judge-sample-size",
        "7",
        "--answer-judge",
        "llm",
        "--answer-judge-sample-size",
        "5",
    ])

    datasets = _datasets_from_args(args)

    assert len(datasets) == 1
    assert datasets[0].name == "historical_38"
    assert args.gold_judge == "llm"
    assert args.gold_judge_sample_size == 7
    assert args.answer_judge == "llm"
    assert args.answer_judge_sample_size == 5


def test_gold_judge_sample_is_stratified_and_prioritizes_risky_items() -> None:
    items = [
        _audit_item("p1", "formula_qa", "safe formula", status="pass"),
        _audit_item("p2", "formula_qa", "risky formula", status="warning"),
        _audit_item("p3", "citation_qa", "citation", status="pass"),
        _audit_item("p4", "table_qa", "table", status="pass"),
        _audit_item("p5", "figure_qa", "figure", status="pass"),
        GoldEvidenceAuditItem(
            question="Should the model abstain?",
            paper_id="p6",
            qa_type="negative_qa",
            status="pass",
            reason="negative",
            gold_chunk_ids=(),
        ),
    ]

    sample = _gold_judge_sample(items, sample_size=6, seed="seed")

    assert {item.qa_type for item in sample} >= {"formula_qa", "citation_qa", "table_qa", "figure_qa"}
    assert next(item for item in sample if item.qa_type == "formula_qa").status == "warning"
    assert "negative_qa" not in {item.qa_type for item in sample}


def test_gold_judge_prompt_uses_extended_evidence_preview() -> None:
    delayed_fact = "FINAL_REWARD_INCLUDES_KL_PENALTY_FOR_STABILITY"
    chunk = _paper_chunk(
        chunk_id="para-long",
        chunk_type="paragraph",
        content=f"{'background text. ' * 40}{delayed_fact}",
    )
    item = GoldEvidenceAuditItem(
        question="How is the formula explained?",
        paper_id="p1",
        qa_type="formula_explanation_qa",
        status="pass",
        reason="ok",
        gold_chunk_ids=("para-long",),
        answer_facts=("The surrounding text explains the final reward.",),
        evidence_previews=(_evidence_preview(chunk),),
    )

    prompt = _judge_prompt(item, max_evidence_chars=1600)

    assert delayed_fact in prompt


def test_gold_audit_preview_includes_evidence_group_context() -> None:
    table = _paper_chunk(
        chunk_id="tbl-results",
        chunk_type="table",
        content="Rows: Model A | 95",
    )
    explanation = _paper_chunk(
        chunk_id="para-result",
        chunk_type="paragraph",
        content="The result paragraph explains that Model A improves accuracy.",
    )
    pair = EvidenceQAPair(
        question="What do the reported results suggest?",
        paper_id="p1",
        qa_type="experiment_result_qa",
        gold_chunk_ids=["tbl-results"],
        equivalent_gold_chunk_ids=["tbl-results", "para-result"],
        supporting_evidence_group_id="eg-result",
        supporting_evidence_group={
            "group_id": "eg-result",
            "primary_evidence_ids": ["tbl-results"],
            "interpretation_context_ids": ["para-result"],
            "equivalent_evidence_ids": ["tbl-results", "para-result"],
        },
        required_evidence_types=["table"],
        answer_facts=["The result paragraph explains that Model A improves accuracy."],
    )

    report = audit_gold_evidence([pair], [table, explanation], sample_size=1, seed="seed")
    preview_ids = [preview["chunk_id"] for preview in report.items[0].evidence_previews]

    assert preview_ids == ["tbl-results", "para-result"]


def test_evidence_pack_hydration_adds_primary_when_group_context_is_hit() -> None:
    formula = _paper_chunk(
        chunk_id="eq-hamiltonian",
        chunk_type="formula",
        content="Equation: dot y = J^{-1} grad H(y).",
    )
    explanation = _paper_chunk(
        chunk_id="para-formula",
        chunk_type="paragraph",
        content="The surrounding text explains the Hamiltonian ODE.",
    )
    pair = EvidenceQAPair(
        question="How is the Hamiltonian ODE defined?",
        paper_id="p1",
        qa_type="formula_qa",
        gold_chunk_ids=["eq-hamiltonian"],
        equivalent_gold_chunk_ids=["eq-hamiltonian", "para-formula"],
        supporting_evidence_group_id="eg_formula",
        required_primary_evidence_ids=["eq-hamiltonian"],
        acceptable_support_evidence_ids=["para-formula"],
        supporting_evidence_group={
            "group_id": "eg_formula",
            "primary_evidence_ids": ["eq-hamiltonian"],
            "equivalent_evidence_ids": ["eq-hamiltonian", "para-formula"],
            "interpretation_context_ids": ["para-formula"],
        },
    )
    retrieval = RetrievalResult(
        parent_chunks=[],
        child_chunks=[explanation],
        ref_chunks=[],
        intent="formula_query",  # type: ignore[arg-type]
    )

    required_ids = _evidence_pack_required_context_ids(pair)
    hydrated, metadata = _hydrate_retrieval_with_evidence_pack(
        retrieval,
        pair,
        chunk_lookup=_ChunkLookup([formula, explanation]),
        required_context_ids=required_ids,
    )
    selection = AnswerContextAssembler(max_context_chunks=2).select(
        hydrated,
        required_context_ids=required_ids,
    )

    assert required_ids == ["eq-hamiltonian", "para-formula"]
    assert [chunk.chunk_id for chunk in hydrated.ref_chunks] == ["eq-hamiltonian"]
    assert metadata["evidence_pack_hit_chunk_ids"] == ["para-formula"]
    assert metadata["evidence_pack_expanded_chunk_ids"] == ["eq-hamiltonian"]
    assert metadata["evidence_pack_expansions"][0]["expansion_reason"] == "formula_group_primary_evidence"
    assert [chunk.chunk_id for chunk in selection.chunks] == ["eq-hamiltonian", "para-formula"]
    assert selection.metadata["context_role_buckets"]["eq-hamiltonian"] == "primary_evidence"
    assert selection.metadata["context_role_buckets"]["para-formula"] == "interpretation_context"
    assert selection.metadata["context_relationships"][0]["evidence_group_id"] == "eg_formula"


def test_evidence_pack_hydration_does_not_add_gold_without_group_hit() -> None:
    formula = _paper_chunk(
        chunk_id="eq-hamiltonian",
        chunk_type="formula",
        content="Equation: dot y = J^{-1} grad H(y).",
    )
    unrelated = _paper_chunk(
        chunk_id="para-other",
        chunk_type="paragraph",
        content="Other paragraph.",
    )
    pair = EvidenceQAPair(
        question="How is the Hamiltonian ODE defined?",
        paper_id="p1",
        qa_type="formula_qa",
        gold_chunk_ids=["eq-hamiltonian"],
        equivalent_gold_chunk_ids=["eq-hamiltonian", "para-formula"],
        supporting_evidence_group_id="eg_formula",
        required_primary_evidence_ids=["eq-hamiltonian"],
        supporting_evidence_group={
            "group_id": "eg_formula",
            "primary_evidence_ids": ["eq-hamiltonian"],
            "equivalent_evidence_ids": ["eq-hamiltonian", "para-formula"],
            "interpretation_context_ids": ["para-formula"],
        },
    )
    retrieval = RetrievalResult(
        parent_chunks=[],
        child_chunks=[unrelated],
        ref_chunks=[],
        intent="formula_query",  # type: ignore[arg-type]
    )

    hydrated, metadata = _hydrate_retrieval_with_evidence_pack(
        retrieval,
        pair,
        chunk_lookup=_ChunkLookup([formula, unrelated]),
        required_context_ids=_evidence_pack_required_context_ids(pair),
    )

    assert hydrated.ref_chunks == []
    assert metadata["evidence_pack_hit_chunk_ids"] == []
    assert metadata["evidence_pack_expanded_chunk_ids"] == []


def test_spot_check_generation_is_capped_by_sample_size(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    _write_research_document_fixtures(papers_dir, ("p1", "p2", "p3"))
    prompts: list[str] = []

    def fake_answer(request) -> str:
        prompts.append(request.user_prompt)
        return "The answer is in the selected context. [1]"

    output_dir = tmp_path / "suite"
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=papers_dir,
        output_dir=output_dir,
        min_papers=3,
        target_min_per_type=1,
        max_pairs_per_type=20,
        render_page_visual=False,
        gold_audit_sample_size=5,
        answer_model=FakePaperAnswerModel(fake_answer),
        spot_check_sample_size=1,
    ))

    assert len(prompts) == 1
    assert result.spot_check is not None
    assert result.spot_check.sample_size == 1


def test_run_benchmark_suite_cli(tmp_path: Path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload("p1"), ensure_ascii=False),
        encoding="utf-8",
    )
    output_dir = tmp_path / "suite"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--output-dir",
        str(output_dir),
        "--min-papers",
        "1",
        "--target-min-per-type",
        "1",
        "--gold-audit-sample-size",
        "2",
        "--quality-threshold",
        "retrieval.hit_rate=0",
        "--question-profile",
        "blind_detemplated",
        "--no-page-visual",
    ])

    assert exit_code == 0
    payload = json.loads((output_dir / "benchmark_suite_report.json").read_text(encoding="utf-8"))
    assert payload["papers_total"] == 1
    assert payload["evaluation_protocol"]["question_profile"] == "blind_detemplated"
    item = payload["gold_audit"]["items"][0]
    assert "evidence_previews" in item
    assert "answer_facts" in item


def test_parse_thresholds_requires_metric_value_pairs() -> None:
    assert _parse_thresholds(["answer.success_rate=0.8"]) == {"answer.success_rate": 0.8}

    try:
        _parse_thresholds(["answer.success_rate"])
    except ValueError as exc:
        assert "METRIC=VALUE" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def _write_research_document_fixtures(papers_dir: Path, paper_ids: tuple[str, ...]) -> None:
    for paper_id in paper_ids:
        paper_dir = papers_dir / paper_id
        (paper_dir / "figures").mkdir(parents=True)
        (paper_dir / "figures" / "fig.png").write_bytes(b"fake")
        payload = _research_document_payload(paper_id)
        (paper_dir / "research_document.json").write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


def _research_document_payload(paper_id: str) -> dict:
    return {
        "paper_id": paper_id,
        "source_hash": f"hash-{paper_id}",
        "sections": [
            {
                "section_id": f"{paper_id}-abstract",
                "title": "Abstract",
                "level": 1,
                "text": "This paper introduces a visual retrieval model.",
                "source_ref": f"paper://{paper_id}/abstract",
            },
            {
                "section_id": f"{paper_id}-results",
                "title": "Results",
                "level": 1,
                "text": "Figure 1 shows the architecture. Table 1 reports better accuracy. Equation 1 defines the score.",
                "source_ref": f"paper://{paper_id}/results",
            },
        ],
        "figures": [
            {
                "figure_id": "fig1",
                "caption": "Figure 1: architecture overview.",
                "source_ref": f"paper://{paper_id}/fig1",
                "image_ref": "figures/fig.png",
                "page": 1,
            }
        ],
        "tables": [
            {
                "table_id": "tbl1",
                "caption": "Table 1: accuracy results.",
                "source_ref": f"paper://{paper_id}/tbl1",
                "columns": ["metric", "value"],
                "rows": [{"metric": "accuracy", "value": "95"}],
                "page": 1,
            }
        ],
        "equations": [
            {
                "equation_id": "eq1",
                "latex": "s = x + y",
                "source_ref": f"paper://{paper_id}/eq1",
                "page": 1,
            }
        ],
        "references": [],
        "lineage": {
            "source_refs": [f"paper://{paper_id}"],
            "source_hash": f"hash-{paper_id}",
            "artifact_refs": [],
            "metadata": {},
        },
        "metadata": {"parse_source": "latex"},
    }
