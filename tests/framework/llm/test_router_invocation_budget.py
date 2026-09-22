from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest

from framework.governance.budget import BudgetLimits, BudgetPolicy
from framework.llm import (
    GlobalBudgetPolicy,
    GlobalBudgetTracker,
    LLMProviderError,
    LLMRequest,
    LLMResponse,
    LLMRouteError,
    LLMRouter,
    ModelContextProfile,
    ModelDeployment,
    ModelPricing,
    ModelRoute,
    TokenUsage,
    bind_llm_budget_invocation,
    current_llm_budget_invocation,
)
from framework.shared.graph_identity import GraphExecutionIdentity


IDENTITY = GraphExecutionIdentity(
    run_id="run-invocation-budget",
    graph_id="graph-invocation-budget",
    graph_version="v1",
    graph_ref="graph-invocation-budget@v1",
    graph_checksum="sha256:" + "b" * 64,
    node_id="analyze",
    node_instance_id="analyze-1",
    activity_id="parent-activity",
    attempt=1,
)
PRICING = ModelPricing(
    input_usd_per_1m_tokens=1.0,
    output_usd_per_1m_tokens=2.0,
)


class _ConcurrentClient:
    def __init__(self) -> None:
        self._barrier = Barrier(2)
        self._lock = Lock()
        self.call_count = 0
        self.max_transport_attempts: list[int | None] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self._barrier.wait(timeout=5)
        with self._lock:
            self.call_count += 1
            self.max_transport_attempts.append(request.max_transport_attempts)
        return LLMResponse(
            content="ok",
            usage=TokenUsage(input_tokens=2, output_tokens=1),
            execution_identity=request.execution_identity,
        )


class _FailingClient:
    def __init__(self) -> None:
        self.call_count = 0
        self.max_transport_attempts: list[int | None] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        self.max_transport_attempts.append(request.max_transport_attempts)
        raise LLMProviderError(
            "primary unavailable",
            provider="test",
            model="primary",
            deployment_id="primary",
            error_type="provider_unavailable",
            retryable=True,
        )


class _SuccessClient:
    def __init__(self) -> None:
        self.call_count = 0
        self.max_transport_attempts: list[int | None] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        self.max_transport_attempts.append(request.max_transport_attempts)
        return LLMResponse(
            content="fallback",
            usage=TokenUsage(input_tokens=3, output_tokens=2),
            execution_identity=request.execution_identity,
        )


def _profile(deployment_id: str) -> ModelContextProfile:
    return ModelContextProfile(
        deployment_id=deployment_id,
        provider="test",
        model=deployment_id,
        physical_context_window_tokens=4_096,
        max_output_tokens=256,
        default_output_tokens=32,
        tokenizer_family="test-byte",
        tokenizer_revision="test-v1",
        normalizer_revision="canonical-request-v1",
        profile_revision="test-profile-v1",
        allow_conservative_fallback=True,
    )


def _deployment(
    deployment_id: str,
    client,
    *,
    pricing: ModelPricing | None = PRICING,
) -> ModelDeployment:
    return ModelDeployment(
        deployment_id,
        "test",
        deployment_id,
        client,
        pricing=pricing,
        context_profile=_profile(deployment_id),
    )


def _root() -> GlobalBudgetTracker:
    return GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=10, max_total_tokens=1_000),
        execution_identity=IDENTITY,
    )


def _child(root: GlobalBudgetTracker, name: str, *, cost=None) -> GlobalBudgetTracker:
    return root.child_tracker(
        name,
        budget_policy=BudgetPolicy(
            policy_revision=f"task-reservation:{name}",
            limits=BudgetLimits(
                llm_calls=3,
                total_tokens=100,
                output_tokens=32,
                estimated_cost_usd=cost,
            ),
        ),
    )


def test_concurrent_router_invocations_consume_isolated_child_scopes() -> None:
    root = _root()
    children = (_child(root, "child-a"), _child(root, "child-b"))
    provider = _ConcurrentClient()
    router = LLMRouter(
        routes=[ModelRoute("writer", "primary")],
        deployments=[_deployment("primary", provider)],
        global_budget_tracker=root,
    )

    def invoke(child: GlobalBudgetTracker) -> LLMResponse:
        with bind_llm_budget_invocation(
            child,
            execution_identity=IDENTITY,
        ):
            return router.complete(
                "writer",
                LLMRequest(messages=[{"role": "user", "content": "analyze"}], execution_identity=IDENTITY),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = tuple(pool.map(invoke, children))

    assert [response.content for response in responses] == ["ok", "ok"]
    assert provider.call_count == 2
    assert provider.max_transport_attempts == [1, 1]
    assert [child.usage.llm_calls for child in children] == [1, 1]
    assert root.usage.llm_calls == 2
    assert current_llm_budget_invocation() is None


def test_router_fallback_stays_on_the_invocation_child_scope() -> None:
    root = _root()
    child = _child(root, "child-fallback")
    primary = _FailingClient()
    fallback = _SuccessClient()
    router = LLMRouter(
        routes=[ModelRoute("writer", "primary", ("fallback",))],
        deployments=[
            _deployment("primary", primary),
            _deployment("fallback", fallback),
        ],
        global_budget_tracker=root,
    )

    with bind_llm_budget_invocation(child, execution_identity=IDENTITY):
        response = router.complete(
            "writer",
            LLMRequest(messages=[{"role": "user", "content": "analyze"}], execution_identity=IDENTITY),
        )

    assert response.content == "fallback"
    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert primary.max_transport_attempts == [1]
    assert fallback.max_transport_attempts == [1]
    assert child.usage.llm_calls == 2
    assert root.usage.llm_calls == 2


def test_cost_bounded_invocation_rejects_missing_pricing_before_dispatch() -> None:
    root = _root()
    child = _child(root, "child-cost-zero", cost="0")
    provider = _SuccessClient()
    router = LLMRouter(
        routes=[ModelRoute("writer", "primary")],
        deployments=[_deployment("primary", provider, pricing=None)],
        global_budget_tracker=root,
    )

    with bind_llm_budget_invocation(child, execution_identity=IDENTITY):
        with pytest.raises(LLMRouteError) as captured:
            router.complete(
                "writer",
                LLMRequest(messages=[], execution_identity=IDENTITY),
            )

    assert captured.value.error_type == "global_budget_exceeded"
    assert provider.call_count == 0
    assert child.usage.llm_calls == 0
    assert (
        captured.value.errors[-1]["global_budget_check"]["violations"]
        == ["trusted_pricing_unavailable"]
    )


def test_router_rejects_disconnected_or_cross_graph_invocation_budget() -> None:
    root = _root()
    disconnected = _child(_root(), "disconnected")
    provider = _SuccessClient()
    router = LLMRouter(
        routes=[ModelRoute("writer", "primary")],
        deployments=[_deployment("primary", provider)],
        global_budget_tracker=root,
    )

    with bind_llm_budget_invocation(disconnected, execution_identity=IDENTITY):
        with pytest.raises(ValueError, match="ledger lineage"):
            router.complete(
                "writer",
                LLMRequest(messages=[], execution_identity=IDENTITY),
            )

    other = GraphExecutionIdentity(
        **{**IDENTITY.to_dict(), "activity_id": "other-activity"}
    )
    child = _child(root, "child-identity")
    with bind_llm_budget_invocation(child, execution_identity=IDENTITY):
        with pytest.raises(ValueError, match="Graph identity"):
            router.complete(
                "writer",
                LLMRequest(messages=[], execution_identity=other),
            )
    assert provider.call_count == 0
