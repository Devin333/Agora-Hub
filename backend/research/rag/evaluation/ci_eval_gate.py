from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.research.rag.evaluation.candidate_experiment_eval import (
    CandidateExperimentQualityResult,
    evaluate_candidate_experiment_quality,
)
from backend.research.rag.evaluation.evidence_eval_runner import (
    EvidenceEvalOptions,
    run_evidence_eval_core,
)
from backend.research.rag.evaluation.paper_benchmark_suite import (
    PROMOTION_THRESHOLDS,
    PolicyPromotionCheck,
    PolicyPromotionChecklist,
)
from backend.research.rag.retrieval.policies import PAPER_BLIND_SEMANTIC_RAG_V1_POLICY

DEFAULT_OUTPUT_DIR = Path(".newsroom/eval/ci")
DEFAULT_RETRIEVAL_POLICY = PAPER_BLIND_SEMANTIC_RAG_V1_POLICY

DEFAULT_RETRIEVAL_THRESHOLDS: dict[str, float] = {
    "retrieval.hit_rate": 0.75,
    "retrieval.evidence_coverage": 0.70,
    "retrieval.required_type_coverage": 0.75,
    "retrieval.source_locator_coverage": 0.75,
    "retrieval.mrr": 0.45,
    "answer.abstention_accuracy": 0.90,
    "answer.success_rate": 0.90,
}

DEFAULT_PROMOTION_THRESHOLDS: dict[str, float] = {
    "overall_hit_at_3": PROMOTION_THRESHOLDS["overall_hit_at_3"],
    "overall_hit_at_5": PROMOTION_THRESHOLDS["overall_hit_at_5"],
    "overall_hit_at_10": PROMOTION_THRESHOLDS["overall_hit_at_10"],
    "overall_evidence_coverage_at_5": PROMOTION_THRESHOLDS["overall_evidence_coverage_at_5"],
    "overall_source_locator_coverage_at_5": PROMOTION_THRESHOLDS["overall_source_locator_coverage_at_5"],
    "overall_mrr": PROMOTION_THRESHOLDS["overall_mrr"],
    "formula_qa_hit_at_10": PROMOTION_THRESHOLDS["formula_qa_hit_at_10"],
    "citation_qa_hit_at_10": PROMOTION_THRESHOLDS["citation_qa_hit_at_10"],
    "figure_qa_hit_at_10": PROMOTION_THRESHOLDS["figure_qa_hit_at_10"],
    "table_qa_hit_at_10": PROMOTION_THRESHOLDS["table_qa_hit_at_10"],
    "answer_abstention_accuracy": 0.90,
    "answer_success_rate": 0.90,
}

_PROMOTION_QA_TYPES = ("citation_qa", "figure_qa", "formula_qa", "table_qa")


@dataclass(frozen=True)
class CIEvalGateResult:
    output_dir: Path
    evidence_report_path: Path
    evidence_markdown_path: Path
    promotion_report_path: Path
    promotion_markdown_path: Path
    golden_set_path: Path
    fixture_papers_dir: Path
    candidate_quality_report_path: Path
    candidate_quality_markdown_path: Path
    candidate_quality: CandidateExperimentQualityResult
    evidence_exit_code: int
    promotion_checklist: PolicyPromotionChecklist

    @property
    def passed(self) -> bool:
        return (
            self.evidence_exit_code == 0
            and self.promotion_checklist.ready_for_promotion
            and self.candidate_quality.passed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "evidence_exit_code": self.evidence_exit_code,
            "output_dir": str(self.output_dir),
            "evidence_report_path": str(self.evidence_report_path),
            "promotion_report_path": str(self.promotion_report_path),
            "golden_set_path": str(self.golden_set_path),
            "fixture_papers_dir": str(self.fixture_papers_dir),
            "candidate_quality_report_path": str(self.candidate_quality_report_path),
            "candidate_quality": self.candidate_quality.report,
            "promotion_checklist": self.promotion_checklist.to_dict(),
        }


