from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_common import PROTOCOL_VERSION, SCHEMA_VERSION, content_digest
from backend.research.rag.evaluation.protocol_dataset import QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_execution import (
    ArtifactReference,
    AttemptIdentity,
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
    EvaluationPreflightError,
    EvaluationRun,
    EvaluationTrack,
    ExecutionEnvironment,
    ExecutionPath,
    ExecutionPathKind,
    ExecutionProfile,
    ExperimentManifest,
    ExperimentPurpose,
    FixtureKind,
    GenerationFactors,
    HarnessFactors,
    InitialState,
    InitialStatePolicy,
    IsolatedNamespaces,
    PlannedEvaluationUnit,
    QualificationDecision,
    RetrievalFactors,
    RunControl,
    RunExecutionStatus,
    SourceAccessGrant,
    VariantConfiguration,
)

D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
D4 = "4" * 64
D5 = "5" * 64
NOW = datetime(2026, 10, 13, tzinfo=UTC)


def _scope(*, version: str = "source-v1") -> QuestionScope:
    return QuestionScope(
        sources=(ScopedPaper(paper_id="paper-1", document_version=version, source_sha256=D1),)
    )


def _scope_projection(case_id: str = "case-1", *, version: str = "source-v1") -> CaseScopeProjection:
    return CaseScopeProjection.create(case_id=case_id, scope=_scope(version=version))


def _grant(*, version: str = "source-v1") -> SourceAccessGrant:
    return SourceAccessGrant(
        paper_id="paper-1",
        document_version=version,
        source_sha256=D1,
        authorization_reference="authorization://eval/source-snapshot",
    )


def _budget(**overrides: Any) -> BudgetLimits:
    payload: dict[str, Any] = {
        "max_requests": 20,
        "max_total_tokens": 50_000,
        "max_cost": None,
        "currency": None,
        "unknown_price": True,
        "unknown_price_reason": "provider price was not available at freeze time",
        "price_basis_reference": None,
        "request_timeout_seconds": 30.0,
        "case_deadline_seconds": 120.0,
        "run_deadline_seconds": 600.0,
        "max_concurrency": 2,
        "max_retries": 1,
        "max_replans": 1,
        "max_turns": 6,
    }
    payload.update(overrides)
    return BudgetLimits(**payload)


def _code(*, dirty: bool = False, patches: tuple[str, ...] = ()) -> CodeIdentity:
    return CodeIdentity.create(
        git_revision="a585f399",
        workspace_content_fingerprint=D2,
        mutable_patch_fingerprints=patches,
        dirty=dirty,
    )


def _retrieval() -> RetrievalFactors:
    return RetrievalFactors(
        parser_config_hash=D1,
        chunker_config_hash=D2,
        embedding_config_hash=D3,
        index_config_hash=D4,
        retriever_config_hash=D5,
        reranker_config_hash=None,
        reranker_not_applicable_reason="reranking disabled by this variant",
        context_policy_hash="6" * 64,
    )


def _generation(*, prompt_hash: str = D2) -> GenerationFactors:
    return GenerationFactors(
        generator_config_hash=D1,
        prompt_hash=prompt_hash,
        output_contract_hash=D3,
        decoding_config_hash=D4,
        context_policy_hash=D5,
        max_input_tokens=8_000,
        max_output_tokens=1_000,
        request_timeout_seconds=30.0,
        max_retries=1,
    )


def _harness() -> HarnessFactors:
    return HarnessFactors(
        application_service="ResearchApplicationService.rag_ask",
        harness_config_hash=D1,
        deterministic_gate_config_hash=D2,
        gated=True,
        plan_execute_verify=True,
    )


