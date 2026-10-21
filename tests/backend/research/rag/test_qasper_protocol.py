from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.qasper_protocol import (
    EQUIVALENT_METRIC_VERSION,
    QASPER_MAPPING_VERSION,
    STRICT_METRIC_VERSION,
    HistoricalCodeClaim,
    LegacyMetricSummary,
    QasperBaselineSnapshot,
    QasperEmbeddingCacheIdentity,
    QasperCaseResult,
    QasperChannelProvenance,
    QasperDataIdentity,
    QasperDenominators,
    QasperExecutionIdentity,
    QasperExperimentManifest,
    QasperFailureRecord,
    QasperMappingPolicy,
    QasperMappingResult,
    QasperMetricDefinition,
    QasperMetricVersions,
    QasperReportArtifact,
    QasperReportFiles,
    QasperMetric,
    RankedResult,
    qualify_qasper_comparison,
)
from backend.research.rag.evaluation.protocol_common import PROTOCOL_VERSION, SCHEMA_VERSION, content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    AlternativeSufficientSet, EvaluationCase, EvidenceAnchor, EvidenceLocation,
    QuestionScope, RequiredEvidenceGroup, ScopedPaper,
)
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    CheckedSourceText, ChunkSourceSpan, ChunkSpanDescriptor, IndexBinding,
    build_index_evidence_mapping, calculate_index_content_sha256, text_sha256,
)
from backend.research.rag.evaluation.protocol_execution import (
    ArtifactReference, BudgetLimits, CachePolicy, CaseScopeProjection, ComparisonIdentity,
    ExecutionEnvironment, ExperimentManifest, ExperimentPurpose, InitialState, InitialStatePolicy,
    IsolatedNamespaces, RetrievalFactors, VariantConfiguration,
)
from backend.research.rag.evaluation.protocol_execution import (
    CodeIdentity,
    ExecutionProfile,
    EvaluationTrack,
    PlannedEvaluationUnit,
)

D = "a" * 64


def baseline() -> QasperBaselineSnapshot:
    code = CodeIdentity.create(
        git_revision="3d205845", workspace_content_fingerprint=D,
        mutable_patch_fingerprints=(), dirty=False,
    )
    return QasperBaselineSnapshot.create(
        baseline_id="qasper-dev-legacy-v1", dataset_id="qasper", dataset_version="dev-legacy-v1",
        dataset_sha256=D, corpus_id="qasper-paper-corpus", corpus_version="legacy-v1",
        corpus_sha256="b" * 64, chunking_version="legacy-chunker-v1", chunk_count=17048,
        paper_count=281, dataset_case_count=1005, evaluable_case_count=888,
        embedding_cache_id="legacy-cache", embedding_cache_sha256="c" * 64,
        embedding_model_id="legacy-model", embedding_model_version="legacy-v1", embedding_dimension=768,
        # Multiple repeated-text chunks may share one embedding key; cache rows are
        # unique cached keys, not a lower bound on the number of chunks.
        embedding_cache_identity=QasperEmbeddingCacheIdentity(
            rows=42, dimension=768, dtype="float32", keys_sha256="e" * 64,
        ),
        execution_profile=ExecutionProfile.FIXTURE,
        raw_result_sha256="d" * 64,
        historical_code_claim=HistoricalCodeClaim(
            git_revision="f9b2530db83e2297cee7d2218ec9d75886d9c22", source="codex-handoff",
            claim="历史代码身份声明，无逐题结果或运行时证明。",
        ),
        observed_reconstruction_code_identity=code,
        legacy_summary=(LegacyMetricSummary(metric_id="legacy_strict_hit_at_5", value=0.600225),),
    )


def retrieval_unit() -> PlannedEvaluationUnit:
    return PlannedEvaluationUnit(
        dataset_version="dataset-v1", case_id="case-1", track=EvaluationTrack.RETRIEVAL,
        variant_fingerprint=D, repetition=1,
    )


def metric_definitions() -> tuple[QasperMetricDefinition, QasperMetricDefinition]:
    return (
        QasperMetricDefinition(metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5),
        QasperMetricDefinition(metric_id="equivalent_hit", version=EQUIVALENT_METRIC_VERSION, k=5),
    )


