from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from framework.agent.models import AgentLoopResult
from framework.governance.budget import InMemoryBudgetEventSink
from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_snapshot import RefAuthoritySnapshot
from framework.harness.subagents.execution import AdmittedChildExecution
from framework.harness.subagents.execution_providers import (
    AdmissionChildToolEvidenceLimitsProvider,
    CanonicalChildBudgetTrackerProvider,
    CanonicalChildExecutionUsageMeter,
)
from framework.harness.subagents.fake import FakeSubAgentRuntime, fake_subagent_spec
from framework.harness.subagents.tool_evidence import (
    ChildToolEvidenceCompleteness,
    ChildToolEvidenceIndex,
)
from framework.llm.budget import GlobalBudgetPolicy, GlobalBudgetTracker
from framework.llm.models.usage import TokenUsage


def _admission(*, invocation=None):
    invocation = invocation or FakeSubAgentRuntime(fake_subagent_spec()).build_invocation()
    identity = RefAuthoritySnapshot.execution_for_attempt(invocation.attempt_identity)
    allocation = {
        "max_turns": 2,
        "max_tool_calls": 3,
        "max_memory_ops": 2,
        "max_output_tokens": 10,
        "token_limit": 20,
        "time_limit_ms": 1000,
        "cost_limit": 1000,
    }
    reservation = BudgetReservation(
        owner_scope=f"{identity.run_id}:step:group",
        reservation_key=f"child-operation:{invocation.attempt_identity.identity_checksum}",
        parent_allocation=allocation,
        attempt_allocation=allocation,
        ledger_version=1,
    )
    admission = object.__new__(AdmittedChildExecution)
    object.__setattr__(admission, "budget_reservation", reservation)
    object.__setattr__(admission, "execution_identity", identity)
    object.__setattr__(
        admission,
        "binding",
        SimpleNamespace(allowed_memory_namespaces=("research.public", "run.private")),
    )
    return admission, invocation


def _evidence_index() -> ChildToolEvidenceIndex:
    return ChildToolEvidenceIndex(
        ref="artifact://evidence",
        checksum="a" * 64,
        content_checksum="b" * 64,
        byte_size=10,
        revision=1,
        completeness=ChildToolEvidenceCompleteness.COMPLETE,
        disposition="closed",
        registered_logical_calls=2,
        rejected_logical_calls=0,
        admitted_physical_attempts=3,
        known_terminal_attempts=3,
        unresolved_attempts=0,
    )


def test_tracker_provider_binds_admission_to_canonical_subagent_scope() -> None:
    admission, invocation = _admission()
    root = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=10),
        execution_identity=admission.execution_identity,
    )
    provider = CanonicalChildBudgetTrackerProvider(root)

    tracker = provider.tracker_for(
        admission,
        attempt_identity=invocation.attempt_identity,
    )

    assert tracker.scope.scope_id.startswith("subagent:")
    assert tracker.scope.execution_identity == admission.execution_identity
    assert tracker.budget_policy.limits.llm_calls == 2
    assert tracker.budget_policy.limits.total_tokens == 20
    assert provider.tracker_for(admission) is tracker


def test_tracker_provider_requires_identity_before_first_binding() -> None:
    admission, _ = _admission()
    provider = CanonicalChildBudgetTrackerProvider(
        GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10))
    )

    with pytest.raises(HarnessValidationError) as error:
        provider.tracker_for(admission)

    assert error.value.code == "task_plan_budget_authority_missing"


def test_tracker_provider_lazily_binds_pristine_template_to_shared_run_ledger() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    admission, invocation = _admission(
        invocation=runtime.build_invocation(parent_run_id="shared-run")
    )
    sibling_admission, sibling_invocation = _admission(
        invocation=runtime.build_invocation(parent_run_id="shared-run")
    )
    template = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10))
    provider = CanonicalChildBudgetTrackerProvider(template)

    first = provider.tracker_for(
        admission,
        attempt_identity=invocation.attempt_identity,
    )
    sibling = provider.tracker_for(
        sibling_admission,
        attempt_identity=sibling_invocation.attempt_identity,
    )

    assert first.scope.execution_identity == admission.execution_identity
    root = provider._execution_roots[admission.execution_identity.run_id]
    assert root.scope.run_id == admission.execution_identity.run_id
    assert root.execution_identity is None
    assert first.is_scope_descendant_of(root) is True
    assert sibling.is_scope_descendant_of(root) is True
    assert sibling.scope.parent_scope_id != first.scope.parent_scope_id
    assert len(provider._execution_roots) == 1
    assert provider.tracker_for(admission) is first