def _variant(track: EvaluationTrack) -> VariantConfiguration:
    if track is EvaluationTrack.RETRIEVAL:
        return VariantConfiguration.create(
            variant_id="retrieval-v1",
            track=track,
            retrieval=_retrieval(),
            retrieval_not_applicable_reason=None,
            generation=None,
            generation_not_applicable_reason="track A does not call an answer model",
            harness=None,
            harness_not_applicable_reason="track A does not run answer Harness",
        )
    if track is EvaluationTrack.ORACLE_GENERATION:
        return VariantConfiguration.create(
            variant_id="oracle-v1",
            track=track,
            retrieval=None,
            retrieval_not_applicable_reason="track B receives reviewed source evidence directly",
            generation=_generation(),
            generation_not_applicable_reason=None,
            harness=None,
            harness_not_applicable_reason="track B uses the answer worker without product routing",
        )
    return VariantConfiguration.create(
        variant_id="end-to-end-v1",
        track=track,
        retrieval=_retrieval(),
        retrieval_not_applicable_reason=None,
        generation=_generation(),
        generation_not_applicable_reason=None,
        harness=_harness(),
        harness_not_applicable_reason=None,
    )


def _environment(*, runtime: str = "python-3.12") -> ExecutionEnvironment:
    return ExecutionEnvironment(
        runtime=runtime,
        dependency_lock_digest=D5,
        hardware_class=None,
        hardware_identity_unavailable_reason="fixture does not record hardware",
    )


def _comparison(
    variant: VariantConfiguration,
    profile: ExecutionProfile,
    *,
    code: CodeIdentity | None = None,
    budget: BudgetLimits | None = None,
    environment: ExecutionEnvironment | None = None,
) -> ComparisonIdentity:
    code = code or _code()
    budget = budget or _budget()
    namespaces = _namespaces()
    state = _initial_state()
    environment = environment or _environment()
    return ComparisonIdentity.create(
        dataset_digest=D1,
        case_scope_fingerprint=content_digest((_scope_projection(),)),
        corpus_content_sha256=D3,
        track=variant.track,
        execution_profile=profile,
        variant_fingerprint=variant.variant_fingerprint,
        code_fingerprint=code.code_fingerprint,
        environment_fingerprint=environment.fingerprint,
        budget_fingerprint=budget.fingerprint,
        initial_state_fingerprint=state.fingerprint,
        namespace_fingerprint=namespaces.fingerprint,
        repetition_plan=(1,),
        timing_boundary="request includes retries and Harness gates; excludes indexing and scoring",
    )


def _manifest(
    track: EvaluationTrack,
    profile: ExecutionProfile,
    *,
    variant: VariantConfiguration | None = None,
    planned_units: tuple[PlannedEvaluationUnit, ...] | None = None,
    case_scopes: tuple[CaseScopeProjection, ...] | None = None,
    code: CodeIdentity | None = None,
    budget: BudgetLimits | None = None,
    environment: ExecutionEnvironment | None = None,
    purpose: ExperimentPurpose = ExperimentPurpose.DEV_DIAGNOSTIC,
) -> ExperimentManifest:
    variant = variant or _variant(track)
    case_scopes = case_scopes or (_scope_projection(),)
    units = planned_units or (
        PlannedEvaluationUnit(
            dataset_version="dataset-v1",
            case_id="case-1",
            track=track,
            variant_fingerprint=variant.variant_fingerprint,
            repetition=1,
        ),
    )
    return ExperimentManifest.create(
        record_type="experiment_manifest",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        experiment_id="experiment-1",
        experiment_purpose=purpose,
        dataset_id="dataset-1",
        dataset_version="dataset-v1",
        dataset_digest=D1,
        case_scopes=case_scopes,
        variants=(variant,),
        planned_units=units,
        comparison_identities=(_comparison(variant, profile, code=code, budget=budget, environment=environment),),
        allowed_differences=("reranker",),
    )


def _namespaces() -> IsolatedNamespaces:
    return IsolatedNamespaces(
        tenant="eval-tenant-1",
        index="eval-index-1",
        memory="eval-memory-1",
        artifact="eval-artifacts-1",
    )


def _initial_state() -> InitialState:
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


