from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.rag.evaluation.evidence_eval_runner import (
    _build_live_retriever,
    _load_chunks_from_papers_dir,
)
from backend.research.rag.evaluation.paper_evidence_eval import (
    EvidenceRetrievalEvaluator,
    load_evidence_golden_set,
)
from backend.research.rag.evaluation.quality_foundation_cases import (
    evaluate_quality_foundation_cases,
)
from backend.research.rag.models import ResearchRetrievalGoal
from backend.research.rag.retrieval.candidate_experiment import (
    CANDIDATE_EXPERIMENT_VERSION,
    ResearchCandidatePlanner,
    RetrievalCandidateExperimentConfig,
)
from backend.research.services.rag_policy import ResearchRAGPolicyBuilder
from framework.harness.rag.planner import DeterministicRAGPlanner
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus


CANDIDATE_EVAL_CASE_SET_VERSION = "research-candidate-cases-v2"
CANDIDATE_EVAL_CONFIG_VERSION = "research-candidate-thresholds-v2"
DEFAULT_CANDIDATE_QUALITY_THRESHOLDS: dict[str, float] = {
    "case_pass_rate": 1.0,
    "constraint_preservation_rate": 1.0,
    "fallback_accuracy": 1.0,
    "control_field_rejection_rate": 1.0,
    "input_integrity_rate": 1.0,
    "claim_qualification_rate": 1.0,
    "max_quality_regression": 0.0,
    "min_rollout_hit_rate_delta": 0.01,
}
_QUALITY_METRICS = (
    "retrieval.hit_rate",
    "retrieval.evidence_coverage",
    "retrieval.required_type_coverage",
    "retrieval.source_locator_coverage",
    "retrieval.citation_accuracy",
    "retrieval.mrr",
)


class _EvalCandidateWorker:
    def __init__(self, candidate: dict[str, Any] | None = None, *, mode: str = "candidate") -> None:
        self.candidate = candidate
        self.mode = mode

    def generate(self, _request: dict[str, Any], **_kwargs: Any) -> HarnessWorkerResult:
        if self.mode == "timeout":
            raise TimeoutError("fixed evaluation timeout")
        if self.mode == "failure":
            return HarnessWorkerResult(status=HarnessWorkerStatus.FAILED, output={})
        if self.mode == "invalid":
            return HarnessWorkerResult(
                status=HarnessWorkerStatus.SUCCEEDED,
                output={"candidate": "invalid-candidate-payload"},
            )
        return HarnessWorkerResult(
            status=HarnessWorkerStatus.SUCCEEDED,
            output={} if self.candidate is None else {"candidate": self.candidate},
        )


@dataclass(frozen=True)
class CandidateExperimentQualityResult:
    passed: bool
    rollout_eligible: bool
    report: dict[str, Any]


