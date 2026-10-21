from __future__ import annotations

import json

import pytest

from data.eval.build_golden_set import build_pairs
from backend.research.document.models import PaperChunk
from backend.research.rag.cli.run_evidence_eval import main
from backend.research.rag.evaluation.evidence_eval_runner import (
    EvidenceEvalOptions,
    _build_live_answer_samples,
    _load_chunks_from_papers_dir,
    run_evidence_eval_core,
)
from backend.research.rag.evaluation.paper_answer_eval import EvidenceAnswerEvaluator
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair, save_evidence_golden_set


class _FakeChunkStore:
    def __init__(self, chunks_by_paper: dict[str, list[PaperChunk]]) -> None:
        self._chunks_by_paper = chunks_by_paper
        self.list_calls: list[str] = []
        self.search_calls: list[tuple[str, str, int]] = []

    def ensure_collection(self) -> None:
        return None

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        self.list_calls.append(paper_id)
        return list(self._chunks_by_paper.get(paper_id, []))

    def search_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters=None,
        limit: int = 10,
        score_threshold=None,
    ) -> list[PaperChunk]:
        self.search_calls.append((paper_id, query_text, limit))
        return list(self._chunks_by_paper.get(paper_id, []))

    def search_with_scores(self, paper_id: str, query_text: str, *, filters=None, limit: int = 30):
        return [(chunk, 1.0) for chunk in self._chunks_by_paper.get(paper_id, [])]

    def get_chunk(self, chunk_id: str):
        for chunks in self._chunks_by_paper.values():
            for chunk in chunks:
                if chunk.chunk_id == chunk_id:
                    return chunk
        return None

    def get_parent_chunk(self, chunk: PaperChunk):
        return self.get_chunk(chunk.parent_chunk_id) if chunk.parent_chunk_id else None


