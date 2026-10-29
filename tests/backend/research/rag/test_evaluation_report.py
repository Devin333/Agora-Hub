from __future__ import annotations

from framework.rag.evaluation import AnswerOutcome

import json

from backend.research.rag.evaluation.paper_answer_eval import (
    EvidenceAnswerEvalResult,
    EvidenceAnswerSample,
    EvidenceAnswerScores,
)
from backend.research.rag.adapters.evaluation_scorecard_adapter import evidence_results_to_rag_scorecard
from backend.research.rag.evaluation.paper_evaluation_report import EvidenceRegressionReport
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceEvalResult, EvidenceQAPair
from backend.research.rag.evaluation.paper_generation_eval import GenerationEvalResult, GenerationScores


def test_evidence_regression_report_writes_json_and_markdown(tmp_path) -> None:
    pair = EvidenceQAPair(
        question="What is supported?",
        paper_id="p1",
        qa_type="citation_qa",
        gold_chunk_ids=["para-1"],
    )
    retrieval = EvidenceEvalResult(
        total=1,
        answerable_total=1,
        abstain_total=0,
        ks=(1,),
        hit_at={1: 1},
        evidence_coverage_at={1: [1.0]},
        required_type_coverage_at={1: [0.0]},
        source_locator_coverage_at={1: [0.0]},
        citation_accuracy_at={1: []},
        image_recall_at={1: [0.0]},
        visual_evidence_coverage_at={1: [0.0]},
        over_retrieval_at={1: [0]},
        reciprocal_ranks=[1.0],
        ndcg_at={1: [1.0]},
    )
    answer = EvidenceAnswerEvalResult(scores=[
        EvidenceAnswerScores(
            sample=EvidenceAnswerSample(pair=pair, answer="Supported. [para-1]", outcome=AnswerOutcome.ANSWERED,
                                                                                 cited_chunk_ids=["para-1"]),
            fact_coverage=None,
            citation_grounding=1.0,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=True,
        )
    ])
    generation = GenerationEvalResult(per_sample=[
        GenerationScores(
            faithfulness=1.0,
            answer_relevancy=0.9,
            context_precision=0.8,
        )
    ])
    report = EvidenceRegressionReport(
        retrieval=retrieval,
        answer=answer,
        generation=generation,
        thresholds={
            "retrieval.evidence_coverage": 0.5,
            "answer.success_rate": 0.9,
            "generation.faithfulness": 0.9,
        },
        metadata={"paper_id": "p1"},
    )

    report.write(tmp_path)

    payload = json.loads((tmp_path / "evidence_regression_report.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "evidence_regression_report.md").read_text(encoding="utf-8")
    assert payload["metadata"]["paper_id"] == "p1"
    assert payload["passed"] is True
    assert payload["retrieval"]["by_k"]["1"]["hit_rate"] == 1.0
    assert payload["answer"]["success_rate"] == 1.0
    assert payload["generation"]["faithfulness"] == 1.0
    scorecard = payload["rag_evaluation_report"]["scorecard"]
    metrics = {metric["name"]: metric["value"] for metric in scorecard["metrics"]}
    assert metrics["retrieval.mrr"] == 1.0
    assert metrics["retrieval.hit_at_1"] == 1.0
    assert metrics["answer.success_rate"] == 1.0
    assert metrics["generation.faithfulness"] == 1.0
    assert "Paper RAG Evidence Regression Report" in markdown
    assert "RAG Scorecard" in markdown
    assert "Evidence Retrieval Eval" in markdown
    assert "Evidence Answer Eval" in markdown
    assert "Generation" in markdown


def test_evidence_regression_report_records_threshold_failures() -> None:
    retrieval = EvidenceEvalResult(
        total=1,
        answerable_total=1,
        abstain_total=0,
        ks=(1,),
        hit_at={1: 0},
        evidence_coverage_at={1: [0.0]},
        required_type_coverage_at={1: [0.0]},
        source_locator_coverage_at={1: [0.0]},
        citation_accuracy_at={1: []},
        image_recall_at={1: [0.0]},
        visual_evidence_coverage_at={1: [0.0]},
        over_retrieval_at={1: [1]},
        reciprocal_ranks=[0.0],
        ndcg_at={1: [0.0]},
    )
    report = EvidenceRegressionReport(
        retrieval=retrieval,
        thresholds={"retrieval.evidence_coverage": 0.8},
    )

    assert report.passed() is False
    assert report.issues() == ["retrieval.evidence_coverage=0.000 is below threshold 0.800"]
    assert "**Status:** FAIL" in report.to_markdown()


def test_evidence_regression_report_maps_answer_failures_to_rag_scorecard() -> None:
    pair = EvidenceQAPair(
        question="What is missing?",
        paper_id="p1",
        qa_type="table_qa",
        gold_chunk_ids=["tbl-1"],
    )
    answer = EvidenceAnswerEvalResult(scores=[
        EvidenceAnswerScores(
            sample=EvidenceAnswerSample(pair=pair, answer="The answer missed the table.", outcome=AnswerOutcome.ANSWERED),
            fact_coverage=1.0,
            citation_grounding=0.0,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=False,
            retrieval_context_coverage=0.0,
            citation_gold_coverage=0.0,
            failure_reason="missing_gold_in_llm_context",
        )
    ])
    report = EvidenceRegressionReport(answer=answer, metadata={"split": "test"})

    scorecard = report.to_dict()["rag_evaluation_report"]["scorecard"]

    assert scorecard["failure_reasons"] == ["context_missing_gold"]
    assert scorecard["metadata"]["raw_failure_reason_counts"] == {"missing_gold_in_llm_context": 1}
    assert scorecard["metadata"]["split"] == "test"


def test_evidence_regression_report_writes_answer_failure_details(tmp_path) -> None:
    failing_pair = EvidenceQAPair(
        question="What does the table report?",
        paper_id="p1",
        qa_type="table_qa",
        gold_chunk_ids=["tbl-1"],
        equivalent_gold_chunk_ids=["tbl-1", "para-1"],
        required_primary_evidence_ids=["tbl-1"],
        acceptable_support_evidence_ids=["para-1"],
        gold_source_locators=["paper://p1/pdf#page=6"],
    )
    passing_pair = EvidenceQAPair(
        question="What is supported?",
        paper_id="p1",
        qa_type="citation_qa",
        gold_chunk_ids=["para-2"],
    )
    answer = EvidenceAnswerEvalResult(scores=[
        EvidenceAnswerScores(
            sample=EvidenceAnswerSample(
                pair=failing_pair,
                answer="The provided context does not mention the table.",
                outcome=AnswerOutcome.ANSWERED,
                cited_chunk_ids=[],
                cited_source_locators=[],
                context_chunk_ids=["other"],
                metadata={
                    "status": "answered",
                    "generation_mode": "abstained",
                    "transcript_id": "transcript-1",
                    "gate_results": [{"gate": "citation", "passed": False}],
                    "decision": {"status": "abstain"},
                },
            ),
            fact_coverage=0.0,
            citation_grounding=0.0,
            source_locator_grounding=0.0,
            abstention_correct=0.0,
            answer_success=False,
            retrieval_context_coverage=0.0,
            citation_gold_coverage=0.0,
            strict_retrieval_context_coverage=0.0,
            equivalent_retrieval_context_coverage=0.0,
            strict_citation_gold_coverage=0.0,
            equivalent_citation_gold_coverage=0.0,
            diagnostic_tags=("true_missing_gold_in_retrieval", "context_missing_primary_evidence"),
            failure_reason="abstained_over_conservative",
            missing_facts=("Table 1 reports accuracy.",),
        ),
        EvidenceAnswerScores(
            sample=EvidenceAnswerSample(
                pair=passing_pair,
                answer="The claim is supported. [para-2]",
                outcome=AnswerOutcome.ANSWERED,
                cited_chunk_ids=["para-2"],
                context_chunk_ids=["para-2"],
            ),
            fact_coverage=None,
            citation_grounding=1.0,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=True,
        ),
    ])
    report = EvidenceRegressionReport(answer=answer)

    report.write(tmp_path)

    rows = [
        json.loads(line)
        for line in (tmp_path / "answer_failure_details.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["paper_id"] == "p1"
    assert rows[0]["question"] == "What does the table report?"
    assert rows[0]["failure_reason"] == "abstained_over_conservative"
    assert rows[0]["diagnostic_tags"] == [
        "true_missing_gold_in_retrieval",
        "context_missing_primary_evidence",
    ]
    assert rows[0]["gold_chunk_ids"] == ["tbl-1"]
    assert rows[0]["context_chunk_ids"] == ["other"]
    assert rows[0]["decision"] == {"status": "abstain"}


def test_scorecard_records_and_omits_undefined_answer_metrics() -> None:
    pair = EvidenceQAPair(
        question="What is supported?",
        paper_id="p1",
        qa_type="citation_qa",
        gold_chunk_ids=["para-1"],
    )
    sample = EvidenceAnswerSample(pair=pair, answer="", outcome=AnswerOutcome.FAILED)
    answer = EvidenceAnswerEvalResult(scores=[
        EvidenceAnswerScores(
            sample=sample,
            fact_coverage=None,
            citation_grounding=None,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=False,
            failure_reason="system_failure",
        )
    ])

    scorecard = evidence_results_to_rag_scorecard(answer=answer)
    metric_names = {metric.name for metric in scorecard.metrics}

    assert "answer.fact_coverage" not in metric_names
    assert "answer.system_failure_rate" in metric_names
    assert "answer.fact_coverage" in scorecard.metadata["skipped_none_metrics"]
    assert "answer.abstention_accuracy" in scorecard.metadata["skipped_none_metrics"]