def evaluate_candidate_experiment_quality(
    evidence_report: Mapping[str, Any],
    *,
    papers_dir: str | Path,
    golden_set_path: str | Path,
    retrieval_policy: str,
    thresholds: Mapping[str, float] | None = None,
) -> CandidateExperimentQualityResult:
    configured_thresholds = _quality_thresholds(thresholds)
    cases = tuple(_evaluate_case_safely(case) for case in _fixed_cases())
    foundation_cases = _evaluate_foundation_contracts()
    all_cases = (*cases, *foundation_cases)
    constraint_cases = tuple(case for case in cases if case["category"] == "constraint")
    fallback_cases = tuple(case for case in cases if case["expected_fallback"])
    control_cases = tuple(case for case in cases if case["category"] == "control")
    input_cases = tuple(case for case in foundation_cases if case["category"] == "input_integrity")
    claim_cases = tuple(case for case in foundation_cases if case["category"] == "claim_qualification")
    aggregate = {
        "case_count": len(all_cases),
        "case_pass_rate": _rate(case["passed"] for case in all_cases),
        "constraint_preservation_rate": _rate(case["passed"] for case in constraint_cases),
        "fallback_accuracy": _rate(case["passed"] for case in fallback_cases),
        "control_field_rejection_rate": _rate(case["passed"] for case in control_cases),
        "input_integrity_rate": _rate(case["passed"] for case in input_cases),
        "claim_qualification_rate": _rate(case["passed"] for case in claim_cases),
    }
    paired_execution_error: str | None = None
    try:
        baseline, candidate = _paired_retrieval_metrics(
            papers_dir=Path(papers_dir),
            golden_set_path=Path(golden_set_path),
            retrieval_policy=retrieval_policy,
        )
    except Exception as exc:
        baseline, candidate = {}, {}
        paired_execution_error = type(exc).__name__
    paired, unavailable = _paired_quality_report(baseline, candidate)
    regressions = {
        key: values
        for key, values in paired.items()
        if values["delta"] is not None
        and values["delta"] < -configured_thresholds["max_quality_regression"]
    }
    contract_failures = [
        {
            "metric": metric,
            "actual": aggregate[metric],
            "threshold": configured_thresholds[metric],
        }
        for metric in (
            "case_pass_rate",
            "constraint_preservation_rate",
            "fallback_accuracy",
            "control_field_rejection_rate",
            "input_integrity_rate",
            "claim_qualification_rate",
        )
        if aggregate[metric] < configured_thresholds[metric]
    ]
    failed_case_ids = [str(case["case_id"]) for case in all_cases if not case["passed"]]
    contract_passed = not failed_case_ids and not contract_failures
    quality_passed = not unavailable and not regressions
    hit_delta = paired.get("retrieval.hit_rate", {}).get("delta")
    rollout_eligible = bool(
        contract_passed
        and quality_passed
        and hit_delta is not None
        and hit_delta >= configured_thresholds["min_rollout_hit_rate_delta"]
    )
    diagnostics: list[dict[str, Any]] = []
    if failed_case_ids:
        diagnostics.append({"code": "fixed_quality_case_failed", "case_ids": failed_case_ids})
    if contract_failures:
        diagnostics.append({"code": "quality_contract_threshold_failed", "failures": contract_failures})
    if paired_execution_error is not None:
        diagnostics.append({
            "code": "paired_quality_execution_failed",
            "error_type": paired_execution_error,
        })
    if unavailable:
        diagnostics.append({"code": "quality_metric_unavailable", "metrics": unavailable})
    if regressions:
        diagnostics.append({"code": "paired_quality_regression", "metrics": sorted(regressions)})
    rollout_blockers: list[str] = []
    if not contract_passed:
        rollout_blockers.append("quality_contract_failed")
    if not quality_passed:
        rollout_blockers.append("paired_quality_failed")
    if rollout_blockers:
        diagnostics.append({
            "code": "rollout_quality_gate_failed",
            "blockers": rollout_blockers,
        })
    if (
        hit_delta is not None
        and hit_delta < configured_thresholds["min_rollout_hit_rate_delta"]
    ):
        rollout_blockers.append("hit_rate_gain_below_threshold")
        diagnostics.append({
            "code": "rollout_gain_threshold_not_met",
            "actual": hit_delta,
            "threshold": configured_thresholds["min_rollout_hit_rate_delta"],
        })
    report = {
        "passed": contract_passed and quality_passed,
        "rollout_eligible": rollout_eligible,
        "case_set_version": CANDIDATE_EVAL_CASE_SET_VERSION,
        "code_version": CANDIDATE_EXPERIMENT_VERSION,
        "config_version": CANDIDATE_EVAL_CONFIG_VERSION,
        "provenance": _provenance(
            all_cases,
            configured_thresholds,
            retrieval_policy=retrieval_policy,
            papers_dir=Path(papers_dir),
            golden_set_path=Path(golden_set_path),
        ),
        "candidate_configuration": RetrievalCandidateExperimentConfig(enabled=True).to_dict(),
        "paired_candidate_configuration": RetrievalCandidateExperimentConfig(enabled=True).to_dict(),
        "baseline": {
            "source": "separate_fixed_corpus_baseline_run",
            "metrics": {metric: values["baseline"] for metric, values in paired.items()},
            "ci_evidence_report_passed": bool(evidence_report.get("passed")),
        },
        "candidate": {
            "source": "separate_fixed_corpus_candidate_run",
            "metrics": {metric: values["candidate"] for metric, values in paired.items()},
        },
        "cases": list(all_cases),
        "aggregate": aggregate,
        "paired_quality": paired,
        "thresholds": configured_thresholds,
        "diagnostics": diagnostics,
        "rollout_blockers": rollout_blockers,
        "decision_owner": "research_rag_ci_gate",
    }
    return CandidateExperimentQualityResult(
        passed=bool(report["passed"]),
        rollout_eligible=rollout_eligible,
        report=report,
    )


