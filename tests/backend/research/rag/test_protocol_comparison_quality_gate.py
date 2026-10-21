from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
    ExecutionProfile,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_execution import (
    BudgetLimits,
    CachePolicy,
    CaseScopeProjection,
    CodeIdentity,
    ComparisonIdentity,
    ComponentClassification,
    ComponentProvenance,
    ComponentRole,
    ContextGraphRequestIdentity,
    CorpusProvenance,
    EvaluationOptions,
    ExecutionEnvironment,
    ExecutionPath,
    ExecutionPathKind,
    ExperimentManifest,
    ExperimentPurpose,
    GenerationFactors,
    HarnessFactors,
    InitialState,
    InitialStatePolicy,
    IsolatedNamespaces,
    PlannedEvaluationUnit,
    QualificationDecision,
    RetrievalFactors,
    RunExecutionStatus,
    SourceAccessGrant,
    VariantConfiguration,
)
from backend.research.rag.evaluation.protocol_quality_gate import (
    CasePaperFamily,
    ComparisonEvidence,
    ComparisonField,
    ComparisonStatus,
    HumanApprovalReference,
    MetricEvidenceSource,
    MetricThresholdRule,
    QualificationScope,
    QualityEvidence,
    QualityPolicy,
    ThresholdBasis,
    evaluate_quality_policy,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EvaluationScore,
    MetricAggregation,
    MetricDefinition,
    MetricDirection,
    MetricGranularity,
    MetricRegistry,
    MetricValueKind,
    MissingValuePolicy,
    NumericDomain,
    ScoreSlot,
    ScoreSource,
    ScoreStatus,
)

D = tuple(str(index) * 64 for index in range(1, 10))
NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _budget() -> BudgetLimits:
    return BudgetLimits(
        max_requests=20,
        max_total_tokens=50_000,
        max_cost=None,
        currency=None,
        unknown_price=True,
        unknown_price_reason="provider price awaits an approved price record",
        price_basis_reference=None,
        request_timeout_seconds=30.0,
        case_deadline_seconds=120.0,
        run_deadline_seconds=600.0,
        max_concurrency=1,
        max_retries=1,
        max_replans=1,
        max_turns=6,
    )


def _state() -> InitialState:
    return InitialState(
        memory_policy=InitialStatePolicy.EMPTY,
        memory_snapshot_digest=None,
        cache_policy=CachePolicy.COLD,
        cache_snapshot_digest=None,
        gold_labels_present=False,
        prior_run_answers_present=False,
        production_business_memory_mounted=False,
        source_fulltext_access="read_only",
    )


def _retrieval(retriever_hash: str) -> RetrievalFactors:
    return RetrievalFactors(
        parser_config_hash=D[0],
        chunker_config_hash=D[1],
        embedding_config_hash=D[2],
        index_config_hash=D[3],
        retriever_config_hash=retriever_hash,
        reranker_config_hash=None,
        reranker_not_applicable_reason="reranking is frozen off",
        context_policy_hash=D[5],
    )


def _generation(prompt_hash: str) -> GenerationFactors:
    return GenerationFactors(
        generator_config_hash=D[0],
        prompt_hash=prompt_hash,
        output_contract_hash=D[2],
        decoding_config_hash=D[3],
        context_policy_hash=D[4],
        max_input_tokens=8_000,
        max_output_tokens=1_000,
        request_timeout_seconds=30.0,
        max_retries=1,
    )


def _variant(name: str, retriever_hash: str, prompt_hash: str) -> VariantConfiguration:
    return VariantConfiguration.create(
        variant_id=name,
        track=EvaluationTrack.END_TO_END,
        retrieval=_retrieval(retriever_hash),
        retrieval_not_applicable_reason=None,
        generation=_generation(prompt_hash),
        generation_not_applicable_reason=None,
        harness=HarnessFactors(
            application_service="ResearchApplicationService.rag_ask",
            harness_config_hash=D[0],
            deterministic_gate_config_hash=D[1],
            gated=True,
            plan_execute_verify=True,
        ),
        harness_not_applicable_reason=None,
    )


def _component(
    role: ComponentRole,
    config_hash: str | None,
    *,
    not_applicable: bool = False,
) -> ComponentProvenance:
    if not_applicable:
        return ComponentProvenance(
            role=role,
            classification=ComponentClassification.NOT_APPLICABLE,
            adapter=None,
            provider=None,
            model=None,
            revision=None,
            identity_unavailable_reason=None,
            fixture_kind=None,
            not_applicable_reason="reranker is disabled by the frozen variant",
            config_hash=None,
            code_revision=None,
            evidence_references=(),
            runtime_provenance_digest=None,
        )
    return ComponentProvenance(
        role=role,
        classification=ComponentClassification.PRODUCTION,
        adapter=f"{role.value}-adapter",
        provider="provider.example",
        model=f"{role.value}-model",
        revision="revision-v1",
        identity_unavailable_reason=None,
        fixture_kind=None,
        not_applicable_reason=None,
        config_hash=config_hash,
        code_revision="component-commit-v1",
        evidence_references=(f"artifact://runtime/{role.value}",),
        runtime_provenance_digest=D[8],
    )