def test_tracker_provider_isolates_canonical_ledgers_by_run_and_keeps_event_sink() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    first_admission, first_invocation = _admission(
        invocation=runtime.build_invocation(parent_run_id="run-a")
    )
    second_admission, second_invocation = _admission(
        invocation=runtime.build_invocation(parent_run_id="run-b")
    )
    sink = InMemoryBudgetEventSink()
    provider = CanonicalChildBudgetTrackerProvider(
        GlobalBudgetTracker(
            GlobalBudgetPolicy(max_llm_calls=10),
            event_sink=sink,
        )
    )

    first = provider.tracker_for(
        first_admission,
        attempt_identity=first_invocation.attempt_identity,
    )
    second = provider.tracker_for(
        second_admission,
        attempt_identity=second_invocation.attempt_identity,
    )
    first.record_llm_call(TokenUsage(input_tokens=1, output_tokens=1))

    assert set(provider._execution_roots) == {"run-a", "run-b"}
    assert first.scope.run_id == "run-a"
    assert second.scope.run_id == "run-b"
    assert first.is_scope_descendant_of(provider._execution_roots["run-a"])
    assert not first.is_scope_descendant_of(provider._execution_roots["run-b"])
    assert sink.events()


def test_tracker_provider_rejects_non_pristine_unbound_template() -> None:
    admission, invocation = _admission()
    template = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10))
    template.record_llm_call(TokenUsage(input_tokens=1, output_tokens=1))
    provider = CanonicalChildBudgetTrackerProvider(template)

    with pytest.raises(HarnessValidationError) as error:
        provider.tracker_for(
            admission,
            attempt_identity=invocation.attempt_identity,
        )

    assert error.value.code == "task_plan_budget_authority_missing"


def test_limits_provider_projects_only_admitted_tool_allocation() -> None:
    admission, _ = _admission()

    limits = AdmissionChildToolEvidenceLimitsProvider().limits_for(admission)

    assert limits.max_physical_attempts == 3
    assert limits.max_logical_calls == 3


def test_usage_meter_uses_settled_ledger_and_closed_evidence() -> None:
    admission, invocation = _admission()
    root = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=10),
        execution_identity=admission.execution_identity,
    )
    tracker_provider = CanonicalChildBudgetTrackerProvider(root)
    tracker = tracker_provider.tracker_for(
        admission,
        attempt_identity=invocation.attempt_identity,
    )
    operation = tracker.reserve_operation(
        operation_id="llm-operation-1",
        idempotency_key="idempotency-1",
        input_tokens=4,
        output_tokens=3,
        reasoning_tokens=1,
        cached_input_tokens=1,
        estimated_cost_usd=Decimal("0.0004"),
    )
    tracker.settle_operation(
        operation,
        TokenUsage(
            input_tokens=4,
            output_tokens=3,
            reasoning_tokens=1,
            cached_input_tokens=1,
        ),
        estimated_cost_usd=Decimal("0.0004"),
    )
    result = replace(
        AgentLoopResult.success_result("critic", {"result": "ok"}),
        iterations=2,
        trace={"iterations": [{"duration_ms": 2.4}, {"duration_ms": 3.2}]},
    )

    usage = CanonicalChildExecutionUsageMeter(tracker_provider).measure(
        admission,
        runner_result=result,
        evidence_index=_evidence_index(),
    )

    assert usage.llm_calls == 1
    assert usage.input_tokens == 4
    assert usage.output_tokens == 3
    assert usage.reasoning_tokens == 1
    assert usage.cached_input_tokens == 1
    assert usage.tokens == 8
    assert usage.tool_calls == 3
    assert usage.logical_tool_calls == 2
    assert usage.memory_ops == 2
    assert usage.time_ms == 6
    assert usage.cost_microusd == 400
