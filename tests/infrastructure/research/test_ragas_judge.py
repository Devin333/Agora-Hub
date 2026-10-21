from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from backend.research.ports.evaluation_judge import (
    AnswerSpan,
    CandidateUsage,
    CitationBinding,
    CitationLocation,
    CitationSupportJudgeRequest,
    FactualJudgeRequest,
    JudgeBudget,
    JudgeCandidateRequest,
    JudgeCandidateResponse,
    JudgeErrorCode,
    JudgeBudgetHookRecord,
    JudgeBudgetHookDecision,
    JudgeBudgetHookRequest,
    JudgeMetric,
    JudgeModality,
    JudgeObservationStatus,
    JudgeRuntimeProfile,
    JudgeScope,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_judging import (
    CITATION_SUPPORT_METRIC_VERSION,
    RAGAS_SEMANTIC_METRIC_VERSION,
)
from infrastructure.research import ragas_judge


class ClaimDecompositionOutput(BaseModel):
    claims: list[str]


class StatementGeneratorOutput(BaseModel):
    statements: list[str]


class StatementFaithfulnessAnswer(BaseModel):
    statement: str
    reason: str
    verdict: int


class NLIStatementOutput(BaseModel):
    statements: list[StatementFaithfulnessAnswer]


class _FixtureFactualCorrectness:
    def __init__(self, *, llm: Any, mode: str, **_: Any) -> None:
        self.llm = llm
        self.mode = mode

    async def ascore(self, *, response: str, reference: str) -> Any:
        response_claims = await self.llm.agenerate(
            f"decompose response: {response}", ClaimDecompositionOutput
        )
        response_verdicts = await self.llm.agenerate(
            f"verify against reference: {reference}; claims={response_claims.claims}",
            NLIStatementOutput,
        )
        if self.mode != "precision":
            reference_claims = await self.llm.agenerate(
                f"decompose reference: {reference}", ClaimDecompositionOutput
            )
            await self.llm.agenerate(
                f"verify against response: {response}; claims={reference_claims.claims}",
                NLIStatementOutput,
            )
        value = sum(item.verdict for item in response_verdicts.statements) / len(
            response_verdicts.statements
        )
        return SimpleNamespace(value=value)


class _FixtureFaithfulness:
    def __init__(self, *, llm: Any) -> None:
        self.llm = llm

    async def ascore(self, *, user_input: str, response: str, retrieved_contexts: list[str]) -> Any:
        statements = await self.llm.agenerate(
            f"question={user_input}; answer={response}", StatementGeneratorOutput
        )
        verdicts = await self.llm.agenerate(
            f"actual_contexts={retrieved_contexts}; statements={statements.statements}",
            NLIStatementOutput,
        )
        return SimpleNamespace(
            value=sum(item.verdict for item in verdicts.statements) / len(verdicts.statements)
        )


class _Callback:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def on_subrequest(self, record: Any) -> None:
        self.records.append(record)


class _CandidatePort:
    def __init__(
        self,
        outputs: list[str],
        *,
        delay_seconds: float = 0.0,
        runtime_profile: JudgeRuntimeProfile = JudgeRuntimeProfile.FIXTURE,
    ) -> None:
        self.outputs = list(outputs)
        self.delay_seconds = delay_seconds
        self.runtime_profile = runtime_profile
        self.requests: list[JudgeCandidateRequest] = []

    def generate_candidate(self, request: JudgeCandidateRequest) -> JudgeCandidateResponse:
        self.requests.append(request)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        raw = self.outputs.pop(0)
        return JudgeCandidateResponse(
            raw_output=raw,
            provider="fixture-provider",
            model_id="fixture-model",
            model_revision=None,
            model_revision_unavailable_reason="fixture model has no provider revision",
            runtime_profile=self.runtime_profile,
            usage=CandidateUsage(
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                cost=None,
                currency=None,
                cost_unknown_reason="fixture provider has no price",
            ),
            finish_reason="stop",
        )


class _BudgetHook:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.reservations: list[JudgeBudgetHookRequest] = []
        self.entries: list[JudgeBudgetHookRecord] = []

    def authorize(self, reservation: JudgeBudgetHookRequest) -> JudgeBudgetHookDecision:
        self.reservations.append(reservation)
        return JudgeBudgetHookDecision(
            allowed=self.allowed,
            reason=None if self.allowed else "shared run judge budget exhausted",
        )

    def record(self, entry: JudgeBudgetHookRecord) -> None:
        self.entries.append(entry)


def _dependencies() -> Any:
    return ragas_judge._Dependencies(
        instructor_base=object,
        factual_correctness=_FixtureFactualCorrectness,
        faithfulness=_FixtureFaithfulness,
        versions={"ragas": "0.4.3"},
    )


def _budget(**overrides: Any) -> JudgeBudget:
    payload: dict[str, Any] = {
        "max_requests": 6,
        "max_total_tokens": 1_000,
        "max_output_tokens_per_request": 100,
        "max_cost": None,
        "currency": None,
        "unknown_price_reason": "fixture provider has no price",
        "request_timeout_seconds": 0.2,
        "deadline_seconds": 2.0,
        "max_retries_per_subrequest": 1,
    }
    payload.update(overrides)
    return JudgeBudget(**payload)


def _fact_request(**overrides: Any) -> FactualJudgeRequest:
    response = "The method improves accuracy."
    reference = "The paper reports that the method improves accuracy."
    payload: dict[str, Any] = {
        "observation_id": "fact-observation",
        "metric": JudgeMetric.FACT_PRECISION,
        "metric_version": RAGAS_SEMANTIC_METRIC_VERSION,
        "execution_profile": JudgeRuntimeProfile.FIXTURE,
        "scope": JudgeScope.REFERENCE_COMPARISON,
        "required_modalities": frozenset({JudgeModality.TEXT}),
        "budget": _budget(),
        "shared_budget_key": None,
        "generation_parameters": {"temperature": 0},
        "evidence_references": ("artifact://answer", "artifact://reference"),
        "response": response,
        "reference": reference,
        "response_digest": content_digest(response),
        "reference_digest": content_digest(reference),
        "atomicity": "high",
        "coverage": "high",
    }
    payload.update(overrides)
    return FactualJudgeRequest(**payload)


def _citation_request(**overrides: Any) -> CitationSupportJudgeRequest:
    exact = "Accuracy increased from 80% to 85% on Dataset A."
    binding = CitationBinding(
        claim_id="claim-1",
        claim_text="Accuracy increased to 85% on Dataset A.",
        answer_span=AnswerSpan(start=0, end=42, text="Accuracy increased to 85% on Dataset A."),
        citation=CitationLocation(
            source_id="paper-1",
            anchor_id="page-4:paragraph-2",
            start=120,
            end=170,
            quoted_text=exact,
            quoted_text_digest=content_digest(exact),
        ),
    )
    rubric = "Require the same metric, value, dataset, and experimental condition."
    payload: dict[str, Any] = {
        "observation_id": "citation-observation",
        "metric": JudgeMetric.CITATION_SUPPORT,
        "metric_version": CITATION_SUPPORT_METRIC_VERSION,
        "execution_profile": JudgeRuntimeProfile.FIXTURE,
        "scope": JudgeScope.TEXT_SUBSET,
        "required_modalities": frozenset({JudgeModality.TEXT}),
        "budget": _budget(),
        "shared_budget_key": None,
        "generation_parameters": {"temperature": 0},
        "evidence_references": ("artifact://claim-1", "source://paper-1/page-4#p2"),
        "binding": binding,
        "binding_digest": content_digest(binding),
        "rubric": rubric,
        "rubric_version": "citation-rubric-v1",
        "rubric_digest": content_digest(rubric),
    }
    payload.update(overrides)
    return CitationSupportJudgeRequest(**payload)


def test_module_imports_without_optional_ragas_and_unavailable_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(package: str) -> str:
        if package == "ragas":
            raise ragas_judge.importlib.metadata.PackageNotFoundError(package)
        return ragas_judge.EXPECTED_DEPENDENCIES[package]

    monkeypatch.setattr(ragas_judge.importlib.metadata, "version", missing)
    callback = _Callback()
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=_CandidatePort([]),
            subrequest_callback=callback,
        ).judge(_fact_request())
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.JUDGE_UNAVAILABLE
    assert observation.value is None
    assert observation.subrequests == ()
    assert callback.records == []