def non_ok_metrics() -> tuple[QasperMetric, QasperMetric]:
    return tuple(
        QasperMetric(
            metric_id=item.metric_id, version=item.version, k=item.k,
            value=None, status="needs_review", numerator=None, denominator=None,
            reason="mapping threshold awaits human review",
        )
        for item in metric_definitions()
    )


def ranked_result(rank: int = 1) -> RankedResult:
    return RankedResult(
        chunk_id="chunk-1", rank=rank, stage="candidate", raw_score=0.75,
        provenance=(QasperChannelProvenance(
            channel="dense", query_id="query-1", query_sha256=D,
            channel_rank=1, raw_score=0.75,
        ),),
    )


def non_ok_case(*, rank: int = 1) -> QasperCaseResult:
    unit = retrieval_unit()
    definitions = metric_definitions()
    return QasperCaseResult(
        manifest_digest=D, run_id="run-1", attempt_id="attempt-1", unit=unit,
        query="What evidence supports the result?", paper_ids=("paper-1",),
        status="needs_review",
        mapping_result=QasperMappingResult(
            case_id=unit.case_id, disposition="needs_review", mapping=None,
            reason="no uniquely verified mapping",
        ),
        final_stage="candidate", raw_ranked_results=(ranked_result(rank),), metrics=non_ok_metrics(),
        failures=(QasperFailureRecord(
            unit=unit, category="mapping_failure", status="needs_review",
            reason="mapping threshold awaits human review", stage="mapping",
            affected_metrics=definitions,
        ),),
        reason="mapping threshold awaits human review",
    )


D1, D2, D3, D4, D5, D6 = (f"{n}" * 64 for n in range(1, 7))


def _scope(case_id: str) -> CaseScopeProjection:
    scope = QuestionScope(sources=(ScopedPaper(
        paper_id="paper-1", document_version="doc-v1", source_sha256=D2,
    ),))
    return CaseScopeProjection.create(case_id=case_id, scope=scope)


def _retrieval_variant() -> VariantConfiguration:
    retrieval = RetrievalFactors(
        parser_config_hash=D1, chunker_config_hash=D2, embedding_config_hash=D3,
        index_config_hash=D4, retriever_config_hash=D5,
        reranker_config_hash=None,
        reranker_not_applicable_reason="fixture profile does not rerank",
        context_policy_hash=D6,
    )
    return VariantConfiguration.create(
        variant_id="qasper-fixture-v1", track=EvaluationTrack.RETRIEVAL,
        retrieval=retrieval, retrieval_not_applicable_reason=None,
        generation=None, generation_not_applicable_reason="retrieval-only contract",
        harness=None, harness_not_applicable_reason="retrieval-only contract",
    )


