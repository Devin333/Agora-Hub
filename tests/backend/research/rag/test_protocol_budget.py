from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import pytest
from pydantic import ValidationError

from backend.research.ports.evaluation_journal import (
    EvaluationJournal,
    EvaluationJournalConflict,
    EvaluationJournalEvent,
)
from backend.research.rag.evaluation.protocol_budget import (
    AttemptReservation,
    AttemptStateError,
    BudgetAdmissionError,
    CostEstimate,
    CostRecord,
    FailureEvidence,
    LedgerCategory,
    PriceRate,
    PricingMetadata,
    ProtocolBudgetLedger,
    ReservationDisposition,
    RetryApprovalReference,
    UncertainEvidence,
    UsageRecord,
)
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import QuestionScope, ScopedPaper
from backend.research.rag.evaluation.protocol_execution import (
    AttemptIdentity,
    BudgetLimits,
    CaseScopeProjection,
    IsolatedNamespaces,
    PlannedEvaluationUnit,
    RunControl,
    SourceAccessGrant,
)

D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
D4 = "4" * 64
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


class MemoryJournal:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[EvaluationJournalEvent] = []

    def read(self) -> EvaluationJournal:
        with self._lock:
            return EvaluationJournal("memory-evaluation-journal", tuple(self._events))

    def append(
        self,
        *,
        expected_sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> EvaluationJournalEvent:
        event = EvaluationJournalEvent(expected_sequence, event_type, dict(payload))
        with self._lock:
            if len(self._events) > expected_sequence:
                existing = self._events[expected_sequence]
                if existing == event:
                    return existing
                raise EvaluationJournalConflict(
                    expected_sequence=expected_sequence,
                    actual_sequence=len(self._events),
                )
            if len(self._events) != expected_sequence:
                raise EvaluationJournalConflict(
                    expected_sequence=expected_sequence,
                    actual_sequence=len(self._events),
                )
            self._events.append(event)
            return event


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _limits(**overrides: Any) -> BudgetLimits:
    values: dict[str, Any] = {
        "max_requests": 8,
        "max_total_tokens": 2_000,
        "max_cost": None,
        "currency": None,
        "unknown_price": True,
        "unknown_price_reason": "provider price is not available",
        "price_basis_reference": None,
        "request_timeout_seconds": 30.0,
        "case_deadline_seconds": 120.0,
        "run_deadline_seconds": 600.0,
        "max_concurrency": 3,
        "max_retries": 1,
        "max_replans": 1,
        "max_turns": 2,
    }
    values.update(overrides)
    return BudgetLimits(**values)


def _priced_limits(**overrides: Any) -> BudgetLimits:
    values = {
        "max_cost": 1.0,
        "currency": "USD",
        "unknown_price": False,
        "unknown_price_reason": None,
        "price_basis_reference": "pricing://provider/table-v3",
    }
    values.update(overrides)
    return _limits(**values)


def _pricing() -> PricingMetadata:
    return PricingMetadata(
        price_table_version="provider-table-v3",
        currency="USD",
        priced_at=NOW,
        basis_reference="pricing://provider/table-v3",
        rates=(PriceRate(meter="total_tokens", unit="1k_tokens", rate=0.01),),
    )


def _estimate(amount: float | None = None) -> CostEstimate:
    if amount is None:
        return CostEstimate(
            amount=None,
            unknown_reason="no trustworthy provider rate",
            pricing=None,
            estimate_reference="estimate://unknown/provider",
        )
    return CostEstimate(
        amount=amount,
        unknown_reason=None,
        pricing=_pricing(),
        estimate_reference="estimate://provider/table-v3/request",
    )


def _usage(total: int | None, *, reason: str | None = None) -> UsageRecord:
    return UsageRecord(
        input_tokens=None,
        output_tokens=None,
        total_tokens=total,
        unknown_reason=reason,
        evidence_reference="provider://usage/request-1",
    )


def _cost(amount: float | None, *, reason: str | None = None) -> CostRecord:
    return CostRecord(
        amount=amount,
        unknown_reason=reason,
        pricing=None if amount is None else _pricing(),
        evidence_reference="provider://billing/request-1",
    )


def _unit(case_id: str = "case-1") -> PlannedEvaluationUnit:
    return PlannedEvaluationUnit(
        dataset_version="dataset-v1",
        case_id=case_id,
        track=EvaluationTrack.END_TO_END,
        variant_fingerprint=D2,
        repetition=1,
    )


def _attempt(case_id: str, sequence: int = 1, *, attempt_id: str | None = None) -> AttemptIdentity:
    return AttemptIdentity(
        attempt_id=attempt_id or f"attempt-{case_id}-{sequence}",
        unit=_unit(case_id),
        sequence=sequence,
        external_request_id=f"client-request://{case_id}/{sequence}",
    )


def _control(attempt: AttemptIdentity, limits: BudgetLimits) -> RunControl:
    source = ScopedPaper(paper_id="paper-1", document_version="source-v1", source_sha256=D1)
    projection = CaseScopeProjection.create(
        case_id=attempt.unit.case_id,
        scope=QuestionScope(sources=(source,)),
    )
    namespaces = IsolatedNamespaces(
        tenant="eval-tenant",
        index="eval-index",
        memory="eval-memory",
        artifact="eval-artifact",
    )
    return RunControl(
        record_type="run_control",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        run_id="run-1",
        unit=attempt.unit,
        attempt_id=attempt.attempt_id,
        options_fingerprint=D3,
        budget_fingerprint=limits.fingerprint,
        namespace_fingerprint=namespaces.fingerprint,
        case_scope=projection,
        namespaces=namespaces,
        budget_limits=limits,
        source_access_grants=(
            SourceAccessGrant(
                paper_id=source.paper_id,
                document_version=source.document_version,
                source_sha256=source.source_sha256,
                authorization_reference="authorization://evaluation/corpus-v1",
            ),
        ),
    )


def _reservation(
    category: LedgerCategory = LedgerCategory.REQUEST,
    *,
    reservation_id: str = "reservation-1",
    max_tokens: int = 100,
    amount: float | None = None,
    completes_unit: bool = True,
) -> AttemptReservation:
    return AttemptReservation(
        reservation_id=reservation_id,
        category=category,
        max_tokens=max_tokens,
        cost_estimate=_estimate(amount),
        request_timeout_seconds=20.0,
        completes_unit=completes_unit,
    )


def test_cross_thread_reservation_is_atomic_and_provider_is_called_only_after_intent() -> None:
    journal = MemoryJournal()
    limits = _limits(max_requests=1, max_concurrency=1)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    barrier = threading.Barrier(2)
    provider_calls: list[str] = []
    admitted: list[str] = []
    rejected: list[str] = []

    def worker(case_id: str) -> None:
        attempt = _attempt(case_id)
        barrier.wait()
        try:
            ledger.reserve_attempt(
                control=_control(attempt, limits),
                attempt=attempt,
                reservation=_reservation(reservation_id=f"reservation-{case_id}"),
            )
        except BudgetAdmissionError:
            rejected.append(case_id)
            return
        admitted.append(case_id)
        provider_calls.append(case_id)

    threads = [threading.Thread(target=worker, args=(case_id,)) for case_id in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    snapshot = ledger.read()
    assert len(admitted) == len(provider_calls) == 1
    assert len(rejected) == 1
    assert snapshot.reserved_requests == 1
    assert snapshot.remaining_requests == 0
    assert [event.event_type for event in journal.read().events][-1] == "evaluation_attempt_reserved"


def test_recovery_preserves_spend_and_completed_response_is_cached_without_new_charge() -> None:
    journal = MemoryJournal()
    limits = _limits()
    first = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    attempt = _attempt("case-1")
    first.reserve_attempt(
        control=_control(attempt, limits),
        attempt=attempt,
        reservation=_reservation(),
    )
    completed = first.commit_response(
        attempt_id=attempt.attempt_id,
        response_bytes=b'{"answer":"durable"}',
        usage=_usage(37),
        cost=_cost(None, reason="provider omitted billing data"),
    )
    assert completed.response_sha256 == hashlib.sha256(b'{"answer":"durable"}').hexdigest()

    recovered = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    snapshot = recovered.read()
    assert snapshot.spent_requests == 1
    assert snapshot.spent_tokens == 37
    assert snapshot.total_cost is None
    next_attempt = _attempt("case-1", 2)
    cached = recovered.reserve_attempt(
        control=_control(next_attempt, limits),
        attempt=next_attempt,
        reservation=_reservation(reservation_id="reservation-2"),
    )
    assert cached.disposition is ReservationDisposition.CACHED
    assert cached.response_bytes == b'{"answer":"durable"}'
    assert recovered.read().spent_requests == 1
    assert len(recovered.read().attempts) == 1

    with pytest.raises(AttemptStateError, match="different immutable content"):
        recovered.commit_response(
            attempt_id=attempt.attempt_id,
            response_bytes=b"different",
            usage=_usage(37),
            cost=_cost(None, reason="provider omitted billing data"),
        )


def test_unknown_cost_is_null_with_reason_and_never_false_zero() -> None:
    with pytest.raises(ValidationError, match="unknown cost requires unknown_reason"):
        CostRecord(amount=None, unknown_reason=None, pricing=None, evidence_reference="provider://billing/missing")

    journal = MemoryJournal()
    limits = _limits()
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    attempt = _attempt("case-unknown")
    ledger.reserve_attempt(
        control=_control(attempt, limits),
        attempt=attempt,
        reservation=_reservation(reservation_id="unknown-price"),
    )
    ledger.mark_uncertain(
        attempt_id=attempt.attempt_id,
        evidence=UncertainEvidence(
            reason="provider accepted request but response was not persisted",
            provider_received_request=True,
            evidence_reference="provider://receipt/request-unknown",
            provider_may_still_be_active=True,
        ),
    )
    snapshot = ledger.read()
    assert snapshot.total_cost is None
    assert snapshot.known_spent_cost == 0.0
    assert snapshot.unknown_cost_reasons == ("no trustworthy provider rate",)
    assert snapshot.cost_limit_guaranteed is False
    assert snapshot.uncertain_attempt_ids == (attempt.attempt_id,)
    assert snapshot.active_attempt_ids == (attempt.attempt_id,)


def test_uncertain_attempt_has_no_automatic_retry_but_typed_approval_and_budget_allow_one() -> None:
    journal = MemoryJournal()
    limits = _limits(max_concurrency=2, max_requests=2, max_retries=1)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    first = _attempt("case-retry")
    ledger.reserve_attempt(
        control=_control(first, limits),
        attempt=first,
        reservation=_reservation(reservation_id="retry-1"),
    )
    ledger.mark_uncertain(
        attempt_id=first.attempt_id,
        evidence=UncertainEvidence(
            reason="receipt exists but response commit is missing",
            provider_received_request=True,
            evidence_reference="provider://receipt/retry-1",
            provider_may_still_be_active=False,
        ),
    )
    second = _attempt("case-retry", 2)
    with pytest.raises(BudgetAdmissionError, match="explicit human approval"):
        ledger.reserve_attempt(
            control=_control(second, limits),
            attempt=second,
            reservation=_reservation(reservation_id="retry-2"),
        )

    approval = RetryApprovalReference(
        approval_id="approval-1",
        reviewer_id="operator-7",
        reviewer_kind="human",
        approved_at=NOW,
        approval_reference="approval://evaluation/retry-1",
        approval_content_sha256=D4,
        prior_attempt_id=first.attempt_id,
        next_attempt_sequence=2,
        unit_digest=content_digest(second.unit),
        reason="operator confirmed duplicate-risk acceptance after provider inquiry",
    )
    lease = ledger.reserve_attempt(
        control=_control(second, limits),
        attempt=second,
        reservation=_reservation(reservation_id="retry-2"),
        retry_approval=approval,
    )
    assert lease.disposition is ReservationDisposition.RESERVED
    snapshot = ledger.read()
    assert snapshot.spent_requests == 1
    assert snapshot.reserved_requests == 1
    assert snapshot.categories[LedgerCategory.REQUEST].retries == 1
    assert snapshot.remaining_requests == 0


def test_cost_cap_refuses_unpriced_reservation_and_records_explicit_pricing_metadata() -> None:
    journal = MemoryJournal()
    limits = _priced_limits(max_cost=0.02)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    attempt = _attempt("case-priced")
    with pytest.raises(BudgetAdmissionError, match="priced reservation estimate"):
        ledger.reserve_attempt(
            control=_control(attempt, limits),
            attempt=attempt,
            reservation=_reservation(reservation_id="unpriced", amount=None),
        )

    reservation = _reservation(reservation_id="priced", amount=0.015)
    ledger.reserve_attempt(control=_control(attempt, limits), attempt=attempt, reservation=reservation)
    snapshot = ledger.read()
    assert snapshot.reserved_cost == pytest.approx(0.015)
    assert snapshot.remaining_cost == pytest.approx(0.005)
    assert reservation.cost_estimate.pricing.price_table_version == "provider-table-v3"
    assert reservation.cost_estimate.pricing.currency == "USD"
    assert reservation.cost_estimate.pricing.priced_at == NOW


def test_cancel_and_deadline_stop_new_requests_without_erasing_partial_ledger() -> None:
    journal = MemoryJournal()
    clock = Clock()
    limits = _limits(run_deadline_seconds=10.0, case_deadline_seconds=10.0)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=clock)
    first = _attempt("case-cancel")
    ledger.reserve_attempt(
        control=_control(first, limits),
        attempt=first,
        reservation=_reservation(reservation_id="cancel-1"),
    )
    cancelled = ledger.cancel(reason="operator requested stop")
    assert cancelled.cancelled is True
    assert cancelled.reserved_requests == 1
    assert cancelled.active_attempt_ids == (first.attempt_id,)
    with pytest.raises(BudgetAdmissionError, match="cancelled"):
        second = _attempt("case-after-cancel")
        ledger.reserve_attempt(
            control=_control(second, limits),
            attempt=second,
            reservation=_reservation(reservation_id="cancel-2"),
        )

    other_journal = MemoryJournal()
    other_clock = Clock()
    other = ProtocolBudgetLedger(other_journal, run_id="run-1", budget_limits=limits, clock=other_clock)
    other_clock.value += timedelta(seconds=11)
    assert other.read().stop_reason == "run_deadline_expired"
    expired = _attempt("case-expired")
    with pytest.raises(BudgetAdmissionError, match="deadline has expired"):
        other.reserve_attempt(
            control=_control(expired, limits),
            attempt=expired,
            reservation=_reservation(reservation_id="expired"),
        )


def test_index_request_judge_ledgers_and_harness_control_budgets_are_shared() -> None:
    journal = MemoryJournal()
    limits = _limits(max_concurrency=3)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    for index, category in enumerate(LedgerCategory, start=1):
        attempt = _attempt(f"case-{category.value}", attempt_id=f"attempt-{category.value}")
        reservation = _reservation(
            category,
            reservation_id=f"reservation-{category.value}",
            max_tokens=10 * index,
            completes_unit=category is LedgerCategory.REQUEST,
        )
        ledger.reserve_attempt(control=_control(attempt, limits), attempt=attempt, reservation=reservation)
    snapshot = ledger.read()
    assert set(snapshot.categories) == set(LedgerCategory)
    assert all(snapshot.categories[category].reserved_requests == 1 for category in LedgerCategory)
    assert snapshot.reserved_tokens == 60

    assert ledger.record_replan(replan_id="replan-1").replan_count == 1
    with pytest.raises(BudgetAdmissionError, match="max_replans"):
        ledger.record_replan(replan_id="replan-2")
    ledger.record_turn(turn_id="turn-1")
    assert ledger.record_turn(turn_id="turn-2").turn_count == 2
    with pytest.raises(BudgetAdmissionError, match="max_turns"):
        ledger.record_turn(turn_id="turn-3")


def test_known_failure_zero_usage_and_cost_require_real_provider_evidence() -> None:
    journal = MemoryJournal()
    limits = _priced_limits()
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    attempt = _attempt("case-rejected")
    ledger.reserve_attempt(
        control=_control(attempt, limits),
        attempt=attempt,
        reservation=_reservation(reservation_id="reject", amount=0.01),
    )
    snapshot = ledger.mark_known_failure(
        attempt_id=attempt.attempt_id,
        failure=FailureEvidence(
            failure_code="provider_rejected_before_processing",
            reason="provider returned a final validation rejection",
            provider_received_request=True,
            evidence_reference="provider://failure/request-rejected",
        ),
        usage=_usage(0),
        cost=_cost(0.0),
    )
    assert snapshot.spent_requests == 1
    assert snapshot.spent_tokens == 0
    assert snapshot.total_cost == 0.0
    assert snapshot.categories[LedgerCategory.REQUEST].failures == 1





def test_late_reconciliation_preserves_response_history_without_overwriting_completed_unit() -> None:
    journal = MemoryJournal()
    limits = _limits(max_requests=2, max_concurrency=2, max_retries=1)
    ledger = ProtocolBudgetLedger(journal, run_id="run-1", budget_limits=limits, clock=Clock())
    first = _attempt("case-late")
    ledger.reserve_attempt(
        control=_control(first, limits),
        attempt=first,
        reservation=_reservation(reservation_id="late-1"),
    )
    ledger.mark_uncertain(
        attempt_id=first.attempt_id,
        evidence=UncertainEvidence(
            reason="provider receipt exists while local response is missing",
            provider_received_request=True,
            evidence_reference="provider://receipt/late-1",
            provider_may_still_be_active=False,
        ),
    )
    second = _attempt("case-late", 2)
    approval = RetryApprovalReference(
        approval_id="approval-late",
        reviewer_id="operator-7",
        reviewer_kind="human",
        approved_at=NOW,
        approval_reference="approval://evaluation/late-retry",
        approval_content_sha256=D4,
        prior_attempt_id=first.attempt_id,
        next_attempt_sequence=2,
        unit_digest=content_digest(second.unit),
        reason="operator accepted duplicate risk",
    )
    ledger.reserve_attempt(
        control=_control(second, limits),
        attempt=second,
        reservation=_reservation(reservation_id="late-2"),
        retry_approval=approval,
    )
    ledger.commit_response(
        attempt_id=second.attempt_id,
        response_bytes=b"selected retry response",
        usage=_usage(9),
        cost=_cost(None, reason="billing pending"),
    )
    reconciled = ledger.reconcile_response(
        attempt_id=first.attempt_id,
        response_bytes=b"late original response",
        usage=_usage(11),
        cost=_cost(None, reason="billing pending"),
        reconciliation_reference="reconciliation://provider/late-1",
    )

    assert not hasattr(reconciled, "response_bytes")
    selected = ledger.completed_unit(first.unit)
    assert selected.response_bytes == b"selected retry response"
    snapshot = ledger.read()
    assert len(snapshot.attempts) == 2
    assert {entry.response_bytes for entry in snapshot.attempts} == {
        b"selected retry response",
        b"late original response",
    }
    assert snapshot.spent_requests == 2