def _components(variant: VariantConfiguration) -> tuple[ComponentProvenance, ...]:
    retrieval = variant.retrieval
    generation = variant.generation
    harness = variant.harness
    assert retrieval is not None and generation is not None and harness is not None
    return (
        _component(ComponentRole.SOURCE_READER, D[0]),
        _component(ComponentRole.PARSER, retrieval.parser_config_hash),
        _component(ComponentRole.CHUNKER, retrieval.chunker_config_hash),
        _component(ComponentRole.EMBEDDING, retrieval.embedding_config_hash),
        _component(ComponentRole.INDEX, retrieval.index_config_hash),
        _component(ComponentRole.RETRIEVER, retrieval.retriever_config_hash),
        _component(ComponentRole.RERANKER, None, not_applicable=True),
        _component(ComponentRole.GENERATOR, generation.generator_config_hash),
        _component(ComponentRole.ANSWER_WORKER, D[1]),
        _component(ComponentRole.HARNESS, harness.harness_config_hash),
    )


def _options_pair(*, candidate_prompt_hash: str = D[1]) -> tuple[EvaluationOptions, EvaluationOptions]:
    scope = CaseScopeProjection.create(
        case_id="case-1",
        scope=QuestionScope(
            sources=(
                ScopedPaper(
                    paper_id="paper-1", document_version="source-v1", source_sha256=D[0]
                ),
            )
        ),
    )
    baseline_variant = _variant("baseline", D[4], D[1])
    candidate_variant = _variant("candidate", D[6], candidate_prompt_hash)
    variants = (baseline_variant, candidate_variant)
    budget = _budget()
    state = _state()
    code = CodeIdentity.create(
        git_revision="commit-v1",
        workspace_content_fingerprint=D[6],
        mutable_patch_fingerprints=(),
        dirty=False,
    )
    environment = ExecutionEnvironment(
        runtime="python-3.12",
        dependency_lock_digest=D[7],
        hardware_class="cpu",
        hardware_identity_unavailable_reason=None,
    )
    namespaces = IsolatedNamespaces(
        tenant="eval-tenant", index="eval-index", memory="eval-memory", artifact="eval-artifact"
    )
    identities = tuple(
        ComparisonIdentity.create(
            dataset_digest=D[0],
            case_scope_fingerprint=content_digest((scope,)),
            corpus_content_sha256=D[2],
            track=EvaluationTrack.END_TO_END,
            execution_profile=ExecutionProfile.PRODUCTION,
            variant_fingerprint=variant.variant_fingerprint,
            code_fingerprint=code.code_fingerprint,
            environment_fingerprint=environment.fingerprint,
            budget_fingerprint=budget.fingerprint,
            initial_state_fingerprint=state.fingerprint,
            namespace_fingerprint=namespaces.fingerprint,
            repetition_plan=(1,),
            timing_boundary="request including retries and Harness gates; scoring excluded",
        )
        for variant in variants
    )
    planned_units = tuple(
        PlannedEvaluationUnit(
            dataset_version="dataset-v1",
            case_id="case-1",
            track=EvaluationTrack.END_TO_END,
            variant_fingerprint=variant.variant_fingerprint,
            repetition=1,
        )
        for variant in variants
    )
    manifest = ExperimentManifest.create(
        record_type="experiment_manifest",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        experiment_id="typed-quality-comparison",
        experiment_purpose=ExperimentPurpose.RELEASE_QUALIFICATION,
        dataset_id="dataset-1",
        dataset_version="dataset-v1",
        dataset_digest=D[0],
        case_scopes=(scope,),
        variants=variants,
        planned_units=planned_units,
        comparison_identities=identities,
        allowed_differences=("variant.retrieval.retriever_config_hash",),
    )
    path = ExecutionPath(
        kind=ExecutionPathKind.RESEARCH_GATED_HARNESS,
        application_service="ResearchApplicationService.rag_ask",
        retrieval_enabled=True,
        answer_generation_enabled=True,
        gated_harness_requested=True,
        context_graph_identity=ContextGraphRequestIdentity(
            run_id="graph-run-1",
            graph_id="research-rag",
            graph_version="graph-v1",
            graph_ref="research-rag@graph-v1",
            graph_checksum=D[0],
            stage_id="answer-stage",
            stage_binding_checksum=D[1],
            node_id="answer-node",
            node_instance_id="answer-node-instance-1",
            activity_id="answer-activity-1",
            activity_attempt=1,
        ),
    )
    grant = SourceAccessGrant(
        paper_id="paper-1",
        document_version="source-v1",
        source_sha256=D[0],
        authorization_reference="authorization://eval/source-snapshot",
    )
    corpus = CorpusProvenance(
        corpus_snapshot_id="corpus-v1",
        corpus_content_sha256=D[2],
        source_material="complete_fulltext",
        source_completeness="complete",
        index_snapshot_id="index-v1",
        index_content_sha256=D[3],
        index_not_applicable_reason=None,
        gold_labels_indexed=False,
        previous_answers_indexed=False,
        evidence_references=("artifact://corpus/provenance",),
    )

    def build(variant: VariantConfiguration) -> EvaluationOptions:
        return EvaluationOptions(
            record_type="evaluation_options",
            schema_version=SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            experiment_manifest=manifest,
            selected_variant_fingerprint=variant.variant_fingerprint,
            execution_profile=ExecutionProfile.PRODUCTION,
            execution_path=path,
            components=_components(variant),
            source_access_grants=(grant,),
            corpus=corpus,
            namespaces=namespaces,
            initial_state=state,
            budget_limits=budget,
            code_identity=code,
            environment=environment,
        )

    return build(baseline_variant), build(candidate_variant)


