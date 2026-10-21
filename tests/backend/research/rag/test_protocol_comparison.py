from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.research.rag.evaluation.paper_benchmark_matrix import compare_saved_benchmark_matrix
from backend.research.rag.evaluation.paper_evaluation_compare import EvidenceABComparator
from backend.research.rag.evaluation.protocol_common import PROTOCOL_VERSION, SCHEMA_VERSION, content_digest
from backend.research.rag.evaluation.protocol_comparison import (
    BOOTSTRAP_METHOD_VERSION, PairedComparisonConfig, compare_saved_scores, qualify_comparison,
)
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest, QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_execution import (
    BudgetLimits, CachePolicy, CaseScopeProjection, CodeIdentity, ComparisonIdentity,
    ComponentClassification, ComponentProvenance, ComponentRole, CorpusProvenance,
    EvaluationOptions, EvaluationTrack, ExecutionEnvironment, ExecutionPath, ExecutionPathKind,
    ExecutionProfile, ExperimentManifest, ExperimentPurpose, GenerationFactors, InitialState, InitialStatePolicy,
    IsolatedNamespaces, PlannedEvaluationUnit, RetrievalFactors, SourceAccessGrant,
    VariantConfiguration,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EvaluationScore, MetricAggregation, MetricDefinition, MetricDirection, MetricGranularity,
    MetricRegistry, MetricValueKind, MissingValuePolicy, NumericDomain, ScoreSource, ScoreStatus,
)

D = tuple(str(i) * 64 for i in range(1, 10))
NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _dataset() -> DatasetManifest:
    sources = [
        {"paper_id": f"paper-{i}", "document_version": "v1", "source_uri": f"https://example.test/paper-{i}.pdf",
         "source_sha256": D[i-1], "retrieved_at": NOW, "fulltext_artifact_digest": D[i+1],
         "completeness": "complete", "access_status": "available", "source_format": "pdf",
         "license_reference_id": "license"}
        for i in (1, 2)
    ]
    cases = [{"case_id": f"case-{i}", "case_digest": D[i+2], "question_digest": D[i+3],
              "reference_digest": D[i+4], "paper_group_id": f"family-{i}", "split": "dev",
              "review_status": "approved"} for i in (1, 2)]
    return DatasetManifest.create(
        record_type="dataset", schema_version=SCHEMA_VERSION, protocol_version=PROTOCOL_VERSION,
        dataset_id="dataset", dataset_version="v1", lifecycle_state="frozen",
        release_purpose="dev_regression", corpus_snapshot_id="corpus-v1", corpus_snapshot_digest=D[7],
        paper_versions=tuple(sources), case_manifest=tuple(cases),
        split_manifest={"method": "paper-family", "method_version": "v1", "deterministic_seed": "seed",
            "groups": tuple({"paper_group_id": f"family-{i}", "split": "dev",
                "sources": ({"paper_id": f"paper-{i}", "document_version": "v1", "source_sha256": D[i-1]},),
                "grouping_basis": "same paper family", "dispute_resolution": None} for i in (1, 2))},
        review_manifest={"status": "approved", "reviewer_id": "human", "reviewer_kind": "human",
            "reviewed_at": NOW, "approved_case_ids": ("case-1", "case-2"), "corpus_checked": True,
            "split_checked": True, "contract_checked": True, "evidence_reference": "artifact://review"},
        annotation_version="annotation-v1",
        license_reference={"reference_id": "license", "reference_uri": "https://example.test/license",
            "terms_digest": D[8], "permitted_use": "internal_evaluation"},
        authoring_protocol={"protocol_id": "question-first", "protocol_version": "v1", "protocol_digest": D[0]},
        supersedes=None, correction_reason=None, frozen_at=NOW,
    )


