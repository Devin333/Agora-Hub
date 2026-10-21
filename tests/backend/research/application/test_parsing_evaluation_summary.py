from __future__ import annotations

from backend.research.application.parsing_evaluation_summary import (
    ParsingEvaluationSummaryService,
)
from backend.research.rag.evaluation.document_parsing_protocol import (
    AggregationUnit,
    MetricOutcome,
    MetricSpec,
    MetricStatus,
    PageOutcome,
    PageStatus,
    ParsingEvaluationManifest,
)
from tests.backend.research.rag.test_document_parsing_protocol import _manifest


def test_metric_mean_handles_finite_extreme_values_without_overflow() -> None:
    payload = _manifest().model_dump(mode="python")
    payload["metrics"] = (MetricSpec(metric="TEDS", aggregation_unit=AggregationUnit.OBJECT),)
    payload["outcomes"] = (
        PageOutcome(
            page_id="page-1",
            status=PageStatus.SUCCEEDED,
            metrics=(
                MetricOutcome(
                    metric="TEDS",
                    status=MetricStatus.SUCCEEDED,
                    aggregation_unit=AggregationUnit.OBJECT,
                    value=1e308,
                    support_id="table-1",
                ),
                MetricOutcome(
                    metric="TEDS",
                    status=MetricStatus.SUCCEEDED,
                    aggregation_unit=AggregationUnit.OBJECT,
                    value=1e308,
                    support_id="table-2",
                ),
            ),
        ),
    )
    manifest = ParsingEvaluationManifest.model_validate(payload)

    summary = ParsingEvaluationSummaryService().summarize(manifest)

    assert summary["metric_value_summary"]["TEDS:object"]["successful_value_mean"] == 1e308
    assert summary["quality_gate"] == {
        "evaluated": False,
        "passed": None,
        "reason": "summary is descriptive only; no quality or publication decision was evaluated",
    }



def test_page_elapsed_seconds_are_reported_without_inventing_missing_measurements() -> None:
    payload = _manifest().model_dump(mode="python")
    payload["outcomes"] = (payload["outcomes"][0] | {"elapsed_seconds": 2.5},)
    manifest = ParsingEvaluationManifest.model_validate(payload)

    summary = ParsingEvaluationSummaryService().summarize(manifest)

    assert summary["runtime_summary"] == {
        "measured_pages": 1,
        "total_seconds": 2.5,
        "median_seconds": 2.5,
        "p95_seconds": 2.5,
    }

    unmeasured = ParsingEvaluationSummaryService().summarize(_manifest())
    assert unmeasured["runtime_summary"] == {
        "measured_pages": 0,
        "total_seconds": None,
        "median_seconds": None,
        "p95_seconds": None,
    }
