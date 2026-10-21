"""Diagnostics exercise real frozen mappings and fully validated report artifacts."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_common import (
    ExecutionProfile,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_execution import (
    ArtifactReference,
    CaseScopeProjection,
    PlannedEvaluationUnit,
)
from backend.research.rag.evaluation.qasper_diagnostics import (
    QasperDiagnostics,
    QasperExecutionTelemetry,
    QasperLatencyObservation,
    QasperUnitTelemetry,
    attribute_qasper_retrieval_failures,
    derive_qasper_question_type,
    diagnose_qasper_report,
)
from backend.research.rag.evaluation.qasper_baseline import _build_documents
from backend.research.rag.evaluation.qasper_mapping import (
    FrozenQasperDataset,
    freeze_qasper_dataset,
)
from backend.research.rag.evaluation.qasper_protocol import (
    QasperCaseResult,
    QasperChannelProvenance,
    QasperDenominators,
    QasperFailureRecord,
    QasperMetric,
    QasperReportArtifact,
    QasperReportFiles,
    RankedResult,
)
from backend.research.rag.evaluation.qasper_scoring import score_qasper_metrics
from tests.backend.research.rag.test_qasper_mapping import _chunk, _inputs
from tests.backend.research.rag.test_qasper_protocol import _qasper_manifest

_EVIDENCE = "Evidence supports the result."
_SOURCE = _EVIDENCE + " Additional evidence. Noise material."


def _dataset(
    *, answers=None, extra_questions=(), omit_extra=False
) -> FrozenQasperDataset:
    chunks = (
        _chunk("left", "Evidence supports"),
        _chunk("right", "supports the result."),
        _chunk("noise", "Noise material."),
    )
    if not omit_extra:
        chunks += (_chunk("extra", "Additional evidence."),)
    raw, documents, chunks = _inputs(_SOURCE, _EVIDENCE, chunks, answers=answers)
    raw["paper-1"]["qas"].extend(extra_questions)
    return freeze_qasper_dataset(raw, documents, chunks)


def _question(identifier, answer, *, kind="comparison"):
    return {
        "question_id": identifier,
        "question": f"Question {identifier}?",
        "question_type": kind,
        "answers": [{"answer": answer}],
    }


def _recreate(model, digest_field, **changes):
    content = {
        name: getattr(model, name)
        for name in type(model).model_fields
        if name != digest_field
    }
    return type(model).create(**{**content, **changes})


def _report(dataset, rows_by_case=None, *, overrides=None, final_stages=None):
    """Bind the shared protocol fixture's run configuration to actual frozen data."""
    rows_by_case, overrides = rows_by_case or {}, overrides or {}
    final_stages = final_stages or {}
    base, _, _ = _qasper_manifest(
        dataset_digest=dataset.dataset_digest, corpus_digest=dataset.corpus_sha256
    )
    binding = dataset.binding
    variant = base.experiment_manifest.variants[0]
    variant = _recreate(
        variant,
        "variant_fingerprint",
        retrieval=variant.retrieval.model_copy(
            update={
                "parser_config_hash": binding.parser_config_sha256,
                "chunker_config_hash": binding.chunker_config_sha256,
                "index_config_hash": binding.index_config_sha256,
            }
        ),
    )
    scopes = []
    for case in dataset.cases:
        chunk = case.mapping_result.mapping.indexed_chunks[0]
        scopes.append(
            CaseScopeProjection.create(
                case_id=case.case_id,
                scope=QuestionScope(
                    sources=(
                        ScopedPaper(
                            paper_id=case.paper_id,
                            document_version=chunk.document_version,
                            source_sha256=chunk.source_sha256,
                        ),
                    )
                ),
            )
        )
    scopes = tuple(scopes)
    comparison = _recreate(
        base.experiment_manifest.comparison_identities[0],
        "comparison_fingerprint",
        variant_fingerprint=variant.variant_fingerprint,
        case_scope_fingerprint=content_digest(scopes),
    )
    units = tuple(
        PlannedEvaluationUnit(
            dataset_version=dataset.dataset_version,
            case_id=case.case_id,
            track=variant.track,
            variant_fingerprint=variant.variant_fingerprint,
            repetition=1,
        )
        for case in dataset.cases
    )
    shared = _recreate(
        base.experiment_manifest,
        "manifest_digest",
        dataset_version=dataset.dataset_version,
        case_scopes=scopes,
        variants=(variant,),
        planned_units=units,
        comparison_identities=(comparison,),
    )
    data = base.data_identity.model_copy(
        update={
            "dataset_version": dataset.dataset_version,
            "dataset_case_count": len(dataset.cases),
            "evaluable_case_count": sum(
                case.mapping_result.disposition == "valid" for case in dataset.cases
            ),
            "corpus_snapshot_id": binding.corpus_snapshot_id,
            "chunk_snapshot_id": binding.index_snapshot_id,
            "chunk_sha256": dataset.index_sha256,
            "chunking_version": binding.chunker_version,
            "chunk_count": len(dataset.cases[0].mapping_result.mapping.indexed_chunks),
        }
    )
    manifest = _recreate(
        base,
        "digest",
        experiment_manifest=shared,
        index_binding=binding,
        data_identity=data,
    )
    results = []
    for case, unit in zip(dataset.cases, units, strict=True):
        status = (
            "ok"
            if case.mapping_result.disposition == "valid"
            else case.mapping_result.disposition
        )
        default_category = (
            "not_applicable" if status == "not_applicable" else "mapping_failure"
        )
        status, category, stage = overrides.get(
            case.case_id, (status, default_category, "mapping")
        )
        rows = rows_by_case.get(case.case_id, ())
        observed = {row.stage for row in rows}
        final_stage = final_stages.get(
            case.case_id,
            "reranked" if "reranked" in observed else "fused" if "fused" in observed else "candidate",
        )
        metrics = score_qasper_metrics(
            case, rows, manifest.metric_definitions, final_stage=final_stage
        )
        reason = None
        failures = ()
        if status != "ok":
            reason = case.mapping_result.reason or "Recorded execution failure."
            metrics = tuple(
                QasperMetric(
                    metric_id=d.metric_id,
                    version=d.version,
                    k=d.k,
                    value=None,
                    status=status,
                    numerator=None,
                    denominator=None,
                    reason=reason,
                )
                for d in manifest.metric_definitions
            )
            failures = (
                QasperFailureRecord(
                    unit=unit,
                    category=category,
                    stage=stage,
                    status=status,
                    reason=reason,
                    affected_metrics=manifest.metric_definitions,
                ),
            )
        results.append(
            QasperCaseResult(
                manifest_digest=manifest.digest,
                run_id="diagnostics-run",
                attempt_id=f"attempt-{case.case_id}",
                unit=unit,
                query=case.query,
                paper_ids=(case.paper_id,),
                status=status,
                mapping_result=case.mapping_result,
                final_stage=final_stage,
                raw_ranked_results=rows,
                metrics=metrics,
                failures=failures,
                reason=reason,
            )
        )
    counts = {
        status: sum(case.status == status for case in results)
        for status in ("ok", "needs_review", "system_failure", "not_applicable")
    }
    aggregate = []
    for definition in manifest.metric_definitions:
        values = [
            m.value
            for result in results
            for m in result.metrics
            if m.key == definition.key and m.status == "ok"
        ]
        aggregate.append(
            QasperMetric(
                metric_id=definition.metric_id,
                version=definition.version,
                k=definition.k,
                value=math.fsum(values) / len(values) if values else None,
                status="ok" if values else "not_applicable",
                numerator=math.fsum(values) if values else None,
                denominator=len(values) if values else None,
                reason=None if values else "No valid observations.",
                aggregation="macro_average",
            )
        )
    references = {
        name: ArtifactReference(
            reference_id=f"diagnostics-{name}",
            kind=name,
            uri=f"fixture://diagnostics/{name}",
            content_sha256="a" * 64,
        )
        for name in ("manifest", "cases", "metrics", "failures", "report")
    }
    return QasperReportArtifact(
        manifest=manifest,
        run_id="diagnostics-run",
        selected_variant_fingerprint=variant.variant_fingerprint,
        case_results=tuple(results),
        denominators=QasperDenominators(
            planned=len(results), valid=counts.pop("ok"), **counts
        ),
        metrics=tuple(aggregate),
        artifacts=QasperReportFiles(**references),
    )