def _budget() -> BudgetLimits:
    return BudgetLimits(max_requests=20, max_total_tokens=10000, max_cost=None, currency=None,
        unknown_price=True, unknown_price_reason="not priced", price_basis_reference=None,
        request_timeout_seconds=30.0, case_deadline_seconds=60.0, run_deadline_seconds=300.0,
        max_concurrency=1, max_retries=0, max_replans=0, max_turns=4)


def _state() -> InitialState:
    return InitialState(memory_policy=InitialStatePolicy.EMPTY, memory_snapshot_digest=None,
        cache_policy=CachePolicy.COLD, cache_snapshot_digest=None, gold_labels_present=False,
        prior_run_answers_present=False, production_business_memory_mounted=False,
        source_fulltext_access="read_only")


def _component(role: ComponentRole, config_hash: str) -> ComponentProvenance:
    return ComponentProvenance(role=role, classification=ComponentClassification.PRODUCTION,
        adapter=f"{role.value}-adapter", provider="provider", model=f"{role.value}-model",
        revision="v1", identity_unavailable_reason=None, fixture_kind=None, not_applicable_reason=None,
        config_hash=config_hash, code_revision="commit-1", evidence_references=(f"artifact://{role.value}",),
        runtime_provenance_digest=D[8])


def _options_pair(*, repetitions: tuple[int, ...] = (1, 2), variable: str = "retriever_config_hash"):
    dataset = _dataset()
    scopes = tuple(CaseScopeProjection.create(case_id=f"case-{i}", scope=QuestionScope(sources=(
        ScopedPaper(paper_id=f"paper-{i}", document_version="v1", source_sha256=D[i-1]),))) for i in (1, 2))
    base_values = dict(parser_config_hash=D[0], chunker_config_hash=D[1], embedding_config_hash=D[2],
        index_config_hash=D[3], retriever_config_hash=D[4], reranker_config_hash=None,
        reranker_not_applicable_reason="disabled", context_policy_hash=D[5])
    candidate_values = dict(base_values)
    candidate_values[variable] = D[6]
    if variable in {"chunker_config_hash", "embedding_config_hash", "parser_config_hash"}:
        candidate_values["index_config_hash"] = D[7]
    variants = tuple(VariantConfiguration.create(variant_id=name, track=EvaluationTrack.RETRIEVAL,
        retrieval=RetrievalFactors(**values), retrieval_not_applicable_reason=None, generation=None,
        generation_not_applicable_reason="retrieval only", harness=None,
        harness_not_applicable_reason="retrieval only")
        for name, values in (("baseline-display-name", base_values), ("candidate-display-name", candidate_values)))
    budget, state = _budget(), _state()
    code = CodeIdentity.create(git_revision="commit-1", workspace_content_fingerprint=D[6],
        mutable_patch_fingerprints=(), dirty=False)
    environment = ExecutionEnvironment(runtime="python", dependency_lock_digest=D[7], hardware_class="cpu",
        hardware_identity_unavailable_reason=None)
    namespaces = IsolatedNamespaces(tenant="eval-tenant", index="eval-index", memory="eval-memory", artifact="eval-artifact")
    identities = tuple(ComparisonIdentity.create(dataset_digest=dataset.dataset_digest,
        case_scope_fingerprint=content_digest(scopes), corpus_content_sha256=dataset.corpus_snapshot_digest,
        track=EvaluationTrack.RETRIEVAL, execution_profile=ExecutionProfile.PRODUCTION,
        variant_fingerprint=variant.variant_fingerprint, code_fingerprint=code.code_fingerprint,
        environment_fingerprint=environment.fingerprint, budget_fingerprint=budget.fingerprint,
        initial_state_fingerprint=state.fingerprint, namespace_fingerprint=namespaces.fingerprint,
        repetition_plan=repetitions, timing_boundary="request including retries, excluding scoring") for variant in variants)
    units = tuple(PlannedEvaluationUnit(dataset_version=dataset.dataset_version, case_id=f"case-{case}",
        track=EvaluationTrack.RETRIEVAL, variant_fingerprint=variant.variant_fingerprint, repetition=repetition)
        for variant in variants for case in (1, 2) for repetition in repetitions)
    manifest = ExperimentManifest.create(record_type="experiment_manifest", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, experiment_id="experiment", experiment_purpose=ExperimentPurpose.DEV_DIAGNOSTIC,
        dataset_id=dataset.dataset_id, dataset_version=dataset.dataset_version, dataset_digest=dataset.dataset_digest,
        case_scopes=scopes, variants=variants, planned_units=units, comparison_identities=identities,
        allowed_differences=(f"variant.retrieval.{variable}",))
    grants = tuple(SourceAccessGrant(paper_id=f"paper-{i}", document_version="v1", source_sha256=D[i-1],
        authorization_reference=f"artifact://grant/{i}") for i in (1, 2))
    path = ExecutionPath(kind=ExecutionPathKind.RETRIEVAL_APPLICATION,
        application_service="ResearchEvaluationPort.retrieve", retrieval_enabled=True,
        answer_generation_enabled=False, gated_harness_requested=False, context_graph_identity=None)
    result = []
    for offset, variant in enumerate(variants):
        factors = variant.retrieval
        assert factors is not None
        corpus = CorpusProvenance(corpus_snapshot_id=dataset.corpus_snapshot_id,
            corpus_content_sha256=dataset.corpus_snapshot_digest, source_material="complete_fulltext",
            source_completeness="complete", index_snapshot_id=("index-a" if offset == 0 else "index-b")
                if variable in {"chunker_config_hash", "embedding_config_hash", "parser_config_hash"} else "index-a",
            index_content_sha256=(D[3] if offset == 0 else D[4])
                if variable in {"chunker_config_hash", "embedding_config_hash", "parser_config_hash"} else D[3],
            index_not_applicable_reason=None, gold_labels_indexed=False, previous_answers_indexed=False,
            evidence_references=("artifact://corpus",))
        components = tuple(_component(role, getattr(factors, field)) for role, field in (
            (ComponentRole.PARSER, "parser_config_hash"), (ComponentRole.CHUNKER, "chunker_config_hash"),
            (ComponentRole.EMBEDDING, "embedding_config_hash"), (ComponentRole.INDEX, "index_config_hash"),
            (ComponentRole.RETRIEVER, "retriever_config_hash")))
        result.append(EvaluationOptions(record_type="evaluation_options", schema_version=SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION, experiment_manifest=manifest,
            selected_variant_fingerprint=variant.variant_fingerprint, execution_profile=ExecutionProfile.PRODUCTION,
            execution_path=path, components=components, source_access_grants=grants, corpus=corpus,
            namespaces=namespaces, initial_state=state, budget_limits=budget, code_identity=code, environment=environment))
    return dataset, result[0], result[1]



