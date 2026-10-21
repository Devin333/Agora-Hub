from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.research.rag.cli.run_parser_bakeoff_report import _build_parser, _parse_thresholds
from backend.research.rag.evaluation.document_parsing_protocol import (
    AggregationUnit,
    ExecutionKind,
    IdentityValue,
    MetricOutcome,
    MetricStatus,
    PageOutcome,
    PageStatus,
    ParsingEvaluationManifest,
    ParsingExecutionIdentity,
    ParsingTrack,
    RunStatus,
    write_parsing_evaluation_manifest,
)
from tests.backend.research.rag.test_document_parsing_protocol import _manifest

from backend.research.rag.evaluation.paper_parser_bakeoff_report import (
    ParserArtifactInput,
    ParserBakeoffReportConfig,
    build_parser_bakeoff_report,
)


def test_build_parser_bakeoff_report_summarizes_parser_artifacts(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-marker"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "hello world", "metadata": {"source_locator": "pdf#page=1"}}],
        figures=[{
            "caption": "Figure 1",
            "image_ref": "fig.png",
            "source_ref": "pdf#page=1",
            "metadata": {"source_locator": "pdf#page=1", "pdf_rect": [1, 2, 3, 4]},
        }],
        tables=[{
            "caption": "Table 1",
            "rows": [{"Model": "base"}],
            "source_ref": "pdf#page=1",
            "metadata": {"source_locator": "pdf#page=1", "pdf_rect": [1, 2, 3, 4]},
        }],
        equations=[{
            "latex": "E=mc^2",
            "source_ref": "pdf#page=1",
            "metadata": {"source_locator": "pdf#page=1", "pdf_rect": [1, 2, 3, 4]},
        }],
    )
    rag_report = tmp_path / "benchmark_suite_report.json"
    rag_report.write_text(
        json.dumps({
            "reports": {
                "candidate": {
                    "metrics": {
                        "mrr": 0.5,
                        "by_k": {
                            "3": {"hit_rate": 0.3},
                            "5": {"hit_rate": 0.5, "ndcg": 0.4, "evidence_coverage": 0.6, "source_locator_coverage": 1.0},
                            "10": {"hit_rate": 0.8, "evidence_coverage": 0.7, "source_locator_coverage": 1.0},
                        },
                    },
                    "by_qa_type": {
                        "table_qa": {"by_k": {"5": {"evidence_coverage": 0.9}}},
                        "figure_qa": {"by_k": {"5": {"evidence_coverage": 0.8}}},
                        "formula_explanation_qa": {"mrr": 0.6},
                    },
                }
            }
        }),
        encoding="utf-8",
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("marker", parser_dir, rag_report),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    payload = report.to_dict()
    metrics = payload["parsers"]["marker"]["parser_metrics"]
    assert metrics["parse_success_rate"] == 1.0
    assert metrics["section_count_avg"] == 1.0
    assert metrics["table_rows_coverage"] == 1.0
    assert metrics["image_ref_coverage"] == 1.0
    assert metrics["element_source_locator_coverage"] == 1.0
    assert metrics["bbox_coverage"] == 1.0
    assert payload["parsers"]["marker"]["rag_metrics"]["hit_at_5"] == 0.5
    assert payload["parsers"]["marker"]["rag_metrics"]["table_qa_evidence_coverage_at_5"] == 0.9
    assert payload["parsers"]["marker"]["parsing_protocol"]["status"] == "not_evaluated"
    assert (tmp_path / "report.json").exists()
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Parser-Level Metrics" in markdown
    assert "RAG-Level Metrics" in markdown
    assert "Document Parsing Protocol" in markdown
    assert "not_evaluated" in markdown


def test_build_parser_bakeoff_report_reads_benchmark_suite_candidate_schema(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-marker"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "hello world"}],
        figures=[],
        tables=[],
        equations=[],
    )
    rag_report = tmp_path / "benchmark_suite_report.json"
    rag_report.write_text(
        json.dumps({
            "papers_total": 20,
            "chunks_total": 100,
            "pairs_total": 50,
            "evaluation_protocol": {"reported_split": "test"},
            "candidate_test_report": {
                "retrieval": {
                    "mrr": 0.55,
                    "by_k": {
                        "3": {"hit_rate": 0.7},
                        "5": {
                            "hit_rate": 0.75,
                            "ndcg": 0.6,
                            "evidence_coverage": 0.72,
                            "source_locator_coverage": 0.82,
                        },
                        "10": {
                            "hit_rate": 0.84,
                            "equivalent_hit_rate": 0.92,
                            "evidence_coverage": 0.8,
                            "source_locator_coverage": 0.9,
                        },
                    },
                    "by_qa_type": {
                        "table_qa": {"by_k": {"5": {"evidence_coverage": 0.91}}},
                        "figure_qa": {"by_k": {"5": {"evidence_coverage": 0.81}}},
                        "formula_explanation_qa": {"mrr": 0.61},
                    },
                }
            },
        }),
        encoding="utf-8",
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("marker", parser_dir, rag_report),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    rag_metrics = report.to_dict()["parsers"]["marker"]["rag_metrics"]
    assert rag_metrics["papers_total"] == 20
    assert rag_metrics["pairs_total"] == 50
    assert rag_metrics["hit_at_3"] == 0.7
    assert rag_metrics["hit_at_10"] == 0.84
    assert rag_metrics["equivalent_hit_at_10"] == 0.92
    assert rag_metrics["mrr"] == 0.55
    assert rag_metrics["table_qa_evidence_coverage_at_5"] == 0.91
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "84.0%" in markdown
    assert "0.55" in markdown


def test_bakeoff_report_uses_artifact_directory_as_canonical_paper_id(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-marker"
    _write_doc(
        parser_dir / "1706.03762" / "research_document.json",
        paper_id="1706.03762_marker_smoke",
        sections=[],
        figures=[],
        tables=[],
        equations=[],
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("marker", parser_dir),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    payload = report.to_dict()
    assert payload["paper_ids"] == ["1706.03762"]
    assert payload["parsers"]["marker"]["paper_ids"] == ["1706.03762"]
    assert payload["parsers"]["marker"]["paper_id_mismatches"] == [{
        "artifact_paper_id": "1706.03762",
        "reported_paper_id": "1706.03762_marker_smoke",
    }]


def test_bakeoff_report_uses_ingest_manifest_for_requested_and_failed_items(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-mineru"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[],
        figures=[],
        tables=[],
        equations=[],
    )
    manifest_path = tmp_path / "mineru-ingest.json"
    manifest_path.write_text(
        json.dumps({
            "backend": "mineru",
            "papers_dir": str(parser_dir),
            "requested": 2,
            "succeeded": 1,
            "skipped": 0,
            "failed": 1,
            "items": [
                {
                    "arxiv_id": "paper1",
                    "paper_id": "paper1",
                    "status": "succeeded",
                    "backend": "mineru",
                    "output_path": str(parser_dir / "paper1" / "research_document.json"),
                },
                {
                    "arxiv_id": "paper2",
                    "paper_id": "paper2",
                    "status": "failed",
                    "backend": "mineru",
                    "reason": "RuntimeError: docker parser failed (exit 1)",
                    "output_path": str(parser_dir / "paper2" / "research_document.json"),
                },
            ],
        }),
        encoding="utf-8",
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("mineru", parser_dir, ingest_manifest_path=manifest_path),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    payload = report.to_dict()
    parser_payload = payload["parsers"]["mineru"]
    metrics = parser_payload["parser_metrics"]
    assert payload["paper_ids"] == ["paper1", "paper2"]
    assert parser_payload["paper_count"] == 2
    assert parser_payload["artifact_document_count"] == 1
    assert metrics["parse_requested_count"] == 2
    assert metrics["parse_success_count"] == 1
    assert metrics["parse_success_rate"] == 0.5
    assert parser_payload["failed_items"][0]["paper_id"] == "paper2"
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "50.0% (1/2)" in markdown
    assert "failed `paper2`" in markdown


def test_bakeoff_report_penalizes_ingest_failures(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-mineru"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "x" * 4000}],
        figures=[],
        tables=[],
        equations=[],
    )
    manifest_path = _write_manifest(
        tmp_path / "mineru-ingest.json",
        parser_dir=parser_dir,
        succeeded_ids=["paper1"],
        failed_ids=["paper2"],
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("mineru", parser_dir, ingest_manifest_path=manifest_path),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    penalized = report.to_dict()["parsers"]["mineru"]["penalized_metrics"]
    penalty_ids = {
        detail["penalty_id"]
        for detail in penalized["penalty_details"]
    }
    assert "ingest_failure_rate" in penalty_ids
    assert penalized["penalty_total"] > 0
    assert penalized["penalized_quality_score"] < penalized["raw_quality_score"]
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Parser Scoring" in markdown
    assert "ingest_failure_rate" in markdown


def test_bakeoff_report_marks_cascade_acceptance_ready_when_thresholds_pass(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-cascade"
    paper_ids = [f"paper{i:02d}" for i in range(20)]
    for paper_id in paper_ids:
        _write_high_quality_doc(parser_dir / paper_id / "research_document.json", paper_id=paper_id)
    manifest_path = _write_manifest(
        tmp_path / "cascade-ingest.json",
        parser_dir=parser_dir,
        succeeded_ids=paper_ids,
        failed_ids=[],
    )
    rag_report = _write_rag_report(tmp_path / "cascade-rag.json")

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("cascade", parser_dir, rag_report, manifest_path),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    recommendations = report.to_dict()["recommendations"]
    acceptance = recommendations["cascade_acceptance"]
    assert recommendations["best_penalized_parser"] == "cascade"
    assert acceptance["ready"] is True
    assert {check["status"] for check in acceptance["checks"]} == {"pass"}
    assert acceptance["thresholds"]["min_requested_papers"] == 20.0
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Cascade Acceptance" in markdown
    assert "min_requested_papers" in markdown


def test_bakeoff_report_marks_cascade_acceptance_failed_when_thresholds_fail(tmp_path: Path) -> None:
    parser_dir = tmp_path / "papers-cascade"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "short"}],
        figures=[],
        tables=[],
        equations=[],
    )

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("cascade", parser_dir),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    acceptance = report.to_dict()["recommendations"]["cascade_acceptance"]
    failing_checks = [
        check
        for check in acceptance["checks"]
        if check["status"] == "fail"
    ]
    assert acceptance["ready"] is False
    assert any(
        check["check_id"] == "min_requested_papers"
        and check["actual"] == 1
        and check["threshold"] == 20.0
        for check in failing_checks
    )
    assert all("actual" in check and "threshold" in check for check in failing_checks)


def test_run_parser_bakeoff_report_parses_acceptance_thresholds() -> None:
    parser = _build_parser()
    args = parser.parse_args([
        "--parser",
        "cascade=.newsroom/eval/cascade",
        "--acceptance-threshold",
        "min_requested_papers=10",
        "--acceptance-threshold",
        "min_rag_hit_at_10=0.75",
    ])

    assert _parse_thresholds(args.acceptance_threshold) == {
        "min_requested_papers": 10.0,
        "min_rag_hit_at_10": 0.75,
    }
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _parse_thresholds(["min_requested_papers"])



def _write_failed_production_parsing_manifest(path: Path) -> Path:
    base = _manifest()
    fingerprints = base.fingerprints.model_copy(update={
        "document_schema": IdentityValue(value="ResearchDocument@v1"),
        "normalizer": IdentityValue(value="research-normalizer@v1"),
    })
    manifest = ParsingEvaluationManifest.model_validate(
        base.model_dump(mode="python")
        | {
            "status": RunStatus.FAILED,
            "track": ParsingTrack.PRODUCTION_DOCUMENT,
            "execution": ParsingExecutionIdentity(
                kind=ExecutionKind.PRODUCTION,
                composition=IdentityValue(value="ResearchComposition@v1"),
                execution_receipt_refs=("artifact://receipt/run-001",),
                parser_invocation=IdentityValue(value="MarkerPdfDocumentParser/invoke-v1"),
            ),
            "fingerprints": fingerprints,
            "outcomes": (
                PageOutcome(
                    page_id="page-1",
                    status=PageStatus.FAILED,
                    reason="parser timed out",
                    elapsed_seconds=7.5,
                    metrics=(
                        MetricOutcome(
                            metric="CDM",
                            status=MetricStatus.TIMEOUT,
                            aggregation_unit=AggregationUnit.PAGE,
                            reason="page parsing exceeded its time budget",
                        ),
                    ),
                ),
            ),
        }
    )
    write_parsing_evaluation_manifest(manifest, path)
    return path


def test_bakeoff_report_includes_production_parsing_protocol_and_failure_denominators(
    tmp_path: Path,
) -> None:
    parser_dir = tmp_path / "papers-marker"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "valid legacy artifact"}],
        figures=[],
        tables=[],
        equations=[],
    )
    parsing_manifest = _write_failed_production_parsing_manifest(
        tmp_path / "parsing-manifest.json"
    )
    config = ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput(
            "marker",
            parser_dir,
            parsing_manifest_path=parsing_manifest,
        ),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    )

    report = build_parser_bakeoff_report(config)

    parser = report.to_dict()["parsers"]["marker"]
    parsing = parser["parsing_protocol"]
    assert parsing["status"] == "reported"
    assert parsing["track"] == "production_document"
    assert parsing["run_status"] == "failed"
    assert parsing["metric_denominators"]["CDM:page"]["status_counts"] == {"timeout": 1}
    assert parsing["metric_denominators"]["CDM:page"]["scored_denominator"] == 0
    assert parsing["fingerprints"]["parser"]["value"] == "MarkerPdfDocumentParser"
    assert parsing["fingerprints"]["parser_config_sha256"]["value"].startswith("sha256:")
    assert parsing["quality_gate"] == {
        "evaluated": False,
        "passed": None,
        "reason": "summary is descriptive only; no quality or publication decision was evaluated",
    }
    legacy_report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput("marker", parser_dir),),
        output_json=tmp_path / "legacy-report.json",
        output_markdown=tmp_path / "legacy-report.md",
    ))
    legacy_parser = legacy_report.to_dict()["parsers"]["marker"]
    assert parser["penalized_metrics"] == legacy_parser["penalized_metrics"]
    assert report.to_dict()["recommendations"]["cascade_acceptance"] == (
        legacy_report.to_dict()["recommendations"]["cascade_acceptance"]
    )
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["parsers"]["marker"]["parsing_protocol"] == parsing
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "production_document" in markdown
    assert "CDM:page=0/1" in markdown
    assert "n=1, total=7.50s, median=7.50s, p95=7.50s" in markdown
    assert "parser=MarkerPdfDocumentParser" in markdown
    assert "descriptive only" in markdown


