from __future__ import annotations

import json

from backend.research.rag.evaluation.document_parsing_acceptance import (
    AcceptanceMetricRule,
    MetricDirection,
    PerformanceAcceptanceRule,
    ParsingAcceptanceProfile,
    evaluate_document_parsing_acceptance,
    load_acceptance_profile,
    write_acceptance_profile,
)
from backend.research.rag.evaluation.document_parsing_protocol import (
    AggregationUnit,
    CacheState,
    ExecutionKind,
    Exposure,
    IdentityValue,
    InputMode,
    MetricOutcome,
    MetricSpec,
    MetricStatus,
    PageOutcome,
    PageStatus,
    ParsingBudget,
    ParsingEvaluationManifest,
    ParsingExecutionIdentity,
    PerformanceRunConditions,
    ParsingFingerprints,
    ParsingTrack,
    RequestedPage,
    RunStatus,
)


def _identity(value: str) -> IdentityValue:
    return IdentityValue(value=value)


def _manifest(run_id: str, *, candidate: bool, edit_values: tuple[float, ...]) -> ParsingEvaluationManifest:
    known = _identity("sha256:" + "a" * 64)
    fingerprints = ParsingFingerprints(
        dataset=_identity("dataset@v1"),
        annotations=_identity("annotations@v1"),
        evaluator_commit=_identity("evaluator@commit"),
        evaluator_config_sha256=known,
        parser=_identity("parser-candidate" if candidate else "parser-baseline"),
        parser_image_digest=_identity("sha256:" + ("b" if candidate else "c") * 64),
        parser_weights_digest=known,
        parser_config_sha256=known,
        code_revision=_identity("revision-candidate" if candidate else "revision-baseline"),
        dirty_content_sha256=known,
        document_schema=_identity("research-document-fidelity.v1"),
        normalizer=_identity("normalizer/v1"),
        exporter=_identity("exporter/v1"),
        chunker=_identity("chunker/v1"),
        hardware=_identity("machine@fixed"),
    )
    pages = tuple(RequestedPage(
        page_id=f"page-{index}",
        input_sha256=f"{index:064x}",
        input_mode=InputMode.NATIVE_PDF,
        source_document_id=f"document-{index}",
        source_group_id=f"family-{index}",
        exposure=Exposure.FROZEN_HELDOUT,
    ) for index in range(1, len(edit_values) + 1))
    outcomes = tuple(PageOutcome(
        page_id=page.page_id,
        status=PageStatus.SUCCEEDED,
        elapsed_seconds=10.0 if not candidate else 11.0,
        metrics=(MetricOutcome(
            metric="body_edit_dist",
            status=MetricStatus.SUCCEEDED,
            aggregation_unit=AggregationUnit.PAGE,
            value=edit_values[index - 1],
            support_id=page.page_id,
        ),),
    ) for index, page in enumerate(pages, start=1))
    return ParsingEvaluationManifest(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        track=ParsingTrack.PRODUCTION_DOCUMENT,
        execution=ParsingExecutionIdentity(
            kind=ExecutionKind.PRODUCTION,
            composition=_identity("ResearchComposition@v1"),
            execution_receipt_refs=(f"artifact://receipt/{run_id}",),
            parser_invocation=_identity(f"parser-invocation/{run_id}"),
        ),
        fingerprints=fingerprints,
        budget=ParsingBudget(
            max_pages=len(edit_values),
            timeout_seconds_per_page=60,
            max_total_seconds=60 * len(edit_values),
            max_concurrency=1,
            max_retries=0,
            max_roi_attempts=0,
        ),
        performance_conditions=PerformanceRunConditions(
            concurrency=1,
            cache_state=CacheState.COLD,
            timing_scope_version=_identity("page-wall-clock/v1"),
        ),
        metrics=(MetricSpec(metric="body_edit_dist", aggregation_unit=AggregationUnit.PAGE),),
        requested_pages=pages,
        outcomes=outcomes,
    )