def _component(
    role: ComponentRole,
    classification: ComponentClassification,
    *,
    adapter: str | None = None,
    fixture_kind: FixtureKind | None = None,
) -> ComponentProvenance:
    if classification is ComponentClassification.NOT_APPLICABLE:
        return ComponentProvenance(
            role=role,
            classification=classification,
            adapter=None,
            provider=None,
            model=None,
            revision=None,
            identity_unavailable_reason=None,
            fixture_kind=None,
            not_applicable_reason=f"{role.value} is not used by this track",
            config_hash=None,
            code_revision=None,
            evidence_references=(),
            runtime_provenance_digest=None,
        )
    return ComponentProvenance(
        role=role,
        classification=classification,
        adapter=adapter or f"{role.value}-adapter",
        provider="provider.example",
        model=f"{role.value}-model",
        revision="revision-v1",
        identity_unavailable_reason=None,
        fixture_kind=fixture_kind if classification is ComponentClassification.FIXTURE else None,
        not_applicable_reason=None,
        config_hash=D2,
        code_revision="component-commit-1",
        evidence_references=(f"artifact://runtime/{role.value}",),
        runtime_provenance_digest=D3,
    )


def _components(track: EvaluationTrack, classification: ComponentClassification) -> tuple[ComponentProvenance, ...]:
    retrieval_roles = {
        ComponentRole.PARSER,
        ComponentRole.CHUNKER,
        ComponentRole.EMBEDDING,
        ComponentRole.INDEX,
        ComponentRole.RETRIEVER,
        ComponentRole.RERANKER,
    }
    answer_roles = {ComponentRole.GENERATOR, ComponentRole.ANSWER_WORKER, ComponentRole.HARNESS}
    values: list[ComponentProvenance] = []
    for role in ComponentRole:
        if role is ComponentRole.JUDGE:
            continue
        applicable = True
        if role is ComponentRole.RERANKER:
            applicable = False
        if track is EvaluationTrack.RETRIEVAL and role in answer_roles:
            applicable = False
        if track is EvaluationTrack.ORACLE_GENERATION and (role in retrieval_roles or role is ComponentRole.HARNESS):
            applicable = False
        values.append(
            _component(
                role,
                classification if applicable else ComponentClassification.NOT_APPLICABLE,
                fixture_kind=FixtureKind.OTHER if classification is ComponentClassification.FIXTURE and applicable else None,
            )
        )
    return tuple(values)


def _graph_identity() -> ContextGraphRequestIdentity:
    return ContextGraphRequestIdentity(
        run_id="graph-run-1",
        graph_id="research-rag",
        graph_version="graph-v1",
        graph_ref="research-rag@graph-v1",
        graph_checksum=D1,
        stage_id="answer-stage",
        stage_binding_checksum=D2,
        node_id="answer-node",
        node_instance_id="answer-node-instance-1",
        activity_id="answer-activity-1",
        activity_attempt=1,
    )


def _path(track: EvaluationTrack) -> ExecutionPath:
    if track is EvaluationTrack.RETRIEVAL:
        return ExecutionPath(
            kind=ExecutionPathKind.RETRIEVAL_APPLICATION,
            application_service="ResearchEvaluationPort.retrieve",
            retrieval_enabled=True,
            answer_generation_enabled=False,
            gated_harness_requested=False,
            context_graph_identity=None,
        )
    if track is EvaluationTrack.ORACLE_GENERATION:
        return ExecutionPath(
            kind=ExecutionPathKind.ORACLE_ANSWER_WORKER,
            application_service="ResearchEvaluationPort.answer_from_evidence",
            retrieval_enabled=False,
            answer_generation_enabled=True,
            gated_harness_requested=False,
            context_graph_identity=None,
        )
    return ExecutionPath(
        kind=ExecutionPathKind.RESEARCH_GATED_HARNESS,
        application_service="ResearchApplicationService.rag_ask",
        retrieval_enabled=True,
        answer_generation_enabled=True,
        gated_harness_requested=True,
        context_graph_identity=_graph_identity(),
    )