def run_ci_eval_gate(
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    retrieval_policy: str = DEFAULT_RETRIEVAL_POLICY,
    retrieval_thresholds: Mapping[str, float] | None = None,
    promotion_thresholds: Mapping[str, float] | None = None,
    candidate_quality_thresholds: Mapping[str, float] | None = None,
) -> CIEvalGateResult:
    output = Path(output_dir)
    fixture_papers_dir = output / "fixtures" / "papers"
    golden_set_path = output / "golden_set.json"
    evidence_output_dir = output / "evidence"
    evidence_report_path = evidence_output_dir / "evidence_regression_report.json"
    evidence_markdown_path = evidence_output_dir / "evidence_regression_report.md"
    promotion_report_path = output / "promotion_gate_report.json"
    promotion_markdown_path = output / "promotion_gate_report.md"
    candidate_quality_report_path = output / "candidate_experiment_quality_report.json"
    candidate_quality_markdown_path = output / "candidate_experiment_quality_report.md"

    thresholds = dict(DEFAULT_RETRIEVAL_THRESHOLDS)
    thresholds.update(dict(retrieval_thresholds or {}))
    promotion_gate_thresholds = dict(DEFAULT_PROMOTION_THRESHOLDS)
    promotion_gate_thresholds.update(dict(promotion_thresholds or {}))

    _write_fixture_papers(fixture_papers_dir)
    evidence_exit_code = run_evidence_eval_core(_evidence_eval_options(
        fixture_papers_dir=fixture_papers_dir,
        golden_set_path=golden_set_path,
        retrieval_policy=retrieval_policy,
        evidence_output_dir=evidence_output_dir,
        thresholds=thresholds,
    ))
    evidence_report = _read_json(evidence_report_path)
    candidate_quality = evaluate_candidate_experiment_quality(
        evidence_report,
        papers_dir=fixture_papers_dir,
        golden_set_path=golden_set_path,
        retrieval_policy=retrieval_policy,
        thresholds=candidate_quality_thresholds,
    )
    _write_candidate_quality_artifacts(
        candidate_quality,
        json_path=candidate_quality_report_path,
        markdown_path=candidate_quality_markdown_path,
    )
    checklist = build_ci_promotion_checklist(
        evidence_report,
        evidence_report_path=evidence_report_path,
        thresholds=promotion_gate_thresholds,
    )
    _write_promotion_artifacts(
        checklist,
        evidence_report_path=evidence_report_path,
        json_path=promotion_report_path,
        markdown_path=promotion_markdown_path,
    )
    return CIEvalGateResult(
        output_dir=output,
        evidence_report_path=evidence_report_path,
        evidence_markdown_path=evidence_markdown_path,
        promotion_report_path=promotion_report_path,
        promotion_markdown_path=promotion_markdown_path,
        golden_set_path=golden_set_path,
        fixture_papers_dir=fixture_papers_dir,
        candidate_quality_report_path=candidate_quality_report_path,
        candidate_quality_markdown_path=candidate_quality_markdown_path,
        candidate_quality=candidate_quality,
        evidence_exit_code=evidence_exit_code,
        promotion_checklist=checklist,
    )


def _evidence_eval_options(
    *,
    fixture_papers_dir: Path,
    golden_set_path: Path,
    retrieval_policy: str,
    evidence_output_dir: Path,
    thresholds: Mapping[str, float],
) -> EvidenceEvalOptions:
    return EvidenceEvalOptions(
        papers_dir=fixture_papers_dir,
        build_golden_set=True,
        golden_set=golden_set_path,
        live_retrieval=True,
        retrieval_policy=retrieval_policy,
        output_dir=evidence_output_dir,
        max_pairs_per_type=2,
        domain="ci",
        deterministic_answer_eval=True,
        thresholds=dict(thresholds),
    )


