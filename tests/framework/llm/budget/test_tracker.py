from __future__ import annotations

import pytest

from framework.governance.budget import (
    BudgetLedger,
    BudgetLimits,
    BudgetPolicy,
    BudgetHistoryError,
    BudgetStateError,
    BudgetScopeRef,
    BudgetScopeType,
)
from framework.llm.budget import (
    GlobalBudgetPolicy,
    GlobalBudgetExceededError,
    GlobalBudgetTracker,
    budget_policy_for_child_allocation,
)
from framework.llm.models import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity


def test_facade_restore_preserves_canonical_identity_and_rejects_policy_drift() -> None:
    source = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=3, max_total_tokens=100),
        run_id="run-source",
    )
    source.record_llm_call(TokenUsage(input_tokens=7, output_tokens=3))
    snapshot = source.canonical_snapshot()

    restored = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=3, max_total_tokens=100),
        run_id="run-resume",
    )
    restored.restore(snapshot)
    assert restored.canonical_snapshot() == snapshot
    assert restored.scope.run_id == "run-source"

    mismatched = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=4, max_total_tokens=100),
        run_id="run-mismatch",
    )
    with pytest.raises(BudgetHistoryError, match="policy"):
        mismatched.restore(snapshot)


def test_facade_identity_generation_is_unique_after_restore() -> None:
    tracker = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=4), run_id="run-id")
    first = tracker.next_operation_identity("operation")
    snapshot = tracker.canonical_snapshot()
    tracker.restore(snapshot)
    second = tracker.next_operation_identity("operation")

    assert first != second
    assert first.startswith("operation:run-id:")
    assert second.startswith("operation:run-id:")


def test_child_facade_cannot_export_or_restore_authoritative_snapshot() -> None:
    root = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=2), run_id="run-scope")
    child = root.child_tracker("agent-a", scope_type=BudgetScopeType.AGENT_LOOP)
    sibling = root.child_tracker("agent-b", scope_type=BudgetScopeType.SUBAGENT)
    operation = child.reserve_direct_operation(
        operation_id="child-operation",
        idempotency_key="child-idempotency",
        input_tokens=1,
        output_tokens=0,
    )

    assert operation.operation_id == "child-operation"
    assert child.usage.llm_calls == 1
    assert sibling.usage.llm_calls == 0
    assert root.usage.llm_calls == 1
    with pytest.raises(ValueError, match="root tracker"):
        child.canonical_snapshot()
    with pytest.raises(ValueError, match="root tracker"):
        child.restore(root.canonical_snapshot())


def test_child_tracker_registers_its_restricted_policy_on_the_parent_ledger() -> None:
    root = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=4, max_total_tokens=100),
        run_id="run-restricted-child",
    )
    child_policy = BudgetPolicy(
        policy_revision="task-reservation:sha256:child-a",
        limits=BudgetLimits(
            llm_calls=1,
            total_tokens=10,
            output_tokens=4,
            estimated_cost_usd="0.000005",
        ),
    )
    child = root.child_tracker("child-a", budget_policy=child_policy)

    assert child.budget_policy == child_policy
    assert child.is_scope_descendant_of(root) is True
    assert root.is_scope_descendant_of(child) is False
    assert child.requires_trusted_pricing() is True

    with pytest.raises(GlobalBudgetExceededError) as captured:
        child.reserve_direct_operation(
            operation_id="child-a:first",
            idempotency_key="child-a:first:reservation",
            input_tokens=1,
            output_tokens=1,
        )
    assert getattr(captured.value, "error_type", None) == "global_budget_exceeded"
    assert getattr(captured.value, "check").violations == (
        "trusted_pricing_unavailable",
    )
    assert root.usage.llm_calls == 0


def test_authoritative_scope_usage_rejects_outstanding_and_indeterminate() -> None:
    root = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=4, max_total_tokens=100),
        run_id="run-authoritative-child",
    )
    child = root.child_tracker("child-authoritative")
    operation = child.reserve_operation(
        operation_id="child-authoritative:first",
        idempotency_key="child-authoritative:first:reservation",
        input_tokens=3,
        output_tokens=2,
    )

    outstanding = child.authoritative_scope_usage()
    assert outstanding.committed.llm_calls == 0
    assert outstanding.reserved.llm_calls == 1
    assert outstanding.outstanding_reservation_ids == (
        operation.reservation.reservation_id,
    )
    assert outstanding.indeterminate_reservation_ids == ()
    assert outstanding.is_settled is False
    with pytest.raises(BudgetStateError, match="outstanding"):
        outstanding.require_settled()

    child.mark_operation_indeterminate(operation, reason="provider_outcome_unknown")
    indeterminate = child.authoritative_scope_usage()
    assert indeterminate.outstanding_reservation_ids == ()
    assert indeterminate.indeterminate_reservation_ids == (
        operation.reservation.reservation_id,
    )
    with pytest.raises(BudgetStateError, match="indeterminate"):
        indeterminate.require_settled()