def _options(
    track: EvaluationTrack,
    profile: ExecutionProfile,
    *,
    components: tuple[ComponentProvenance, ...] | None = None,
    path: ExecutionPath | None = None,
    budget: BudgetLimits | None = None,
    code: CodeIdentity | None = None,
    purpose: ExperimentPurpose = ExperimentPurpose.DEV_DIAGNOSTIC,
) -> EvaluationOptions:
    budget = budget or _budget()
    code = code or _code()
    environment = _environment()
    manifest = _manifest(
        track, profile, budget=budget, code=code, environment=environment, purpose=purpose
    )
    return EvaluationOptions(
        record_type="evaluation_options",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        experiment_manifest=manifest,
        selected_variant_fingerprint=manifest.variants[0].variant_fingerprint,
        execution_profile=profile,
        execution_path=path or _path(track),
        components=components or _components(track, ComponentClassification.FIXTURE),
        source_access_grants=(_grant(),),
        corpus=CorpusProvenance(
            corpus_snapshot_id="corpus-v1",
            corpus_content_sha256=D3,
            source_material="complete_fulltext" if profile is ExecutionProfile.PRODUCTION else "reduced_fixture",
            source_completeness="complete" if profile is ExecutionProfile.PRODUCTION else "incomplete",
            index_snapshot_id=None if track is EvaluationTrack.ORACLE_GENERATION else "index-v1",
            index_content_sha256=None if track is EvaluationTrack.ORACLE_GENERATION else D4,
            index_not_applicable_reason=(
                "oracle generation does not use an index" if track is EvaluationTrack.ORACLE_GENERATION else None
            ),
            gold_labels_indexed=False,
            previous_answers_indexed=False,
            evidence_references=("artifact://corpus/provenance",),
        ),
        namespaces=_namespaces(),
        initial_state=_initial_state(),
        budget_limits=budget,
        code_identity=code,
        environment=environment,
    )


def test_versions_and_required_budget_are_strict_protocol_inputs() -> None:
    options = _options(EvaluationTrack.RETRIEVAL, ExecutionProfile.FIXTURE)
    payload = options.model_dump(mode="json")
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError, match="schema_version"):
        EvaluationOptions.model_validate(payload)

    payload = options.model_dump(mode="json")
    payload["protocol_version"] = "research-rag-eval/v2"
    with pytest.raises(ValidationError, match="protocol_version"):
        EvaluationOptions.model_validate(payload)

    payload = options.model_dump(mode="json")
    del payload["budget_limits"]
    with pytest.raises(ValidationError, match="budget_limits"):
        EvaluationOptions.model_validate(payload)

    with pytest.raises(ValidationError, match="latest"):
        _scope_projection(version="latest")


def test_budget_rejects_bool_non_finite_negative_and_fake_zero_price() -> None:
    for field, value in (
        ("max_requests", True),
        ("max_total_tokens", -1),
        ("request_timeout_seconds", float("inf")),
        ("max_cost", float("nan")),
        ("max_concurrency", False),
    ):
        with pytest.raises(ValidationError):
            _budget(**{field: value})

    with pytest.raises(ValidationError, match="fabricated cost"):
        _budget(max_cost=0.0)

    budget = _budget()
    assert budget.max_cost is None
    assert budget.unknown_price is True
    assert budget.max_requests == 20
    assert budget.max_total_tokens == 50_000


def test_production_preflight_rejects_hash_embedding_even_with_real_generator() -> None:
    components = list(_components(EvaluationTrack.END_TO_END, ComponentClassification.PRODUCTION))
    embedding_index = next(i for i, item in enumerate(components) if item.role is ComponentRole.EMBEDDING)
    components[embedding_index] = _component(
        ComponentRole.EMBEDDING,
        ComponentClassification.FIXTURE,
        adapter="InMemoryHashEmbeddingAdapter",
        fixture_kind=FixtureKind.HASH_EMBEDDING,
    )
    options = _options(
        EvaluationTrack.END_TO_END,
        ExecutionProfile.PRODUCTION,
        components=tuple(components),
    )

    report = options.preflight()
    assert report.accepted is False
    assert report.paid_calls_allowed is False
    assert "fixture_component_in_production" in {item.code for item in report.findings}
    with pytest.raises(EvaluationPreflightError):
        options.assert_preflight()


def test_fixture_end_to_end_is_legal_but_not_production_identity() -> None:
    options = _options(EvaluationTrack.END_TO_END, ExecutionProfile.FIXTURE)
    report = options.assert_preflight()

    assert report.accepted is True
    assert report.paid_calls_allowed is True
    assert all(item.classification is not ComponentClassification.PRODUCTION for item in options.components if item.classification is not ComponentClassification.NOT_APPLICABLE)