def build_ci_promotion_checklist(
    evidence_report: Mapping[str, Any],
    *,
    evidence_report_path: str | Path,
    thresholds: Mapping[str, float] | None = None,
) -> PolicyPromotionChecklist:
    promotion_thresholds = dict(DEFAULT_PROMOTION_THRESHOLDS)
    promotion_thresholds.update(dict(thresholds or {}))
    retrieval = dict(evidence_report.get("retrieval") or {})
    answer = dict(evidence_report.get("answer") or {})
    metadata = dict(evidence_report.get("metadata") or {})
    policy_name = str(metadata.get("retrieval_policy") or "")
    checks = [
        _promotion_check(
            "evidence_report_passed",
            "Evidence regression report passed configured CI thresholds",
            bool(evidence_report.get("passed")),
            actual=bool(evidence_report.get("passed")),
            threshold="report.passed is true",
            details="; ".join(str(issue) for issue in evidence_report.get("issues") or []),
        ),
        _promotion_check(
            "policy_name",
            "Promotion-eligible Paper RAG policy selected",
            policy_name == PAPER_BLIND_SEMANTIC_RAG_V1_POLICY,
            actual=policy_name,
            threshold=PAPER_BLIND_SEMANTIC_RAG_V1_POLICY,
        ),
        _promotion_check(
            "top_k_retrieval_metrics",
            "Top-k retrieval metrics include @3, @5, and @10",
            _top_k_metrics_present(retrieval),
            actual=sorted((retrieval.get("by_k") or {}).keys()),
            threshold=["3", "5", "10"],
        ),
        _promotion_check(
            "route_distribution",
            "Route distribution is present",
            bool(retrieval.get("route_distribution")),
            actual=retrieval.get("route_distribution") or {},
            threshold="non-empty",
        ),
        _promotion_check(
            "field_embedding_distribution",
            "Field embedding distribution is present",
            _field_distribution_present(retrieval),
            actual=(retrieval.get("field_embedding_distribution") or {}).get("matched_evidence_count", 0),
            threshold="matched_evidence_count > 0",
        ),
        _promotion_check(
            "rerank_distribution",
            "Rerank distribution is present",
            _rerank_distribution_present(retrieval),
            actual=(retrieval.get("rerank_distribution") or {}).get("reranked_evidence_count", 0),
            threshold="reranked_evidence_count > 0",
        ),
        _metric_check(
            "overall_hit_at_3",
            "Overall Hit@3 meets staged retrieval gate",
            _hit_at_k(retrieval, 3),
            promotion_thresholds["overall_hit_at_3"],
        ),
        _metric_check(
            "overall_hit_at_5",
            "Overall Hit@5 meets staged retrieval gate",
            _hit_at_k(retrieval, 5),
            promotion_thresholds["overall_hit_at_5"],
        ),
        _metric_check(
            "overall_hit_at_10",
            "Overall Hit@10 meets staged retrieval gate",
            _hit_at_k(retrieval, 10),
            promotion_thresholds["overall_hit_at_10"],
        ),
        _metric_check(
            "overall_evidence_coverage_at_5",
            "Overall evidence coverage@5 meets staged retrieval gate",
            _by_k_metric(retrieval, 5, "evidence_coverage"),
            promotion_thresholds["overall_evidence_coverage_at_5"],
        ),
        _metric_check(
            "overall_source_locator_coverage_at_5",
            "Overall source locator coverage@5 meets staged retrieval gate",
            _by_k_metric(retrieval, 5, "source_locator_coverage"),
            promotion_thresholds["overall_source_locator_coverage_at_5"],
        ),
        _metric_check(
            "overall_mrr",
            "Overall MRR meets staged retrieval gate",
            _metric(retrieval, "mrr"),
            promotion_thresholds["overall_mrr"],
        ),
        _qa_type_presence_check(retrieval),
        _metric_check(
            "formula_qa_hit_at_10",
            "Formula QA Hit@10 meets staged retrieval gate",
            _qa_type_hit_at_k(retrieval, "formula_qa", 10),
            promotion_thresholds["formula_qa_hit_at_10"],
        ),
        _metric_check(
            "citation_qa_hit_at_10",
            "Citation QA Hit@10 meets staged retrieval gate",
            _qa_type_hit_at_k(retrieval, "citation_qa", 10),
            promotion_thresholds["citation_qa_hit_at_10"],
        ),
        _metric_check(
            "figure_qa_hit_at_10",
            "Figure QA Hit@10 meets staged retrieval gate",
            _qa_type_hit_at_k(retrieval, "figure_qa", 10),
            promotion_thresholds["figure_qa_hit_at_10"],
        ),
        _metric_check(
            "table_qa_hit_at_10",
            "Table QA Hit@10 meets staged retrieval gate",
            _qa_type_hit_at_k(retrieval, "table_qa", 10),
            promotion_thresholds["table_qa_hit_at_10"],
        ),
        _promotion_check(
            "negative_abstention_samples",
            "CI gate includes expected-abstain samples",
            _expected_behavior_count(metadata, "abstain") > 0,
            actual=metadata.get("expected_behavior_counts") or {},
            threshold={"abstain": "> 0"},
        ),
        _metric_check(
            "answer_abstention_accuracy",
            "Deterministic answer-eval pipeline abstention accuracy meets PR gate",
            _metric(answer, "abstention_accuracy"),
            promotion_thresholds["answer_abstention_accuracy"],
        ),
        _metric_check(
            "answer_success_rate",
            "Deterministic answer-eval pipeline success rate meets PR gate",
            _metric(answer, "success_rate"),
            promotion_thresholds["answer_success_rate"],
        ),
        _promotion_check(
            "evidence_report_artifact",
            "Evidence report path is recorded",
            bool(str(evidence_report_path)),
            actual=str(evidence_report_path),
            threshold="non-empty path",
        ),
    ]
    return PolicyPromotionChecklist(
        policy_name=policy_name,
        ready_for_promotion=all(check.status == "pass" for check in checks),
        reported_split="ci-mini-corpus",
        tuning_split="none",
        thresholds=promotion_thresholds,
        checks=tuple(checks),
    )