def _fixed_cases() -> tuple[dict[str, Any], ...]:
    original = (
        "From paper://atlas-paper in English, which Atlas v1 results in 2024 "
        "show accuracy, are not evaluated, and use the reported test split?"
    )
    base = {
        "category": "constraint",
        "mode": "candidate",
        "original": original,
        "expected_accept": False,
        "expected_fallback": True,
        "candidate_metadata": {},
    }
    control_base = {
        **base,
        "category": "control",
        "candidate": original,
        "expected_reason": "candidate_invalid",
    }
    fallback_base = {
        **base,
        "category": "fallback",
        "candidate": original,
    }
    return (
        {
            **base,
            "case_id": "candidate-preserves-protected-query",
            "candidate": original,
            "expected_accept": True,
            "expected_fallback": False,
            "expected_reason": "candidate_accepted",
        },
        {
            **base,
            "case_id": "candidate-drops-entity",
            "candidate": original.replace("Atlas", "Beacon"),
            "expected_reason": "entity_constraint_missing",
            "expected_violation": "entity_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-changes-time",
            "candidate": original.replace("2024", "2025"),
            "expected_reason": "time_constraint_missing",
            "expected_violation": "time_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-changes-source",
            "candidate": original.replace("paper://atlas-paper", "paper://beacon-paper"),
            "expected_reason": "source_constraint_missing",
            "expected_violation": "source_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-changes-language",
            "candidate": original.replace("English", "French"),
            "expected_reason": "language_constraint_missing",
            "expected_violation": "language_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-changes-version",
            "candidate": original.replace("v1", "v2"),
            "expected_reason": "version_constraint_missing",
            "expected_violation": "version_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-changes-metric",
            "candidate": original.replace("accuracy", "recall"),
            "expected_reason": "metric_constraint_missing",
            "expected_violation": "metric_constraint_missing",
        },
        {
            **base,
            "case_id": "candidate-reverses-negation",
            "candidate": original.replace("are not evaluated", "are not not evaluated"),
            "expected_reason": "unbounded_candidate_expansion",
            "expected_violation": "negation_constraint_changed",
        },
        {
            **base,
            "case_id": "candidate-drops-configured-constraint",
            "candidate": original.replace("reported test split", "validation split"),
            "expected_reason": "configured_constraint_missing",
            "expected_violation": "configured_constraint_missing",
        },
        {
            **control_base,
            "case_id": "candidate-attempts-routing-control",
            "candidate_metadata": {"routing": {"next_step": "publish"}},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-quality-control",
            "candidate_metadata": {"diagnostics": {"quality_passed": True}},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-memory-control",
            "candidate_metadata": {"memory_write": {"namespace": "research:ci"}},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-tool-control",
            "candidate_metadata": {"tool_authorization": {"allowed": True}},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-publication-control",
            "candidate_metadata": {"publish": True},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-qualification-control",
            "candidate_metadata": {"result": {"eligible": True, "qualification_ref": "forged://qualification"}},
        },
        {
            **control_base,
            "case_id": "candidate-attempts-skill-promotion-control",
            "candidate_metadata": {"result": {"skill_promotion": "candidate-v2"}},
        },
        {
            **fallback_base,
            "case_id": "candidate-invalid-payload",
            "mode": "invalid",
            "expected_reason": "candidate_invalid",
        },
        {
            **fallback_base,
            "case_id": "candidate-worker-failure",
            "mode": "failure",
            "expected_reason": "worker_failed",
        },
        {
            **fallback_base,
            "case_id": "candidate-worker-timeout",
            "mode": "timeout",
            "expected_reason": "worker_timeout",
        },
        {
            **fallback_base,
            "case_id": "candidate-worker-budget-exhausted",
            "mode": "budget",
            "expected_reason": "worker_budget_exhausted",
        },
    )