def _oracle_option() -> EvaluationOptions:
    dataset = _dataset()
    scopes = tuple(CaseScopeProjection.create(case_id=f"case-{i}", scope=QuestionScope(sources=(
        ScopedPaper(paper_id=f"paper-{i}", document_version="v1", source_sha256=D[i-1]),))) for i in (1, 2))
    generation = GenerationFactors(generator_config_hash=D[0], prompt_hash=D[1], output_contract_hash=D[2],
        decoding_config_hash=D[3], context_policy_hash=D[4], max_input_tokens=4000, max_output_tokens=500,
        request_timeout_seconds=30.0, max_retries=0)
    variant = VariantConfiguration.create(variant_id="oracle-display", track=EvaluationTrack.ORACLE_GENERATION,
        retrieval=None, retrieval_not_applicable_reason="reviewed evidence is supplied", generation=generation,
        generation_not_applicable_reason=None, harness=None, harness_not_applicable_reason="direct answer worker")
    budget, state = _budget(), _state()
    code = CodeIdentity.create(git_revision="commit-1", workspace_content_fingerprint=D[6],
        mutable_patch_fingerprints=(), dirty=False)
    environment = ExecutionEnvironment(runtime="python", dependency_lock_digest=D[7], hardware_class="cpu",
        hardware_identity_unavailable_reason=None)
    namespaces = IsolatedNamespaces(tenant="eval-tenant", index="eval-index", memory="eval-memory", artifact="eval-artifact")
    identity = ComparisonIdentity.create(dataset_digest=dataset.dataset_digest,
        case_scope_fingerprint=content_digest(scopes), corpus_content_sha256=dataset.corpus_snapshot_digest,
        track=EvaluationTrack.ORACLE_GENERATION, execution_profile=ExecutionProfile.PRODUCTION,
        variant_fingerprint=variant.variant_fingerprint, code_fingerprint=code.code_fingerprint,
        environment_fingerprint=environment.fingerprint, budget_fingerprint=budget.fingerprint,
        initial_state_fingerprint=state.fingerprint, namespace_fingerprint=namespaces.fingerprint,
        repetition_plan=(1, 2), timing_boundary="request including retries, excluding scoring")
    units = tuple(PlannedEvaluationUnit(dataset_version=dataset.dataset_version, case_id=f"case-{case}",
        track=EvaluationTrack.ORACLE_GENERATION, variant_fingerprint=variant.variant_fingerprint, repetition=repetition)
        for case in (1, 2) for repetition in (1, 2))
    manifest = ExperimentManifest.create(record_type="experiment_manifest", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, experiment_id="oracle-experiment",
        experiment_purpose=ExperimentPurpose.DEV_DIAGNOSTIC, dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version, dataset_digest=dataset.dataset_digest, case_scopes=scopes,
        variants=(variant,), planned_units=units, comparison_identities=(identity,), allowed_differences=())
    grants = tuple(SourceAccessGrant(paper_id=f"paper-{i}", document_version="v1", source_sha256=D[i-1],
        authorization_reference=f"artifact://grant/{i}") for i in (1, 2))
    component = _component(ComponentRole.GENERATOR, generation.generator_config_hash)
    return EvaluationOptions(record_type="evaluation_options", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, experiment_manifest=manifest,
        selected_variant_fingerprint=variant.variant_fingerprint, execution_profile=ExecutionProfile.PRODUCTION,
        execution_path=ExecutionPath(kind=ExecutionPathKind.ORACLE_ANSWER_WORKER,
            application_service="ResearchEvaluationPort.answer_from_evidence", retrieval_enabled=False,
            answer_generation_enabled=True, gated_harness_requested=False, context_graph_identity=None),
        components=(component,), source_access_grants=grants,
        corpus=CorpusProvenance(corpus_snapshot_id=dataset.corpus_snapshot_id,
            corpus_content_sha256=dataset.corpus_snapshot_digest, source_material="complete_fulltext",
            source_completeness="complete", index_snapshot_id=None, index_content_sha256=None,
            index_not_applicable_reason="oracle generation has no index", gold_labels_indexed=False,
            previous_answers_indexed=False, evidence_references=("artifact://corpus",)),
        namespaces=namespaces, initial_state=state, budget_limits=budget, code_identity=code, environment=environment)