def _experiment_manifest(*, profile: ExecutionProfile = ExecutionProfile.FIXTURE,
                         dataset_digest: str = D1, corpus_digest: str = D3,
                         code: CodeIdentity | None = None) -> tuple:
    variant = _retrieval_variant()
    scopes = (_scope("case-1"), _scope("case-2"))
    code = code or CodeIdentity.create(
        git_revision="3d205845", workspace_content_fingerprint=D6,
        mutable_patch_fingerprints=(), dirty=False,
    )
    budget = BudgetLimits(
        max_requests=10, max_total_tokens=1000, max_cost=None, currency=None,
        unknown_price=True, unknown_price_reason="offline fixture", price_basis_reference=None,
        request_timeout_seconds=5.0, case_deadline_seconds=10.0, run_deadline_seconds=30.0,
        max_concurrency=1, max_retries=0, max_replans=0, max_turns=1,
    )
    namespaces = IsolatedNamespaces(
        tenant="qasper-test-tenant", index="qasper-test-index",
        memory="qasper-test-memory", artifact="qasper-test-artifact",
    )
    initial = InitialState(
        memory_policy=InitialStatePolicy.EMPTY, memory_snapshot_digest=None,
        cache_policy=CachePolicy.COLD, cache_snapshot_digest=None,
        gold_labels_present=False, prior_run_answers_present=False,
        production_business_memory_mounted=False, source_fulltext_access="read_only",
    )
    environment = ExecutionEnvironment(
        runtime="python-3.14", dependency_lock_digest=D5,
        hardware_class=None, hardware_identity_unavailable_reason="offline fixture",
    )
    comparison = ComparisonIdentity.create(
        dataset_digest=dataset_digest,
        case_scope_fingerprint=content_digest(scopes), corpus_content_sha256=corpus_digest,
        track=EvaluationTrack.RETRIEVAL, execution_profile=profile,
        variant_fingerprint=variant.variant_fingerprint,
        code_fingerprint=code.code_fingerprint,
        environment_fingerprint=environment.fingerprint,
        budget_fingerprint=budget.fingerprint,
        initial_state_fingerprint=initial.fingerprint,
        namespace_fingerprint=namespaces.fingerprint,
        repetition_plan=(1,), timing_boundary="offline fixture request boundary",
    )
    manifest = ExperimentManifest.create(
        record_type="experiment_manifest", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, experiment_id="qasper-exp-1",
        experiment_purpose=ExperimentPurpose.DEV_DIAGNOSTIC,
        dataset_id="qasper", dataset_version="qasper-v1", dataset_digest=dataset_digest,
        case_scopes=scopes, variants=(variant,), planned_units=tuple(
            PlannedEvaluationUnit(
                dataset_version="qasper-v1", case_id=case_id,
                track=EvaluationTrack.RETRIEVAL,
                variant_fingerprint=variant.variant_fingerprint, repetition=1,
            ) for case_id in ("case-1", "case-2")
        ), comparison_identities=(comparison,), allowed_differences=(),
    )
    return manifest, variant, code


def _mapping_fixture(*, dataset_digest: str = D1, text: str = "ABCDEFGHIJ"):
    from backend.research.document.models import PaperChunk
    source = CheckedSourceText.create(
        paper_id="paper-1", document_version="doc-v1", source_sha256=D2,
        normalized_text=text,
    )
    chunk = PaperChunk(
        chunk_id="chunk-1", paper_id="paper-1", parse_source="pdf", content=text,
        section_title="Results", metadata={
            "source_locator": "paper://paper-1#page=0", "document_version": "doc-v1",
            "source_sha256": D2,
        },
    )
    location = EvidenceLocation(
        content_type="text", pdf_page_index=0, printed_page=None, section="Results",
        paragraph_id="p1", char_start=0, char_end=10, element_id=None,
        table_id=None, row_labels=(), column_labels=(), cell_references=(),
        figure_id=None, caption=None, normalized_fulltext_sha256=text_sha256(text),
    )
    anchor = EvidenceAnchor(
        anchor_id="anchor-1", paper_id="paper-1", document_version="doc-v1",
        source_sha256=D2, claim_ids=("claim-1",), location=location,
        source_excerpt=text, source_excerpt_digest=content_digest(text),
    )
    group = RequiredEvidenceGroup(
        group_id="group-1", claim_ids=("claim-1",), alternative_sufficient_sets=(
            AlternativeSufficientSet(set_id="alt-1", required_anchor_ids=("anchor-1",)),
        ),
    )
    case = EvaluationCase.model_construct(
        record_type="case", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, dataset_id="qasper",
        dataset_version="qasper-v1", case_id="case-1", case_digest="e" * 64,
        answerability="answerable", evidence_anchors=(anchor,),
        required_evidence_groups=(group,),
    )
    binding = IndexBinding(
        corpus_snapshot_id="corpus-v1", index_snapshot_id="index-v1",
        index_content_sha256=calculate_index_content_sha256((chunk,)),
        parser_id="paper-parser", parser_version="parser-v1", parser_config_sha256=D1,
        chunker_id="paper-chunker", chunker_version="chunker-v1", chunker_config_sha256=D2,
        index_config_sha256=D4,
    )
    descriptor = ChunkSpanDescriptor.for_chunk(
        chunk, document_version="doc-v1", source_sha256=D2,
        source_locator="paper://paper-1#page=0", spans=(ChunkSourceSpan(
            source_start=0, source_end=10, chunk_start=0, chunk_end=10,
            source_locator="paper://paper-1#page=0", normalized_text_sha256=source.normalized_text_sha256,
        ),),
    )
    return build_index_evidence_mapping(
        case=case, dataset_digest=dataset_digest, mapping_version=QASPER_MAPPING_VERSION,
        binding=binding, actual_chunks=(chunk,), checked_sources=(source,),
        chunk_span_descriptors=(descriptor,),
    ), binding


