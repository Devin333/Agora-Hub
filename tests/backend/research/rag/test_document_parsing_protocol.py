from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.document_parsing_protocol import (
    AggregationUnit,
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
    ParsingFingerprints,
    ParsingTrack,
    RequestedPage,
    RunStatus,
    load_parsing_evaluation_manifest,
    metric_denominators,
    write_parsing_evaluation_manifest,
)


def _identity(value: str) -> IdentityValue:
    return IdentityValue(value=value)


def _fingerprints() -> ParsingFingerprints:
    known = _identity("sha256:" + "a" * 64)
    unknown = IdentityValue(unknown_reason="not recorded by historical runner")
    return ParsingFingerprints(
        dataset=_identity("OmniDocBench@aa1ee96"),
        annotations=_identity("OmniDocBench annotations@aa1ee96"),
        evaluator_commit=_identity("f133a71e9e91c3621c7ce8994200a7b394a06eb3"),
        evaluator_config_sha256=known,
        parser=_identity("MarkerPdfDocumentParser"),
        parser_image_digest=_identity("sha256:" + "b" * 64),
        parser_weights_digest=unknown,
        parser_config_sha256=known,
        code_revision=_identity("a" * 40),
        dirty_content_sha256=known,
        document_schema=unknown,
        normalizer=unknown,
        exporter=_identity("omnidoc-raw-export/v1"),
        chunker=unknown,
        hardware=_identity("local-cpu-runtime"),
    )


def _page() -> RequestedPage:
    return RequestedPage(
        page_id="page-1",
        input_sha256="c" * 64,
        input_mode=InputMode.PAGE_IMAGE,
        source_document_id=None,
        source_group_id=None,
        source_mapping_reason="OmniDocBench page has no verified source-document mapping",
        exposure=Exposure.EXPOSED_DEV,
        batch_id="batch-001",
        batch_page_index=0,
    )


def _manifest(*, track: ParsingTrack = ParsingTrack.BACKEND_RAW) -> ParsingEvaluationManifest:
    return ParsingEvaluationManifest(
        run_id="run-001",
        status=RunStatus.COMPLETED,
        track=track,
        execution=ParsingExecutionIdentity(
            kind=ExecutionKind.DIAGNOSTIC,
            composition=IdentityValue(unknown_reason="raw benchmark subprocess is not production composition"),
            parser_invocation=_identity("MarkerPdfDocumentParser/direct-subprocess"),
        ),
        fingerprints=_fingerprints(),
        budget=ParsingBudget(
            max_pages=25, timeout_seconds_per_page=900, max_total_seconds=21_600,
            max_concurrency=1, max_retries=0, max_roi_attempts=0,
        ),
        metrics=(MetricSpec(metric="CDM", aggregation_unit=AggregationUnit.PAGE),),
        requested_pages=(_page(),),
        outcomes=(
            PageOutcome(
                page_id="page-1",
                status=PageStatus.SUCCEEDED,
                metrics=(MetricOutcome(
                    metric="CDM",
                    status=MetricStatus.SUCCEEDED,
                    aggregation_unit=AggregationUnit.PAGE,
                    value=0.5,
                    support_id="page-1/formula",
                ),),
            ),
        ),
    )


def test_manifest_records_explicit_unknowns_and_page_identity():
    manifest = _manifest()

    payload = manifest.model_dump(mode="json")

    assert payload["protocol"] == "research-document-parsing-eval/v1"
    assert payload["requested_pages"][0]["exposure"] == "exposed_dev"
    assert payload["fingerprints"]["parser_weights_digest"]["value"] is None
    assert payload["fingerprints"]["parser_weights_digest"]["unknown_reason"]


def test_manifest_json_codec_round_trips_through_path(tmp_path):
    expected = _manifest()
    path = tmp_path / "manifest.json"

    write_parsing_evaluation_manifest(expected, path)

    assert load_parsing_evaluation_manifest(path) == expected
    assert path.read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize("contents", ["{", '{"run_id":"one","run_id":"two"}', '{"value":NaN}'])
def test_manifest_json_loader_rejects_damaged_or_nonstandard_json(tmp_path, contents):
    path = tmp_path / "broken-manifest.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError):
        load_parsing_evaluation_manifest(path)


def test_outcomes_must_cover_every_requested_page_exactly():
    with pytest.raises(ValidationError, match="cover requested pages exactly"):
        ParsingEvaluationManifest.model_validate(_manifest().model_dump(mode="python") | {"outcomes": ()})


def test_floating_parser_identity_is_rejected():
    with pytest.raises(ValidationError, match="immutable"):
        IdentityValue.model_validate({"value": "newsroom-marker:latest"})
    with pytest.raises(ValidationError, match="sha256"):
        ParsingFingerprints.model_validate(_fingerprints().model_dump(mode="python") | {
            "parser_image_digest": {"value": "missing-digest"},
        })


