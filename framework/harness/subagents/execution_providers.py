"""Production bindings for admitted child execution accounting.

The providers in this module are deliberately projections of durable state.  An
admission allocation establishes the child budget and evidence limits, while
the canonical budget ledger and closed evidence index establish measured usage.
No value reported by the worker's metrics object is used as an authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from math import ceil, isfinite
from threading import RLock
from typing import Any

from framework.agent.models import AgentLoopResult
from framework.governance.budget import BudgetScopeType
from framework.governance.budget.errors import BudgetContractError
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.execution import (
    AdmittedChildExecution,
    ChildBudgetTrackerProviderPort,
    TrustedChildUsage,
)
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.subagents.tool_evidence import (
    ChildToolEvidenceCompleteness,
    ChildToolEvidenceIndex,
    ChildToolEvidenceLimits,
)
from framework.llm.budget import (
    GlobalBudgetTracker,
    budget_policy_for_child_allocation,
)
from framework.shared.graph_identity import GraphExecutionIdentity


class CanonicalChildBudgetTrackerProvider:
    """Bind admitted attempts to child scopes on one canonical budget ledger."""

    def __init__(
        self,
        canonical_tracker: GlobalBudgetTracker,
        *,
        policy_revision: str = "harness-child-admission/v1",
    ) -> None:
        if not isinstance(canonical_tracker, GlobalBudgetTracker):
            raise TypeError("canonical_tracker must be GlobalBudgetTracker")
        if not isinstance(policy_revision, str) or not policy_revision.strip():
            raise ValueError("policy_revision must be non-empty text")
        self._canonical_tracker = canonical_tracker
        self._policy_revision = policy_revision
        self._trackers: dict[str, GlobalBudgetTracker] = {}
        self._execution_roots: dict[str, GlobalBudgetTracker] = {}
        self._identities: dict[str, str] = {}
        self._lock = RLock()

    def tracker_for(
        self,
        admission: AdmittedChildExecution,
        *,
        attempt_identity: SubAgentAttemptIdentity | None = None,
    ) -> GlobalBudgetTracker:
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        operation_key = admission.operation_key
        with self._lock:
            existing = self._trackers.get(operation_key)
            if existing is not None:
                if (
                    attempt_identity is not None
                    and self._identities[operation_key]
                    != attempt_identity.identity_checksum
                ):
                    raise HarnessValidationError(
                        "child tracker identity differs from the admitted attempt",
                        code="subagent_runner_budget_scope_mismatch",
                    )
                return existing
            if not isinstance(attempt_identity, SubAgentAttemptIdentity):
                raise HarnessValidationError(
                    "attempt identity is required to bind a child budget scope",
                    code="task_plan_budget_authority_missing",
                )
            if (
                _execution_for_attempt(attempt_identity)
                != admission.execution_identity
            ):
                raise HarnessValidationError(
                    "attempt identity does not belong to the admitted execution",
                    code="task_plan_child_execution_admission_mismatch",
                )

            try:
                execution_tracker = self._execution_tracker_for(
                    admission.execution_identity
                )
                policy = budget_policy_for_child_allocation(
                    admission.budget_reservation.attempt_allocation,
                    policy_revision=self._policy_revision,
                )
                tracker = execution_tracker.child_tracker(
                    attempt_identity.identity_checksum,
                    scope_type=BudgetScopeType.SUBAGENT,
                    budget_policy=policy,
                )
            except (BudgetContractError, TypeError, ValueError, KeyError) as exc:
                raise HarnessValidationError(
                    "admitted child budget cannot be bound to the canonical ledger",
                    code="task_plan_budget_authority_missing",
                ) from exc

            expected_scope_id = "subagent:" + sha256(
                attempt_identity.identity_checksum.encode("utf-8")
            ).hexdigest()
            if (
                tracker.scope.scope_id != expected_scope_id
                or tracker.scope.scope_type is not BudgetScopeType.SUBAGENT
                or tracker.execution_identity != admission.execution_identity
            ):
                raise HarnessValidationError(
                    "canonical child tracker scope does not match the admitted attempt",
                    code="subagent_runner_budget_scope_mismatch",
                )
            self._trackers[operation_key] = tracker
            self._identities[operation_key] = attempt_identity.identity_checksum
            return tracker

    def _execution_tracker_for(
        self,
        identity: GraphExecutionIdentity,
    ) -> GlobalBudgetTracker:
        """Resolve one exact Graph scope while retaining a shared run ledger.

        Composition often constructs the child provider before a physical Graph
        activity identity exists.  A pristine default tracker is therefore a
        template: bind it to a run-scoped canonical root on first admission and
        reuse that root for every activity in the run.  A tracker that already
        contains history cannot be re-rooted because doing so would discard
        authoritative usage.
        """

        if self._canonical_tracker.execution_identity is not None:
            return self._canonical_tracker.for_execution_identity(identity)
        if self._canonical_tracker.scope.run_id != "standalone-budget":
            return self._canonical_tracker.for_execution_identity(identity)

        run_id = identity.run_id
        root = self._execution_roots.get(run_id)
        if root is None:
            snapshot = self._canonical_tracker.canonical_snapshot()
            if (
                snapshot.get("root_scope_id") != "run:standalone-budget"
                or len(snapshot.get("scopes", ())) != 1
                or snapshot.get("open_reservations")
                or snapshot.get("operation_records")
                or snapshot.get("ledger_revision") != 0
            ):
                raise ValueError(
                    "unbound canonical budget tracker contains authoritative history"
                )
            root = GlobalBudgetTracker(
                self._canonical_tracker.policy,
                budget_policy=self._canonical_tracker.budget_policy,
                estimator=self._canonical_tracker.estimator,
                run_id=run_id,
                event_sink=self._canonical_tracker.event_sink,
            )
            self._execution_roots[run_id] = root
        return root.for_execution_identity(identity)


class AdmissionChildToolEvidenceLimitsProvider:
    """Project the pinned admission call allocation into evidence limits."""

    def limits_for(self, admission: AdmittedChildExecution) -> ChildToolEvidenceLimits:
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        allocation = admission.budget_reservation.attempt_allocation
        try:
            physical_attempts = _non_negative_int(
                allocation["max_tool_calls"], "max_tool_calls"
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "admitted child allocation has no valid tool call limit",
                code="subagent_tool_evidence_policy_invalid",
            ) from exc
        return ChildToolEvidenceLimits(
            max_logical_calls=max(1, physical_attempts),
            max_physical_attempts=physical_attempts,
        )


class CanonicalChildExecutionUsageMeter:
    """Measure child usage from settled ledger state and canonical evidence."""

    def __init__(self, tracker_provider: ChildBudgetTrackerProviderPort) -> None:
        if not isinstance(tracker_provider, ChildBudgetTrackerProviderPort):
            raise TypeError("tracker_provider must implement ChildBudgetTrackerProviderPort")
        self._tracker_provider = tracker_provider

    @property
    def tracker_provider(self) -> ChildBudgetTrackerProviderPort:
        """Return the canonical ledger provider used for settlement."""

        return self._tracker_provider

    def measure(
        self,
        admission: AdmittedChildExecution,
        *,
        runner_result: AgentLoopResult,
        evidence_index: ChildToolEvidenceIndex,
    ) -> TrustedChildUsage:
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        if not isinstance(runner_result, AgentLoopResult):
            raise TypeError("runner_result must be AgentLoopResult")
        if not isinstance(evidence_index, ChildToolEvidenceIndex):
            raise TypeError("evidence_index must be ChildToolEvidenceIndex")
        if (
            evidence_index.completeness
            is not ChildToolEvidenceCompleteness.COMPLETE
            or evidence_index.unresolved_attempts != 0
        ):
            raise HarnessValidationError(
                "canonical child evidence is incomplete",
                code="subagent_tool_evidence_incomplete",
            )
        try:
            settled = (
                self._tracker_provider.tracker_for(admission)
                .authoritative_scope_usage()
                .require_settled()
            )
        except Exception as exc:
            if isinstance(exc, HarnessValidationError):
                raise
            raise HarnessValidationError(
                "canonical child usage is not settled",
                code="subagent_trusted_usage_unavailable",
            ) from exc

        committed = settled.committed
        allocation = admission.budget_reservation.attempt_allocation
        cost = (
            settled.committed_cost_microusd
            if "cost_limit" in allocation
            else None
        )
        return TrustedChildUsage(
            turns=_non_negative_int(runner_result.iterations, "iterations"),
            llm_calls=committed.llm_calls,
            tool_calls=_non_negative_int(
                evidence_index.admitted_physical_attempts,
                "admitted_physical_attempts",
            ),
            logical_tool_calls=_non_negative_int(
                evidence_index.registered_logical_calls,
                "registered_logical_calls",
            ),
            memory_ops=len(self.memory_namespaces_for(admission)),
            input_tokens=committed.input_tokens,
            output_tokens=committed.output_tokens,
            reasoning_tokens=committed.reasoning_tokens,
            cached_input_tokens=committed.cached_input_tokens,
            tokens=committed.total_tokens,
            time_ms=_trace_time_ms(runner_result.trace),
            cost_microusd=cost,
        )

    @staticmethod
    def memory_namespaces_for(admission: AdmittedChildExecution) -> tuple[str, ...]:
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        namespaces = admission.binding.allowed_memory_namespaces
        if not isinstance(namespaces, tuple):
            namespaces = tuple(namespaces)
        if any(not isinstance(namespace, str) or not namespace for namespace in namespaces):
            raise HarnessValidationError(
                "admitted memory namespace binding is invalid",
                code="subagent_memory_namespace_policy_invalid",
            )
        return tuple(sorted(set(namespaces)))


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _trace_time_ms(trace: Mapping[str, Any]) -> int:
    """Sum durable iteration durations; worker metrics are intentionally ignored."""

    if not isinstance(trace, Mapping):
        return 0
    iterations = trace.get("iterations")
    if not isinstance(iterations, (list, tuple)):
        return 0
    total = 0.0
    for item in iterations:
        if not isinstance(item, Mapping):
            continue
        duration = item.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            continue
        if isfinite(float(duration)) and duration >= 0:
            total += float(duration)
    return max(0, ceil(total))


def _execution_for_attempt(identity: SubAgentAttemptIdentity):
    from framework.harness.ref_snapshot import RefAuthoritySnapshot

    return RefAuthoritySnapshot.execution_for_attempt(identity)


__all__ = [
    "AdmissionChildToolEvidenceLimitsProvider",
    "CanonicalChildBudgetTrackerProvider",
    "CanonicalChildExecutionUsageMeter",
]