def _profile() -> ParsingAcceptanceProfile:
    return ParsingAcceptanceProfile(
        profile_id="body-edit-v1",
        track=ParsingTrack.PRODUCTION_DOCUMENT,
        metrics=(AcceptanceMetricRule(
            metric="body_edit_dist",
            aggregation_unit=AggregationUnit.PAGE,
            direction=MetricDirection.LOWER_IS_BETTER,
            minimum_improvement=0.01,
            maximum_regression=0.0,
            minimum_documents=2,
            confidence_level=0.95,
            bootstrap_replicates=1000,
        ),),
        performance=PerformanceAcceptanceRule(
            max_median_elapsed_ratio=2.0,
            max_p95_elapsed_ratio=2.0,
        ),
    )


def test_acceptance_passes_on_paired_document_heldout_with_ci_above_threshold():
    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.20, 0.15)),
        _profile(),
    )

    assert result["status"] == "pass"
    assert result["metrics"][0]["actual"]["document_count"] == 2
    assert result["metrics"][0]["actual"]["confidence_interval"][0] >= 0.01


def test_acceptance_fails_when_preregistered_quality_threshold_is_missed():
    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.298, 0.248)),
        _profile(),
    )

    assert result["status"] == "fail"
    assert result["metrics"][0]["reason"] == "confidence_bound_did_not_meet_preregistered_threshold"


def test_acceptance_blocks_diagnostic_or_exposed_dev_evidence():
    from backend.research.rag.evaluation.document_parsing_protocol import ExecutionKind

    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.2))
    payload = baseline.model_dump(mode="json")
    payload["track"] = ParsingTrack.BACKEND_RAW.value
    payload["execution"]["kind"] = ExecutionKind.DIAGNOSTIC.value
    payload["execution"]["execution_receipt_refs"] = []
    payload["execution"]["composition"] = {"value": None, "unknown_reason": "raw diagnostic"}
    diagnostic = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)
    candidate_payload = _manifest("candidate", candidate=True, edit_values=(0.2, 0.1)).model_dump(mode="json")
    candidate_payload["track"] = ParsingTrack.BACKEND_RAW.value
    candidate_payload["execution"]["kind"] = ExecutionKind.DIAGNOSTIC.value
    candidate_payload["execution"]["execution_receipt_refs"] = []
    candidate_payload["execution"]["composition"] = {"value": None, "unknown_reason": "raw diagnostic"}
    diagnostic_candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(candidate_payload), strict=True)
    result = evaluate_document_parsing_acceptance(
        diagnostic, diagnostic_candidate, _profile().model_copy(update={"track": ParsingTrack.BACKEND_RAW})
    )

    assert result["status"] == "blocked"
    assert result["quality_gate_decision"] == "not_evaluated"
    assert "baseline_execution_not_production" in result["reasons"]


def test_acceptance_blocks_when_metrics_have_missing_or_failed_outcomes():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.2))
    payload = baseline.model_dump(mode="json")
    failed_metric = MetricOutcome(
        metric="body_edit_dist",
        status=MetricStatus.ERROR,
        aggregation_unit=AggregationUnit.PAGE,
        reason="scorer_error",
    )
    payload["outcomes"][0]["metrics"] = [failed_metric.model_dump(mode="json")]
    invalid = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(invalid, _manifest("candidate", candidate=True, edit_values=(0.2, 0.1)), _profile())

    assert result["status"] == "blocked"
    assert result["reasons"][0].startswith("metric_outcomes_incomplete")


def test_acceptance_profile_is_immutable_and_fingerprint_verified(tmp_path):
    path = tmp_path / "acceptance-profile.json"
    profile = _profile()
    write_acceptance_profile(profile, str(path))
    assert load_acceptance_profile(str(path)) == profile
    write_acceptance_profile(profile, str(path))

    changed = profile.model_copy(update={"profile_id": "changed-profile"})
    try:
        write_acceptance_profile(changed, str(path))
    except FileExistsError:
        pass
    else:
        raise AssertionError("registered acceptance profile must not be replaceable")

    payload = path.read_text(encoding="utf-8").replace("body_edit_dist", "table_teds")
    path.write_text(payload, encoding="utf-8")
    try:
        load_acceptance_profile(str(path))
    except ValueError as error:
        assert "fingerprint mismatch" in str(error)
    else:
        raise AssertionError("tampered profile must be rejected")