def _evaluate_case_safely(case: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _evaluate_case(case)
    except Exception as exc:
        expected_fallback = bool(case.get("expected_fallback"))
        expected_reason = str(case.get("expected_reason") or "")
        return {
            "case_set_version": CANDIDATE_EVAL_CASE_SET_VERSION,
            "case_id": str(case.get("case_id") or "unknown-case"),
            "category": str(case.get("category") or "unknown"),
            "passed": False,
            "expected_accept": bool(case.get("expected_accept")),
            "actual_accept": False,
            "expected_fallback": expected_fallback,
            "actual_fallback": False,
            "expected_reason": expected_reason,
            "actual_reason": "case_execution_error",
            "expected_fallback_reason": expected_reason if expected_fallback else None,
            "actual_fallback_reason": None,
            "expected_violation": case.get("expected_violation"),
            "actual_violation_codes": [],
            "source_locator_checksums": [],
            "qualification_ref": None,
            "candidate_configuration": None,
            "diagnostics": {
                "code": "case_execution_error",
                "error_type": type(exc).__name__,
            },
            "lineage": {
                "original_query": str(case.get("original") or ""),
                "candidate_id": None,
                "candidate_queries": [],
                "baseline_queries": [],
                "executed_queries": [],
            },
        }


def _evaluate_case(case: Mapping[str, Any]) -> dict[str, Any]:
    original = str(case["original"])
    goal = ResearchRetrievalGoal(
        goal_id=str(case["case_id"]),
        paper_id="atlas-paper",
        question=original,
        required_evidence_types=["experiment"],
        target_claims=["Atlas"],
        allowed_source_refs=["paper://atlas-paper"],
        allowed_memory_namespaces=["research:ci"],
        constraints={
            "source": "paper://atlas-paper",
            "language": "English",
            "version": "v1",
            "metric": "accuracy",
            "time": "2024",
            "evaluation_scope": "reported test split",
        },
    )
    spec = ResearchRAGPolicyBuilder().build_session_spec(
        graph_identity=build_paper_analysis_context_graph_identity(
            run_id=f"candidate-eval-{case['case_id']}",
            stage_id="run_research_rag",
        ),
        session_id=f"candidate-eval-{case['case_id']}",
        goal=goal,
        allowed_corpora=("research-papers",),
    )
    payload = {
        "candidate_id": str(case["case_id"]),
        "queries": [{
            "step_id": f"{case['case_id']}:query",
            "operation": "search_corpus",
            "query": str(case["candidate"]),
            "corpus": "research-papers",
        }],
        "metadata": dict(case.get("candidate_metadata") or {}),
    }
    mode = str(case.get("mode") or "candidate")
    config = RetrievalCandidateExperimentConfig(
        enabled=True,
        max_worker_calls=0 if mode == "budget" else 1,
        configured_hard_constraints={
            "source": ("paper://atlas-paper",),
            "language": ("English",),
            "configured": ("reported test split",),
        },
    )
    baseline = DeterministicRAGPlanner().plan(spec, round_index=0, gap_report={})
    planner = ResearchCandidatePlanner(
        _EvalCandidateWorker(payload, mode=mode),
        config=config,
    )
    plan = planner.plan(spec, round_index=0, gap_report={})
    lineage = dict(plan.metadata.get("candidate_experiment") or {})
    actual_accept = bool(lineage.get("accepted"))
    actual_fallback = bool(lineage.get("used_fallback"))
    actual_reason = str(lineage.get("reason") or "")
    actual_fallback_reason = lineage.get("fallback_reason")
    expected_accept = bool(case["expected_accept"])
    expected_fallback = bool(case["expected_fallback"])
    expected_reason = str(case["expected_reason"])
    expected_fallback_reason = expected_reason if expected_fallback else None
    expected_violation = case.get("expected_violation")
    violations = tuple(dict(item) for item in lineage.get("violations") or ())
    violation_codes = tuple(str(item.get("code") or "") for item in violations)
    query_matches = (
        tuple(step.query for step in plan.queries)
        == tuple(step.query for step in baseline.queries)
        if expected_fallback
        else tuple(step.query for step in plan.queries) == (str(case["candidate"]),)
    )
    passed = (
        actual_accept is expected_accept
        and actual_fallback is expected_fallback
        and actual_reason == expected_reason
        and actual_fallback_reason == expected_fallback_reason
        and query_matches
        and (expected_violation is None or expected_violation in violation_codes)
    )
    return {
        "case_set_version": CANDIDATE_EVAL_CASE_SET_VERSION,
        "case_id": case["case_id"],
        "category": case["category"],
        "passed": passed,
        "expected_accept": expected_accept,
        "actual_accept": actual_accept,
        "expected_fallback": expected_fallback,
        "actual_fallback": actual_fallback,
        "expected_reason": expected_reason,
        "actual_reason": actual_reason,
        "expected_fallback_reason": expected_fallback_reason,
        "actual_fallback_reason": actual_fallback_reason,
        "expected_violation": expected_violation,
        "actual_violation_codes": list(violation_codes),
        "source_locator_checksums": [],
        "qualification_ref": None,
        "candidate_configuration": config.to_dict(),
        "diagnostics": {
            "violations": list(violations),
            "query_matches_expected_plan": query_matches,
        },
        "lineage": {
            "original_query": original,
            "candidate_id": lineage.get("candidate_id"),
            "candidate_queries": lineage.get("candidate_queries") or [],
            "baseline_queries": [step.query for step in baseline.queries],
            "executed_queries": [step.query for step in plan.queries],
        },
    }


def _evaluate_foundation_contracts() -> tuple[dict[str, Any], ...]:
    return tuple(
        _normalize_foundation_case(case)
        for case in evaluate_quality_foundation_cases()
    )


def _normalize_foundation_case(case: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(case)
    diagnostics = normalized.get("diagnostics")
    diagnostic_mapping = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
    normalized.setdefault("case_set_version", CANDIDATE_EVAL_CASE_SET_VERSION)
    normalized.setdefault("expected_accept", None)
    normalized.setdefault("actual_accept", None)
    normalized.setdefault("expected_fallback", False)
    normalized.setdefault("actual_fallback", False)
    normalized.setdefault("expected_fallback_reason", None)
    normalized.setdefault("actual_fallback_reason", None)
    normalized.setdefault("expected_violation", None)
    normalized.setdefault("actual_violation_codes", [])
    normalized.setdefault(
        "source_locator_checksums",
        list(diagnostic_mapping.get("source_locator_checksums") or ()),
    )
    normalized.setdefault("qualification_ref", diagnostic_mapping.get("qualification_ref"))
    normalized.setdefault("candidate_configuration", None)
    normalized["diagnostics"] = diagnostics if diagnostics is not None else {}
    return normalized


def _paired_retrieval_metrics(
    *,
    papers_dir: Path,
    golden_set_path: Path,
    retrieval_policy: str,
) -> tuple[dict[str, float], dict[str, float]]:
    chunks = _load_chunks_from_papers_dir(papers_dir)
    pairs = load_evidence_golden_set(golden_set_path)
    baseline_retriever, _ = _build_live_retriever(
        chunks,
        visual_enabled=False,
        image_root=None,
        retrieval_policy=retrieval_policy,
    )
    candidate_retriever, _ = _build_live_retriever(
        chunks,
        visual_enabled=False,
        image_root=None,
        retrieval_policy=retrieval_policy,
    )
    baseline_result = EvidenceRetrievalEvaluator(baseline_retriever).evaluate(pairs)
    candidate_pairs = [_admitted_candidate_pair(pair) for pair in pairs]
    candidate_result = EvidenceRetrievalEvaluator(candidate_retriever).evaluate(candidate_pairs)
    return _retrieval_metrics(baseline_result), _retrieval_metrics(candidate_result)


def _admitted_candidate_pair(pair: Any) -> Any:
    case_id = hashlib.sha256(f"{pair.paper_id}\n{pair.qa_type}\n{pair.question}".encode("utf-8")).hexdigest()[:16]
    evidence_types = list(pair.required_evidence_types or [pair.qa_type])
    goal = ResearchRetrievalGoal(
        goal_id=f"candidate-quality-{case_id}", paper_id=pair.paper_id, question=pair.question,
        required_evidence_types=evidence_types, allowed_source_refs=[f"paper://{pair.paper_id}"],
        allowed_memory_namespaces=["research:ci"],
    )
    spec = ResearchRAGPolicyBuilder().build_session_spec(
        graph_identity=build_paper_analysis_context_graph_identity(run_id=f"candidate-quality-{case_id}", stage_id="run_research_rag"),
        session_id=f"candidate-quality-{case_id}", goal=goal, allowed_corpora=("research-papers",),
    )
    candidate_query = f"{pair.question} {evidence_types[0]}"
    payload = {
        "candidate_id": f"candidate-quality-{case_id}",
        "queries": [{"step_id": f"candidate-quality-{case_id}:query", "operation": "search_corpus", "query": candidate_query, "corpus": "research-papers"}],
    }
    plan = ResearchCandidatePlanner(
        _EvalCandidateWorker(payload),
        config=RetrievalCandidateExperimentConfig(enabled=True),
    ).plan(spec, round_index=0, gap_report={})
    lineage = dict(plan.metadata.get("candidate_experiment") or {})
    if not lineage.get("accepted"):
        raise ValueError(f"fixed candidate quality case {case_id!r} was rejected: {lineage.get('reason')}")
    return replace(pair, question=str(plan.queries[0].query))


def _retrieval_metrics(result: Any) -> dict[str, float]:
    return {
        "retrieval.hit_rate": result.hit_rate(5),
        "retrieval.evidence_coverage": result.evidence_coverage(5),
        "retrieval.required_type_coverage": result.required_type_coverage(5),
        "retrieval.source_locator_coverage": result.source_locator_coverage(5),
        "retrieval.citation_accuracy": result.citation_accuracy(5),
        "retrieval.mrr": result.mrr(),
    }


def _quality_thresholds(overrides: Mapping[str, float] | None) -> dict[str, float]:
    configured = dict(DEFAULT_CANDIDATE_QUALITY_THRESHOLDS)
    if overrides is None:
        return configured
    if not isinstance(overrides, Mapping):
        raise TypeError("candidate quality thresholds must be a mapping")
    unknown = sorted(set(map(str, overrides)) - set(configured))
    if unknown:
        raise ValueError(f"unknown candidate quality thresholds: {unknown}")
    for key, raw in overrides.items():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise TypeError(f"candidate quality threshold {key!r} must be numeric")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"candidate quality threshold {key!r} must be finite")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"candidate quality threshold {key!r} must be between 0 and 1")
        configured[str(key)] = value
    return configured