def _qasper_manifest(*, profile: ExecutionProfile = ExecutionProfile.FIXTURE,
                     dataset_digest: str = D1, corpus_digest: str = D3,
                     chunk_text: str = "ABCDEFGHIJ", cache_sha256: str = D5,
                     mapping_artifact_digest: str | None = None) -> tuple[QasperExperimentManifest, object, VariantConfiguration]:
    shared, variant, code = _experiment_manifest(
        profile=profile, dataset_digest=dataset_digest, corpus_digest=corpus_digest,
    )
    mapping, binding = _mapping_fixture(dataset_digest=dataset_digest, text=chunk_text)
    data = QasperDataIdentity(
        dataset_id="qasper", dataset_version="qasper-v1", dataset_digest=dataset_digest,
        dataset_sha256=D2, dataset_case_count=2, evaluable_case_count=2,
        corpus_snapshot_id=binding.corpus_snapshot_id, corpus_sha256=corpus_digest,
        chunk_snapshot_id=binding.index_snapshot_id, chunk_sha256=binding.index_content_sha256,
        chunking_version=binding.chunker_version, chunk_count=1, paper_count=1,
        embedding_cache_id="cache-v1", embedding_cache_sha256=cache_sha256,
        embedding_model_id="embedding-v1", embedding_model_version="embedding-v1",
        embedding_dimension=4,
    )
    definitions = tuple(
        QasperMetricDefinition(metric_id=metric_id, version=version, k=k)
        for metric_id, version, k in (
            ("strict_hit", STRICT_METRIC_VERSION, 5),
            ("equivalent_hit", EQUIVALENT_METRIC_VERSION, 5),
            ("strict_coverage", STRICT_METRIC_VERSION, 5),
            ("equivalent_coverage", EQUIVALENT_METRIC_VERSION, 5),
            ("strict_mrr", STRICT_METRIC_VERSION, None),
            ("equivalent_mrr", EQUIVALENT_METRIC_VERSION, None),
            ("strict_ndcg", STRICT_METRIC_VERSION, 5),
            ("equivalent_ndcg", EQUIVALENT_METRIC_VERSION, 5),
        )
    )
    wrapped = QasperExperimentManifest.create(
        experiment_manifest=shared, data_identity=data, index_binding=binding,
        mapping_artifact=ArtifactReference(
            reference_id="mapping-1", kind="mapping", uri="external-sha256://" + (mapping_artifact_digest or mapping.mapping_digest),
            content_sha256=mapping_artifact_digest or mapping.mapping_digest,
        ),
        mapping_policy=QasperMappingPolicy(), metric_versions=QasperMetricVersions(),
        metric_definitions=definitions,
        execution_identity=QasperExecutionIdentity(
            execution_profile=profile,
            version=("qasper-fixture-execution/v1" if profile is ExecutionProfile.FIXTURE else "qasper-production-execution/v1"),
            implementation_id="offline-fixture", configuration_sha256=D6,
        ), code_identity=code,
    )
    return wrapped, mapping, variant



def _case_metric(definition: QasperMetricDefinition, *, status: str = "ok") -> QasperMetric:
    if status != "ok":
        return QasperMetric(
            metric_id=definition.metric_id, version=definition.version, k=definition.k,
            value=None, status=status, numerator=None, denominator=None,
            reason="mapping is unavailable for this planned case",
        )
    return QasperMetric(
        metric_id=definition.metric_id, version=definition.version, k=definition.k,
        value=1.0, status="ok", numerator=1.0, denominator=1, reason=None,
    )


