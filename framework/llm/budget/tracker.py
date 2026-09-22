from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from hashlib import sha256
from math import isfinite
from threading import local
from typing import Any, Mapping

from framework.governance.budget import (
    BudgetAmounts,
    BudgetDecision,
    BudgetEventSink,
    BudgetHistoryError,
    BudgetLedger,
    BudgetLimits,
    BudgetPolicy,
    BudgetReservationStatus,
    BudgetScopeRef,
    BudgetScopeType,
    BudgetSettlementOutcome,
    BudgetSnapshot,
    BudgetStateError,
    restore_legacy_budget_snapshot,
)
from framework.llm.budget.adapter import LLMBudgetAdapter, LLMBudgetOperation
from framework.llm.budget.estimator import CostEstimator
from framework.llm.budget.policy import GlobalBudgetPolicy
from framework.llm.budget.pricing import ModelPricing
from framework.llm.models.usage import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps


GLOBAL_BUDGET_COMPATIBILITY_INTRODUCED_RELEASE = "0.1.0"
GLOBAL_BUDGET_COMPATIBILITY_EXPIRES_RELEASE = "0.2.0"


@dataclass(frozen=True)
class GlobalBudgetUsage:
    llm_calls: int = 0
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_usage", TokenUsage.from_any(self.token_usage))

    def to_dict(self) -> dict[str, object]:
        return {
            "llm_calls": self.llm_calls,
            "token_usage": self.token_usage.to_dict(),
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class GlobalBudgetCheck:
    usage: GlobalBudgetUsage
    within_budget: bool
    violations: tuple[str, ...] = field(default_factory=tuple)
    reservation_id: str | None = None
    operation_id: str | None = None
    ledger_revision: int = 0
    run_id: str | None = None
    scope_id: str | None = None
    policy_digest: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "usage": self.usage.to_dict(),
            "within_budget": self.within_budget,
            "violations": list(self.violations),
            "reservation_id": self.reservation_id,
            "operation_id": self.operation_id,
            "ledger_revision": self.ledger_revision,
            "run_id": self.run_id,
            "scope_id": self.scope_id,
            "policy_digest": self.policy_digest,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeBudgetScopeUsage:
    """Immutable canonical-ledger usage for one scope and its descendants."""

    scope: BudgetScopeRef
    policy: BudgetPolicy
    committed: BudgetAmounts
    reserved: BudgetAmounts
    ledger_revision: int
    outstanding_reservation_ids: tuple[str, ...] = field(default_factory=tuple)
    indeterminate_reservation_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def committed_total_tokens(self) -> int:
        return self.committed.total_tokens

    @property
    def committed_cost_microusd(self) -> int:
        """Return conservative integer USD micro-units without float conversion."""

        return int(
            (self.committed.estimated_cost_usd * Decimal(1_000_000)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )

    @property
    def is_settled(self) -> bool:
        return not (
            self.outstanding_reservation_ids
            or self.indeterminate_reservation_ids
            or self.reserved != BudgetAmounts()
        )

    def require_settled(self) -> "AuthoritativeBudgetScopeUsage":
        if self.indeterminate_reservation_ids:
            raise BudgetStateError(
                "budget scope contains indeterminate provider operations"
            )
        if self.outstanding_reservation_ids or self.reserved != BudgetAmounts():
            raise BudgetStateError("budget scope contains outstanding reservations")
        return self


class GlobalBudgetExceededError(RuntimeError):
    def __init__(self, check: GlobalBudgetCheck) -> None:
        super().__init__("global budget exceeded: " + ", ".join(check.violations))
        self.check = check
        self.error_type = "global_budget_exceeded"

    def to_dict(self) -> dict[str, object]:
        return {
            "message": str(self),
            "error_type": self.error_type,
            "budget_check": self.check.to_dict(),
        }


class GlobalBudgetGuard:
    def __init__(self, policy: GlobalBudgetPolicy) -> None:
        self.policy = policy

    def check(self, usage: GlobalBudgetUsage) -> GlobalBudgetCheck:
        limits = _canonical_policy(self.policy).limits
        amounts = _amounts_from_legacy_usage(usage)
        violations = limits.violations(amounts)
        return GlobalBudgetCheck(
            usage=usage,
            within_budget=not violations,
            violations=violations,
        )


class GlobalBudgetTracker:
    """One-release LLM facade over the canonical cumulative budget ledger."""

    def __init__(
        self,
        policy: GlobalBudgetPolicy,
        *,
        budget_policy: BudgetPolicy | None = None,
        estimator: CostEstimator | None = None,
        ledger: BudgetLedger | None = None,
        scope: BudgetScopeRef | None = None,
        run_id: str = "standalone-budget",
        execution_identity: GraphExecutionIdentity | dict[str, Any] | None = None,
        event_sink: BudgetEventSink | None = None,
    ) -> None:
        self.policy = policy
        if execution_identity is not None and not isinstance(
            execution_identity, GraphExecutionIdentity
        ):
            execution_identity = GraphExecutionIdentity.from_dict(execution_identity)
        if execution_identity is not None:
            run_id = execution_identity.run_id
        self.estimator = estimator or CostEstimator()
        canonical_policy = budget_policy or _canonical_policy(policy)
        if not isinstance(canonical_policy, BudgetPolicy):
            raise TypeError("budget_policy must be BudgetPolicy")
        self._budget_policy = canonical_policy
        if scope is None and ledger is not None:
            scope = ledger.root_scope
        if execution_identity is None and scope is not None:
            scoped_identity = scope.execution_identity
            if isinstance(scoped_identity, GraphExecutionIdentity):
                execution_identity = scoped_identity
                run_id = execution_identity.run_id
        if execution_identity is not None:
            if scope is None:
                scope = BudgetScopeRef(
                    run_id=run_id,
                    scope_id=f"run:{run_id}",
                    scope_type=BudgetScopeType.RUN,
                    policy_revision=canonical_policy.policy_revision,
                    execution_identity=execution_identity,
                )
            elif scope.run_id != execution_identity.run_id:
                raise ValueError(
                    "budget tracker scope run does not match execution identity"
                )
            elif scope.execution_identity != execution_identity:
                raise ValueError(
                    "budget tracker scope does not match execution identity"
                )
        self._execution_identity = execution_identity
        if ledger is None:
            scope = scope or BudgetScopeRef(
                run_id=run_id,
                scope_id=f"run:{run_id}",
                scope_type=BudgetScopeType.RUN,
                policy_revision=canonical_policy.policy_revision,
                execution_identity=execution_identity,
            )
            ledger = BudgetLedger(
                scope,
                canonical_policy,
                event_sink=event_sink,
            )
        self._event_sink = event_sink
        self._ledger = ledger
        self._scope = scope
        if self._ledger.view(self._scope).policy != self._budget_policy:
            raise ValueError("budget tracker policy does not match its ledger scope")
        self._adapter = LLMBudgetAdapter(
            ledger,
            scope=scope,
            estimator=self.estimator,
        )
        self._local = local()

    @property
    def scope(self) -> BudgetScopeRef:
        return self._scope

    @property
    def execution_identity(self) -> GraphExecutionIdentity | None:
        return self._execution_identity

    @property
    def budget_policy(self) -> BudgetPolicy:
        return self._budget_policy

    def for_execution_identity(
        self,
        identity: GraphExecutionIdentity,
    ) -> "GlobalBudgetTracker":
        """Return a ledger view scoped to one exact Graph activity."""

        if not isinstance(identity, GraphExecutionIdentity):
            raise TypeError("identity must be GraphExecutionIdentity")
        if self._execution_identity is not None and self._execution_identity != identity:
            raise ValueError(
                "budget tracker is already bound to a different Graph execution identity"
            )
        if self._execution_identity == identity:
            return self
        digest = sha256(
            stable_identity_text(identity).encode("utf-8")
        ).hexdigest()
        scope = BudgetScopeRef(
            run_id=identity.run_id,
            scope_id=f"{BudgetScopeType.AGENT_LOOP.value}:{digest}",
            scope_type=BudgetScopeType.AGENT_LOOP,
            parent_scope_id=self._scope.scope_id,
            policy_revision=self._budget_policy.policy_revision,
            execution_identity=identity,
        )
        self._ledger.register_scope(scope, self._budget_policy)
        return GlobalBudgetTracker(
            self.policy,
            budget_policy=self._budget_policy,
            estimator=self.estimator,
            ledger=self._ledger,
            scope=scope,
            event_sink=self._event_sink,
            execution_identity=identity,
        )

    def next_operation_identity(self, prefix: str = "llm-operation") -> str:
        return self._adapter.next_identity(prefix)

    def child_tracker(
        self,
        identity: str,
        *,
        scope_type: BudgetScopeType | str = BudgetScopeType.SUBAGENT,
        budget_policy: BudgetPolicy | None = None,
    ) -> "GlobalBudgetTracker":
        digest = sha256(identity.encode("utf-8")).hexdigest()
        canonical_policy = budget_policy or self._budget_policy
        if not isinstance(canonical_policy, BudgetPolicy):
            raise TypeError("budget_policy must be BudgetPolicy")
        child_scope = BudgetScopeRef(
            run_id=self._scope.run_id,
            scope_id=f"{BudgetScopeType(scope_type).value}:{digest}",
            scope_type=scope_type,
            parent_scope_id=self._scope.scope_id,
            policy_revision=canonical_policy.policy_revision,
            execution_identity=self._execution_identity,
        )
        self._ledger.register_scope(child_scope, canonical_policy)
        return GlobalBudgetTracker(
            self.policy,
            budget_policy=canonical_policy,
            estimator=self.estimator,
            ledger=self._ledger,
            scope=child_scope,
            event_sink=self._event_sink,
            execution_identity=self._execution_identity,
        )

    def is_scope_descendant_of(self, ancestor: "GlobalBudgetTracker") -> bool:
        """Return whether both views share one ledger and this scope descends from ancestor."""

        if not isinstance(ancestor, GlobalBudgetTracker):
            raise TypeError("ancestor must be GlobalBudgetTracker")
        if self._ledger is not ancestor._ledger:
            return False
        target_scope_id = ancestor.scope.scope_id
        scope_id: str | None = self.scope.scope_id
        visited: set[str] = set()
        while scope_id is not None:
            if scope_id == target_scope_id:
                return True
            if scope_id in visited:
                return False
            visited.add(scope_id)
            scope_id = self._ledger.view(scope_id).scope.parent_scope_id
        return False

    def requires_trusted_pricing(self) -> bool:
        """Whether this scope or an ancestor enforces a finite cost ceiling."""

        scope_id: str | None = self.scope.scope_id
        visited: set[str] = set()
        while scope_id is not None:
            if scope_id in visited:
                raise ValueError("budget tracker scope lineage contains a cycle")
            visited.add(scope_id)
            view = self._ledger.view(scope_id)
            if view.policy.limits.estimated_cost_usd is not None:
                return True
            scope_id = view.scope.parent_scope_id
        return False

    def authoritative_scope_usage(self) -> AuthoritativeBudgetScopeUsage:
        """Read canonical committed/reserved state without exposing ledger internals."""

        snapshot = self._ledger.snapshot()
        scopes = {item.scope.scope_id: item for item in snapshot.scopes}
        scoped = scopes.get(self._scope.scope_id)
        if scoped is None or scoped.scope != self._scope or scoped.policy != self._budget_policy:
            raise BudgetStateError("budget tracker scope is absent from canonical ledger")

        def belongs_to_scope(scope_id: str) -> bool:
            visited: set[str] = set()
            while scope_id:
                if scope_id == self._scope.scope_id:
                    return True
                if scope_id in visited:
                    raise BudgetStateError("budget scope lineage contains a cycle")
                visited.add(scope_id)
                child = scopes.get(scope_id)
                if child is None:
                    raise BudgetStateError(
                        "budget reservation references an unknown canonical scope"
                    )
                scope_id = child.scope.parent_scope_id or ""
            return False

        relevant = tuple(
            reservation
            for reservation in snapshot.open_reservations
            if belongs_to_scope(reservation.scope.scope_id)
        )
        return AuthoritativeBudgetScopeUsage(
            scope=scoped.scope,
            policy=scoped.policy,
            committed=scoped.committed,
            reserved=scoped.reserved,
            ledger_revision=snapshot.ledger_revision,
            outstanding_reservation_ids=tuple(
                item.reservation_id
                for item in relevant
                if item.status is BudgetReservationStatus.RESERVED
            ),
            indeterminate_reservation_ids=tuple(
                item.reservation_id
                for item in relevant
                if item.status is BudgetReservationStatus.INDETERMINATE
            ),
        )

    def require_trusted_pricing(self, pricing: ModelPricing | None) -> None:
        """Fail closed when a cost-bounded provider lacks complete trusted pricing."""

        if not self.requires_trusted_pricing():
            return
        rates = (
            None
            if pricing is None
            else (
                pricing.input_usd_per_1m_tokens,
                pricing.output_usd_per_1m_tokens,
            )
        )
        if rates is not None and all(
            not isinstance(rate, bool)
            and isinstance(rate, (int, float))
            and isfinite(float(rate))
            and float(rate) >= 0
            for rate in rates
        ):
            return
        raise GlobalBudgetExceededError(
            GlobalBudgetCheck(
                usage=self.usage,
                within_budget=False,
                violations=("trusted_pricing_unavailable",),
                ledger_revision=self._ledger.ledger_revision,
                run_id=self._scope.run_id,
                scope_id=self._scope.scope_id,
                policy_digest=self._budget_policy.digest,
            )
        )


    @property
    def usage(self) -> GlobalBudgetUsage:
        view = self._ledger.view(self._scope)
        return _legacy_usage_from_amounts(
            view.usage.committed.add(view.usage.reserved)
        )

    def snapshot(self) -> dict[str, object]:
        return self.usage.to_dict()

    def canonical_snapshot(self) -> dict[str, Any]:
        if self._scope.scope_id != self._ledger.root_scope.scope_id:
            raise ValueError(
                "canonical budget snapshots are only available from the root tracker"
            )
        return self._ledger.snapshot().to_dict()

    def restore(self, snapshot: dict[str, Any]) -> None:
        if self._scope.scope_id != self._ledger.root_scope.scope_id:
            raise ValueError("budget restore is only supported by the root tracker")
        if snapshot.get("schema_version") is not None:
            snapshot_model = BudgetSnapshot.from_dict(snapshot)
            root_snapshot = next(
                (
                    item
                    for item in snapshot_model.scopes
                    if item.scope.scope_id == snapshot_model.root_scope_id
                ),
                None,
            )
            if root_snapshot is None:
                raise BudgetHistoryError("snapshot root scope is missing")
            if root_snapshot.policy != self._budget_policy:
                raise BudgetHistoryError(
                    "snapshot policy does not match the tracker policy"
                )
            restored = BudgetLedger.restore(
                snapshot_model,
                event_sink=self._event_sink,
            )
        else:
            restored = restore_legacy_budget_snapshot(
                snapshot,
                run_id=self._scope.run_id,
                policy=self._budget_policy,
                scope_id=self._scope.scope_id,
                event_sink=self._event_sink,
            )
        self._ledger = restored
        self._scope = restored.root_scope
        self._budget_policy = restored.view(self._scope).policy
        self._adapter = LLMBudgetAdapter(
            restored,
            scope=self._scope,
            estimator=self.estimator,
        )
        self._local = local()

    def record(self, route_id: str, usage: TokenUsage, cost: float) -> GlobalBudgetCheck:
        operation = self.reserve_operation(
            operation_id=f"legacy-route:{route_id}:{self._adapter.next_identity('call')}",
            idempotency_key=self._adapter.next_identity("idempotency"),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            estimated_cost_usd=cost,
        )
        if isinstance(operation, GlobalBudgetCheck):
            return operation
        return self.settle_operation(
            operation,
            usage,
            estimated_cost_usd=cost,
        )

    def check_before_llm_call(
        self,
        estimated_prompt_tokens: int | None = None,
    ) -> GlobalBudgetCheck:
        requested = BudgetAmounts(
            llm_calls=1,
            input_tokens=_non_negative_token_estimate(estimated_prompt_tokens),
        )
        decision = self._ledger.preflight(self._scope, requested)
        check = self._check_from_decision(decision)
        self._raise_if_required(check)
        return check

    def reserve_llm_call(
        self,
        estimated_prompt_tokens: int | None = None,
    ) -> GlobalBudgetCheck:
        operation = self.reserve_operation(
            operation_id=self._adapter.next_identity("legacy-operation"),
            idempotency_key=self._adapter.next_identity("legacy-idempotency"),
            input_tokens=_non_negative_token_estimate(estimated_prompt_tokens),
        )
        if isinstance(operation, GlobalBudgetCheck):
            return operation
        self._local.pending_operation = operation
        return self._check_from_operation(operation)

    def record_llm_call(
        self,
        usage: TokenUsage,
        pricing: ModelPricing | None = None,
        *,
        estimated_cost_usd: float | None = None,
        replace_reserved_prompt_tokens: int | None = None,
        count_request: bool = True,
    ) -> GlobalBudgetCheck:
        normalized = TokenUsage.from_any(usage)
        pending = getattr(self._local, "pending_operation", None)
        if pending is not None:
            self._local.pending_operation = None
            return self.settle_operation(
                pending,
                normalized,
                pricing,
                estimated_cost_usd=estimated_cost_usd,
            )
        prompt_reserve = (
            normalized.input_tokens
            if replace_reserved_prompt_tokens is None
            else _non_negative_token_estimate(replace_reserved_prompt_tokens)
        )
        call_cost = (
            estimated_cost_usd
            if estimated_cost_usd is not None
            else self.estimator.estimate(normalized, pricing)
        )
        operation = self.reserve_operation(
            operation_id=self._adapter.next_identity("legacy-operation"),
            idempotency_key=self._adapter.next_identity("legacy-idempotency"),
            input_tokens=max(prompt_reserve, normalized.input_tokens),
            output_tokens=normalized.output_tokens,
            reasoning_tokens=normalized.reasoning_tokens,
            cached_input_tokens=normalized.cached_input_tokens,
            estimated_cost_usd=call_cost,
            count_request=count_request,
        )
        if isinstance(operation, GlobalBudgetCheck):
            return operation
        return self.settle_operation(
            operation,
            normalized,
            pricing,
            estimated_cost_usd=call_cost,
        )

    def reserve_operation(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        input_tokens: int,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        cached_input_tokens: int = 0,
        estimated_cost_usd: Decimal | str | float | int = Decimal("0"),
        count_request: bool = True,
    ) -> LLMBudgetOperation | GlobalBudgetCheck:
        requested = BudgetAmounts(
            llm_calls=1 if count_request else 0,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cached_input_tokens=cached_input_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )
        result = self._ledger.reserve(
            self._scope,
            requested,
            operation_id,
            idempotency_key,
        )
        if isinstance(result, BudgetDecision):
            check = self._check_from_decision(
                result,
                operation_id=operation_id,
            )
            self._raise_if_required(check)
            return check
        return LLMBudgetOperation(operation_id=operation_id, reservation=result)

    def reserve_prepared_operation(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        input_tokens: int,
        output_tokens: int,
        pricing: ModelPricing | None,
        count_request: bool = True,
    ) -> LLMBudgetOperation | GlobalBudgetCheck:
        result = self._adapter.reserve_prepared(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            pricing=pricing,
            count_request=count_request,
        )
        if isinstance(result, BudgetDecision):
            check = self._check_from_decision(
                result,
                operation_id=operation_id,
            )
            self._raise_if_required(check)
            return check
        return result

    def available_total_tokens(self) -> int | None:
        """Return the tightest remaining total-token allowance for this scope."""

        return self._ledger.available_total_tokens(self._scope)

    def available_output_tokens(self) -> int | None:
        """Return the tightest remaining output-token allowance for this scope."""

        return self._ledger.view(self._scope).usage.available.output_tokens

    def reserve_direct_operation(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        input_tokens: int,
        output_tokens: int | None,
        count_request: bool = True,
    ) -> LLMBudgetOperation | GlobalBudgetCheck:
        """Reserve a direct client call when no provider context profile is available."""

        self.require_trusted_pricing(None)
        output_ceiling = output_tokens
        if output_ceiling is None:
            remaining_total = self._ledger.available_total_tokens(self._scope)
            output_ceiling = (
                0
                if remaining_total is None
                else max(0, remaining_total - input_tokens)
            )
        return self.reserve_prepared_operation(
            operation_id=operation_id,
            idempotency_key=idempotency_key,
            input_tokens=input_tokens,
            output_tokens=output_ceiling,
            pricing=None,
            count_request=count_request,
        )

    def check_for_operation(self, operation: LLMBudgetOperation) -> GlobalBudgetCheck:
        return self._check_from_operation(operation)

    def settle_operation(
        self,
        operation: LLMBudgetOperation,
        usage: TokenUsage,
        pricing: ModelPricing | None = None,
        *,
        estimated_cost_usd: float | Decimal | str | None = None,
        request_dispatched: bool = True,
        cache_hit: bool = False,
        outcome: str = "succeeded",
        reason_code: str | None = None,
    ) -> GlobalBudgetCheck:
        settlement = self._adapter.settle(
            operation,
            usage,
            pricing,
            estimated_cost_usd=estimated_cost_usd,
            request_dispatched=request_dispatched,
            cache_hit=cache_hit,
            outcome=outcome,
            reason_code=reason_code,
        )
        check = GlobalBudgetCheck(
            usage=self.usage,
            within_budget=settlement.outcome is not BudgetSettlementOutcome.INDETERMINATE,
            violations=(
                (settlement.reason_code or "budget_indeterminate",)
                if settlement.outcome is BudgetSettlementOutcome.INDETERMINATE
                else ()
            ),
            reservation_id=operation.reservation.reservation_id,
            operation_id=operation.operation_id,
            ledger_revision=self._ledger.ledger_revision,
            run_id=operation.reservation.scope.run_id,
            scope_id=operation.reservation.scope.scope_id,
            policy_digest=operation.reservation.policy_digest,
        )
        if not check.within_budget:
            raise GlobalBudgetExceededError(check)
        return check

    def settle_cache_hit(
        self,
        operation: LLMBudgetOperation,
        *,
        observed_usage: TokenUsage | None = None,
    ) -> GlobalBudgetCheck:
        self._adapter.settle_cache_hit(operation, observed_usage=observed_usage)
        return GlobalBudgetCheck(
            usage=self.usage,
            within_budget=True,
            reservation_id=operation.reservation.reservation_id,
            operation_id=operation.operation_id,
            ledger_revision=self._ledger.ledger_revision,
            run_id=operation.reservation.scope.run_id,
            scope_id=operation.reservation.scope.scope_id,
            policy_digest=operation.reservation.policy_digest,
        )

    def release_operation(
        self,
        operation: LLMBudgetOperation,
        *,
        reason: str,
    ) -> GlobalBudgetCheck:
        self._adapter.release(operation, reason=reason)
        return GlobalBudgetCheck(
            usage=self.usage,
            within_budget=True,
            reservation_id=operation.reservation.reservation_id,
            operation_id=operation.operation_id,
            ledger_revision=self._ledger.ledger_revision,
            run_id=operation.reservation.scope.run_id,
            scope_id=operation.reservation.scope.scope_id,
            policy_digest=operation.reservation.policy_digest,
        )

    def mark_operation_indeterminate(
        self,
        operation: LLMBudgetOperation,
        *,
        reason: str,
    ) -> GlobalBudgetCheck:
        self._adapter.mark_indeterminate(operation, reason=reason)
        check = GlobalBudgetCheck(
            usage=self.usage,
            within_budget=False,
            violations=(reason,),
            reservation_id=operation.reservation.reservation_id,
            operation_id=operation.operation_id,
            ledger_revision=self._ledger.ledger_revision,
            run_id=operation.reservation.scope.run_id,
            scope_id=operation.reservation.scope.scope_id,
            policy_digest=operation.reservation.policy_digest,
        )
        return check

    def _check_from_operation(self, operation: LLMBudgetOperation) -> GlobalBudgetCheck:
        return GlobalBudgetCheck(
            usage=self.usage,
            within_budget=True,
            reservation_id=operation.reservation.reservation_id,
            operation_id=operation.operation_id,
            ledger_revision=self._ledger.ledger_revision,
            run_id=operation.reservation.scope.run_id,
            scope_id=operation.reservation.scope.scope_id,
            policy_digest=operation.reservation.policy_digest,
        )

    def _check_from_decision(
        self,
        decision: BudgetDecision,
        *,
        operation_id: str | None = None,
    ) -> GlobalBudgetCheck:
        return GlobalBudgetCheck(
            usage=_legacy_usage_from_amounts(
                decision.projected_usage.committed.add(
                    decision.projected_usage.reserved
                )
            ),
            within_budget=decision.allowed,
            violations=decision.violations,
            reservation_id=decision.reservation_id,
            operation_id=operation_id,
            ledger_revision=decision.ledger_revision,
            run_id=self._scope.run_id,
            scope_id=self._scope.scope_id,
            policy_digest=self._budget_policy.digest,
        )

    def _raise_if_required(self, check: GlobalBudgetCheck) -> None:
        if not check.within_budget and self.policy.on_budget_exceeded == "fail":
            raise GlobalBudgetExceededError(check)


def budget_policy_for_child_allocation(
    allocation: Mapping[str, object],
    *,
    policy_revision: str,
) -> BudgetPolicy:
    """Project a trusted Harness allocation into one canonical ledger policy.

    ``cost_limit`` is expressed in USD micro-units by the Harness contract.
    Its absence leaves the cost dimension unbounded; an explicit zero remains
    a zero cost ceiling.
    """

    if not isinstance(allocation, Mapping):
        raise TypeError("allocation must be a mapping")
    required = {"max_turns", "max_tool_calls", "max_memory_ops", "max_output_tokens"}
    missing = sorted(required - set(allocation))
    if missing:
        raise ValueError(
            "child allocation is missing required fields: " + ", ".join(missing)
        )
    allowed = {
        "max_turns",
        "max_tool_calls",
        "max_memory_ops",
        "max_output_tokens",
        "token_limit",
        "time_limit_ms",
        "cost_limit",
    }
    unknown = sorted(set(allocation) - allowed)
    if unknown:
        raise ValueError(
            "child allocation contains unknown fields: " + ", ".join(unknown)
        )
    values = {
        name: _non_negative_child_allocation_value(allocation[name], name)
        for name in required
    }
    extended = set(allocation) & {"token_limit", "time_limit_ms", "cost_limit"}
    if extended and not {"token_limit", "time_limit_ms"}.issubset(extended):
        raise ValueError(
            "child execution budget requires both token_limit and time_limit_ms"
        )
    token_limit = (
        _non_negative_child_allocation_value(allocation["token_limit"], "token_limit")
        if "token_limit" in allocation
        else None
    )
    if token_limit is not None and values["max_output_tokens"] > token_limit:
        raise ValueError("child max_output_tokens exceeds token_limit")
    cost_limit = None
    if "cost_limit" in allocation:
        cost_limit = Decimal(
            _non_negative_child_allocation_value(
                allocation["cost_limit"],
                "cost_limit",
            )
        ) / Decimal(1_000_000)
    return BudgetPolicy(
        policy_revision=policy_revision,
        limits=BudgetLimits(
            llm_calls=values["max_turns"],
            total_tokens=token_limit,
            output_tokens=values["max_output_tokens"],
            estimated_cost_usd=cost_limit,
        ),
    )


def _canonical_policy(policy: GlobalBudgetPolicy) -> BudgetPolicy:
    return BudgetPolicy(
        policy_revision="legacy-global-budget/v1",
        limits=BudgetLimits(
            llm_calls=policy.max_llm_calls,
            total_tokens=policy.max_total_tokens,
            estimated_cost_usd=policy.max_total_cost_usd,
        ),
    )


def stable_identity_text(identity: GraphExecutionIdentity) -> str:
    return stable_json_dumps(identity.to_dict())


def _legacy_usage_from_amounts(amounts: BudgetAmounts) -> GlobalBudgetUsage:
    return GlobalBudgetUsage(
        llm_calls=amounts.llm_calls,
        token_usage=TokenUsage(
            input_tokens=amounts.input_tokens,
            output_tokens=amounts.output_tokens,
            reasoning_tokens=amounts.reasoning_tokens,
            cached_input_tokens=amounts.cached_input_tokens,
        ),
        estimated_cost_usd=float(amounts.estimated_cost_usd),
    )


def _amounts_from_legacy_usage(usage: GlobalBudgetUsage) -> BudgetAmounts:
    return BudgetAmounts(
        llm_calls=usage.llm_calls,
        input_tokens=usage.token_usage.input_tokens,
        output_tokens=usage.token_usage.output_tokens,
        reasoning_tokens=usage.token_usage.reasoning_tokens,
        cached_input_tokens=usage.token_usage.cached_input_tokens,
        estimated_cost_usd=usage.estimated_cost_usd,
    )


def _non_negative_token_estimate(value: int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("estimated prompt tokens must be an integer")
    if value < 0:
        raise ValueError("estimated prompt tokens must be non-negative")
    return value


def _non_negative_child_allocation_value(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"child allocation {field_name} must be a non-negative integer"
        )
    return value


__all__ = [
    "AuthoritativeBudgetScopeUsage",
    "GLOBAL_BUDGET_COMPATIBILITY_EXPIRES_RELEASE",
    "GLOBAL_BUDGET_COMPATIBILITY_INTRODUCED_RELEASE",
    "GlobalBudgetCheck",
    "GlobalBudgetExceededError",
    "GlobalBudgetGuard",
    "GlobalBudgetTracker",
    "GlobalBudgetUsage",
    "budget_policy_for_child_allocation",
]