def test_unvalidated_dependency_version_is_rejected_before_candidate_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ragas_judge.importlib.metadata,
        "version",
        lambda package: "0.4.4" if package == "ragas" else ragas_judge.EXPECTED_DEPENDENCIES[package],
    )
    candidate = _CandidatePort([])
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=_Callback(),
        ).judge(_fact_request())
    )
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.DEPENDENCY_VERSION_MISMATCH
    assert candidate.requests == []


def test_visual_requirement_is_not_silently_relabelled_as_text_subset() -> None:
    candidate = _CandidatePort([])
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=_Callback(),
        ).judge(
            _fact_request(
                required_modalities=frozenset({JudgeModality.TEXT, JudgeModality.IMAGE})
            )
        )
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.CAPABILITY_UNAVAILABLE
    assert observation.scope is JudgeScope.REFERENCE_COMPARISON
    assert candidate.requests == []


def test_invalid_json_retries_are_bounded_and_every_attempt_is_callback_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    callback = _Callback()
    candidate = _CandidatePort(["not-json", "still-not-json"])
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=callback,
        ).judge(_fact_request())
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.RETRY_EXHAUSTED
    assert observation.usage.requests == 2
    assert len(callback.records) == len(observation.subrequests) == 2
    assert all(item.error_code is JudgeErrorCode.INVALID_OUTPUT for item in callback.records)
    assert all(item.usage is not None for item in callback.records)