def _case_pair(manifest: QasperExperimentManifest, mapping: object, *, non_ok: bool = False):
    unit = manifest.experiment_manifest.planned_units[1 if non_ok else 0]
    definitions = manifest.metric_definitions
    mapping_result = QasperMappingResult(
        case_id=unit.case_id,
        disposition="needs_review" if non_ok else "valid",
        mapping=None if non_ok else mapping,
        reason="mapping is unavailable for this planned case" if non_ok else None,
    )
    ranked = RankedResult(
        chunk_id="chunk-1", rank=1, stage="candidate", raw_score=0.8,
        provenance=(QasperChannelProvenance(
            channel="dense", query_id="query-1", query_sha256=D6,
            channel_rank=1, raw_score=0.8,
        ),),
    )
    metrics = tuple(_case_metric(definition, status="needs_review" if non_ok else "ok") for definition in definitions)
    failures = ()
    reason = None
    if non_ok:
        reason = "mapping is unavailable for this planned case"
        failures = (QasperFailureRecord(
            unit=unit, category="mapping_failure", status="needs_review", reason=reason,
            stage="mapping", affected_metrics=definitions,
        ),)
    return QasperCaseResult(
        manifest_digest=manifest.digest, run_id="run-1", attempt_id=f"attempt-{unit.case_id}",
        unit=unit, query="Which evidence supports the result?", paper_ids=("paper-1",),
        status="needs_review" if non_ok else "ok", mapping_result=mapping_result,
        final_stage="candidate", raw_ranked_results=(ranked,), metrics=metrics, failures=failures, reason=reason,
    )


def _report(manifest: QasperExperimentManifest, mapping: object) -> QasperReportArtifact:
    valid_case = _case_pair(manifest, mapping)
    missing_case = _case_pair(manifest, mapping, non_ok=True)
    aggregates = []
    for definition in manifest.metric_definitions:
        numerator = 1.0 if definition.metric_id.endswith("_mrr") else 1.0
        aggregates.append(QasperMetric(
            metric_id=definition.metric_id, version=definition.version, k=definition.k,
            value=numerator, status="ok", numerator=numerator, denominator=1, reason=None,
            aggregation="macro_average",
        ))
    refs = tuple(ArtifactReference(
        reference_id=f"artifact-{name}", kind=name, uri=f"external://qasper/{name}", content_sha256=D6,
    ) for name in ("manifest", "cases", "metrics", "failures", "report"))
    return QasperReportArtifact(
        manifest=manifest, run_id="run-1",
        selected_variant_fingerprint=manifest.experiment_manifest.variants[0].variant_fingerprint,
        case_results=(valid_case, missing_case),
        denominators=QasperDenominators(planned=2, valid=1, needs_review=1, system_failure=0, not_applicable=0),
        metrics=tuple(aggregates), artifacts=QasperReportFiles(
            manifest=refs[0], cases=refs[1], metrics=refs[2], failures=refs[3], report=refs[4],
        ),
    )


def test_baseline_digest_and_strict_json_roundtrip() -> None:
    item = baseline()
    item.verify_digest()
    assert QasperBaselineSnapshot.model_validate_json(item.model_dump_json()) == item


def test_baseline_digest_tampering_is_rejected() -> None:
    item = baseline()
    data = item.model_dump(mode="json")
    data["raw_result_sha256"] = "e" * 64
    with pytest.raises(ValidationError, match="digest"):
        QasperBaselineSnapshot.model_validate_json(json.dumps(data))


def test_baseline_is_diagnostic_and_does_not_claim_protocol_metrics() -> None:
    item = baseline()
    assert item.qualification == "diagnostic_only"
    assert item.historical_code_claim.git_revision != item.observed_reconstruction_code_identity.git_revision
    with pytest.raises(ValidationError):
        LegacyMetricSummary(metric_id="strict_hit", value=0.5)


def test_legacy_count_mismatch_requires_explicit_diagnostic_override() -> None:
    content = baseline().model_dump(exclude={"digest"})
    content["evaluable_case_count"] = 881
    content["legacy_summary"] = (LegacyMetricSummary(
        metric_id="legacy_strict_hit_at_5", value=0.600225, denominator=888,
    ),)

    with pytest.raises(ValidationError, match="allow_legacy_count_mismatch"):
        QasperBaselineSnapshot.create(**content)

    item = QasperBaselineSnapshot.create(
        **content, allow_legacy_count_mismatch=True,
    )
    assert item.evaluable_case_count == 881
    assert item.legacy_summary[0].denominator == 888
    assert item.legacy_count_policy == "explicit_diagnostic_mismatch"
    assert QasperBaselineSnapshot.model_validate_json(item.model_dump_json()) == item


