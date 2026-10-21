from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend.research.rag.cli.run_ci_eval_gate import main
from backend.research.rag.evaluation import candidate_experiment_eval
from backend.research.rag.evaluation import ci_eval_gate
from backend.research.rag.evaluation.ci_eval_gate import run_ci_eval_gate


def test_ci_eval_gate_writes_evidence_and_promotion_reports(tmp_path: Path) -> None:
    output_dir = tmp_path / "ci-gate"

    result = run_ci_eval_gate(output_dir=output_dir)

    assert result.passed is True
    assert result.evidence_exit_code == 0
    assert result.evidence_report_path.exists()
    assert result.evidence_markdown_path.exists()
    assert result.promotion_report_path.exists()
    assert result.promotion_markdown_path.exists()
    assert result.golden_set_path.exists()
    assert result.candidate_quality_report_path.exists()
    assert result.candidate_quality_markdown_path.exists()

    evidence = json.loads(result.evidence_report_path.read_text(encoding="utf-8"))
    promotion = json.loads(result.promotion_report_path.read_text(encoding="utf-8"))
    candidate_quality = json.loads(result.candidate_quality_report_path.read_text(encoding="utf-8"))

    assert evidence["metadata"]["mode"] == "live_retrieval"
    assert evidence["metadata"]["answer_eval_mode"] == "deterministic"
    assert evidence["metadata"]["protocol"] == "legacy_evidence_regression_diagnostic"
    assert evidence["metadata"]["track"] == "retrieval"
    assert evidence["metadata"]["execution_profile"] == "fixture"
    assert evidence["metadata"]["purpose"] == "diagnostic_regression"
    assert evidence["metadata"]["component_identity"]["retrieval_adapter"] == (
        "in_memory_chunk_store_with_sha256_hash_field_index"
    )
    assert evidence["metadata"]["component_identity"]["answer_adapter"] == (
        "deterministic_gold_supported_answer"
    )
    assert evidence["metadata"]["gold_context_used"] is True
    assert evidence["metadata"]["production_qualification"]["eligible"] is False
    assert evidence["metadata"]["total_pairs"] > 0
    assert evidence["metadata"]["expected_behavior_counts"]["abstain"] > 0
    assert evidence["metadata"]["expected_behavior_counts"]["answer"] > 0
    assert evidence["retrieval"]["answerable_total"] > 0
    assert evidence["retrieval"]["abstain_total"] > 0
    assert evidence["answer"]["abstention_accuracy"] >= 0.9
    assert evidence["answer"]["success_rate"] >= 0.9
    assert evidence["thresholds"]["retrieval.hit_rate"] >= 0.75
    assert evidence["thresholds"]["answer.abstention_accuracy"] >= 0.9
    assert promotion["ready_for_promotion"] is True
    assert candidate_quality["passed"] is True
    assert isinstance(candidate_quality["rollout_eligible"], bool)
    assert candidate_quality["aggregate"]["case_pass_rate"] == 1.0
    assert candidate_quality["aggregate"]["constraint_preservation_rate"] == 1.0
    assert candidate_quality["aggregate"]["fallback_accuracy"] == 1.0
    assert candidate_quality["aggregate"]["control_field_rejection_rate"] == 1.0
    case_ids = {case["case_id"] for case in candidate_quality["cases"]}
    assert {
        "candidate-drops-entity",
        "candidate-changes-time",
        "candidate-changes-source",
        "candidate-changes-language",
        "candidate-changes-version",
        "candidate-changes-metric",
        "candidate-reverses-negation",
        "candidate-drops-configured-constraint",
        "candidate-invalid-payload",
        "candidate-worker-failure",
        "candidate-worker-timeout",
        "candidate-worker-budget-exhausted",
        "candidate-attempts-routing-control",
        "candidate-attempts-quality-control",
        "candidate-attempts-memory-control",
        "candidate-attempts-tool-control",
        "candidate-attempts-publication-control",
        "candidate-attempts-qualification-control",
    } <= case_ids
    assert candidate_quality["paired_quality"]["retrieval.citation_accuracy"]["measurement"] == "separate_fixed_corpus_retrieval_runs"
    foundation_cases = {
        case["case_id"]: case for case in candidate_quality["cases"]
        if case["category"] in {"input_integrity", "claim_qualification"}
    }
    assert {
        "input-final-request-constraint-loss-rejected",
        "input-final-request-source-loss-rejected",
        "claim-supported",
        "claim-contradicted",
        "claim-insufficient",
        "claim-unavailable",
        "claim-trusted-source-gate-positive",
        "claim-forged-source-binding-rejected",
    } <= set(foundation_cases)
    assert all(case["passed"] for case in foundation_cases.values())
    assert foundation_cases["claim-trusted-source-gate-positive"]["diagnostics"]["source_locator_checksums"]
    assert candidate_quality["provenance"]["source_checksum_sha256"]
    assert candidate_quality["provenance"]["config_checksum_sha256"]
    assert candidate_quality["provenance"]["fixture_checksum_sha256"]
    assert candidate_quality["provenance"]["fixture_inputs"]["papers"]["exists"] is True
    assert candidate_quality["provenance"]["fixture_inputs"]["golden_set"]["exists"] is True
    budget_case = next(case for case in candidate_quality["cases"] if case["case_id"] == "candidate-worker-budget-exhausted")
    assert budget_case["candidate_configuration"]["max_worker_calls"] == 0
    assert "backend/research/application/research_input_contract.py" in candidate_quality["provenance"]["source_checksums_sha256"]
    assert "backend/research/graphs/claim_evidence_eligibility_gate.py" in candidate_quality["provenance"]["source_checksums_sha256"]
    core_execution_sources = {
        "infrastructure/research/filesystem_run_store.py",
        "infrastructure/storage/harness/planning_observation.py",
        "framework/harness/task_plan/planning_observation.py",
        "backend/research/application/__init__.py",
        "backend/research/domain/__init__.py",
        "backend/research/graphs/__init__.py",
        "backend/research/graphs/gates.py",
        "backend/research/services/__init__.py",
        "backend/research/rag/retrieval/__init__.py",
        "framework/agent/loop/__init__.py",
        "framework/agent/artifacts/stores/fs_safety.py",
        "framework/agent/loop/loop.py",
        "framework/agent/loop/runner.py",
        "framework/harness/subagents/agent_runner.py",
        "framework/harness/subagents/execution.py",
        "framework/harness/subagents/runtime.py",
        "framework/harness/subagents/supervisor.py",
        "framework/harness/task_plan/parallel.py",
        "framework/llm/clients/openai_compatible.py",
        "infrastructure/storage/harness/subagent_transcript.py",
    }
    source_checksums = candidate_quality["provenance"]["source_checksums_sha256"]
    assert core_execution_sources <= set(source_checksums)
    repository_root = Path(__file__).resolve().parents[4]
    assert {
        source_path: source_checksums[source_path]
        for source_path in core_execution_sources
    } == {
        source_path: hashlib.sha256(
            (repository_root / source_path).read_bytes()
        ).hexdigest()
        for source_path in core_execution_sources
    }
    assert promotion["evidence_report_path"] == str(result.evidence_report_path)
    assert promotion["status_counts"] == {"pass": len(promotion["checks"])}
    assert {check["check_id"] for check in promotion["checks"]} >= {
        "evidence_report_passed",
        "overall_hit_at_3",
        "overall_evidence_coverage_at_5",
        "overall_source_locator_coverage_at_5",
        "by_qa_type_metrics",
        "negative_abstention_samples",
        "answer_abstention_accuracy",
        "answer_success_rate",
    }
    labels = {check["check_id"]: check["label"] for check in promotion["checks"]}
    assert labels["answer_abstention_accuracy"].startswith("Deterministic answer-eval pipeline")
    assert labels["answer_success_rate"].startswith("Deterministic answer-eval pipeline")