def _row(
    chunk,
    rank,
    stage="candidate",
    channel="dense",
    channel_rank=None,
    query_id="query-1",
):
    return RankedResult(
        chunk_id=chunk,
        rank=rank,
        stage=stage,
        raw_score=1.0,
        provenance=(
            QasperChannelProvenance(
                channel=channel,
                query_id=query_id,
                query_sha256=content_digest(query_id),
                channel_rank=rank if channel_rank is None else channel_rank,
                raw_score=1.0,
            ),
        ),
    )


def _rows(**stages):
    return tuple(
        _row(chunk, rank, stage)
        for stage, chunks in stages.items()
        for rank, chunk in enumerate(chunks, start=1)
    )


def _telemetry(
    report,
    *,
    stages=None,
    channels=("dense",),
    latencies=(),
    pool=None,
):
    return QasperExecutionTelemetry(
        run_id=report.run_id,
        execution_profile=ExecutionProfile.FIXTURE,
        channel_order=channels,
        units=tuple(
            QasperUnitTelemetry(
                case_id=result.unit.case_id,
                repetition=result.unit.repetition,
                attempt_id=result.attempt_id,
                executed_stages=(
                    stages
                    if stages is not None
                    else ("candidate", "fused", "reranked")
                    if result.final_stage == "reranked"
                    else ("candidate", "fused")
                    if result.final_stage == "fused"
                    else ()
                ),
                latencies=latencies,
                rerank_input_chunk_ids=pool,
            )
            for result in report.case_results
        ),
    )