def test_metric_aggregates_allow_fractional_numerators_but_keep_valid_denominator() -> None:
    mrr = QasperMetric(
        metric_id="strict_mrr", version=STRICT_METRIC_VERSION, value=(1 + 1 / 3) / 2,
        status="ok", numerator=1 + 1 / 3, denominator=2, reason=None,
    )
    ndcg = QasperMetric(
        metric_id="equivalent_ndcg", version=EQUIVALENT_METRIC_VERSION, k=10,
        value=0.63, status="ok", numerator=1.26, denominator=2, reason=None,
    )
    hit = QasperMetric(
        metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5,
        value=1 / 2, status="ok", numerator=1, denominator=2, reason=None,
    )
    assert mrr.denominator == 2 and ndcg.denominator == 2 and hit.numerator == 1
    with pytest.raises(ValidationError, match="integer count"):
        QasperMetric(metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5,
                     value=0.75, status="ok", numerator=1.5, denominator=2, reason=None)


def test_non_ok_metric_requires_null_and_reason_and_version_is_fail_closed() -> None:
    with pytest.raises(ValidationError, match="non-ok metric"):
        QasperMetric(metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5, value=0.0,
                     status="system_failure", numerator=None, denominator=None,
                     reason="mapping unavailable", mapping_version=QASPER_MAPPING_VERSION)
    with pytest.raises(ValidationError):
        QasperMetric(metric_id="strict_hit", version="latest", k=5, value=0.5,
                     status="ok", numerator=1, denominator=2, reason=None)
    with pytest.raises(ValidationError):
        QasperMetric(metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5, value=True,
                     status="ok", numerator=1, denominator=2, reason=None)
    with pytest.raises(ValidationError):
        QasperMetric(metric_id="equivalent_ndcg", version=EQUIVALENT_METRIC_VERSION, k=5,
                     value=float("nan"), status="ok", numerator=float("nan"), denominator=2, reason=None)
    with pytest.raises(ValidationError):
        QasperMetric(metric_id="equivalent_ndcg", version=EQUIVALENT_METRIC_VERSION, k=5,
                     value=0.5, status="ok", numerator=True, denominator=2, reason=None)


def test_case_preserves_typed_provenance_and_rejects_rank_corruption() -> None:
    case = non_ok_case()
    assert case.raw_ranked_results[0].provenance[0].channel == "dense"
    with pytest.raises(ValidationError, match="ordered"):
        non_ok_case(rank=2)


def test_denominator_partition_and_non_ok_reason_are_explicit() -> None:
    assert QasperDenominators(planned=5, valid=2, needs_review=1,
                              system_failure=1, not_applicable=1).non_ok == 3
    with pytest.raises(ValidationError, match="partition"):
        QasperDenominators(planned=5, valid=2, needs_review=1,
                           system_failure=0, not_applicable=1)
    with pytest.raises(ValidationError, match="non-ok metric"):
        QasperMetric(metric_id="strict_hit", version=STRICT_METRIC_VERSION, k=5,
                     value=0.0, status="needs_review", numerator=None, denominator=None,
                     reason=None)