def _write_fixture_papers(papers_dir: Path) -> None:
    for payload in _fixture_research_documents():
        paper_dir = papers_dir / str(payload["paper_id"])
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / "research_document.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def write_ci_eval_fixture_papers(papers_dir: str | Path) -> None:
    _write_fixture_papers(Path(papers_dir))


def _fixture_research_documents() -> list[dict[str, Any]]:
    return [
        _research_document_payload(
            paper_id="ci_atlas_retrieval",
            source_hash="ci-atlas-v1",
            topic="Atlas retrieval",
            method="semantic evidence router",
            result_metric="answer accuracy",
            result_value="95.0",
        ),
        _research_document_payload(
            paper_id="ci_beacon_tables",
            source_hash="ci-beacon-v1",
            topic="Beacon table fusion",
            method="typed table context expander",
            result_metric="table grounding F1",
            result_value="91.5",
        ),
        _research_document_payload(
            paper_id="ci_cedar_formulas",
            source_hash="ci-cedar-v1",
            topic="Cedar formula recall",
            method="formula sparse recall channel",
            result_metric="formula recall",
            result_value="93.2",
        ),
    ]


def _research_document_payload(
    *,
    paper_id: str,
    source_hash: str,
    topic: str,
    method: str,
    result_metric: str,
    result_value: str,
) -> dict[str, Any]:
    source_ref = f"paper://{paper_id}"
    return {
        "paper_id": paper_id,
        "source_hash": source_hash,
        "sections": [
            {
                "section_id": "abstract",
                "title": "Abstract",
                "level": 1,
                "text": (
                    f"{topic} introduces a {method} for grounded research retrieval. "
                    f"The approach improves {result_metric} while preserving source locators."
                ),
                "source_ref": f"{source_ref}/abstract",
                "metadata": {"source_locator": f"{source_ref}#section=abstract&page=1"},
            },
            {
                "section_id": "method",
                "title": "Method",
                "level": 1,
                "text": (
                    f"The method uses a {method} with deterministic evidence typing. "
                    "Equation 1 defines the confidence score used by the retrieval gate. "
                    "Figure 1 shows the architecture used for the retrieval pipeline."
                ),
                "source_ref": f"{source_ref}/method",
                "metadata": {"source_locator": f"{source_ref}#section=method&page=2"},
            },
            {
                "section_id": "results",
                "title": "Results",
                "level": 1,
                "text": (
                    f"Table 1 reports {result_metric} of {result_value} for {topic}. "
                    f"The result confirms that {method} improves grounded evidence recall."
                ),
                "source_ref": f"{source_ref}/results",
                "metadata": {"source_locator": f"{source_ref}#section=results&page=3"},
            },
        ],
        "figures": [
            {
                "figure_id": "fig1",
                "caption": f"Figure 1: {topic} architecture with the {method}.",
                "source_ref": f"{source_ref}/fig1",
                "image_ref": "figures/architecture.png",
                "page": 2,
                "metadata": {"source_locator": f"{source_ref}#figure=1&page=2"},
            }
        ],
        "tables": [
            {
                "table_id": "tbl1",
                "caption": f"Table 1: {topic} result for {result_metric}.",
                "source_ref": f"{source_ref}/tbl1",
                "columns": ["metric", "value", "method"],
                "rows": [
                    {
                        "metric": result_metric,
                        "value": result_value,
                        "method": method,
                    }
                ],
                "page": 3,
                "metadata": {"source_locator": f"{source_ref}#table=1&page=3"},
            }
        ],
        "equations": [
            {
                "equation_id": "eq1",
                "latex": r"s = \alpha r + \beta g",
                "source_ref": f"{source_ref}/eq1",
                "page": 2,
                "metadata": {
                    "source_locator": f"{source_ref}#equation=1&page=2",
                    "equation_label": "1",
                },
            }
        ],
        "references": [],
        "lineage": {
            "source_refs": [source_ref],
            "source_hash": source_hash,
            "artifact_refs": [],
            "metadata": {},
        },
        "metadata": {"parse_source": "latex", "ci_eval_fixture": True},
    }