def test_oracle_requires_explicit_not_applicable_retrieval_components() -> None:
    options = _options(EvaluationTrack.ORACLE_GENERATION, ExecutionProfile.FIXTURE)
    report = options.assert_preflight()
    by_role = {item.role: item for item in options.components}

    assert report.accepted is True
    assert by_role[ComponentRole.EMBEDDING].classification is ComponentClassification.NOT_APPLICABLE
    assert by_role[ComponentRole.EMBEDDING].not_applicable_reason

    components = tuple(item for item in options.components if item.role is not ComponentRole.INDEX)
    invalid = options.model_copy(update={"components": components})
    report = invalid.preflight()
    assert "missing_component_provenance" in {item.code for item in report.findings}


def test_end_to_end_requires_gated_harness_request_and_physical_graph_activity() -> None:
    wrong_path = ExecutionPath(
        kind=ExecutionPathKind.RESEARCH_GATED_HARNESS,
        application_service="ResearchApplicationService.rag_ask",
        retrieval_enabled=True,
        answer_generation_enabled=True,
        gated_harness_requested=True,
        context_graph_identity=None,
    )
    options = _options(
        EvaluationTrack.END_TO_END,
        ExecutionProfile.FIXTURE,
        path=wrong_path,
    )
    assert "end_to_end_not_gated" in {item.code for item in options.preflight().findings}

    with pytest.raises(ValidationError, match="activity_attempt"):
        ContextGraphRequestIdentity(
            **{**_graph_identity().model_dump(), "activity_attempt": 0}
        )


def test_initial_state_and_namespace_reject_production_memory_or_contamination() -> None:
    with pytest.raises(ValidationError, match="production business memory"):
        IsolatedNamespaces(
            tenant="eval-tenant",
            index="eval-index",
            memory="production-memory",
            artifact="eval-artifact",
        )

    with pytest.raises(ValidationError, match="gold_labels_present"):
        InitialState.model_validate(
            {**_initial_state().model_dump(mode="json"), "gold_labels_present": True}
        )

    assert _initial_state().source_fulltext_access == "read_only"


def test_code_identity_allows_production_dev_but_qualification_requires_committed_code() -> None:
    clean = _code()
    dirty = _code(dirty=True, patches=(D4,))
    changed = _code(dirty=True, patches=(D5,))

    assert clean.code_fingerprint != dirty.code_fingerprint
    assert dirty.code_fingerprint != changed.code_fingerprint
    with pytest.raises(ValidationError, match="dirty"):
        _code(dirty=False, patches=(D4,))

    components = _components(EvaluationTrack.END_TO_END, ComponentClassification.PRODUCTION)
    dev = _options(
        EvaluationTrack.END_TO_END,
        ExecutionProfile.PRODUCTION,
        components=components,
        code=dirty,
        purpose=ExperimentPurpose.DEV_DIAGNOSTIC,
    )
    dev_report = dev.assert_preflight()
    assert dev_report.accepted is True
    assert dev_report.release_evidence_eligible is False

    heldout = _options(
        EvaluationTrack.END_TO_END,
        ExecutionProfile.PRODUCTION,
        components=components,
        code=dirty,
        purpose=ExperimentPurpose.HELDOUT_QUALIFICATION,
    )
    heldout_report = heldout.preflight()
    assert heldout_report.accepted is False
    assert heldout_report.release_evidence_eligible is False
    assert "qualification_code_has_mutable_patches" in {item.code for item in heldout_report.findings}

    clean_release = _options(
        EvaluationTrack.END_TO_END,
        ExecutionProfile.PRODUCTION,
        components=components,
        code=clean,
        purpose=ExperimentPurpose.RELEASE_QUALIFICATION,
    )
    assert clean_release.assert_preflight().release_evidence_eligible is True