def _run(
    rows,
    *,
    k=5,
    stages=("candidate", "fused", "reranked"),
    channels=("dense",),
    policy="equivalent",
    pool=None,
):
    dataset = _dataset()
    report = _report(
        dataset,
        {dataset.cases[0].case_id: rows},
        final_stages={dataset.cases[0].case_id: stages[-1] if stages else "candidate"},
    )
    return diagnose_qasper_report(
        dataset,
        report,
        _telemetry(report, stages=stages, channels=channels, pool=pool),
        k=k,
        policy=policy,
    )


def test_complete_multichunk_path_and_original_duplicate_ranks():
    partial = _run(
        _rows(candidate=("left",), fused=("left",)), stages=("candidate", "fused")
    )
    case = partial.cases[0]
    assert case.first_hit_rank is None
    assert case.attribution == "candidate_recall_miss"
    assert case.channels[0].first_gold_rank is None
    assert case.channels[0].new_anchor_ids == ()
    rows = _rows(candidate=("left", "left", "right"), fused=("left", "left", "right"))
    complete = _run(rows, stages=("candidate", "fused"))
    case = complete.cases[0]
    assert case.first_hit_rank == 3  # no deduplication/rank compaction
    assert case.hit_at_k is True
    assert case.attribution == "hit"
    assert case.channels[0].first_gold_rank == 3
    assert len(case.channels[0].new_anchor_ids) == 1
    assert case.stages[0].candidate_count == 2
    assert case.covered_groups == case.required_groups == 1
    strict = _run(rows, stages=("candidate", "fused"), policy="strict")
    # Strict paths are frozen explicitly too; an exact multi-chunk span is not
    # automatically Equivalent-only merely because it spans adjacent chunks.
    assert _dataset().cases[0].strict_anchors[0].paths == (("left", "right"),)
    assert strict.cases[0].first_hit_rank == 3


def test_cross_channel_paths_receive_only_marginal_anchor_credit():
    candidates = (
        _row("left", 1, channel="dense", channel_rank=1),
        _row("right", 2, channel="bm25", channel_rank=1),
        _row("left", 3, channel="field", channel_rank=2),
        _row("right", 4, channel="field", channel_rank=4),
    )
    result = _run(
        candidates + _rows(fused=("left", "right")),
        stages=("candidate", "fused"),
        channels=("dense", "bm25", "field"),
    )
    dense, bm25, field = result.cases[0].channels
    assert (dense.first_gold_rank, bm25.first_gold_rank, field.first_gold_rank) == (
        None,
        None,
        4,
    )
    assert dense.new_anchor_ids == ()
    assert len(bm25.new_anchor_ids) == 1
    assert field.new_anchor_ids == ()  # same anchor, not two fresh gold chunks
    assert field.final_first_hit_rank == 2
    assert result.cases[0].first_hit_rank == 2