def _identity(options: EvaluationOptions) -> ComparisonIdentity:
    return next(
        item
        for item in options.experiment_manifest.comparison_identities
        if item.variant_fingerprint == options.selected_variant_fingerprint
    )


def _score(options: EvaluationOptions, value: float, run_id: str) -> EvaluationScore:
    provenance = {"source": "saved-production-shaped-score", "run": run_id}
    return EvaluationScore(
        record_type="score",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id="dataset-1",
        dataset_version="dataset-v1",
        dataset_digest=D[0],
        run_id=run_id,
        case_id="case-1",
        attempt_id=f"{run_id}-attempt-1",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.PRODUCTION,
        variant_fingerprint=options.selected_variant_fingerprint,
        repetition=1,
        scope="full_task",
        scoring_revision="score-v1",
        metric_id="answer.quality",
        version="1.0",
        value=value,
        status=ScoreStatus.OK,
        numerator=None,
        denominator=None,
        source=ScoreSource.RULE,
        provenance=provenance,
        provenance_digest=content_digest(provenance),
        evidence=(f"artifact://scores/{run_id}",),
        reason=None,
    )


def _evidence(
    options: EvaluationOptions | None,
    score: EvaluationScore,
    *,
    comparison_identity: ComparisonIdentity | None = None,
) -> QualityEvidence:
    identity = comparison_identity or (_identity(options) if options is not None else ComparisonIdentity.create(
        dataset_digest=score.dataset_digest,
        case_scope_fingerprint=content_digest(("case-1",)),
        corpus_content_sha256=D[2],
        track=score.track,
        execution_profile=score.execution_profile,
        variant_fingerprint=score.variant_fingerprint,
        code_fingerprint=D[6],
        environment_fingerprint=D[7],
        budget_fingerprint=D[5],
        initial_state_fingerprint=D[4],
        namespace_fingerprint=D[3],
        repetition_plan=(1,),
        timing_boundary="request including retries and Harness gates; scoring excluded",
    ))
    return QualityEvidence(
        execution_status=RunExecutionStatus.COMPLETED,
        comparison_identity=identity,
        evaluation_options=options,
        evaluation_run=None,
        planned_score_slots=(ScoreSlot.from_score(score, applicable=True),),
        scores=(score,),
        end_to_end_summary=None,
        stages=(),
        review_coverage=(),
        case_paper_families=(CasePaperFamily(case_id="case-1", paper_family_id="family-1"),),
        uncertainty=(),
    )


def _registry() -> MetricRegistry:
    return MetricRegistry(
        (
            MetricDefinition(
                metric_id="answer.quality",
                version="1.0",
                required_inputs=frozenset({"answer"}),
                applicable_tracks=frozenset({EvaluationTrack.END_TO_END}),
                applicable_execution_profiles=frozenset({ExecutionProfile.PRODUCTION}),
                applicable_scopes=frozenset({"full_task"}),
                granularity=MetricGranularity.ATTEMPT,
                direction=MetricDirection.HIGHER_IS_BETTER,
                domain=NumericDomain(minimum=0.0, maximum=1.0),
                value_kind=MetricValueKind.SCALAR,
                aggregation=MetricAggregation.MEAN,
                missing_policy=MissingValuePolicy.ERROR,
            ),
        )
    )