def test_authoritative_scope_usage_reports_conservative_cost_micro_units() -> None:
    root = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=2), run_id="run-cost")
    child = root.child_tracker("child-cost")
    operation = child.reserve_operation(
        operation_id="child-cost:first",
        idempotency_key="child-cost:first:reservation",
        input_tokens=1,
        output_tokens=1,
        estimated_cost_usd="0.0000001",
    )
    child.settle_operation(
        operation,
        TokenUsage(input_tokens=1, output_tokens=1),
        estimated_cost_usd="0.0000001",
    )

    usage = child.authoritative_scope_usage().require_settled()
    assert usage.committed.llm_calls == 1
    assert usage.committed.input_tokens == 1
    assert usage.committed.output_tokens == 1
    assert usage.committed_total_tokens == 2
    assert usage.committed_cost_microusd == 1


def test_child_allocation_policy_preserves_absent_and_zero_cost() -> None:
    base = {
        "max_turns": 2,
        "max_tool_calls": 1,
        "max_memory_ops": 0,
        "max_output_tokens": 4,
        "token_limit": 10,
        "time_limit_ms": 1_000,
    }

    absent = budget_policy_for_child_allocation(
        base,
        policy_revision="task-reservation:absent-cost",
    )
    zero = budget_policy_for_child_allocation(
        {**base, "cost_limit": 0},
        policy_revision="task-reservation:zero-cost",
    )

    assert absent.limits.llm_calls == 2
    assert absent.limits.total_tokens == 10
    assert absent.limits.output_tokens == 4
    assert absent.limits.estimated_cost_usd is None
    assert zero.limits.estimated_cost_usd == 0

    legacy = budget_policy_for_child_allocation(
        {
            "max_turns": 2,
            "max_tool_calls": 1,
            "max_memory_ops": 0,
            "max_output_tokens": 4,
        },
        policy_revision="task-reservation:legacy",
    )
    assert legacy.limits.total_tokens is None
    assert legacy.limits.estimated_cost_usd is None


def test_execution_bound_tracker_rejects_cross_activity_rebinding() -> None:
    def identity(activity_id: str) -> GraphExecutionIdentity:
        return GraphExecutionIdentity(
            run_id="run-budget",
            graph_id="research.graph",
            graph_version="v1",
            graph_ref="research.graph@v1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analyze",
            node_instance_id="analyze-1",
            activity_id=activity_id,
            attempt=1,
        )

    tracker = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=2),
        execution_identity=identity("activity-a"),
    )

    with pytest.raises(ValueError, match="different Graph execution identity"):
        tracker.for_execution_identity(identity("activity-b"))


def test_execution_bound_tracker_rejects_scope_without_exact_identity() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-budget",
        graph_id="research.graph",
        graph_version="v1",
        graph_ref="research.graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze-1",
        activity_id="activity-a",
        attempt=1,
    )
    scope = BudgetScopeRef(
        run_id=identity.run_id,
        scope_id="run:run-budget",
        scope_type=BudgetScopeType.RUN,
        policy_revision="default",
    )

    with pytest.raises(ValueError, match="does not match execution identity"):
        GlobalBudgetTracker(
            GlobalBudgetPolicy(max_llm_calls=2),
            scope=scope,
            execution_identity=identity,
        )


def test_scope_exact_identity_binds_tracker_execution_identity() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-budget",
        graph_id="research.graph",
        graph_version="v1",
        graph_ref="research.graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze-1",
        activity_id="activity-a",
        attempt=1,
    )
    scope = BudgetScopeRef(
        run_id=identity.run_id,
        scope_id="run:run-budget",
        scope_type=BudgetScopeType.RUN,
        policy_revision="legacy-global-budget/v1",
        execution_identity=identity,
    )

    tracker = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=2),
        scope=scope,
    )

    assert tracker.execution_identity == identity
    assert tracker.scope.execution_identity == identity


def test_ledger_root_identity_binds_tracker_without_explicit_scope() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-budget",
        graph_id="research.graph",
        graph_version="v1",
        graph_ref="research.graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze-1",
        activity_id="activity-a",
        attempt=1,
    )
    root_scope = BudgetScopeRef(
        run_id=identity.run_id,
        scope_id="run:run-budget",
        scope_type=BudgetScopeType.RUN,
        policy_revision="legacy-global-budget/v1",
        execution_identity=identity,
    )
    ledger = BudgetLedger(
        root_scope,
        BudgetPolicy(
            policy_revision="legacy-global-budget/v1",
            limits=BudgetLimits(llm_calls=2),
        ),
    )

    tracker = GlobalBudgetTracker(
        GlobalBudgetPolicy(max_llm_calls=2),
        ledger=ledger,
    )

    assert tracker.execution_identity == identity
    assert tracker.scope == root_scope


def test_execution_bound_tracker_rejects_scope_from_another_run() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-budget",
        graph_id="research.graph",
        graph_version="v1",
        graph_ref="research.graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze-1",
        activity_id="activity-a",
        attempt=1,
    )
    scope = BudgetScopeRef(
        run_id="run-other",
        scope_id="run:run-other",
        scope_type=BudgetScopeType.RUN,
        policy_revision="default",
    )

    with pytest.raises(ValueError, match="scope run"):
        GlobalBudgetTracker(
            GlobalBudgetPolicy(max_llm_calls=2),
            scope=scope,
            execution_identity=identity,
        )