def _registry(*, direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER, versions=("v1",)) -> MetricRegistry:
    return MetricRegistry(tuple(MetricDefinition(metric_id="quality", version=version,
        required_inputs=frozenset({"saved_answer"}), applicable_tracks=frozenset({EvaluationTrack.RETRIEVAL}),
        applicable_execution_profiles=frozenset({ExecutionProfile.PRODUCTION}), applicable_scopes=frozenset({"overall"}),
        granularity=MetricGranularity.CASE, direction=direction, domain=NumericDomain(minimum=0, maximum=1),
        value_kind=MetricValueKind.SCALAR, aggregation=MetricAggregation.MEAN,
        missing_policy=MissingValuePolicy.ERROR) for version in versions))


def _score(options: EvaluationOptions, case: int, repetition: int, value: float | None, *,
           attempt: str = "1", status: ScoreStatus = ScoreStatus.OK, version: str = "v1") -> EvaluationScore:
    provenance = {"artifact": f"score/{case}/{repetition}/{attempt}"}
    return EvaluationScore(record_type="score", schema_version=SCHEMA_VERSION, protocol_version=PROTOCOL_VERSION,
        dataset_id=options.experiment_manifest.dataset_id, dataset_version=options.experiment_manifest.dataset_version,
        dataset_digest=options.experiment_manifest.dataset_digest, run_id="baseline-run" if options.selected_variant.variant_id.startswith("baseline") else "candidate-run",
        case_id=f"case-{case}", attempt_id=f"attempt-{case}-{repetition}-{attempt}", track=EvaluationTrack.RETRIEVAL,
        execution_profile=ExecutionProfile.PRODUCTION, variant_fingerprint=options.selected_variant_fingerprint,
        repetition=repetition, scope="overall", scoring_revision="score-v1", metric_id="quality", version=version,
        value=value, status=status, numerator=None, denominator=None, source=ScoreSource.RULE,
        provenance=provenance, provenance_digest=content_digest(provenance), evidence=("artifact://score",),
        reason=None if status is ScoreStatus.OK else "required stage unavailable")