def test_acceptance_fails_instead_of_dropping_candidate_page_failures():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2, 0.15))
    payload = candidate.model_dump(mode="json")
    payload["outcomes"][1] = {
        "page_id": "page-2",
        "status": "failed",
        "reason": "parser_output_missing",
        "elapsed_seconds": 2.0,
        "metrics": [{
            "metric": "body_edit_dist",
            "status": "missing_output",
            "aggregation_unit": "page",
            "value": None,
            "support_id": None,
            "reason": "parser_output_missing",
        }],
    }
    failed_candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, failed_candidate, _profile())

    assert result["status"] == "fail"
    assert result["actual"]["candidate_succeeded_pages"] == 1
    assert result["threshold"]["requested_page_success_ratio"] == 1.0
    assert result["reasons"] == ["requested_page_coverage_below_100_percent"]


def test_relative_error_improvement_uses_per_document_baseline_denominator():
    profile = _profile().model_copy(update={
        "metrics": (_profile().metrics[0].model_copy(update={
            "improvement_mode": "relative", "minimum_improvement": 0.09,
        }),),
    })
    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.27, 0.225)),
        profile,
    )

    assert result["status"] == "pass"
    assert abs(result["metrics"][0]["actual"]["mean_improvement"] - 0.1) < 1e-12
    assert result["metrics"][0]["threshold"]["improvement_mode"] == "relative"


def test_relative_improvement_blocks_zero_baseline_metric():
    profile = _profile().model_copy(update={
        "metrics": (_profile().metrics[0].model_copy(update={
            "improvement_mode": "relative", "minimum_improvement": 0.09,
        }),),
    })
    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.0, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.0, 0.20)),
        profile,
    )

    assert result["status"] == "blocked"
    assert "relative_improvement_baseline_zero:body_edit_dist" in result["reasons"]


def _replace_page_metrics(manifest, page_id: str, metrics: tuple[MetricOutcome, ...]):
    payload = manifest.model_dump(mode="json")
    outcome = next(item for item in payload["outcomes"] if item["page_id"] == page_id)
    outcome["metrics"] = [item.model_dump(mode="json") for item in metrics]
    return ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)


def _object_manifest(manifest, page_supports):
    payload = manifest.model_dump(mode="json")
    payload["metrics"] = [{"metric": "body_edit_dist", "aggregation_unit": "object"}]
    for outcome in payload["outcomes"]:
        outcome["metrics"] = [
            {
                "metric": "body_edit_dist",
                "status": "succeeded",
                "aggregation_unit": "object",
                "value": value,
                "support_id": support,
            }
            for support, value in page_supports.get(outcome["page_id"], ())
        ]
    return ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)


def _object_profile():
    return _profile().model_copy(update={
        "metrics": (_profile().metrics[0].model_copy(update={"aggregation_unit": AggregationUnit.OBJECT}),),
    })


def test_object_supports_pair_individually_when_a_page_has_multiple_objects():
    baseline = _object_manifest(
        _manifest("baseline", candidate=False, edit_values=(0.3, 0.25)),
        {"page-1": (("table-a", 0.3), ("table-b", 0.4)), "page-2": (("table-c", 0.25),)},
    )
    candidate = _object_manifest(
        _manifest("candidate", candidate=True, edit_values=(0.2, 0.15)),
        {"page-1": (("table-a", 0.2), ("table-b", 0.3)), "page-2": (("table-c", 0.15),)},
    )

    result = evaluate_document_parsing_acceptance(baseline, candidate, _object_profile())

    assert result["status"] == "pass"
    assert result["metrics"][0]["actual"]["paired_support_count"] == 3