def test_bakeoff_report_marks_present_invalid_parsing_manifest_invalid(
    tmp_path: Path,
) -> None:
    parser_dir = tmp_path / "papers-marker"
    _write_doc(
        parser_dir / "paper1" / "research_document.json",
        paper_id="paper1",
        sections=[{"text": "valid legacy artifact"}],
        figures=[],
        tables=[],
        equations=[],
    )
    invalid_manifest = tmp_path / "invalid-parsing-manifest.json"
    invalid_manifest.write_text('{"run_id": NaN}', encoding="utf-8")

    report = build_parser_bakeoff_report(ParserBakeoffReportConfig(
        inputs=(ParserArtifactInput(
            "marker",
            parser_dir,
            parsing_manifest_path=invalid_manifest,
        ),),
        output_json=tmp_path / "report.json",
        output_markdown=tmp_path / "report.md",
    ))

    parser = report.to_dict()["parsers"]["marker"]
    parsing = parser["parsing_protocol"]
    assert parsing["status"] == "invalid"
    assert "run_status" not in parsing
    assert "metric_denominators" not in parsing
    assert parsing["quality_gate"] == {
        "evaluated": False,
        "passed": None,
    }
    assert parser["penalized_metrics"]["raw_quality_score"] is not None
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "| marker | invalid |" in markdown