def _config(**changes) -> PairedComparisonConfig:
    values = dict(metric_id="quality", metric_version="v1", scoring_revision="score-v1", scope="overall",
        minimum_cases=2, minimum_groups=2, minimum_repetitions=2, require_complete_pairs=True,
        bootstrap_resamples=200, confidence_level=0.90, seed=17, method_version=BOOTSTRAP_METHOD_VERSION)
    values.update(changes)
    return PairedComparisonConfig(**values)


def _complete_scores(base, candidate):
    baseline = [_score(base, case, repetition, 0.2 + case / 10) for case in (1, 2) for repetition in (1, 2)]
    improved = [_score(candidate, case, repetition, 0.5 + case / 10) for case in (1, 2) for repetition in (1, 2)]
    return baseline, improved


def test_saved_scores_pair_cases_aggregate_repetitions_and_cluster_bootstrap() -> None:
    dataset, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    # Two raw attempts with the same score stay one repetition and one case.
    b.append(_score(candidate, 1, 1, 0.6, attempt="retry"))
    result = compare_saved_scores(baseline_options=baseline, candidate_options=candidate,
        baseline_scores=a, candidate_scores=b, dataset_manifest=dataset, registry=_registry(), config=_config())
    assert result.valid_pairs == 2
    assert result.valid_repetition_pairs == 4
    assert result.independent_groups == 2
    assert result.candidate_minus_baseline == pytest.approx(0.3)
    assert result.confidence is not None
    assert result.bootstrap_completed == 200
    assert result.conclusion == "improvement"
    assert result.comparison_qualification.status == "comparable"
    assert {item.group_id for item in result.case_differences} == {"family-1", "family-2"}


def test_small_sample_and_missing_or_unknown_scores_are_inconclusive_not_zero() -> None:
    dataset, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    b = [score for score in b if not (score.case_id == "case-2" and score.repetition == 2)]
    b.append(_score(candidate, 2, 2, None, status=ScoreStatus.ERROR))
    result = EvidenceABComparator.compare_saved_scores(baseline_options=baseline, candidate_options=candidate,
        baseline_scores=a, candidate_scores=b, dataset_manifest=dataset, registry=_registry(), config=_config())
    assert result.valid_pairs == 1
    assert result.missing_pairs == 1
    assert result.comparison_qualification.status == "inconclusive"
    assert result.conclusion == "inconclusive"
    missing = next(item for item in result.case_differences if item.case_id == "case-2")
    assert missing.candidate_minus_baseline is None
    assert missing.missing_repetitions == (2,)


