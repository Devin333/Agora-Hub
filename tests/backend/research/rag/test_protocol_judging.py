from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from backend.research.ports.evaluation_judge import (
    CandidateUsage,
    CitationBinding,
    CitationLocation,
    CitationSupportJudgeRequest,
    FactualJudgeRequest,
    FaithfulnessJudgeRequest,
    JudgeBudget,
    JudgeCapability,
    JudgeMetric,
    JudgeModality,
    JudgeObservationStatus,
    JudgeRuntimeProfile,
    JudgeScope,
    JudgeUsageSummary,
    SemanticJudgeObservation,
    TextContext,
    AnswerSpan,
)
from backend.research.rag.evaluation.protocol_common import (
    EvaluationTrack,
    ExecutionProfile,
    content_digest,
)
from backend.research.rag.evaluation.protocol_judging import (
    CITATION_SUPPORT_METRIC_VERSION,
    RAGAS_SEMANTIC_METRIC_VERSION,
    JudgeScoreIdentity,
    build_semantic_judge_registry,
    collect_independent_judge_observations,
    observation_to_evaluation_score,
)
from backend.research.rag.evaluation.protocol_scoring import ScoreStatus

D1 = "1" * 64
D2 = "2" * 64


def _budget() -> JudgeBudget:
    return JudgeBudget(
        max_requests=6,
        max_total_tokens=4_000,
        max_output_tokens_per_request=500,
        max_cost=None,
        currency=None,
        unknown_price_reason="fixture provider has no price",
        request_timeout_seconds=2.0,
        deadline_seconds=10.0,
        max_retries_per_subrequest=1,
    )


def _common(metric: JudgeMetric, version: str) -> dict[str, Any]:
    return {
        "observation_id": f"observation-{metric.value}",
        "metric": metric,
        "metric_version": version,
        "execution_profile": JudgeRuntimeProfile.FIXTURE,
        "scope": (
            JudgeScope.REFERENCE_COMPARISON
            if metric in {JudgeMetric.FACT_PRECISION, JudgeMetric.FACT_RECALL, JudgeMetric.FACT_F1}
            else JudgeScope.TEXT_SUBSET
        ),
        "required_modalities": frozenset({JudgeModality.TEXT}),
        "budget": _budget(),
        "shared_budget_key": None,
        "generation_parameters": {"temperature": 0},
        "evidence_references": ("artifact://answer", "artifact://judge-input"),
    }


def _fact_request(metric: JudgeMetric = JudgeMetric.FACT_PRECISION) -> FactualJudgeRequest:
    response = "The method improves accuracy."
    reference = "The paper reports that the method improves accuracy."
    return FactualJudgeRequest(
        **_common(metric, RAGAS_SEMANTIC_METRIC_VERSION),
        response=response,
        reference=reference,
        response_digest=content_digest(response),
        reference_digest=content_digest(reference),
        atomicity="high",
        coverage="high",
    )


def _faith_request() -> FaithfulnessJudgeRequest:
    question = "What improved?"
    response = "The method improves accuracy."
    context = "The paper reports that the method improves accuracy."
    return FaithfulnessJudgeRequest(
        **_common(JudgeMetric.FAITHFULNESS, RAGAS_SEMANTIC_METRIC_VERSION),
        user_input=question,
        response=response,
        actual_contexts=(
            TextContext(
                context_id="actual-context-1",
                text=context,
                text_digest=content_digest(context),
            ),
        ),
        user_input_digest=content_digest(question),
        response_digest=content_digest(response),
    )


def _citation_request() -> CitationSupportJudgeRequest:
    exact = "Accuracy increased from 80% to 85% on Dataset A."
    binding = CitationBinding(
        claim_id="claim-1",
        claim_text="Accuracy increased to 85% on Dataset A.",
        answer_span=AnswerSpan(start=0, end=46, text="Accuracy increased to 85% on Dataset A."),
        citation=CitationLocation(
            source_id="paper-1",
            anchor_id="page-4:paragraph-2",
            start=120,
            end=170,
            quoted_text=exact,
            quoted_text_digest=content_digest(exact),
        ),
    )
    rubric = "Support requires the same metric, value, dataset, and experimental condition."
    return CitationSupportJudgeRequest(
        **_common(JudgeMetric.CITATION_SUPPORT, CITATION_SUPPORT_METRIC_VERSION),
        binding=binding,
        binding_digest=content_digest(binding),
        rubric=rubric,
        rubric_version="citation-rubric-v1",
        rubric_digest=content_digest(rubric),
    )