def test_ci_eval_gate_returns_failed_result_for_threshold_regression(tmp_path: Path) -> None:
    result = run_ci_eval_gate(
        output_dir=tmp_path / "ci-gate",
        retrieval_thresholds={"retrieval.hit_rate": 1.01},
    )

    evidence = json.loads(result.evidence_report_path.read_text(encoding="utf-8"))
    promotion = json.loads(result.promotion_report_path.read_text(encoding="utf-8"))

    assert result.passed is False
    assert result.evidence_exit_code == 1
    assert evidence["passed"] is False
    assert any("retrieval.hit_rate" in issue for issue in evidence["issues"])
    assert any(
        check["check_id"] == "evidence_report_passed" and check["status"] == "fail"
        for check in promotion["checks"]
    )


def test_ci_eval_gate_returns_failed_result_for_abstention_threshold_regression(tmp_path: Path) -> None:
    result = run_ci_eval_gate(
        output_dir=tmp_path / "ci-gate",
        retrieval_thresholds={"answer.abstention_accuracy": 1.01},
    )

    evidence = json.loads(result.evidence_report_path.read_text(encoding="utf-8"))
    promotion = json.loads(result.promotion_report_path.read_text(encoding="utf-8"))

    assert result.passed is False
    assert result.evidence_exit_code == 1
    assert evidence["passed"] is False
    assert any("answer.abstention_accuracy" in issue for issue in evidence["issues"])
    assert any(
        check["check_id"] == "evidence_report_passed" and check["status"] == "fail"
        for check in promotion["checks"]
    )