def test_models_forbid_extra_fields_and_json_roundtrip() -> None:
    item = baseline()
    data = json.loads(item.model_dump_json())
    data["untyped_business_payload"] = {"arbitrary": "dict"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QasperBaselineSnapshot.model_validate_json(json.dumps(data))
    assert content_digest({"stable": 1}) == content_digest({"stable": 1})



def test_experiment_manifest_wraps_shared_plan_and_roundtrips_with_digest() -> None:
    manifest, _, _ = _qasper_manifest()
    restored = QasperExperimentManifest.model_validate_json(manifest.model_dump_json())
    assert restored == manifest
    manifest.verify_digest()
    payload = json.loads(manifest.model_dump_json())
    payload["mapping_policy"]["review_status"] = "pending_human_review"
    payload["mapping_artifact"]["content_sha256"] = D6
    with pytest.raises(ValidationError, match="digest"):
        QasperExperimentManifest.model_validate_json(json.dumps(payload))


def test_mapping_result_valid_and_missing_are_typed() -> None:
    manifest, mapping, _ = _qasper_manifest()
    valid = QasperMappingResult(case_id="case-1", disposition="valid", mapping=mapping, reason=None)
    missing = QasperMappingResult(
        case_id="case-2", disposition="not_applicable", mapping=None,
        reason="no verified evidence mapping exists",
    )
    assert valid.mapping_digest == mapping.mapping_digest
    assert missing.mapping_digest is None
    with pytest.raises(ValidationError):
        QasperMappingResult(case_id="case-1", disposition="valid", mapping=None, reason=None)


def test_non_ok_case_retains_failure_record_and_null_metrics() -> None:
    manifest, mapping, _ = _qasper_manifest()
    case = _case_pair(manifest, mapping, non_ok=True)
    assert case.status == "needs_review"
    assert case.failures[0].category == "mapping_failure"
    assert all(metric.value is None and metric.numerator is None for metric in case.metrics)
    corrupted_case = json.loads(case.model_dump_json())
    corrupted_case["reason"] = None
    with pytest.raises(ValidationError, match="non-ok case"):
        QasperCaseResult.model_validate_json(json.dumps(corrupted_case))


def test_report_requires_complete_planned_partition_and_aligned_valid_metrics() -> None:
    manifest, mapping, _ = _qasper_manifest()
    report = _report(manifest, mapping)
    assert report.denominators.valid == 1
    assert report.denominators.non_ok == 1
    assert all(metric.denominator == report.denominators.valid for metric in report.metrics)
    incomplete = json.loads(report.model_dump_json())
    incomplete["case_results"] = incomplete["case_results"][:1]
    with pytest.raises(ValidationError, match="every selected planned unit"):
        QasperReportArtifact.model_validate_json(json.dumps(incomplete))
    duplicate = report.model_copy(update={"case_results": (report.case_results[0], report.case_results[0])})
    with pytest.raises(ValidationError, match="unique"):
        QasperReportArtifact.model_validate_json(duplicate.model_dump_json())
    altered_mapping, _ = _mapping_fixture(dataset_digest=D6)
    altered = json.loads(report.model_dump_json())
    altered["case_results"][0]["mapping_result"]["mapping"] = json.loads(altered_mapping.model_dump_json())
    with pytest.raises(ValidationError, match="mapping dataset/index binding"):
        QasperReportArtifact.model_validate_json(json.dumps(altered))


@pytest.mark.parametrize("change", ("dataset", "corpus", "chunk", "cache", "mapping", "profile"))
def test_qasper_comparison_rejects_changed_identity(change: str) -> None:
    baseline_manifest, _, _ = _qasper_manifest()
    kwargs = {}
    if change == "dataset":
        kwargs["dataset_digest"] = D6
    elif change == "corpus":
        kwargs["corpus_digest"] = D6
    elif change == "chunk":
        kwargs["chunk_text"] = "KLMNOPQRST"
    elif change == "cache":
        kwargs["cache_sha256"] = D6
    elif change == "mapping":
        kwargs["mapping_artifact_digest"] = D6
    else:
        kwargs["profile"] = ExecutionProfile.PRODUCTION
    candidate, _, _ = _qasper_manifest(**kwargs)
    result = qualify_qasper_comparison(baseline_manifest, candidate)
    assert result.status == "incomparable"
    assert not result.ranking_eligible
    assert result.reasons


def test_case_requires_explicit_final_stage_in_persisted_dto() -> None:
    payload = non_ok_case().model_dump(mode="json")
    payload.pop("final_stage")

    with pytest.raises(ValidationError):
        QasperCaseResult.model_validate(payload)


def test_report_aggregate_metrics_must_declare_macro_average() -> None:
    manifest, mapping, _ = _qasper_manifest()
    report = _report(manifest, mapping)
    payload = report.model_dump(mode="json")
    payload["metrics"][0]["aggregation"] = "per_case"

    with pytest.raises(ValidationError, match="macro_average"):
        QasperReportArtifact.model_validate(payload)