def test_multiquery_first_channel_hit_is_full_path_completion_not_earliest_fragment():
    rows = (
        _row("left", 1, channel_rank=1, query_id="original"),
        _row("right", 2, channel_rank=4, query_id="expansion"),
        _row("right", 3, channel_rank=2, query_id="original"),
    )
    result = _run(rows, stages=("candidate",))
    channel = result.cases[0].channels[0]
    assert channel.first_gold_rank == 2
    assert channel.candidate_count == 2
    assert len(channel.new_anchor_ids) == 1


@pytest.mark.parametrize(
    ("stages", "candidate", "fused", "reranked", "expected"),
    [
        (
            ("candidate", "fused", "reranked"),
            ("left",),
            ("left",),
            (),
            "candidate_recall_miss",
        ),
        (
            ("candidate", "fused", "reranked"),
            ("left", "right"),
            ("left",),
            (),
            "fusion_miss",
        ),
        (
            ("candidate", "fused", "reranked"),
            ("left", "right"),
            ("left", "right"),
            ("left",),
            "rerank_miss",
        ),
        (
            ("candidate", "fused", "reranked"),
            ("left", "right"),
            ("left", "right"),
            (),
            "rerank_miss",
        ),
        (("candidate", "fused"), ("left", "right"), (), (), "fusion_miss"),
        (("fused",), (), (), (), "telemetry_unavailable"),
        ((), (), (), (), "telemetry_unavailable"),
    ],
)
def test_miss_attribution_and_empty_vs_absent_stages(
    stages, candidate, fused, reranked, expected
):
    rows = _rows(candidate=candidate, fused=fused, reranked=reranked)
    result = _run(rows, stages=stages)
    case = result.cases[0]
    assert case.attribution == expected
    assert case.first_hit_rank is None
    assert {item.attribution for item in case.anchor_misses} <= {expected}
    if not stages:
        assert case.measured is False
        assert case.hit_at_k is None


def test_top_k_uses_path_completion_but_first_hit_slice_retains_untruncated_rank():
    result = _run(
        _rows(
            candidate=("left", "noise", "right"),
            fused=("noise", "left", "right"),
            reranked=("left", "noise", "right"),
        ),
        k=2,
    )
    case = result.cases[0]
    assert case.first_hit_rank == 3
    assert case.hit_at_k is False
    assert (
        case.attribution == "top_k_miss"
    )  # complete path exists only below the final Top-K cutoff
    sliced = next(item for item in result.slices if item.dimension == "first_hit_rank")
    assert sliced.value == "3"
    assert sliced.hit_count == 0
    assert sliced.denominators.valid == 1


def test_actual_reranker_pool_distinguishes_fusion_truncation_from_reranker_loss():
    rows = _rows(
        candidate=("left", "right"), fused=("left", "right"), reranked=("left",)
    )
    diagnostic = _run(rows, pool=("left",))
    assert diagnostic.cases[0].attribution == "fusion_miss"
    assert diagnostic.cases[0].anchor_misses[0].attribution == "fusion_miss"


def test_all_slices_retain_nonok_denominators_without_mislabeling_mapping_failures():
    questions = (
        _question(
            "review",
            {"free_form_answer": "unknown", "evidence": ["Absent from source."]},
        ),
        _question(
            "missing",
            {"free_form_answer": "extra", "evidence": ["Additional evidence."]},
        ),
        _question("unanswerable", {"unanswerable": True, "evidence": []}),
    )
    dataset = _dataset(extra_questions=questions, omit_extra=True)
    valid = dataset.cases[0]
    report = _report(
        dataset,
        {valid.case_id: _rows(candidate=("left", "right"))},
        overrides={
            "paper-1:missing": ("system_failure", "source_index_failure", "index"),
        },
    )
    result = diagnose_qasper_report(
        dataset, report, _telemetry(report, stages=("candidate",)), k=5
    )
    assert result.denominators == QasperDenominators(
        planned=4, valid=1, needs_review=1, system_failure=1, not_applicable=1
    )
    assert [item.attribution for item in result.cases] == [
        "hit",
        "mapping_failure",
        "source_index_failure",
        "not_applicable",
    ]
    assert all(item.hit_at_k is None for item in result.cases[1:])
    assert {item.dimension for item in result.slices} == {
        "question_type",
        "paper",
        "section",
        "field",
        "evidence_count",
        "mapping_status",
        "first_hit_rank",
    }
    paper = next(item for item in result.slices if item.dimension == "paper")
    assert paper.denominators == result.denominators
    assert paper.hit_count == paper.diagnosed_count == 1
    assert len(paper.unit_keys) == 4
    assert all(metric.denominator == 1 for metric in paper.metrics)
    nonmeasured = next(
        item
        for item in result.slices
        if item.dimension == "first_hit_rank" and item.value == "not_measured"
    )
    assert nonmeasured.denominators.planned == 3
    assert all(
        metric.value is None and metric.denominator == 0
        for metric in nonmeasured.metrics
    )
    assert nonmeasured.hit_count == 0
    assert QasperDiagnostics.model_validate_json(result.model_dump_json()) == result