def test_object_support_missing_on_one_side_blocks_without_dropping_support():
    baseline = _object_manifest(
        _manifest("baseline", candidate=False, edit_values=(0.3, 0.25)),
        {"page-1": (("table-a", 0.3), ("table-b", 0.4)), "page-2": (("table-c", 0.25),)},
    )
    candidate = _object_manifest(
        _manifest("candidate", candidate=True, edit_values=(0.2, 0.15)),
        {"page-1": (("table-a", 0.2),), "page-2": (("table-c", 0.15),)},
    )

    result = evaluate_document_parsing_acceptance(baseline, candidate, _object_profile())

    assert result["status"] == "blocked"
    assert result["reasons"] == ["metric_outcomes_incomplete:body_edit_dist:object"]
    assert any("table-b" in support for support in result["details"]["missing_or_invalid_supports"])

def test_identical_na_on_both_sides_blocks_when_gold_applicability_is_unverifiable():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2, 0.15))
    na = MetricOutcome(
        metric="body_edit_dist",
        status=MetricStatus.NOT_APPLICABLE,
        aggregation_unit=AggregationUnit.PAGE,
        reason="no_table_annotation",
    )
    baseline = _replace_page_metrics(baseline, "page-1", (na,))
    candidate = _replace_page_metrics(candidate, "page-1", (na,))

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert result["status"] == "blocked"
    assert result["reasons"] == ["metric_outcomes_incomplete:body_edit_dist:page"]
    assert any(
        reason.startswith("not_applicable_semantics_unverifiable:")
        for reason in result["details"]["missing_or_invalid_supports"]
    )


def test_unknown_formal_comparison_fingerprints_block_even_when_equal():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2, 0.15))
    payload = baseline.model_dump(mode="json")
    for field in ("dataset", "annotations", "evaluator_commit", "evaluator_config_sha256", "exporter", "hardware"):
        payload["fingerprints"][field] = {"value": None, "unknown_reason": "not recorded"}
    baseline = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)
    candidate_payload = candidate.model_dump(mode="json")
    for field in ("dataset", "annotations", "evaluator_commit", "evaluator_config_sha256", "exporter", "hardware"):
        candidate_payload["fingerprints"][field] = {"value": None, "unknown_reason": "not recorded"}
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(candidate_payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert result["status"] == "blocked"
    assert result["reasons"] == [
        "paired_fingerprint_unknown:dataset",
        "paired_fingerprint_unknown:annotations",
        "paired_fingerprint_unknown:evaluator_commit",
        "paired_fingerprint_unknown:evaluator_config_sha256",
        "paired_fingerprint_unknown:exporter",
        "paired_fingerprint_unknown:hardware",
    ]


def test_exporter_version_mismatch_blocks_paired_delta():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2, 0.15))
    payload = candidate.model_dump(mode="json")
    payload["fingerprints"]["exporter"] = {"value": "exporter/v2", "unknown_reason": None}
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert result["status"] == "blocked"
    assert result["reasons"] == ["paired_fingerprint_mismatch:exporter"]


def test_minimum_documents_counts_documents_but_bootstrap_requires_independent_groups():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2, 0.15))

    def one_source_group(manifest):
        payload = manifest.model_dump(mode="json")
        for page in payload["requested_pages"]:
            page["source_group_id"] = "shared-version-family"
        return ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(
        one_source_group(baseline), one_source_group(candidate), _profile()
    )

    assert result["status"] == "blocked"
    assert result["reasons"] == ["minimum_source_groups_not_met:body_edit_dist"]
    assert result["details"] == {"observed_source_groups": 1, "minimum_source_groups": 2}


def test_cluster_bootstrap_does_not_treat_same_family_documents_as_independent():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.3,) * 10)
    candidate = _manifest("candidate", candidate=True, edit_values=(0.2,) * 9 + (0.4,))

    def group_versions(manifest):
        payload = manifest.model_dump(mode="json")
        for index, page in enumerate(payload["requested_pages"], start=1):
            page["source_group_id"] = "repeated-version-family" if index < 10 else "independent-family"
        return ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(
        group_versions(baseline), group_versions(candidate), _profile()
    )

    assert result["status"] == "fail"
    actual = result["metrics"][0]["actual"]
    assert actual["document_count"] == 10
    assert actual["source_group_count"] == 2
    assert actual["mean_improvement"] > 0.01
    assert actual["confidence_interval"][0] < 0.0