def _write_promotion_artifacts(
    checklist: PolicyPromotionChecklist,
    *,
    evidence_report_path: Path,
    json_path: Path,
    markdown_path: Path,
) -> None:
    payload = {
        **checklist.to_dict(),
        "evidence_report_path": str(evidence_report_path),
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(
        _promotion_markdown(payload),
        encoding="utf-8",
    )


def _promotion_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Paper RAG CI Promotion Gate",
        "",
        f"**Status:** {'PASS' if payload.get('ready_for_promotion') else 'FAIL'}",
        "",
        f"- policy: `{payload.get('policy_name')}`",
        f"- reported_split: `{payload.get('reported_split')}`",
        f"- tuning_split: `{payload.get('tuning_split')}`",
        f"- evidence_report_path: `{payload.get('evidence_report_path')}`",
        "",
        "## Checks",
        "",
        "| Check | Status | Actual | Threshold |",
        "| --- | --- | --- | --- |",
    ]
    for check in payload.get("checks") or []:
        lines.append(
            "| "
            + " | ".join([
                f"`{check.get('check_id')}`",
                f"`{check.get('status')}`",
                _markdown_value(check.get("actual")),
                _markdown_value(check.get("threshold")),
            ])
            + " |"
        )
        details = str(check.get("details") or "").strip()
        if details:
            lines.append(f"<!-- {check.get('check_id')}: {details} -->")
    lines.extend(["", "## Thresholds", ""])
    thresholds = payload.get("thresholds") or {}
    for key in sorted(thresholds):
        lines.append(f"- `{key}`: `{thresholds[key]}`")
    return "\n".join(lines).rstrip() + "\n"


def _write_candidate_quality_artifacts(
    result: CandidateExperimentQualityResult,
    *,
    json_path: Path,
    markdown_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result.report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_candidate_quality_markdown(result.report), encoding="utf-8")