def test_config_qualification_uses_typed_paths_not_variant_names() -> None:
    _, baseline, candidate = _options_pair()
    assert qualify_comparison(baseline, candidate).comparison_qualification.status == "comparable"
    # Display labels are outside the fingerprint. Comparing the exact same typed
    # variant cannot become an experiment merely by calling it a candidate.
    rejected = qualify_comparison(baseline, baseline)
    assert rejected.comparison_qualification.status == "incomparable"
    assert any("display-only" in reason for reason in rejected.comparison_qualification.reasons)


def test_chunking_change_requires_declared_real_derived_index_change() -> None:
    _, baseline, candidate = _options_pair(variable="chunker_config_hash")
    assert qualify_comparison(baseline, candidate).comparison_qualification.status == "comparable"
    forged = candidate.model_copy(update={"corpus": baseline.corpus})
    rejected = qualify_comparison(baseline, forged)
    assert rejected.comparison_qualification.status == "incomparable"
    assert any("index snapshot" in reason for reason in rejected.comparison_qualification.reasons)


def test_score_versions_and_unpaired_raw_attempts_do_not_form_evidence() -> None:
    dataset, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    b[0] = _score(candidate, 1, 1, 0.6, version="v2")
    result = compare_saved_scores(baseline_options=baseline, candidate_options=candidate,
        baseline_scores=a, candidate_scores=b, dataset_manifest=dataset,
        registry=_registry(versions=("v1", "v2")), config=_config())
    assert result.comparison_qualification.status == "incomparable"
    assert result.valid_pairs == 0

    _, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    b.append(_score(candidate, 1, 1, 0.1, attempt="conflict"))
    conflict = compare_saved_scores(baseline_options=baseline, candidate_options=candidate,
        baseline_scores=a, candidate_scores=b, dataset_manifest=dataset, registry=_registry(), config=_config())
    assert conflict.valid_pairs == 1
    assert conflict.missing_pairs == 1



def test_common_variables_and_unknown_allowed_paths_are_rejected() -> None:
    _, baseline, candidate = _options_pair()
    changed_budget = candidate.model_copy(update={"budget_limits": _budget().model_copy(update={"max_requests": 21})})
    rejected = qualify_comparison(baseline, changed_budget)
    assert rejected.comparison_qualification.status == "incomparable"

    bad_manifest = candidate.experiment_manifest.model_copy(update={
        "allowed_differences": ("variant.generation.prompt_hash",)
    })
    forbidden = candidate.model_copy(update={"experiment_manifest": bad_manifest})
    rejected = qualify_comparison(baseline, forbidden)
    assert rejected.comparison_qualification.status == "incomparable"


def test_oracle_is_diagnostic_and_never_ranking_eligible() -> None:
    oracle = _oracle_option()
    result = qualify_comparison(oracle, oracle)
    assert result.comparison_qualification.status == "diagnostic_only"
    assert result.comparison_qualification.ranking_eligible is False


def test_registry_direction_controls_improvement_sign() -> None:
    dataset, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    result = compare_saved_scores(baseline_options=baseline, candidate_options=candidate,
        baseline_scores=a, candidate_scores=b, dataset_manifest=dataset,
        registry=_registry(direction=MetricDirection.LOWER_IS_BETTER), config=_config())
    assert result.candidate_minus_baseline == pytest.approx(0.3)
    assert result.directed_improvement == pytest.approx(-0.3)
    assert result.conclusion == "regression"

def test_matrix_keeps_legacy_shape_and_does_not_publish_comparison() -> None:
    dataset, baseline, candidate = _options_pair()
    a, b = _complete_scores(baseline, candidate)
    matrix = compare_saved_benchmark_matrix(output_dir="ignored", baseline_options=baseline,
        baseline_scores=a, candidate_options={"candidate": candidate}, candidate_scores={"candidate": b},
        dataset_manifest=dataset, registry=_registry(), config=_config())
    assert matrix.ready_for_promotion is False
    assert matrix.to_dict()["datasets"] == {}
    assert matrix.to_dict()["protocol_comparisons"]["candidate"]["valid_pairs"] == 2
    assert "not a promotion decision" in matrix.to_markdown()