def test_options_reject_stale_comparison_after_budget_or_environment_change() -> None:
    options = _options(EvaluationTrack.END_TO_END, ExecutionProfile.FIXTURE)
    values = {name: getattr(options, name) for name in EvaluationOptions.model_fields}

    with pytest.raises(ValidationError, match="budget_fingerprint"):
        EvaluationOptions(**{**values, "budget_limits": _budget(max_requests=21)})

    with pytest.raises(ValidationError, match="environment_fingerprint"):
        EvaluationOptions(**{**values, "environment": _environment(runtime="python-3.13")})

    with pytest.raises(ValidationError, match="code_fingerprint"):
        EvaluationOptions(
            **{**values, "code_identity": _code(dirty=True, patches=(D4,))}
        )

    changed_state = InitialState(
        memory_policy=InitialStatePolicy.RESET_TO_SNAPSHOT,
        memory_snapshot_digest=D4,
        cache_policy=CachePolicy.COLD,
        cache_snapshot_digest=None,
        gold_labels_present=False,
        prior_run_answers_present=False,
        production_business_memory_mounted=False,
        source_fulltext_access="read_only",
    )
    with pytest.raises(ValidationError, match="initial_state_fingerprint"):
        EvaluationOptions(**{**values, "initial_state": changed_state})


def test_oracle_variant_fingerprint_excludes_retrieval_factors() -> None:
    one = _variant(EvaluationTrack.ORACLE_GENERATION)
    two = VariantConfiguration.create(
        variant_id="another-display-name",
        track=EvaluationTrack.ORACLE_GENERATION,
        retrieval=None,
        retrieval_not_applicable_reason="different prose is not an execution factor",
        generation=_generation(),
        generation_not_applicable_reason=None,
        harness=None,
        harness_not_applicable_reason="not used",
    )
    changed_generation = VariantConfiguration.create(
        variant_id="oracle-v2",
        track=EvaluationTrack.ORACLE_GENERATION,
        retrieval=None,
        retrieval_not_applicable_reason="no retrieval",
        generation=_generation(prompt_hash=D5),
        generation_not_applicable_reason=None,
        harness=None,
        harness_not_applicable_reason="not used",
    )

    assert one.variant_fingerprint == two.variant_fingerprint
    assert one.variant_fingerprint != changed_generation.variant_fingerprint
    with pytest.raises(ValidationError, match="retrieval factors are forbidden"):
        VariantConfiguration.create(
            variant_id="confounded-oracle",
            track=EvaluationTrack.ORACLE_GENERATION,
            retrieval=_retrieval(),
            retrieval_not_applicable_reason=None,
            generation=_generation(),
            generation_not_applicable_reason=None,
            harness=None,
            harness_not_applicable_reason="not used",
        )


def test_comparison_identity_contains_content_not_machine_path_or_secret() -> None:
    comparison = _comparison(_variant(EvaluationTrack.RETRIEVAL), ExecutionProfile.FIXTURE)
    assert "path" not in ComparisonIdentity.model_fields
    assert "credential" not in ComparisonIdentity.model_fields
    with pytest.raises(ValidationError):
        ComparisonIdentity.model_validate(
            {**comparison.model_dump(mode="json"), "machine_path": "F:/secret/index", "api_key": "secret"}
        )
    with pytest.raises(ValidationError, match="local path"):
        ArtifactReference(
            reference_id="artifact-1",
            kind="trace",
            uri="F:/machine/run.json",
            content_sha256=D1,
        )


