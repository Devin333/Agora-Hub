from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import socket
from typing import Any

import pytest

from backend.research.ports.evaluation_judge import (
    CandidateUsage,
    FaithfulnessJudgeRequest,
    FactualJudgeRequest,
    JudgeBudget,
    JudgeCandidateRequest,
    JudgeCandidateResponse,
    JudgeMetric,
    JudgeModality,
    JudgeObservationStatus,
    JudgeRuntimeProfile,
    JudgeScope,
    TextContext,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_judging import RAGAS_SEMANTIC_METRIC_VERSION
from infrastructure.research.ragas_judge import EXPECTED_DEPENDENCIES, RagasJudgeAdapter

RAGAS_AVAILABLE = importlib.util.find_spec("ragas") is not None
pytestmark = pytest.mark.skipif(
    not RAGAS_AVAILABLE,
    reason="optional rag-eval-judge dependency is not installed",
)


class _Callback:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def on_subrequest(self, record: Any) -> None:
        self.records.append(record)


class _ScriptedBoundedCandidate:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.requests: list[JudgeCandidateRequest] = []

    def generate_candidate(self, request: JudgeCandidateRequest) -> JudgeCandidateResponse:
        self.requests.append(request)
        output = self.outputs.pop(0)
        return JudgeCandidateResponse(
            raw_output=json.dumps(output),
            provider="offline-fixture-provider",
            model_id="offline-structured-fixture",
            model_revision="fixture-revision-1",
            model_revision_unavailable_reason=None,
            runtime_profile=JudgeRuntimeProfile.FIXTURE,
            usage=CandidateUsage(
                input_tokens=20,
                output_tokens=10,
                total_tokens=30,
                cost=None,
                currency=None,
                cost_unknown_reason="offline fixture performs no paid call",
            ),
            finish_reason="stop",
        )


def _budget() -> JudgeBudget:
    return JudgeBudget(
        max_requests=8,
        max_total_tokens=2_000,
        max_output_tokens_per_request=200,
        max_cost=None,
        currency=None,
        unknown_price_reason="offline fixture performs no paid call",
        request_timeout_seconds=2.0,
        deadline_seconds=10.0,
        max_retries_per_subrequest=0,
    )


def _fact_request(answer: str, reference: str) -> FactualJudgeRequest:
    return FactualJudgeRequest(
        observation_id="actual-ragas-fact",
        metric=JudgeMetric.FACT_PRECISION,
        metric_version=RAGAS_SEMANTIC_METRIC_VERSION,
        execution_profile=JudgeRuntimeProfile.FIXTURE,
        scope=JudgeScope.REFERENCE_COMPARISON,
        required_modalities=frozenset({JudgeModality.TEXT}),
        budget=_budget(),
        shared_budget_key=None,
        generation_parameters={"temperature": 0},
        evidence_references=("artifact://answer", "artifact://reference"),
        response=answer,
        reference=reference,
        response_digest=content_digest(answer),
        reference_digest=content_digest(reference),
        atomicity="high",
        coverage="high",
    )


def _faith_request(answer: str, context: str) -> FaithfulnessJudgeRequest:
    question = "What does the paper report and provide?"
    return FaithfulnessJudgeRequest(
        observation_id="actual-ragas-faithfulness",
        metric=JudgeMetric.FAITHFULNESS,
        metric_version=RAGAS_SEMANTIC_METRIC_VERSION,
        execution_profile=JudgeRuntimeProfile.FIXTURE,
        scope=JudgeScope.TEXT_SUBSET,
        required_modalities=frozenset({JudgeModality.TEXT}),
        budget=_budget(),
        shared_budget_key=None,
        generation_parameters={"temperature": 0},
        evidence_references=("artifact://answer", "artifact://actual-model-context"),
        user_input=question,
        response=answer,
        actual_contexts=(
            TextContext(
                context_id="actual-model-context-1",
                text=context,
                text_digest=content_digest(context),
            ),
        ),
        user_input_digest=content_digest(question),
        response_digest=content_digest(answer),
    )


def test_ragas_043_real_collections_metrics_use_only_bounded_offline_subrequests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {
        package: importlib.metadata.version(package)
        for package in EXPECTED_DEPENDENCIES
    }
    assert versions == EXPECTED_DEPENDENCIES

    external_calls: list[Any] = []
    original_create_connection = socket.create_connection
    original_socket_connect = socket.socket.connect

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_create_connection(address, *args, **kwargs)
        external_calls.append((address, args, kwargs))
        raise AssertionError("real compatibility test must not open an external connection")

    def guarded_socket_connect(sock: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if host in {"127.0.0.1", "::1", "localhost"}:
            return original_socket_connect(sock, address)
        external_calls.append((address, (), {}))
        raise AssertionError("real compatibility test must not open an external connection")

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_socket_connect)
    monkeypatch.delenv("RAGAS_DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    core = "The method improves accuracy."
    legal_extra = "The implementation is available under an Apache license."
    answer = f"{core} {legal_extra}"
    reference = "The paper reports that the method improves accuracy."

    fact_callback = _Callback()
    fact_candidate = _ScriptedBoundedCandidate(
        [
            {"claims": [core, legal_extra]},
            {
                "statements": [
                    {
                        "statement": core,
                        "reason": "The supplied reference supports the core result.",
                        "verdict": 1,
                    },
                    {
                        "statement": legal_extra,
                        "reason": "The supplied reference does not discuss licensing.",
                        "verdict": 0,
                    },
                ]
            },
        ]
    )
    fact = asyncio.run(
        RagasJudgeAdapter(
            candidate_port=fact_candidate,
            subrequest_callback=fact_callback,
        ).judge(_fact_request(answer, reference))
    )
    assert fact.status is JudgeObservationStatus.NEEDS_REVIEW
    assert fact.scope is JudgeScope.REFERENCE_COMPARISON
    assert fact.value is None
    assert fact.raw_judgment["ragas_metric_result"] == 0.5
    assert fact.raw_judgment["unsupported_reference_claims"] == [legal_extra]
    assert fact.raw_judgment["requires_global_fact_review"] is True
    assert len(fact_callback.records) == len(fact_candidate.requests) == 2

    # Faithfulness observes only what the answer worker actually saw. Even a
    # perfect score cannot resolve whether that context was OCR-correct or
    # whether the extra claim is globally true.
    actual_model_context = (
        "The paper reports that the method improves accuracy. "
        "The implementation is available under an Apache license."
    )
    faith_callback = _Callback()
    faith_candidate = _ScriptedBoundedCandidate(
        [
            {"statements": [core, legal_extra]},
            {
                "statements": [
                    {
                        "statement": core,
                        "reason": "The actual model context contains the core result.",
                        "verdict": 1,
                    },
                    {
                        "statement": legal_extra,
                        "reason": "The actual model context contains the license statement.",
                        "verdict": 1,
                    },
                ]
            },
        ]
    )
    faith = asyncio.run(
        RagasJudgeAdapter(
            candidate_port=faith_candidate,
            subrequest_callback=faith_callback,
        ).judge(_faith_request(answer, actual_model_context))
    )
    assert faith.status is JudgeObservationStatus.OK
    assert faith.scope is JudgeScope.TEXT_SUBSET
    assert (faith.value, faith.numerator, faith.denominator) == (1.0, 2, 2)
    assert "OCR" in faith.scope_limitation
    assert len(faith_callback.records) == len(faith_candidate.requests) == 2
    assert fact.status is JudgeObservationStatus.NEEDS_REVIEW

    from ragas import _analytics as ragas_analytics

    assert os.environ["RAGAS_DO_NOT_TRACK"] == "true"
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert ragas_analytics.do_not_track() is True
    assert external_calls == []