def _write_doc(
    path: Path,
    *,
    paper_id: str,
    sections: list[dict[str, object]],
    figures: list[dict[str, object]],
    tables: list[dict[str, object]],
    equations: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "paper_id": paper_id,
            "source_hash": "hash",
            "sections": sections,
            "figures": figures,
            "tables": tables,
            "equations": equations,
            "lineage": {"source_refs": ["pdf"], "source_hash": "hash"},
            "metadata": {
                "parser_duration_seconds": 1.5,
                "parser_warnings": [],
            },
        }),
        encoding="utf-8",
    )


def _write_high_quality_doc(path: Path, *, paper_id: str) -> None:
    located_element = {
        "caption": "Located artifact",
        "source_ref": "pdf#page=1",
        "metadata": {"source_locator": "pdf#page=1", "pdf_rect": [1, 2, 3, 4]},
    }
    _write_doc(
        path,
        paper_id=paper_id,
        sections=[{"text": "x" * 8000, "metadata": {"source_locator": "pdf#page=1"}}],
        figures=[{**located_element, "image_ref": "figure.png"}],
        tables=[{**located_element, "rows": [{"Model": "cascade"}]}],
        equations=[{**located_element, "latex": "E=mc^2"}],
    )


def _write_manifest(
    path: Path,
    *,
    parser_dir: Path,
    succeeded_ids: list[str],
    failed_ids: list[str],
) -> Path:
    items = [
        {
            "arxiv_id": paper_id,
            "paper_id": paper_id,
            "status": "succeeded",
            "backend": path.stem,
            "output_path": str(parser_dir / paper_id / "research_document.json"),
        }
        for paper_id in succeeded_ids
    ]
    items.extend(
        {
            "arxiv_id": paper_id,
            "paper_id": paper_id,
            "status": "failed",
            "backend": path.stem,
            "reason": "RuntimeError: parser failed",
            "output_path": str(parser_dir / paper_id / "research_document.json"),
        }
        for paper_id in failed_ids
    )
    path.write_text(
        json.dumps({
            "backend": path.stem,
            "papers_dir": str(parser_dir),
            "requested": len(succeeded_ids) + len(failed_ids),
            "succeeded": len(succeeded_ids),
            "skipped": 0,
            "failed": len(failed_ids),
            "items": items,
        }),
        encoding="utf-8",
    )
    return path


def _write_rag_report(path: Path) -> Path:
    path.write_text(
        json.dumps({
            "papers_total": 20,
            "chunks_total": 200,
            "pairs_total": 80,
            "candidate_test_report": {
                "retrieval": {
                    "mrr": 0.7,
                    "by_k": {
                        "5": {
                            "hit_rate": 0.72,
                            "ndcg": 0.66,
                            "evidence_coverage": 0.64,
                            "source_locator_coverage": 0.9,
                        },
                        "10": {
                            "hit_rate": 0.82,
                            "equivalent_hit_rate": 0.9,
                            "evidence_coverage": 0.7,
                            "source_locator_coverage": 0.92,
                        },
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    return path