def test_group_alternatives_are_not_required_to_all_hit():
    dataset = _dataset(
        answers=[
            {"answer": {"free_form_answer": "first", "evidence": [_EVIDENCE]}},
            {
                "answer": {
                    "free_form_answer": "alternative",
                    "evidence": ["Additional evidence."],
                }
            },
        ]
    )
    report = _report(
        dataset, {dataset.cases[0].case_id: _rows(candidate=("left", "right"))}
    )
    result = diagnose_qasper_report(
        dataset, report, _telemetry(report, stages=("candidate",)), k=5
    )
    case = result.cases[0]
    assert case.gold_anchor_count == 2
    assert case.covered_groups == case.required_groups == 1
    assert case.attribution == "hit"
    assert len(case.anchor_misses) == 1  # diagnostic only: other annotation is optional


def test_fixture_latency_percentiles_missing_samples_and_zero_external_costs():
    dataset = _dataset(
        extra_questions=(
            _question("second", {"free_form_answer": "x", "evidence": [_EVIDENCE]}),
        )
    )
    report = _report(dataset)
    telemetry = _telemetry(report, stages=("candidate",))
    units = tuple(
        unit.model_copy(
            update={
                "latencies": (
                    QasperLatencyObservation(phase="request", milliseconds=ms),
                )
            }
        )
        for unit, ms in zip(telemetry.units, (10.0, 30.0), strict=True)
    )
    telemetry = telemetry.model_copy(
        update={"units": units, "index_cost_usd": 0.0, "request_cost_usd": 0.0}
    )
    result = diagnose_qasper_report(dataset, report, telemetry, k=5)
    request = next(item for item in result.latency if item.phase == "request")
    assert (
        request.sample_count,
        request.missing_unit_count,
        request.p50_ms,
        request.p95_ms,
    ) == (2, 0, 20.0, 29.0)
    assert all(
        item.p50_ms is None and item.p95_ms is None and item.missing_unit_count == 2
        for item in result.latency
        if item.phase != "request"
    )
    assert result.execution_profile is ExecutionProfile.FIXTURE
    assert result.represents_production_latency is False
    assert "not Qdrant production" in result.latency_note
    assert [(cost.scope, cost.status, cost.usd) for cost in result.external_costs] == [
        ("index", "reported", 0.0),
        ("request", "reported", 0.0),
        ("judge", "not_applicable", None),
    ]
    # Telemetry order is not an execution ordering signal.
    reversed_telemetry = telemetry.model_copy(update={"units": tuple(reversed(units))})
    assert diagnose_qasper_report(dataset, report, reversed_telemetry, k=5) == result


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True])
def test_bad_latency_is_rejected(value):
    with pytest.raises(ValidationError):
        QasperLatencyObservation(phase="request", milliseconds=value)


@pytest.mark.parametrize(
    "change",
    [
        {"units": ()},
        {"run_id": "another-run"},
        {"execution_profile": ExecutionProfile.PRODUCTION},
        {"request_cost_usd": 0.01},
        {"channel_order": ()},
    ],
)
def test_misaligned_telemetry_or_fixture_costs_are_rejected(change):
    dataset = _dataset()
    report = _report(dataset, {dataset.cases[0].case_id: _rows(candidate=("left",))})
    telemetry = _telemetry(report, stages=("candidate",)).model_copy(update=change)
    with pytest.raises(ValueError):
        diagnose_qasper_report(dataset, report, telemetry, k=5)


@pytest.mark.parametrize("k", [0, -1, True, 1.5])
def test_bad_cutoff_is_rejected(k):
    dataset = _dataset()
    report = _report(dataset)
    with pytest.raises(ValueError, match="positive integer"):
        diagnose_qasper_report(dataset, report, _telemetry(report), k=k)