def test_terminal_run_cannot_omit_terminal_page_outcome():
    payload = _manifest().model_dump(mode="python")
    payload["outcomes"] = (PageOutcome(page_id="page-1", status=PageStatus.RUNNING),)

    with pytest.raises(ValidationError, match="terminal run"):
        ParsingEvaluationManifest.model_validate(payload)


def test_production_run_requires_known_parser_and_schema_fingerprints():
    payload = _manifest().model_dump(mode="python")
    payload["execution"] = ParsingExecutionIdentity(
        kind=ExecutionKind.PRODUCTION,
        composition=_identity("ResearchComposition@v1"),
        execution_receipt_refs=("artifact://receipt/run-001",),
        parser_invocation=_identity("MarkerPdfDocumentParser"),
    )

    with pytest.raises(ValidationError, match="requires known identities"):
        ParsingEvaluationManifest.model_validate(payload)


def test_heldout_page_requires_verified_document_and_family_mapping():
    with pytest.raises(ValidationError, match="heldout pages require known document"):
        RequestedPage(
            page_id="heldout-page",
            input_sha256="d" * 64,
            input_mode=InputMode.NATIVE_PDF,
            source_document_id=None,
            source_group_id=None,
            source_mapping_reason="not available",
            exposure=Exposure.FROZEN_HELDOUT,
        )


def test_completed_page_must_account_for_every_planned_metric():
    payload = _manifest().model_dump(mode="python")
    payload["outcomes"] = (
        PageOutcome(page_id="page-1", status=PageStatus.SUCCEEDED),
    )

    with pytest.raises(ValidationError, match="every planned metric"):
        ParsingEvaluationManifest.model_validate(payload)


def test_batch_page_mapping_must_be_unique():
    manifest = _manifest()
    duplicate_page = _page().model_copy(update={"page_id": "page-2", "input_sha256": "d" * 64})
    payload = manifest.model_dump(mode="python")
    payload["requested_pages"] = (manifest.requested_pages[0], duplicate_page)
    payload["budget"] = ParsingBudget(
        max_pages=25, timeout_seconds_per_page=900, max_total_seconds=21_600,
        max_concurrency=1, max_retries=0, max_roi_attempts=0,
    )
    payload["outcomes"] = (
        manifest.outcomes[0],
        PageOutcome(page_id="page-2", status=PageStatus.SUCCEEDED, metrics=manifest.outcomes[0].metrics),
    )

    with pytest.raises(ValidationError, match="batch page mappings must be unique"):
        ParsingEvaluationManifest.model_validate(payload)


def test_production_track_requires_real_composition_and_receipt():
    with pytest.raises(ValidationError, match="diagnostic execution"):
        _manifest(track=ParsingTrack.PRODUCTION_DOCUMENT)


def test_failed_page_metric_missing_output_remains_in_metric_denominator():
    manifest = ParsingEvaluationManifest.model_validate(_manifest().model_dump(mode="python") | {
        "outcomes": (
            PageOutcome(
                page_id="page-1",
                status=PageStatus.FAILED,
                reason="parser timed out",
                metrics=(MetricOutcome(
                    metric="CDM",
                    status=MetricStatus.MISSING_OUTPUT,
                    aggregation_unit=AggregationUnit.PAGE,
                    reason="prediction export was not produced",
                    support_id="page-1/table",
                ),),
            ),
        ),
    })

    denominator = metric_denominators(manifest)["CDM:page"]

    assert denominator["requested_outcomes"] == 1
    assert denominator["scored_denominator"] == 0
    assert denominator["status_counts"] == {"missing_output": 1}


def test_object_metric_requires_traceable_support_identity():
    with pytest.raises(ValidationError, match="require a support_id"):
        MetricOutcome(
            metric="TEDS",
            status=MetricStatus.NOT_APPLICABLE,
            aggregation_unit=AggregationUnit.OBJECT,
            reason="no table annotation on this page",
        )


def test_successful_metric_must_have_value_and_non_success_reason():
    with pytest.raises(ValidationError, match="requires a value"):
        MetricOutcome(
            metric="TEDS",
            status=MetricStatus.SUCCEEDED,
            aggregation_unit=AggregationUnit.PAGE,
        )
    with pytest.raises(ValidationError, match="finite"):
        MetricOutcome(
            metric="TEDS",
            status=MetricStatus.SUCCEEDED,
            aggregation_unit=AggregationUnit.PAGE,
            value=float("nan"),
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        MetricOutcome(
            metric="TEDS",
            status=MetricStatus.TIMEOUT,
            aggregation_unit=AggregationUnit.PAGE,
        )