def test_manifest_and_run_reject_duplicate_units_attempts_and_references() -> None:
    variant = _variant(EvaluationTrack.RETRIEVAL)
    unit = PlannedEvaluationUnit(
        dataset_version="dataset-v1",
        case_id="case-1",
        track=EvaluationTrack.RETRIEVAL,
        variant_fingerprint=variant.variant_fingerprint,
        repetition=1,
    )
    with pytest.raises(ValidationError, match="duplicate formal unit"):
        _manifest(
            EvaluationTrack.RETRIEVAL,
            ExecutionProfile.FIXTURE,
            variant=variant,
            planned_units=(unit, unit),
        )

    options = _options(EvaluationTrack.RETRIEVAL, ExecutionProfile.FIXTURE)
    manifest = options.experiment_manifest
    attempt = AttemptIdentity(
        attempt_id="attempt-1",
        unit=manifest.planned_units[0],
        sequence=1,
        external_request_id=None,
    )
    reference = ArtifactReference(
        reference_id="trace-1",
        kind="trace",
        uri="artifact://run/trace-1",
        content_sha256=D4,
    )
    run_payload = {
        "record_type": "run",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "run_id": "run-1",
        "experiment_id": manifest.experiment_id,
        "experiment_purpose": manifest.experiment_purpose,
        "experiment_manifest_digest": manifest.manifest_digest,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "dataset_digest": manifest.dataset_digest,
        "corpus_snapshot_id": options.corpus.corpus_snapshot_id,
        "corpus_content_sha256": options.corpus.corpus_content_sha256,
        "index_snapshot_id": options.corpus.index_snapshot_id,
        "index_not_applicable_reason": options.corpus.index_not_applicable_reason,
        "track": EvaluationTrack.RETRIEVAL,
        "execution_profile": ExecutionProfile.FIXTURE,
        "selected_variant_fingerprint": options.selected_variant_fingerprint,
        "options_fingerprint": options.preflight().options_fingerprint,
        "code_identity": options.code_identity,
        "environment": options.environment,
        "execution_path": options.execution_path,
        "variant_configuration": manifest.variants[0],
        "comparison_identity": manifest.comparison_identities[0],
        "components": options.components,
        "case_scopes": manifest.case_scopes,
        "source_access_grants": options.source_access_grants,
        "budget_limits": options.budget_limits,
        "namespaces": options.namespaces,
        "initial_state": options.initial_state,
        "planned_units": manifest.planned_units,
        "attempts": (attempt,),
        "artifact_references": (reference,),
        "transcript_references": (),
        "started_at": NOW,
        "completed_at": NOW,
        "execution_status": RunExecutionStatus.COMPLETED,
        "qualification": QualificationDecision.INCONCLUSIVE,
        "qualification_reason": "execution completed but required scoring/review is pending",
        "qualification_evidence_references": (),
    }
    run = EvaluationRun(**run_payload)
    assert run.execution_status is RunExecutionStatus.COMPLETED
    assert run.qualification is QualificationDecision.INCONCLUSIVE

    with pytest.raises(ValidationError, match="attempts.attempt_id"):
        EvaluationRun(**{**run_payload, "attempts": (attempt, attempt)})
    with pytest.raises(ValidationError, match="same content reference"):
        EvaluationRun(
            **{
                **run_payload,
                "transcript_references": (
                    ArtifactReference(
                        reference_id="trace-2",
                        kind="trace",
                        uri=reference.uri,
                        content_sha256=reference.content_sha256,
                    ),
                ),
            }
        )
    with pytest.raises(ValidationError, match="independent scoring/review evidence"):
        EvaluationRun(**{**run_payload, "qualification": QualificationDecision.PASS})


def test_run_control_is_typed_and_forbids_gold_paths_callbacks_or_filters() -> None:
    options = _options(EvaluationTrack.RETRIEVAL, ExecutionProfile.FIXTURE)
    control = RunControl(
        record_type="run_control",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        run_id="run-1",
        unit=options.experiment_manifest.planned_units[0],
        attempt_id="attempt-1",
        options_fingerprint=options.preflight().options_fingerprint,
        budget_fingerprint=options.budget_limits.fingerprint,
        namespace_fingerprint=options.namespaces.fingerprint,
        case_scope=options.experiment_manifest.case_scopes[0],
        namespaces=options.namespaces,
        budget_limits=options.budget_limits,
        source_access_grants=options.source_access_grants,
    )
    assert control.case_scope.scope == _scope()
    with pytest.raises(ValidationError, match="budget_fingerprint"):
        RunControl(**{**{name: getattr(control, name) for name in RunControl.model_fields}, "budget_limits": _budget(max_requests=21)})
    for forbidden in ("reference_answer", "gold_path", "callback", "metadata", "filter"):
        with pytest.raises(ValidationError):
            RunControl.model_validate(
                {**control.model_dump(mode="json"), forbidden: "forbidden"}
            )