def test_frozen_identity_and_stage_invention_are_rejected():
    dataset = _dataset()
    report = _report(dataset)
    tampered = dataset.model_copy(update={"dataset_digest": "b" * 64})
    with pytest.raises(ValueError, match="digest mismatch"):
        diagnose_qasper_report(tampered, report, _telemetry(report), k=5)
    report = _report(
        dataset,
        {dataset.cases[0].case_id: _rows(candidate=("left",), fused=("left", "right"))},
    )
    with pytest.raises(ValueError, match="invent candidate"):
        diagnose_qasper_report(
            dataset, report, _telemetry(report, stages=("candidate", "fused")), k=5
        )
    with pytest.raises(ValueError, match="absent from execution telemetry"):
        diagnose_qasper_report(
            dataset, report, _telemetry(report, stages=("candidate",)), k=5
        )


def test_duplicate_telemetry_boundaries_are_rejected():
    dataset = _dataset()
    report = _report(dataset)
    telemetry = _telemetry(report)
    with pytest.raises(ValidationError, match="unique unit keys"):
        QasperExecutionTelemetry.model_validate_json(
            telemetry.model_copy(
                update={"units": telemetry.units * 2}
            ).model_dump_json()
        )
    unit = telemetry.units[0]
    observation = QasperLatencyObservation(phase="request", milliseconds=1.0)
    with pytest.raises(ValidationError, match="one latency"):
        QasperUnitTelemetry.model_validate_json(
            unit.model_copy(
                update={"latencies": (observation, observation)}
            ).model_dump_json()
        )


def test_partial_coverage_keeps_missing_anchor_stage_attribution():
    dataset = _dataset(
        answers=[
            {
                "answer": {
                    "free_form_answer": "both",
                    "evidence": [_EVIDENCE, "Additional evidence."],
                }
            }
        ]
    )
    rows = _rows(candidate=("left", "right", "extra"), fused=("left", "right"))
    report = _report(dataset, {dataset.cases[0].case_id: rows})
    result = diagnose_qasper_report(
        dataset, report, _telemetry(report, stages=("candidate", "fused")), k=5
    )
    case = result.cases[0]
    assert case.hit_at_k is True
    assert case.covered_groups == 0
    assert case.required_groups == 1
    assert case.attribution == "partial_coverage"
    assert len(case.anchor_misses) == 1
    assert case.anchor_misses[0].attribution == "fusion_miss"


def test_single_sample_latency_is_not_lost_or_padded_with_zero():
    dataset = _dataset(
        extra_questions=(
            _question("second", {"free_form_answer": "x", "evidence": [_EVIDENCE]}),
        )
    )
    report = _report(dataset)
    telemetry = _telemetry(report, stages=("candidate",))
    measured = telemetry.units[0].model_copy(
        update={
            "latencies": (QasperLatencyObservation(phase="request", milliseconds=7.5),)
        }
    )
    telemetry = telemetry.model_copy(update={"units": (measured, telemetry.units[1])})
    result = diagnose_qasper_report(dataset, report, telemetry, k=5)
    latency = next(item for item in result.latency if item.phase == "request")
    assert latency.sample_count == latency.missing_unit_count == 1
    assert latency.p50_ms == latency.p95_ms == 7.5


def test_same_dataset_identity_with_different_frozen_index_is_rejected():
    dataset = _dataset()
    report = _report(dataset)
    changed_dataset = _dataset(omit_extra=True)
    assert changed_dataset.dataset_digest == dataset.dataset_digest
    assert changed_dataset.binding != dataset.binding
    with pytest.raises(ValueError, match="dataset/index identities differ"):
        diagnose_qasper_report(changed_dataset, report, _telemetry(report), k=5)


def test_gold_evidence_count_slice_deduplicates_annotations_and_excludes_floats():
    dataset = _dataset(
        answers=[
            {
                "answer": {
                    "free_form_answer": "first",
                    "evidence": [_EVIDENCE, "FLOAT SELECTED"],
                }
            },
            {"answer": {"free_form_answer": "same", "evidence": [_EVIDENCE.upper()]}},
        ]
    )
    report = _report(dataset)
    result = diagnose_qasper_report(dataset, report, _telemetry(report), k=5)
    case = result.cases[0]
    assert case.raw_evidence_count == 3
    assert case.evidence_count == case.gold_anchor_count == 1
    evidence_slice = next(
        item for item in result.slices if item.dimension == "evidence_count"
    )
    assert evidence_slice.value == "1"