def _observation(
    request: Any,
    value: float,
    numerator: int | None,
    denominator: int | None,
) -> SemanticJudgeObservation:
    return SemanticJudgeObservation(
        observation_id=request.observation_id,
        metric=request.metric,
        metric_version=request.metric_version,
        status=JudgeObservationStatus.OK,
        value=value,
        numerator=numerator,
        denominator=denominator,
        scope=request.scope,
        capability=JudgeCapability(
            modalities=frozenset({JudgeModality.TEXT}),
            scopes=frozenset({JudgeScope.TEXT_SUBSET, JudgeScope.REFERENCE_COMPARISON}),
        ),
        execution_profile=request.execution_profile,
        provider="fixture-provider",
        model_id="fixture-model",
        model_revision=None,
        model_revision_unavailable_reason="fixture model has no provider revision",
        adapter_id="fixture.semantic-judge",
        adapter_version="1.0.0",
        library_name="fixture",
        library_version="1.0.0",
        library_unavailable_reason=None,
        prompt_version="fixture-prompt-v1",
        rubric_version=None,
        rubric_digest=None,
        generation_parameters={"temperature": 0},
        input_digests={"input": D1},
        subrequests=(),
        raw_judgment={"fixture": True},
        reasons=("fixture observation for contract testing",),
        usage=JudgeUsageSummary(
            requests=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cost=None,
            currency=None,
            cost_unknown_reason="fixture provider has no price",
        ),
        evidence_references=request.evidence_references,
        error=None,
        shared_budget_hook_connected=False,
        scope_limitation="Fixture is text-only and cannot qualify visual requirements.",
    )


def test_metric_inputs_are_physically_separated_and_forbid_gold_context_leakage() -> None:
    fact = _fact_request()
    assert set(fact.model_dump()) >= {"response", "reference"}
    assert "actual_contexts" not in FactualJudgeRequest.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FactualJudgeRequest.model_validate(
            {**fact.model_dump(mode="json"), "actual_contexts": ["gold must not enter facts"]}
        )

    faith = _faith_request()
    assert [item.context_id for item in faith.actual_contexts] == ["actual-context-1"]
    assert "reference" not in FaithfulnessJudgeRequest.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FaithfulnessJudgeRequest.model_validate(
            {**faith.model_dump(mode="json"), "reference": "gold answer"}
        )


def test_citation_request_binds_claim_and_exact_location_not_arbitrary_retrieval() -> None:
    request = _citation_request()
    assert request.binding.claim_id == "claim-1"
    assert request.binding.citation.anchor_id == "page-4:paragraph-2"
    assert "retrieved_contexts" not in CitationSupportJudgeRequest.model_fields
    with pytest.raises(ValidationError, match="quoted_text_digest"):
        CitationSupportJudgeRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "binding": {
                    **request.binding.model_dump(mode="json"),
                    "citation": {
                        **request.binding.citation.model_dump(mode="json"),
                        "quoted_text": "another retrieval passage",
                    },
                },
            }
        )


def test_metric_registry_keeps_facts_faithfulness_and_citation_independent() -> None:
    registry = build_semantic_judge_registry()
    definitions = {(item.metric_id, item.version): item for item in registry.definitions}
    fact_definition = definitions[("fact_precision", RAGAS_SEMANTIC_METRIC_VERSION)]
    assert fact_definition.required_inputs == frozenset({"response", "reference"})
    assert fact_definition.applicable_scopes == frozenset({"reference_comparison"})
    assert definitions[("faithfulness", RAGAS_SEMANTIC_METRIC_VERSION)].required_inputs == frozenset(
        {"user_input", "response", "actual_contexts"}
    )
    assert definitions[("citation_support", CITATION_SUPPORT_METRIC_VERSION)].required_inputs == frozenset(
        {"claim_id", "claim_text", "answer_span", "citation_location_text"}
    )


class _FixtureJudge:
    @property
    def capability(self) -> JudgeCapability:
        return JudgeCapability(
            modalities=frozenset({JudgeModality.TEXT}),
            scopes=frozenset({JudgeScope.TEXT_SUBSET, JudgeScope.REFERENCE_COMPARISON}),
        )

    async def judge(self, request: Any) -> SemanticJudgeObservation:
        values = {
            JudgeMetric.FACT_PRECISION: (1.0, 1, 1),
            JudgeMetric.FAITHFULNESS: (0.0, 0, 1),
            JudgeMetric.CITATION_SUPPORT: (1.0, 1, 1),
        }
        return _observation(request, *values[request.metric])