def test_run_evidence_eval_writes_summary_report(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"
    save_evidence_golden_set([
        EvidenceQAPair(
            question="What is supported?",
            paper_id="p1",
            qa_type="citation_qa",
            gold_chunk_ids=["para-1"],
        ),
        EvidenceQAPair.negative(
            question="Does the paper discuss unrelated future work?",
            paper_id="p1",
        ),
    ], golden)

    exit_code = main(["--golden-set", str(golden), "--output-dir", str(output)])

    assert exit_code == 0
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["total_pairs"] == 2
    assert payload["metadata"]["qa_type_counts"] == {"citation_qa": 1, "negative_qa": 1}
    assert payload["metadata"]["answer_eval_mode"] == "none"
    assert payload["metadata"]["protocol"] == "legacy_evidence_regression_diagnostic"
    assert payload["metadata"]["track"] == "not_applicable"
    assert payload["metadata"]["execution_profile"] == "not_applicable"
    assert payload["metadata"]["purpose"] == "diagnostic_regression"
    assert payload["metadata"]["component_identity"]["retrieval_adapter"] == "not_executed"
    assert payload["metadata"]["component_identity"]["generation_adapter"] == "not_executed"
    assert payload["metadata"]["gold_context_used"] is False
    assert payload["metadata"]["production_qualification"]["eligible"] is False
    assert (output / "evidence_regression_report.md").exists()


def test_run_evidence_eval_core_accepts_structured_options(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"
    save_evidence_golden_set([
        EvidenceQAPair(
            question="What is supported?",
            paper_id="p1",
            qa_type="citation_qa",
            gold_chunk_ids=["para-1"],
        )
    ], golden)

    exit_code = run_evidence_eval_core(EvidenceEvalOptions(
        golden_set=golden,
        output_dir=output,
        thresholds={"retrieval.evidence_coverage": 0.8},
    ))

    assert exit_code == 1
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["golden_set"] == str(golden)
    assert payload["thresholds"] == {"retrieval.evidence_coverage": 0.8}


def test_run_evidence_eval_core_classifies_deterministic_gold_answer_without_model(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"
    save_evidence_golden_set([
        EvidenceQAPair(
            question="What improves retrieval?",
            paper_id="p1",
            qa_type="citation_qa",
            gold_chunk_ids=["para-1"],
            answer_facts=["The architecture improves retrieval."],
        )
    ], golden)

    exit_code = run_evidence_eval_core(EvidenceEvalOptions(
        golden_set=golden,
        output_dir=output,
        deterministic_answer_eval=True,
    ))

    assert exit_code == 0
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    metadata = payload["metadata"]
    assert metadata["answer_eval_mode"] == "deterministic"
    assert metadata["track"] == "not_applicable"
    assert metadata["execution_profile"] == "fixture"
    assert metadata["component_identity"]["answer_adapter"] == "deterministic_gold_supported_answer"
    assert metadata["component_identity"]["generation_adapter"] == (
        "not_applicable_no_generation_model_executed"
    )
    assert metadata["gold_context_used"] is True
    assert metadata["gold_context_scope"] == "deterministic_answer_construction"
    assert metadata["production_qualification"]["eligible"] is False


def test_build_golden_set_entrypoint_uses_evidence_pairs_with_negatives() -> None:
    chunk = PaperChunk(
        chunk_id="para-1",
        paper_id="p1",
        parse_source="nougat",
        chunk_type="paragraph",
        section_title="Results",
        section_role=["experiment"],
        section_index=1,
        content="The paper reports a visual architecture that improves retrieval accuracy.",
        metadata={"source_locator": "paper://p1/results"},
    )
    store = _FakeChunkStore({"p1": [chunk]})

    pairs = build_pairs(store, {"p1": "nlp"}, max_pairs_per_type=2)

    assert store.list_calls == ["p1"]
    assert store.search_calls == []
    assert any(pair.expected_behavior == "answer" for pair in pairs)
    assert any(pair.expected_behavior == "abstain" for pair in pairs)
    assert all(pair.domain == "nlp" for pair in pairs)


def test_live_answer_eval_samples_preserve_gated_payload_semantics() -> None:
    answer_pair = EvidenceQAPair(
        question="What improves retrieval?",
        paper_id="p1",
        qa_type="citation_qa",
        gold_chunk_ids=["para-1"],
        gold_source_locators=["paper://p1/results"],
        answer_facts=["The visual architecture improves retrieval accuracy."],
    )
    abstain_pair = EvidenceQAPair.negative(
        question="Does the paper discuss unrelated weather data?",
        paper_id="p1",
    )

    def ask(pair: EvidenceQAPair) -> dict:
        if pair.expected_behavior == "abstain":
            return {
                "status": "insufficient_evidence",
                "generation_mode": "gated_harness",
                "answer": None,
                "answer_candidate": {"abstained": True, "answer_text": ""},
                "citations": [],
                "passages": [],
                "gate_results": [],
                "decision": {"decision_type": "abstain"},
                "transcript_id": "rag-transcript://p1/abstain",
            }
        return {
            "status": "succeeded",
            "generation_mode": "gated_harness",
            "answer": "The visual architecture improves retrieval accuracy.",
            "answer_candidate": {"abstained": False},
            "citations": [
                {
                    "chunk_id": "para-1",
                    "source_locator": "paper://p1/results",
                }
            ],
            "passages": [{"chunk_id": "para-1"}],
            "gate_results": [{"check_id": "answer.grounded", "passed": True}],
            "decision": {"decision_type": "answer"},
            "transcript_id": "rag-transcript://p1/answer",
        }

    samples = _build_live_answer_samples([answer_pair, abstain_pair], ask=ask)
    result = EvidenceAnswerEvaluator().evaluate(samples)

    assert [sample.metadata["answer_eval"] for sample in samples] == [
        "live_gated_harness",
        "live_gated_harness",
    ]
    assert samples[0].cited_chunk_ids == ["para-1"]
    assert samples[0].context_chunk_ids == ["para-1"]
    assert "provided context does not mention" in samples[1].answer
    assert result.abstention_accuracy() == 1.0
    assert result.success_rate() == 1.0


def test_run_evidence_eval_records_live_answer_eval_mode(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"
    save_evidence_golden_set([
        EvidenceQAPair.negative(
            question="Does the paper discuss unrelated weather data?",
            paper_id="p1",
        )
    ], golden)

    def ask(pair: EvidenceQAPair) -> dict:
        return {
            "status": "insufficient_evidence",
            "generation_mode": "gated_harness",
            "answer": None,
            "answer_candidate": {"abstained": True, "answer_text": ""},
            "citations": [],
            "passages": [],
            "gate_results": [],
            "decision": {"decision_type": "abstain"},
            "transcript_id": "rag-transcript://p1/live",
        }

    exit_code = main(
        [
            "--golden-set",
            str(golden),
            "--output-dir",
            str(output),
            "--live-answer-eval",
        ],
        live_answer_ask=ask,
    )

    assert exit_code == 0
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["answer_eval_mode"] == "live"
    assert payload["metadata"]["track"] == "not_applicable"
    assert "cannot claim an end-to-end track" in payload["metadata"]["track_reason"]
    assert payload["metadata"]["execution_profile"] == "fixture"
    assert payload["metadata"]["component_identity"]["answer_adapter"] == (
        "injected_live_answer_callable"
    )
    assert payload["metadata"]["gold_context_used"] is True
    assert payload["metadata"]["gold_context_scope"] == (
        "full_evaluation_pair_exposed_to_injected_callable"
    )
    assert payload["metadata"]["production_qualification"]["eligible"] is False
    assert payload["answer"]["abstention_accuracy"] == 1.0


def test_run_evidence_eval_rejects_live_answer_eval_without_papers_or_injection(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    save_evidence_golden_set([
        EvidenceQAPair.negative(
            question="Does the paper discuss unrelated weather data?",
            paper_id="p1",
        )
    ], golden)

    with pytest.raises(ValueError, match="requires --papers-dir"):
        main([
            "--golden-set",
            str(golden),
            "--live-answer-eval",
        ])


def test_run_evidence_eval_rejects_conflicting_answer_eval_modes(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    save_evidence_golden_set([
        EvidenceQAPair.negative(
            question="Does the paper discuss unrelated weather data?",
            paper_id="p1",
        )
    ], golden)

    with pytest.raises(ValueError, match="mutually exclusive"):
        main([
            "--golden-set",
            str(golden),
            "--deterministic-answer-eval",
            "--live-answer-eval",
        ])


def test_run_evidence_eval_returns_failure_for_unavailable_threshold(tmp_path) -> None:
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"
    save_evidence_golden_set([
        EvidenceQAPair(
            question="What is supported?",
            paper_id="p1",
            qa_type="citation_qa",
            gold_chunk_ids=["para-1"],
        ),
    ], golden)

    exit_code = main([
        "--golden-set",
        str(golden),
        "--output-dir",
        str(output),
        "--threshold",
        "retrieval.evidence_coverage=0.8",
    ])

    assert exit_code == 1
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert "unavailable" in payload["issues"][0]


def test_run_evidence_eval_can_run_live_retrieval_from_parsed_papers(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    golden = tmp_path / "golden.json"
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--golden-set",
        str(golden),
        "--live-retrieval",
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    payload = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert payload["metadata"]["mode"] == "live_retrieval"
    assert payload["metadata"]["track"] == "retrieval"
    assert payload["metadata"]["execution_profile"] == "fixture"
    assert payload["metadata"]["component_identity"]["retrieval_adapter"] == (
        "in_memory_chunk_store_with_sha256_hash_field_index"
    )
    assert payload["metadata"]["gold_context_used"] is False
    assert payload["metadata"]["production_qualification"]["eligible"] is False
    assert payload["metadata"]["chunks_total"] > 0
    assert payload["retrieval"]["answerable_total"] > 0
    distribution = payload["retrieval"]["field_embedding_distribution"]
    assert distribution["search_hits_by_field"]
    assert distribution["matched_evidence_count"] > 0
    components = payload["retrieval"]["score_breakdown_summary"]["components"]
    embedding_components = {
        name: stats
        for name, stats in components.items()
        if name.endswith("_embedding_score")
    }
    assert embedding_components
    assert any(stats["max"] > 0 for stats in embedding_components.values())
    assert golden.exists()


def test_run_evidence_eval_live_retrieval_can_enable_visual_index(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    image_dir = paper_dir / "figures"
    image_dir.mkdir(parents=True)
    (image_dir / "arch.png").write_bytes(b"not-a-real-image")
    payload = _research_document_payload()
    payload["figures"][0]["image_ref"] = "figures/arch.png"
    (paper_dir / "research_document.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--live-retrieval",
        "--visual",
        "--image-root",
        str(papers_dir),
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert report["metadata"]["visual_fusion_enabled"] is True
    assert report["metadata"]["visual_indexed_chunks"] == 1


def test_run_evidence_eval_can_add_page_visual_chunks(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    image_dir = paper_dir / "figures"
    page_dir = paper_dir / "page_images"
    image_dir.mkdir(parents=True)
    page_dir.mkdir(parents=True)
    (image_dir / "arch.png").write_bytes(b"not-a-real-image")
    (page_dir / "p1_page_001.png").write_bytes(b"fake-page-image")
    payload = _research_document_payload()
    payload["figures"][0]["image_ref"] = "figures/arch.png"
    (paper_dir / "research_document.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--live-retrieval",
        "--visual",
        "--page-visual",
        "--no-render-page-visual",
        "--image-root",
        str(papers_dir),
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert report["metadata"]["page_visual_enabled"] is True
    assert report["metadata"]["page_visual_chunks"] == 1
    assert report["metadata"]["visual_indexed_chunks"] == 2


def test_run_evidence_eval_records_retrieval_policy_metadata(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--live-retrieval",
        "--retrieval-policy",
        "paper_visual_rag_tuned",
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    metadata = report["metadata"]
    assert metadata["retrieval_policy"] == "paper_visual_rag_tuned"
    assert metadata["retrieval_policy_overfetch_multiplier"] == 5
    assert metadata["retrieval_policy_child_score_weights"] == {
        "semantic": 0.45,
        "field": 0.4,
        "position": 0.05,
        "graph": 0.1,
    }
    assert metadata["retrieval_policy_visual_fusion_weights"] == {
        "text": 0.85,
        "visual": 0.15,
    }


def test_run_evidence_eval_live_retrieval_can_enable_lightweight_reranker(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--live-retrieval",
        "--retrieval-policy",
        "paper_visual_rag_tuned",
        "--lightweight-reranker",
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    markdown = (output / "evidence_regression_report.md").read_text(encoding="utf-8")
    distribution = report["retrieval"]["rerank_distribution"]
    assert report["metadata"]["lightweight_reranker_enabled"] is True
    assert distribution["reranker_enabled_sample_count"] > 0
    assert distribution["reranked_evidence_count"] > 0
    assert distribution["max_score"] > 0.0
    assert "## Rerank Distribution" in markdown


def test_run_evidence_eval_hydrates_external_golden_set_from_parsed_papers(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    chunks = _load_chunks_from_papers_dir(papers_dir)
    target = next(chunk for chunk in chunks if chunk.section_title == "Results")
    golden = tmp_path / "legacy-golden.json"
    golden.write_text(
        json.dumps([
            {
                "question": "What reports stronger accuracy?",
                "paper_id": "p1",
                "source_chunk_id": target.chunk_id,
                "expected_behavior": "answer",
            }
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--golden-set",
        str(golden),
        "--live-retrieval",
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    hydration = report["metadata"]["golden_set_hydration"]
    assert hydration["hydrated_pairs"] == 1
    assert hydration["locator_attached_pairs"] == 1
    assert hydration["type_attached_pairs"] == 1
    assert hydration["missing_gold_chunk_pairs"] == 0
    assert report["retrieval"]["by_k"]["10"]["source_locator_coverage"] == 1.0


def test_run_evidence_eval_blind_semantic_policy_auto_enables_lightweight_reranker(tmp_path) -> None:
    papers_dir = tmp_path / "papers"
    paper_dir = papers_dir / "p1"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text(
        json.dumps(_research_document_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "report"

    exit_code = main([
        "--papers-dir",
        str(papers_dir),
        "--build-golden-set",
        "--live-retrieval",
        "--retrieval-policy",
        "paper_blind_semantic_rag_v1",
        "--output-dir",
        str(output),
    ])

    assert exit_code == 0
    report = json.loads((output / "evidence_regression_report.json").read_text(encoding="utf-8"))
    distribution = report["retrieval"]["rerank_distribution"]
    assert report["metadata"]["retrieval_policy"] == "paper_blind_semantic_rag_v1"
    assert report["metadata"]["lightweight_reranker_enabled"] is True
    assert distribution["reranked_evidence_count"] > 0


def _research_document_payload() -> dict:
    return {
        "paper_id": "p1",
        "source_hash": "hash",
        "sections": [
            {
                "section_id": "abstract",
                "title": "Abstract",
                "level": 1,
                "text": "This paper introduces a visual architecture.",
                "source_ref": "paper://p1/abstract",
            },
            {
                "section_id": "intro",
                "title": "Introduction",
                "level": 1,
                "text": "The visual architecture is shown in Figure 1. It improves retrieval.",
                "source_ref": "paper://p1/intro",
            },
            {
                "section_id": "method",
                "title": "Method",
                "level": 1,
                "text": "The model architecture uses encoder and decoder blocks.",
                "source_ref": "paper://p1/method",
            },
            {
                "section_id": "results",
                "title": "Results",
                "level": 1,
                "text": "Table 1 reports stronger accuracy and the conclusion confirms the gain.",
                "source_ref": "paper://p1/results",
            },
        ],
        "figures": [
            {
                "figure_id": "fig1",
                "caption": "Figure 1: visual architecture overview.",
                "source_ref": "paper://p1/fig1",
                "image_ref": "figures/arch.png",
                "page": 1,
            }
        ],
        "tables": [
            {
                "table_id": "tbl1",
                "caption": "Table 1: accuracy results.",
                "source_ref": "paper://p1/tbl1",
                "columns": ["metric", "value"],
                "rows": [{"metric": "accuracy", "value": "95"}],
                "page": 2,
            }
        ],
        "equations": [
            {
                "equation_id": "eq1",
                "latex": "y = f(x)",
                "source_ref": "paper://p1/eq1",
                "page": 2,
            }
        ],
        "references": [],
        "lineage": {
            "source_refs": ["paper://p1"],
            "source_hash": "hash",
            "artifact_refs": [],
            "metadata": {},
        },
        "metadata": {"parse_source": "nougat"},
    }


def test_diagnostic_identity_accounts_for_retrieval_inside_the_gated_answer_session() -> None:
    from backend.research.rag.evaluation.evidence_eval_runner import _diagnostic_execution_identity

    identity = _diagnostic_execution_identity(
        EvidenceEvalOptions(live_answer_eval=True, live_retrieval=False),
        answer_adapter="paper_rag_session_unity_llm",
    )

    assert identity["track"] == "end_to_end"
    assert identity["execution_profile"] == "fixture"
    assert identity["component_identity"]["retrieval_adapter"] == (
        "in_memory_chunk_store_with_sha256_hash_field_index"
    )
    assert identity["component_identity"]["generation_adapter"] == (
        "configured_unity_llm_via_gated_paper_rag_session"
    )
    assert identity["gold_context_used"] is False
    assert identity["production_qualification"]["eligible"] is False