def test_reference_only_precision_preserves_extra_claim_as_needs_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    core = "The method improves accuracy."
    extra = "The implementation is available under an Apache license."
    candidate = _CandidatePort(
        [
            json.dumps({"claims": [core, extra]}),
            json.dumps(
                {
                    "statements": [
                        {"statement": core, "reason": "The reference supports the core result.", "verdict": 1},
                        {
                            "statement": extra,
                            "reason": "The supplied reference does not mention licensing.",
                            "verdict": 0,
                        },
                    ]
                }
            ),
        ]
    )
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=_Callback(),
        ).judge(_fact_request(response=f"{core} {extra}", response_digest=content_digest(f"{core} {extra}")))
    )
    assert observation.scope is JudgeScope.REFERENCE_COMPARISON
    assert observation.status is JudgeObservationStatus.NEEDS_REVIEW
    assert observation.value is None
    assert observation.raw_judgment["ragas_metric_result"] == 0.5
    assert observation.raw_judgment["unsupported_reference_claims"] == [extra]
    assert observation.raw_judgment["reference_comparison_only"] is True


def test_request_budget_stops_later_ragas_subcalls_without_hiding_first_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    callback = _Callback()
    claim = "The method improves accuracy."
    candidate = _CandidatePort([json.dumps({"claims": [claim]})])
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=callback,
        ).judge(_fact_request(budget=_budget(max_requests=1, max_retries_per_subrequest=0)))
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.BUDGET_EXHAUSTED
    assert observation.error.budget_exhausted is True
    assert observation.usage.requests == 1
    assert observation.usage.total_tokens == 15
    assert len(candidate.requests) == 1
    assert len(callback.records) == 2
    assert callback.records[-1].error_code is JudgeErrorCode.BUDGET_EXHAUSTED


def test_timeout_stops_with_null_score_and_records_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    callback = _Callback()
    candidate = _CandidatePort(
        [json.dumps({"claims": ["The method improves accuracy."]})],
        delay_seconds=0.05,
    )
    request = _fact_request(
        budget=_budget(
            request_timeout_seconds=0.01,
            deadline_seconds=0.02,
            max_retries_per_subrequest=0,
        )
    )
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=callback,
        ).judge(request)
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.TIMEOUT
    assert observation.value is None
    assert len(callback.records) == 1
    assert callback.records[0].error_code is JudgeErrorCode.TIMEOUT
    assert observation.usage.cost is None
    assert "timed-out" in observation.usage.cost_unknown_reason


def test_shared_budget_hook_authorizes_and_records_each_paid_subrequest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    callback = _Callback()
    budget_hook = _BudgetHook()
    claim = "The method improves accuracy."
    outputs = [
        json.dumps({"claims": [claim]}),
        json.dumps(
            {
                "statements": [
                    {"statement": claim, "reason": "The reference directly supports it.", "verdict": 1}
                ]
            }
        ),
    ]
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=_CandidatePort(outputs),
            subrequest_callback=callback,
            shared_budget_hook=budget_hook,
        ).judge(_fact_request(shared_budget_key="run-1:judge"))
    )
    assert observation.status is JudgeObservationStatus.OK
    assert observation.shared_budget_hook_connected is True
    assert (observation.value, observation.numerator, observation.denominator) == (1.0, 1, 1)
    assert len(budget_hook.reservations) == len(budget_hook.entries) == len(callback.records) == 2
    assert all(entry.usage is not None for entry in budget_hook.entries)


def test_missing_required_shared_budget_hook_never_falls_back_to_local_unbounded_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    candidate = _CandidatePort([])
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=_Callback(),
        ).judge(_fact_request(shared_budget_key="run-1:judge"))
    )
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.BUDGET_HOOK_REJECTED
    assert candidate.requests == []


def test_fixture_candidate_cannot_qualify_a_production_judge_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    claim = "The method improves accuracy."
    candidate = _CandidatePort(
        [json.dumps({"claims": [claim]})],
        runtime_profile=JudgeRuntimeProfile.FIXTURE,
    )
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=_Callback(),
        ).judge(_fact_request(execution_profile=JudgeRuntimeProfile.PRODUCTION))
    )
    assert observation.status is JudgeObservationStatus.ERROR
    assert observation.error is not None
    assert observation.error.code is JudgeErrorCode.INVALID_OUTPUT
    assert observation.value is None


def test_citation_prompt_contains_only_bound_claim_and_exact_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragas_judge, "_load_dependencies", _dependencies)
    request = _citation_request()
    output = {
        "claim_id": request.binding.claim_id,
        "supported": True,
        "reason": "The exact quote states the same value and dataset.",
        "cited_text": request.binding.citation.quoted_text,
        "conditions_considered": ["metric", "value", "dataset"],
    }
    candidate = _CandidatePort([json.dumps(output)])
    callback = _Callback()
    observation = asyncio.run(
        ragas_judge.RagasJudgeAdapter(
            candidate_port=candidate,
            subrequest_callback=callback,
        ).judge(request)
    )
    assert observation.status is JudgeObservationStatus.OK
    assert (observation.value, observation.numerator, observation.denominator) == (1.0, 1, 1)
    assert len(candidate.requests) == 1
    prompt = candidate.requests[0].prompt
    assert request.binding.claim_text in prompt
    assert request.binding.citation.quoted_text in prompt
    assert "unquoted retrieval context" in prompt
    assert observation.raw_judgment["citation_location_digest"] == request.binding.citation.quoted_text_digest