def _policy() -> QualityPolicy:
    rule = MetricThresholdRule(
        rule_id="answer-quality",
        rule_version="1.0",
        applicable=True,
        metric_id="answer.quality",
        metric_version="1.0",
        scoring_revision="score-v1",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.PRODUCTION,
        scope="full_task",
        evidence_source=MetricEvidenceSource.SCORE,
        aggregation=MetricAggregation.MEAN,
        direction=MetricDirection.HIGHER_IS_BETTER,
        threshold_basis=ThresholdBasis.POINT_ESTIMATE,
        min=0.7,
        max=None,
        delta_min=0.0,
        delta_max=None,
        min_coverage=1.0,
        min_independent_cases=1,
        min_paper_families=1,
    )
    content = {
        "record_type": "quality_policy",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "policy_id": "typed-comparison-policy",
        "policy_version": "2026-10-14",
        "scoring_revision": "score-v1",
        "allowed_comparison_differences": frozenset({ComparisonField.VARIANT_FINGERPRINT}),
        "rules": (rule,),
    }
    digest = QualityPolicy.calculate_policy_digest(**content)
    approval = HumanApprovalReference(
        policy_id="typed-comparison-policy",
        policy_version="2026-10-14",
        scoring_revision="score-v1",
        reviewer_id="quality-owner",
        reviewer_kind="human",
        approved_at=NOW,
        approval_reference="artifact://approvals/typed-comparison-policy",
        approval_content_sha256=D[8],
        approved_policy_digest=digest,
    )
    return QualityPolicy.create(**content, approval=approval)


def _comparison(baseline: QualityEvidence, candidate: QualityEvidence) -> ComparisonEvidence:
    return ComparisonEvidence(
        baseline_identity=baseline.comparison_identity,
        candidate_identity=candidate.comparison_identity,
        allowed_differences=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
        paired_case_ids=("case-1",),
        evidence_references=("artifact://comparisons/typed-quality",),
    )


def test_quality_gate_uses_real_typed_comparison_for_delta() -> None:
    baseline_options, candidate_options = _options_pair()
    baseline = _evidence(baseline_options, _score(baseline_options, 0.75, "baseline-run"))
    candidate = _evidence(candidate_options, _score(candidate_options, 0.80, "candidate-run"))

    result = evaluate_quality_policy(
        policy=_policy(),
        registry=_registry(),
        candidate=candidate,
        baseline=baseline,
        comparison=_comparison(baseline, candidate),
    )

    assert result.decision is QualificationDecision.PASS
    assert result.comparison_status is ComparisonStatus.COMPARABLE
    assert result.rule_evaluations[0].delta == pytest.approx(0.05)
    assert result.production_qualification_eligible is False
    assert result.qualification_scope is QualificationScope.LOCAL_POLICY
    assert result.production_qualification_missing_requirements == ("production_evaluation_run",)


def test_generator_prompt_change_is_incomparable_even_when_scalar_identity_difference_is_allowed() -> None:
    baseline_options, candidate_options = _options_pair(candidate_prompt_hash=D[7])
    baseline = _evidence(baseline_options, _score(baseline_options, 0.75, "baseline-run"))
    candidate = _evidence(candidate_options, _score(candidate_options, 0.80, "candidate-run"))

    result = evaluate_quality_policy(
        policy=_policy(),
        registry=_registry(),
        candidate=candidate,
        baseline=baseline,
        comparison=_comparison(baseline, candidate),
    )

    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert result.comparison_status is ComparisonStatus.INCOMPARABLE
    assert any("generation" in error for error in result.errors)
    assert result.production_qualification_eligible is False


def test_identity_only_comparison_without_typed_options_is_inconclusive() -> None:
    baseline_options, candidate_options = _options_pair()
    baseline_score = _score(baseline_options, 0.75, "baseline-run")
    candidate_score = _score(candidate_options, 0.80, "candidate-run")
    baseline = _evidence(
        None, baseline_score, comparison_identity=_identity(baseline_options)
    )
    candidate = _evidence(
        None, candidate_score, comparison_identity=_identity(candidate_options)
    )

    result = evaluate_quality_policy(
        policy=_policy(),
        registry=_registry(),
        candidate=candidate,
        baseline=baseline,
        comparison=_comparison(baseline, candidate),
    )

    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert result.comparison_status is ComparisonStatus.MISSING
    assert "typed_evaluation_options" in result.errors