def test_legacy_quality_profile_remains_readable_but_blocks_formal_total_gate():
    profile = _profile().model_copy(update={"performance": None})
    legacy_payload = profile.model_dump(mode="json")
    legacy_payload.pop("performance")
    restored = ParsingAcceptanceProfile.model_validate_json(json.dumps(legacy_payload), strict=True)

    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.20, 0.15)),
        restored,
    )

    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_profile_missing"]
    assert result["metrics"][0]["status"] == "pass"
    assert result["performance"]["status"] == "not_evaluated"


def test_performance_gate_reports_page_wall_clock_ratios():
    result = evaluate_document_parsing_acceptance(
        _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)),
        _manifest("candidate", candidate=True, edit_values=(0.20, 0.15)),
        _profile(),
    )

    assert result["performance"]["status"] == "pass"
    assert result["performance"]["actual"]["measurement"] == "PageOutcome.elapsed_seconds; per-page wall-clock seconds"
    assert result["performance"]["actual"]["median_elapsed_ratio"] == 1.1
    assert result["performance"]["actual"]["p95_elapsed_ratio"] == 1.1


def test_performance_gate_blocks_unknown_or_mismatched_conditions():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.30, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.20, 0.15))
    payload = candidate.model_dump(mode="json")
    payload["performance_conditions"]["cache_state"] = "warm"
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_cache_state_mismatch"]

    payload["performance_conditions"] = {
        "concurrency": None,
        "cache_state": "unknown",
        "timing_scope_version": None,
    }
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)
    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())
    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_concurrency_unknown"]


def test_performance_gate_fails_when_median_or_p95_ratio_exceeds_registered_limit():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.30, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.20, 0.15))
    payload = candidate.model_dump(mode="json")
    for outcome in payload["outcomes"]:
        outcome["elapsed_seconds"] = 30.0
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert result["status"] == "fail"
    assert result["performance"]["status"] == "fail"
    assert result["reasons"] == ["median_elapsed_ratio_exceeded", "p95_elapsed_ratio_exceeded"]


def test_performance_gate_blocks_missing_elapsed_and_zero_baseline():
    baseline = _manifest("baseline", candidate=False, edit_values=(0.30, 0.25))
    candidate = _manifest("candidate", candidate=True, edit_values=(0.20, 0.15))
    payload = candidate.model_dump(mode="json")
    payload["outcomes"][0]["elapsed_seconds"] = None
    candidate_missing = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)
    result = evaluate_document_parsing_acceptance(baseline, candidate_missing, _profile())
    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_elapsed_seconds_missing"]

    payload = baseline.model_dump(mode="json")
    for outcome in payload["outcomes"]:
        outcome["elapsed_seconds"] = 0.0
    baseline_zero = ParsingEvaluationManifest.model_validate_json(json.dumps(payload), strict=True)
    result = evaluate_document_parsing_acceptance(baseline_zero, candidate, _profile())
    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_baseline_median_zero"]



def test_legacy_manifest_without_performance_conditions_is_readable_but_blocks_resource_gate():
    baseline_payload = _manifest("baseline", candidate=False, edit_values=(0.30, 0.25)).model_dump(mode="json")
    candidate_payload = _manifest("candidate", candidate=True, edit_values=(0.20, 0.15)).model_dump(mode="json")
    baseline_payload.pop("performance_conditions")
    candidate_payload.pop("performance_conditions")
    baseline = ParsingEvaluationManifest.model_validate_json(json.dumps(baseline_payload), strict=True)
    candidate = ParsingEvaluationManifest.model_validate_json(json.dumps(candidate_payload), strict=True)

    result = evaluate_document_parsing_acceptance(baseline, candidate, _profile())

    assert baseline.performance_conditions is None
    assert result["status"] == "blocked"
    assert result["reasons"] == ["performance_conditions_missing"]
    assert result["performance"]["coverage"] == "page_elapsed_only_not_gpu_resource_acceptance"