def test_independent_observations_do_not_copy_fact_score_into_faithfulness() -> None:
    requests = (_fact_request(), _faith_request(), _citation_request())
    observations = asyncio.run(
        collect_independent_judge_observations(_FixtureJudge(), requests)
    )
    assert [item.metric for item in observations] == [
        JudgeMetric.FACT_PRECISION,
        JudgeMetric.FAITHFULNESS,
        JudgeMetric.CITATION_SUPPORT,
    ]
    assert [item.value for item in observations] == [1.0, 0.0, 1.0]


def test_observation_projects_through_shared_evaluation_score_registry() -> None:
    request = _faith_request()
    observation = _observation(request, 0.0, 0, 1)
    identity = JudgeScoreIdentity(
        dataset_id="dev",
        dataset_version="2026-10-14",
        dataset_digest=D1,
        run_id="run-1",
        case_id="case-1",
        attempt_id="attempt-1",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.FIXTURE,
        variant_fingerprint=D2,
        repetition=1,
        scope="text_subset",
        scoring_revision="judge-score-v1",
    )
    score = observation_to_evaluation_score(
        identity=identity,
        observation=observation,
        registry=build_semantic_judge_registry(),
    )
    assert score.metric_id == "faithfulness"
    assert score.status is ScoreStatus.OK
    assert (score.value, score.numerator, score.denominator) == (0.0, 0, 1)
    assert score.provenance["semantic_judge"]["raw_judgment"] == {"fixture": True}
    assert "qualification_limit" in score.provenance


def test_reference_comparison_needs_review_projects_without_global_fact_score() -> None:
    request = _fact_request()
    payload = _observation(request, 0.5, 1, 2).model_dump()
    payload.update(
        status=JudgeObservationStatus.NEEDS_REVIEW,
        value=None,
        numerator=None,
        denominator=None,
        raw_judgment={
            "ragas_metric_result": 0.5,
            "unsupported_reference_claims": ["A lawful extra fact absent from reference."],
            "reference_comparison_only": True,
        },
        reasons=(
            "claim absent from reference requires independent source review; it is not a factual contradiction",
        ),
    )
    observation = SemanticJudgeObservation(**payload)
    identity = JudgeScoreIdentity(
        dataset_id="dev",
        dataset_version="2026-10-14",
        dataset_digest=D1,
        run_id="run-1",
        case_id="case-1",
        attempt_id="attempt-1",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.FIXTURE,
        variant_fingerprint=D2,
        repetition=1,
        scope="reference_comparison",
        scoring_revision="judge-score-v1",
    )
    score = observation_to_evaluation_score(
        identity=identity,
        observation=observation,
        registry=build_semantic_judge_registry(),
    )
    assert score.status is ScoreStatus.NEEDS_REVIEW
    assert score.value is None
    assert score.provenance["semantic_judge"]["raw_judgment"]["ragas_metric_result"] == 0.5


def test_text_subset_identity_cannot_be_promoted_to_full_task() -> None:
    with pytest.raises(ValidationError, match="scope=text_subset or reference_comparison"):
        JudgeScoreIdentity(
            dataset_id="dev",
            dataset_version="2026-10-14",
            dataset_digest=D1,
            run_id="run-1",
            case_id="case-1",
            attempt_id="attempt-1",
            track=EvaluationTrack.END_TO_END,
            execution_profile=ExecutionProfile.FIXTURE,
            variant_fingerprint=D2,
            repetition=1,
            scope="full_task",
            scoring_revision="judge-score-v1",
        )


def test_empty_or_unknown_semantic_observation_cannot_be_vacuous_one() -> None:
    request = _faith_request()
    payload = _observation(request, 1.0, 1, 1).model_dump()
    payload.update(
        status=JudgeObservationStatus.NOT_APPLICABLE,
        value=None,
        numerator=None,
        denominator=None,
        reasons=("no_verifiable_claims",),
    )
    observation = SemanticJudgeObservation(**payload)
    assert observation.value is None
    with pytest.raises(ValidationError, match="non-ok observation requires null value"):
        SemanticJudgeObservation(**{**payload, "value": 1.0})