def _candidate_quality_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Retrieval Candidate Experiment Quality Gate",
        "",
        f"- Passed: `{bool(report.get('passed'))}`",
        f"- Rollout eligible: `{bool(report.get('rollout_eligible'))}`",
        f"- Case set: `{report.get('case_set_version')}`",
        f"- Code version: `{report.get('code_version')}`",
        f"- Config version: `{report.get('config_version')}`",
        "",
        "## Aggregate contract",
        "",
    ]
    for metric, value in sorted((report.get("aggregate") or {}).items()):
        threshold = (report.get("thresholds") or {}).get(metric)
        lines.append(f"- `{metric}`: actual=`{value}`, threshold=`{threshold}`")
    lines.extend([
        "",
        "## Cases",
        "",
        "| Case | Passed | Accepted | Fallback | Expected reason | Actual reason | Fallback reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    for case in report.get("cases") or []:
        lines.append(
            f"| `{case.get('case_id')}` | `{case.get('passed')}` | "
            f"`{case.get('actual_accept')}` | `{case.get('actual_fallback')}` | "
            f"`{case.get('expected_reason')}` | `{case.get('actual_reason')}` | "
            f"`{case.get('actual_fallback_reason')}` |"
        )
    lines.extend(["", "## Paired quality", ""])
    for metric, values in sorted((report.get("paired_quality") or {}).items()):
        lines.append(
            f"- `{metric}`: baseline=`{values.get('baseline')}`, "
            f"candidate=`{values.get('candidate')}`, delta=`{values.get('delta')}`"
        )
    lines.extend(["", "## Diagnostics", ""])
    for diagnostic in report.get("diagnostics") or []:
        lines.append(f"- `{diagnostic.get('code')}`: `{json.dumps(diagnostic, sort_keys=True)}`")
    return "\n".join(lines).rstrip() + "\n"


def _promotion_check(
    check_id: str,
    label: str,
    passed: bool,
    *,
    actual: Any = None,
    threshold: Any = None,
    details: str = "",
) -> PolicyPromotionCheck:
    return PolicyPromotionCheck(
        check_id=check_id,
        label=label,
        status="pass" if passed else "fail",
        actual=actual,
        threshold=threshold,
        details=details,
    )


def _metric_check(check_id: str, label: str, actual: float, threshold: float) -> PolicyPromotionCheck:
    return _promotion_check(
        check_id,
        label,
        actual >= threshold,
        actual=round(actual, 6),
        threshold=threshold,
    )


def _qa_type_presence_check(retrieval: Mapping[str, Any]) -> PolicyPromotionCheck:
    actual = sorted((retrieval.get("by_qa_type") or {}).keys())
    missing = [qa_type for qa_type in _PROMOTION_QA_TYPES if qa_type not in actual]
    return _promotion_check(
        "by_qa_type_metrics",
        "CI gate includes required by-QA-type retrieval metrics",
        not missing,
        actual=actual,
        threshold=list(_PROMOTION_QA_TYPES),
        details=f"missing={missing}" if missing else "",
    )


def _expected_behavior_count(metadata: Mapping[str, Any], behavior: str) -> int:
    raw = metadata.get("expected_behavior_counts") or {}
    if not isinstance(raw, Mapping):
        return 0
    try:
        return int(raw.get(behavior) or 0)
    except (TypeError, ValueError):
        return 0


def _top_k_metrics_present(retrieval: Mapping[str, Any]) -> bool:
    by_k = retrieval.get("by_k") or {}
    required = ("3", "5", "10")
    required_metrics = (
        "hit_rate",
        "evidence_coverage",
        "required_type_coverage",
        "source_locator_coverage",
    )
    return all(
        k in by_k and all(metric in (by_k.get(k) or {}) for metric in required_metrics)
        for k in required
    )


def _field_distribution_present(retrieval: Mapping[str, Any]) -> bool:
    distribution = retrieval.get("field_embedding_distribution") or {}
    return int(distribution.get("matched_evidence_count") or 0) > 0


def _rerank_distribution_present(retrieval: Mapping[str, Any]) -> bool:
    distribution = retrieval.get("rerank_distribution") or {}
    return int(distribution.get("reranked_evidence_count") or 0) > 0


def _qa_type_hit_at_k(retrieval: Mapping[str, Any], qa_type: str, k: int) -> float:
    sub = (retrieval.get("by_qa_type") or {}).get(qa_type) or {}
    return _hit_at_k(sub, k)


def _hit_at_k(retrieval: Mapping[str, Any], k: int) -> float:
    return _by_k_metric(retrieval, k, "hit_rate")


def _by_k_metric(retrieval: Mapping[str, Any], k: int, key: str) -> float:
    return _metric(((retrieval.get("by_k") or {}).get(str(k)) or {}), key)


def _metric(values: Mapping[str, Any], key: str) -> float:
    try:
        return float(values.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _markdown_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return "`" + json.dumps(value, sort_keys=True) + "`"
    return f"`{value}`"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_thresholds(values: Sequence[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"threshold must use METRIC=VALUE form: {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"threshold metric is empty: {raw!r}")
        thresholds[key] = float(value)
    return thresholds


__all__ = [
    "CIEvalGateResult",
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_PROMOTION_THRESHOLDS",
    "DEFAULT_RETRIEVAL_POLICY",
    "DEFAULT_RETRIEVAL_THRESHOLDS",
    "build_ci_promotion_checklist",
    "parse_thresholds",
    "run_ci_eval_gate",
    "write_ci_eval_fixture_papers",
]