def _paired_quality_report(
    baseline: Any,
    candidate: Any,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    paired: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []
    for metric in _QUALITY_METRICS:
        baseline_value, baseline_issue = _quality_metric_value(baseline, metric)
        candidate_value, candidate_issue = _quality_metric_value(candidate, metric)
        if baseline_issue is not None:
            unavailable.append({"metric": metric, "side": "baseline", "reason": baseline_issue})
        if candidate_issue is not None:
            unavailable.append({"metric": metric, "side": "candidate", "reason": candidate_issue})
        paired[metric] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": (
                candidate_value - baseline_value
                if baseline_issue is None and candidate_issue is None
                else None
            ),
            "measurement": "separate_fixed_corpus_retrieval_runs",
        }
    return paired, unavailable


def _quality_metric_value(
    metrics: Any,
    metric: str,
) -> tuple[float | None, str | None]:
    if not isinstance(metrics, Mapping):
        return None, "report_not_mapping"
    if metric not in metrics:
        return None, "missing"
    raw = metrics[metric]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "not_numeric"
    value = float(raw)
    if not math.isfinite(value):
        return None, "not_finite"
    if not 0.0 <= value <= 1.0:
        return value, "out_of_range"
    return value, None


def _provenance(
    cases: tuple[dict[str, Any], ...],
    thresholds: Mapping[str, float],
    *,
    retrieval_policy: str,
    papers_dir: Path,
    golden_set_path: Path,
) -> dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[4]
    tested_sources = (
        "backend/research/application/__init__.py",
        "backend/research/application/paper_rag_session.py",
        "backend/research/application/single_paper_runtime.py",
        "backend/research/application/research_input_contract.py",
        "backend/research/application/claim_evidence_eligibility.py",
        "backend/research/application/graph_result_committer.py",
        "backend/research/application/rag_memory_admission.py",
        "backend/research/domain/__init__.py",
        "backend/research/domain/input_contract.py",
        "backend/research/domain/claim_review.py",
        "backend/research/graphs/__init__.py",
        "backend/research/graphs/gates.py",
        "backend/research/graphs/contracts.py",
        "backend/research/graphs/paper_analysis.py",
        "backend/research/graphs/task_plan.py",
        "backend/research/graphs/claim_evidence_eligibility_gate.py",
        "backend/research/services/__init__.py",
        "backend/research/services/claim_evidence_eligibility.py",
        "backend/research/services/claim_review_resolver.py",
        "backend/research/services/evidence_builder.py",
        "backend/research/rag/retrieval/__init__.py",
        "backend/research/rag/retrieval/candidate_experiment.py",
        "backend/research/rag/retrieval/contracts.py",
        "backend/research/rag/retrieval/plan.py",
        "backend/research/rag/retrieval/planner.py",
        "backend/research/rag/retrieval_port.py",
        "backend/research/rag/adapters/plan_worker.py",
        "backend/research/rag/adapters/llm_plan_worker.py",
        "backend/research/application/llm_client.py",
        "backend/research/services/rag_policy.py",
        "backend/research/rag/evaluation/evidence_eval_runner.py",
        "backend/research/rag/evaluation/paper_evidence_eval.py",
        "backend/research/rag/evaluation/quality_foundation_cases.py",
        "backend/research/rag/evaluation/candidate_experiment_eval.py",
        "backend/research/rag/evaluation/ci_eval_gate.py",
        "backend/research/rag/cli/run_ci_eval_gate.py",
        "interfaces/composition/research.py",
        "infrastructure/research/candidate_worker.py",
        "infrastructure/research/filesystem_run_store.py",
        "framework/agent/loop/__init__.py",
        "framework/agent/loop/prompt.py",
        "framework/agent/loop/loop.py",
        "framework/agent/loop/runner.py",
        "framework/llm/clients/openai_compatible.py",
        "framework/harness/subagents/execution.py",
        "framework/harness/subagents/runtime.py",
        "framework/harness/subagents/supervisor.py",
        "framework/harness/task_plan/parallel.py",
        "framework/harness/task_plan/planning_observation.py",
        "infrastructure/storage/harness/subagent_transcript.py",
        "infrastructure/storage/harness/planning_observation.py",
        "framework/agent/artifacts/stores/fs_safety.py",
        "framework/harness/rag/models.py",
        "framework/harness/rag/planner.py",
        "framework/harness/subagents/agent_runner.py",
        "framework/llm/models/request.py",
        "scripts/dev.py",
        ".github/workflows/ci.yml",
    )
    source_checksums = {
        relative_path: hashlib.sha256((repository_root / relative_path).read_bytes()).hexdigest()
        for relative_path in tested_sources
    }
    aggregate_source_checksum = _json_checksum(source_checksums)
    case_configurations = {
        str(case.get("case_id")): case.get("candidate_configuration")
        for case in cases
        if case.get("candidate_configuration") is not None
    }
    configuration_identity = {
        "config_version": CANDIDATE_EVAL_CONFIG_VERSION,
        "thresholds": dict(thresholds),
        "retrieval_policy": retrieval_policy,
        "paired_candidate_configuration": RetrievalCandidateExperimentConfig(enabled=True).to_dict(),
        "case_candidate_configurations": case_configurations,
    }
    fixture_inputs = {
        "papers": _directory_content_identity(papers_dir),
        "golden_set": _file_content_identity(golden_set_path),
    }
    return {
        "git_revision": os.environ.get("GITHUB_SHA") or os.environ.get("NEWSROOM_GIT_REVISION") or "unavailable",
        "worktree_dirty": None,
        "source_checksums_sha256": source_checksums,
        "source_checksum_sha256": aggregate_source_checksum,
        "case_checksum_sha256": _json_checksum(cases),
        "configuration_identity": configuration_identity,
        "config_checksum_sha256": _json_checksum(configuration_identity),
        "fixture_inputs": fixture_inputs,
        "fixture_checksum_sha256": _json_checksum({
            "papers": fixture_inputs["papers"]["sha256"],
            "golden_set": fixture_inputs["golden_set"]["sha256"],
        }),
    }


def _file_content_identity(path: Path) -> dict[str, Any]:
    exists = path.is_file()
    return {
        "path": str(path),
        "exists": exists,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if exists else None,
    }


def _directory_content_identity(path: Path) -> dict[str, Any]:
    files = {
        item.relative_to(path).as_posix(): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in sorted(
            (entry for entry in path.rglob("*") if entry.is_file()),
            key=lambda entry: entry.as_posix(),
        )
    } if path.is_dir() else {}
    return {
        "path": str(path),
        "exists": path.is_dir(),
        "files_sha256": files,
        "sha256": _json_checksum(files) if path.is_dir() else None,
    }


def _json_checksum(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _rate(values: Any) -> float:
    items = tuple(bool(value) for value in values)
    return sum(items) / len(items) if items else 0.0


__all__ = [
    "CANDIDATE_EVAL_CASE_SET_VERSION",
    "CANDIDATE_EVAL_CONFIG_VERSION",
    "CandidateExperimentQualityResult",
    "DEFAULT_CANDIDATE_QUALITY_THRESHOLDS",
    "evaluate_candidate_experiment_quality",
]