def test_ci_eval_gate_uses_structured_evidence_eval_options(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_run_evidence_eval_core(options, *, live_answer_ask=None) -> int:
        captured["options"] = options
        captured["live_answer_ask"] = live_answer_ask
        output_dir = Path(options.output_dir)
        output_dir.mkdir(parents=True)
        (output_dir / "evidence_regression_report.json").write_text(
            json.dumps({
                "passed": True,
                "issues": [],
                "metadata": {
                    "retrieval_policy": ci_eval_gate.DEFAULT_RETRIEVAL_POLICY,
                    "expected_behavior_counts": {"abstain": 1, "answer": 1},
                },
                "retrieval": {
                    "mrr": 1.0,
                    "by_k": {
                        str(k): {
                            "hit_rate": 1.0,
                            "evidence_coverage": 1.0,
                            "required_type_coverage": 1.0,
                            "source_locator_coverage": 1.0,
                            "citation_accuracy": 1.0,
                        }
                        for k in (3, 5, 10)
                    },
                    "by_qa_type": {
                        qa_type: {"by_k": {"10": {"hit_rate": 1.0}}}
                        for qa_type in ("citation_qa", "figure_qa", "formula_qa", "table_qa")
                    },
                    "route_distribution": {"default": 1},
                    "field_embedding_distribution": {"matched_evidence_count": 1},
                    "rerank_distribution": {"reranked_evidence_count": 1},
                },
                "answer": {
                    "abstention_accuracy": 1.0,
                    "success_rate": 1.0,
                },
            }),
            encoding="utf-8",
        )
        (output_dir / "evidence_regression_report.md").write_text("passed\n", encoding="utf-8")
        return 0

    def fake_candidate_quality(*_args, **_kwargs):
        report = {
            "passed": True,
            "rollout_eligible": False,
            "case_set_version": "test",
            "code_version": "test",
            "config_version": "test",
            "cases": [],
            "paired_quality": {},
            "diagnostics": [],
        }
        return ci_eval_gate.CandidateExperimentQualityResult(
            passed=True,
            rollout_eligible=False,
            report=report,
        )

    monkeypatch.setattr(ci_eval_gate, "run_evidence_eval_core", fake_run_evidence_eval_core)
    monkeypatch.setattr(ci_eval_gate, "evaluate_candidate_experiment_quality", fake_candidate_quality)

    result = ci_eval_gate.run_ci_eval_gate(output_dir=tmp_path / "ci-gate")

    options = captured["options"]
    assert result.passed is True
    assert result.evidence_exit_code == 0
    assert captured["live_answer_ask"] is None
    assert options.build_golden_set is True
    assert options.live_retrieval is True
    assert options.deterministic_answer_eval is True
    assert options.live_answer_eval is False
    assert options.max_pairs_per_type == 2
    assert options.domain == "ci"
    assert options.thresholds["answer.abstention_accuracy"] >= 0.9
    assert options.papers_dir == result.fixture_papers_dir
    assert options.golden_set == result.golden_set_path


def test_ci_eval_gate_candidate_contract_failure_is_part_of_passed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    report = {
        "passed": False,
        "rollout_eligible": False,
        "case_set_version": "test",
        "code_version": "test",
        "config_version": "test",
        "cases": [{"case_id": "failed-fixed-case", "passed": False}],
        "aggregate": {"case_pass_rate": 0.0},
        "paired_quality": {},
        "diagnostics": [{"code": "quality_contract_threshold_failed"}],
    }

    monkeypatch.setattr(
        ci_eval_gate,
        "evaluate_candidate_experiment_quality",
        lambda *_args, **_kwargs: ci_eval_gate.CandidateExperimentQualityResult(
            passed=False,
            rollout_eligible=False,
            report=report,
        ),
    )

    result = run_ci_eval_gate(output_dir=tmp_path / "ci-gate")

    assert result.passed is False
    assert result.candidate_quality.report["diagnostics"][0]["code"] == "quality_contract_threshold_failed"


def test_fixed_case_failure_cannot_be_overridden_by_relaxed_aggregate_thresholds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    failed_case = {
        "case_id": "forced-fixed-case-failure",
        "category": "constraint",
        "passed": False,
        "expected_fallback": False,
    }
    metrics = {metric: 0.5 for metric in candidate_experiment_eval._QUALITY_METRICS}
    papers_dir = tmp_path / "papers"
    papers_dir.mkdir()
    (papers_dir / "paper.json").write_text("{}", encoding="utf-8")
    golden_set = tmp_path / "golden.json"
    golden_set.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(candidate_experiment_eval, "_fixed_cases", lambda: (failed_case,))
    monkeypatch.setattr(candidate_experiment_eval, "_evaluate_case_safely", lambda _case: dict(failed_case))
    monkeypatch.setattr(candidate_experiment_eval, "_evaluate_foundation_contracts", lambda: ())
    monkeypatch.setattr(
        candidate_experiment_eval,
        "_paired_retrieval_metrics",
        lambda **_kwargs: (dict(metrics), dict(metrics)),
    )

    result = candidate_experiment_eval.evaluate_candidate_experiment_quality(
        {"passed": True},
        papers_dir=papers_dir,
        golden_set_path=golden_set,
        retrieval_policy="fixed-test-policy",
        thresholds={
            "case_pass_rate": 0.0,
            "constraint_preservation_rate": 0.0,
            "fallback_accuracy": 0.0,
            "control_field_rejection_rate": 0.0,
            "input_integrity_rate": 0.0,
            "claim_qualification_rate": 0.0,
            "max_quality_regression": 1.0,
            "min_rollout_hit_rate_delta": 0.0,
        },
    )

    assert result.passed is False
    assert result.rollout_eligible is False
    assert "quality_contract_failed" in result.report["rollout_blockers"]
    assert any(
        diagnostic["code"] == "fixed_quality_case_failed"
        and diagnostic["case_ids"] == ["forced-fixed-case-failure"]
        for diagnostic in result.report["diagnostics"]
    )


def test_candidate_quality_metrics_fail_closed_on_missing_or_nonfinite_values() -> None:
    baseline = {metric: 0.5 for metric in candidate_experiment_eval._QUALITY_METRICS}
    candidate = dict(baseline)
    candidate.pop("retrieval.citation_accuracy")
    candidate["retrieval.mrr"] = float("nan")

    paired, unavailable = candidate_experiment_eval._paired_quality_report(baseline, candidate)

    assert paired["retrieval.citation_accuracy"]["delta"] is None
    assert paired["retrieval.mrr"]["delta"] is None
    assert {tuple(sorted(issue.items())) for issue in unavailable} >= {
        tuple(sorted({"metric": "retrieval.citation_accuracy", "side": "candidate", "reason": "missing"}.items())),
        tuple(sorted({"metric": "retrieval.mrr", "side": "candidate", "reason": "not_finite"}.items())),
    }


@pytest.mark.parametrize(
    "thresholds",
    [
        {"fallback_accuracy": float("nan")},
        {"fallback_accuracy": -0.1},
        {"fallback_accuracy": 1.1},
        {"unknown_threshold": 0.5},
        {"fallback_accuracy": True},
    ],
)
def test_candidate_quality_thresholds_reject_invalid_configuration(thresholds) -> None:
    with pytest.raises((TypeError, ValueError)):
        candidate_experiment_eval._quality_thresholds(thresholds)


def test_ci_eval_gate_cli_returns_nonzero_on_failed_threshold(tmp_path: Path) -> None:
    exit_code = main([
        "--output-dir",
        str(tmp_path / "ci-gate"),
        "--threshold",
        "retrieval.hit_rate=1.01",
    ])

    assert exit_code == 1