@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("Which method does the paper propose?", "method"),
        ("What dataset is used for evaluation?", "dataset"),
        ("What results does the model achieve?", "result"),
        ("How does the proposed model compare with the baseline?", "comparison"),
        ("How many samples are in the corpus?", "numeric"),
        ("Why does the approach improve accuracy?", "why_how"),
        ("What is discussed in the paper?", "unknown"),
    ),
)
def test_unknown_dataset_question_types_use_versioned_derived_not_gold_rules(
    query, expected
):
    case = _dataset().cases[0].model_copy(
        update={"query": query, "question_type": "unknown"}
    )

    label, source, derivation_id = derive_qasper_question_type(case)

    assert label == expected
    assert source == ("unknown" if expected == "unknown" else "derived_not_gold")
    assert derivation_id == "qasper-question-type-rules/v1"


def test_explicit_dataset_question_type_has_priority_over_text_derivation() -> None:
    case = _dataset().cases[0].model_copy(
        update={
            "query": "How many samples are in the dataset?",
            "question_type": "gold_custom_type",
        }
    )

    assert derive_qasper_question_type(case) == (
        "gold_custom_type", "dataset", None
    )


def test_failure_records_distinguish_top_k_from_partial_multichunk_overlap() -> None:
    dataset = _dataset()
    case = dataset.cases[0]
    rows = _rows(candidate=("left", "noise", "right"), fused=("left", "noise", "right"))
    report = _report(
        dataset,
        {case.case_id: rows},
        final_stages={case.case_id: "fused"},
    )
    result = report.case_results[0]

    failures = attribute_qasper_retrieval_failures(
        case, result.unit, rows, "fused", result.metrics
    )

    assert any(failure.category == "top_k_miss" for failure in failures) is False
    top_two_metrics = score_qasper_metrics(
        case, rows, report.manifest.metric_definitions, final_stage="fused"
    )
    # Manifest K is 5, so build a true Top-2 metric cohort for the regression.
    definitions = tuple(
        metric.model_copy(update={"k": 2})
        if metric.k is not None
        else metric
        for metric in report.manifest.metric_definitions
    )
    top_two_metrics = score_qasper_metrics(
        case, rows, definitions, final_stage="fused"
    )
    failures = attribute_qasper_retrieval_failures(
        case, result.unit, rows, "fused", top_two_metrics
    )
    assert any(failure.category == "top_k_miss" for failure in failures)

    partial_rows = _rows(candidate=("left",), fused=("left",))
    partial_metrics = score_qasper_metrics(
        case, partial_rows, definitions, final_stage="fused"
    )
    partial_failures = attribute_qasper_retrieval_failures(
        case, result.unit, partial_rows, "fused", partial_metrics
    )
    assert any(
        failure.category == "candidate_recall_miss"
        for failure in partial_failures
    )


def test_real_raw_questions_without_labels_cover_all_deterministic_fallback_categories() -> None:
    queries = (
        ("method", "Which method is proposed?"),
        ("dataset", "What dataset is used?"),
        ("result", "What results are reported?"),
        ("comparison", "How does it compare with the baseline?"),
        ("numeric", "How many samples are evaluated?"),
        ("why_how", "Why does accuracy improve?"),
    )
    raw = {
        "paper-1": {
            "title": "Paper",
            "abstract": "",
            "full_text": [
                {
                    "section_name": "Results",
                    "paragraphs": ["Evidence supports the result."],
                }
            ],
            "qas": [
                {
                    "question_id": label,
                    "question": query,
                    "answers": [
                        {
                            "answer": {
                                "free_form_answer": "yes",
                                "evidence": ["Evidence supports the result."],
                            }
                        }
                    ],
                }
                for label, query in queries
            ],
        }
    }
    documents, chunks = _build_documents(raw)
    dataset = freeze_qasper_dataset(raw, tuple(documents), tuple(chunks))

    observed = {
        derive_qasper_question_type(case)[0]
        for case in dataset.cases
    }

    assert all(case.question_type == "unknown" for case in dataset.cases)
    assert observed == {label for label, _ in queries}
