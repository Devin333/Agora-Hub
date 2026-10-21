"""Shared durable budget, attempt-intent, recovery and accounting protocol.

The ledger is not a runner and never calls providers. Callers must reserve an
attempt before every real external subcall, then persist a response, known
failure, or uncertain outcome. Recovery replays the injected durable journal and
never resets consumption.

Timeouts and deadlines are admission/accounting boundaries only. They cannot
promise that abandoning a Python thread cancels work accepted by a remote
provider; uncertain attempts retain reservations and block automatic retry until
reconciliation or explicit human approval.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from backend.research.ports.evaluation_journal import EvaluationJournalConflict, EvaluationJournalPort
from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_execution import (
    AttemptIdentity,
    BudgetLimits,
    PlannedEvaluationUnit,
    RunControl,
)

_LEDGER_SCHEMA = "research-rag-evaluation-budget/v1"
_PORTABLE_REF = re.compile(r"^(?![A-Za-z]:[\\/])(?!\\\\)(?!/)(?!file:).+", re.IGNORECASE)
_EPSILON = 1e-9


class LedgerCategory(StrEnum):
    INDEX = "index"
    REQUEST = "request"
    JUDGE = "judge"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    RESPONSE_COMMITTED = "response_committed"
    KNOWN_FAILURE = "known_failure"
    UNCERTAIN = "uncertain"
    RECONCILED_RESPONSE = "reconciled_response"
    RECONCILED_FAILURE = "reconciled_failure"


class ReservationDisposition(StrEnum):
    RESERVED = "reserved"
    CACHED = "cached"


class BudgetAdmissionError(RuntimeError):
    """A new external call cannot be admitted under the durable limits."""


class AttemptStateError(RuntimeError):
    """An attempt transition conflicts with immutable journal history."""


class PriceRate(ProtocolModel):
    meter: NonEmptyStr
    unit: NonEmptyStr
    rate: float

    @field_validator("rate", mode="before")
    @classmethod
    def _valid_rate(cls, value: Any) -> Any:
        return _non_negative_number(value, "rate")


class PricingMetadata(ProtocolModel):
    price_table_version: ImmutableVersionStr
    currency: NonEmptyStr
    priced_at: datetime
    basis_reference: NonEmptyStr
    rates: tuple[PriceRate, ...] = Field(min_length=1)

    @field_validator("priced_at")
    @classmethod
    def _aware_priced_at(cls, value: datetime) -> datetime:
        return _aware(value, "priced_at")

    @field_validator("basis_reference")
    @classmethod
    def _portable_basis(cls, value: str) -> str:
        return _portable_reference(value, "basis_reference")

    @model_validator(mode="after")
    def _unique_rates(self) -> Self:
        keys = tuple((item.meter, item.unit) for item in self.rates)
        if len(keys) != len(set(keys)):
            raise ValueError("pricing rates must have unique meter/unit identities")
        return self


class CostEstimate(ProtocolModel):
    amount: float | None
    unknown_reason: NonEmptyStr | None
    pricing: PricingMetadata | None
    estimate_reference: NonEmptyStr

    @field_validator("amount", mode="before")
    @classmethod
    def _valid_amount(cls, value: Any) -> Any:
        return None if value is None else _non_negative_number(value, "amount")

    @field_validator("estimate_reference")
    @classmethod
    def _portable_estimate(cls, value: str) -> str:
        return _portable_reference(value, "estimate_reference")

    @model_validator(mode="after")
    def _known_or_unknown(self) -> Self:
        if self.amount is None:
            if self.unknown_reason is None:
                raise ValueError("unknown estimate amount requires unknown_reason")
        elif self.unknown_reason is not None or self.pricing is None:
            raise ValueError("known estimate amount requires pricing and null unknown_reason")
        return self


class UsageRecord(ProtocolModel):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    unknown_reason: NonEmptyStr | None
    evidence_reference: NonEmptyStr

    @field_validator("input_tokens", "output_tokens", "total_tokens", mode="before")
    @classmethod
    def _valid_tokens(cls, value: Any, info: Any) -> Any:
        return None if value is None else _non_negative_int(value, info.field_name)

    @field_validator("evidence_reference")
    @classmethod
    def _portable_evidence(cls, value: str) -> str:
        return _portable_reference(value, "evidence_reference")

    @model_validator(mode="after")
    def _validate_usage(self) -> Self:
        if self.total_tokens is None:
            if self.unknown_reason is None:
                raise ValueError("unknown total_tokens requires unknown_reason")
        else:
            if self.unknown_reason is not None:
                raise ValueError("known total_tokens requires null unknown_reason")
            if self.input_tokens is not None and self.output_tokens is not None:
                if self.input_tokens + self.output_tokens != self.total_tokens:
                    raise ValueError("input_tokens plus output_tokens must equal total_tokens")
        return self


class CostRecord(ProtocolModel):
    amount: float | None
    unknown_reason: NonEmptyStr | None
    pricing: PricingMetadata | None
    evidence_reference: NonEmptyStr

    @field_validator("amount", mode="before")
    @classmethod
    def _valid_amount(cls, value: Any) -> Any:
        return None if value is None else _non_negative_number(value, "amount")

    @field_validator("evidence_reference")
    @classmethod
    def _portable_evidence(cls, value: str) -> str:
        return _portable_reference(value, "evidence_reference")

    @model_validator(mode="after")
    def _validate_cost(self) -> Self:
        if self.amount is None:
            if self.unknown_reason is None:
                raise ValueError("unknown cost requires unknown_reason; it must not be encoded as zero")
        elif self.unknown_reason is not None or self.pricing is None:
            raise ValueError("known cost requires pricing and null unknown_reason")
        return self


class RetryApprovalReference(ProtocolModel):
    approval_id: NonEmptyStr
    reviewer_id: NonEmptyStr
    reviewer_kind: Literal["human"]
    approved_at: datetime
    approval_reference: NonEmptyStr
    approval_content_sha256: Sha256Digest
    prior_attempt_id: NonEmptyStr
    next_attempt_sequence: int
    unit_digest: Sha256Digest
    reason: NonEmptyStr

    @field_validator("approved_at")
    @classmethod
    def _aware_approval(cls, value: datetime) -> datetime:
        return _aware(value, "approved_at")

    @field_validator("approval_reference")
    @classmethod
    def _portable_approval(cls, value: str) -> str:
        return _portable_reference(value, "approval_reference")

    @field_validator("next_attempt_sequence", mode="before")
    @classmethod
    def _positive_sequence(cls, value: Any) -> Any:
        value = _non_negative_int(value, "next_attempt_sequence")
        if value == 0:
            raise ValueError("next_attempt_sequence must be positive")
        return value


class FailureEvidence(ProtocolModel):
    failure_code: NonEmptyStr
    reason: NonEmptyStr
    provider_received_request: bool
    evidence_reference: NonEmptyStr

    @field_validator("evidence_reference")
    @classmethod
    def _portable_failure(cls, value: str) -> str:
        return _portable_reference(value, "evidence_reference")


class UncertainEvidence(ProtocolModel):
    reason: NonEmptyStr
    provider_received_request: Literal[True]
    evidence_reference: NonEmptyStr
    provider_may_still_be_active: bool = True

    @field_validator("evidence_reference")
    @classmethod
    def _portable_uncertain(cls, value: str) -> str:
        return _portable_reference(value, "evidence_reference")


class AttemptReservation(ProtocolModel):
    reservation_id: NonEmptyStr
    category: LedgerCategory
    max_tokens: int
    cost_estimate: CostEstimate
    request_timeout_seconds: float
    completes_unit: bool = False

    @field_validator("max_tokens", mode="before")
    @classmethod
    def _valid_tokens(cls, value: Any) -> Any:
        return _non_negative_int(value, "max_tokens")

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _valid_timeout(cls, value: Any) -> Any:
        value = _non_negative_number(value, "request_timeout_seconds")
        if value == 0:
            raise ValueError("request_timeout_seconds must be positive")
        return value

    @model_validator(mode="after")
    def _completion_category(self) -> Self:
        if self.completes_unit and self.category is not LedgerCategory.REQUEST:
            raise ValueError("only request attempts may complete a planned evaluation unit")
        return self


@dataclass(frozen=True, slots=True)
class AttemptLease:
    disposition: ReservationDisposition
    attempt: AttemptIdentity
    reservation: AttemptReservation
    reserved_at: datetime
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class CompletedUnitResult:
    disposition: ReservationDisposition
    attempt: AttemptIdentity
    response_bytes: bytes
    response_sha256: str
    usage: UsageRecord
    cost: CostRecord


@dataclass(frozen=True, slots=True)
class CategoryLedger:
    category: LedgerCategory
    spent_requests: int
    reserved_requests: int
    spent_tokens: int
    reserved_tokens: int
    known_spent_cost: float
    reserved_cost: float
    total_cost: float | None
    unknown_cost_reasons: tuple[str, ...]
    failures: int
    retries: int


@dataclass(frozen=True, slots=True)
class AttemptLedgerEntry:
    attempt: AttemptIdentity
    reservation: AttemptReservation
    status: AttemptStatus
    reserved_at: datetime
    usage: UsageRecord | None
    cost: CostRecord | None
    failure: FailureEvidence | None
    uncertain: UncertainEvidence | None
    response_sha256: str | None
    response_bytes: bytes | None
    retry_approval: RetryApprovalReference | None


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    run_id: str
    budget_limits: BudgetLimits
    started_at: datetime
    next_sequence: int
    attempts: tuple[AttemptLedgerEntry, ...]
    categories: Mapping[LedgerCategory, CategoryLedger]
    spent_requests: int
    reserved_requests: int
    spent_tokens: int
    reserved_tokens: int
    known_spent_cost: float
    reserved_cost: float
    total_cost: float | None
    unknown_cost_reasons: tuple[str, ...]
    remaining_requests: int
    remaining_tokens: int
    remaining_cost: float | None
    remaining_concurrency: int
    active_attempt_ids: tuple[str, ...]
    uncertain_attempt_ids: tuple[str, ...]
    replan_count: int
    turn_count: int
    cancelled: bool
    stop_reason: str | None
    cost_limit_guaranteed: bool


@dataclass(slots=True)
class _AttemptState:
    attempt: AttemptIdentity
    control: RunControl
    reservation: AttemptReservation
    status: AttemptStatus
    reserved_at: datetime
    retry_approval: RetryApprovalReference | None
    usage: UsageRecord | None = None
    cost: CostRecord | None = None
    failure: FailureEvidence | None = None
    uncertain: UncertainEvidence | None = None
    response_sha256: str | None = None
    response_bytes: bytes | None = None


@dataclass(slots=True)
class _ReplayState:
    run_id: str
    limits: BudgetLimits
    started_at: datetime
    next_sequence: int
    attempts: dict[str, _AttemptState]
    completed_units: dict[str, str]
    operations: set[str]
    replans: set[str]
    turns: set[str]
    cancelled: bool
    cancel_reason: str | None

class ProtocolBudgetLedger:
    """Journal-backed shared admission and accounting service."""

    def __init__(
        self,
        journal: EvaluationJournalPort,
        *,
        run_id: str,
        budget_limits: BudgetLimits,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(run_id) is not str or not run_id.strip() or run_id != run_id.strip():
            raise ValueError("run_id must be a non-empty normalized string")
        self._journal = journal
        self._run_id = run_id
        self._limits = budget_limits
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ensure_initialized()

    def read(self) -> BudgetSnapshot:
        state = self._replay()
        return self._snapshot(state, self._now())

    def reserve_attempt(
        self,
        *,
        control: RunControl,
        attempt: AttemptIdentity,
        reservation: AttemptReservation,
        retry_approval: RetryApprovalReference | None = None,
    ) -> AttemptLease | CompletedUnitResult:
        operation_id = f"reserve:{attempt.attempt_id}"
        while True:
            state = self._replay()
            self._validate_control_binding(state, control, attempt)
            existing = state.attempts.get(attempt.attempt_id)
            if existing is not None:
                if (
                    existing.attempt == attempt
                    and existing.control == control
                    and existing.reservation == reservation
                    and existing.retry_approval == retry_approval
                ):
                    return self._existing_reservation_result(state, existing)
                raise AttemptStateError("attempt_id is already bound to different reservation content")

            unit_key = _unit_digest(attempt.unit)
            completed_attempt_id = state.completed_units.get(unit_key)
            if reservation.category is LedgerCategory.REQUEST and completed_attempt_id is not None:
                return self._completed_result(state.attempts[completed_attempt_id])

            now = self._now()
            self._validate_reservation(state, control, attempt, reservation, retry_approval, now)
            payload = self._base_payload(operation_id) | {
                "control": control.model_dump(mode="json"),
                "attempt": attempt.model_dump(mode="json"),
                "reservation": reservation.model_dump(mode="json"),
                "retry_approval": None if retry_approval is None else retry_approval.model_dump(mode="json"),
                "reserved_at": now.isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_attempt_reserved",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            deadline = min(
                state.started_at + timedelta(seconds=state.limits.run_deadline_seconds),
                now + timedelta(seconds=state.limits.case_deadline_seconds),
            )
            return AttemptLease(
                disposition=ReservationDisposition.RESERVED,
                attempt=attempt,
                reservation=reservation,
                reserved_at=now,
                deadline_at=deadline,
            )

    def commit_response(
        self,
        *,
        attempt_id: str,
        response_bytes: bytes,
        usage: UsageRecord,
        cost: CostRecord,
    ) -> CompletedUnitResult | BudgetSnapshot:
        return self._record_response(
            attempt_id=attempt_id,
            response_bytes=response_bytes,
            usage=usage,
            cost=cost,
            reconciled=False,
            reconciliation_reference=None,
        )

    def mark_known_failure(
        self,
        *,
        attempt_id: str,
        failure: FailureEvidence,
        usage: UsageRecord,
        cost: CostRecord,
    ) -> BudgetSnapshot:
        operation_id = f"outcome:{attempt_id}"
        while True:
            state = self._replay()
            current = self._require_attempt(state, attempt_id)
            if current.status is AttemptStatus.KNOWN_FAILURE:
                if current.failure == failure and current.usage == usage and current.cost == cost:
                    return self._snapshot(state, self._now())
                raise AttemptStateError("known failure is already committed with different content")
            if current.status is not AttemptStatus.RESERVED:
                raise AttemptStateError(f"cannot record known failure from {current.status}")
            self._validate_actual_cost(cost, state.limits)
            payload = self._base_payload(operation_id) | {
                "attempt_id": attempt_id,
                "status": AttemptStatus.KNOWN_FAILURE,
                "failure": failure.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
                "cost": cost.model_dump(mode="json"),
                "recorded_at": self._now().isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_attempt_outcome",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return self.read()

    def mark_uncertain(
        self,
        *,
        attempt_id: str,
        evidence: UncertainEvidence,
        usage: UsageRecord | None = None,
        cost: CostRecord | None = None,
    ) -> BudgetSnapshot:
        operation_id = f"outcome:{attempt_id}"
        while True:
            state = self._replay()
            current = self._require_attempt(state, attempt_id)
            if current.status is AttemptStatus.UNCERTAIN:
                if current.uncertain == evidence and current.usage == usage and current.cost == cost:
                    return self._snapshot(state, self._now())
                raise AttemptStateError("uncertain outcome is already committed with different content")
            if current.status is not AttemptStatus.RESERVED:
                raise AttemptStateError(f"cannot mark uncertain from {current.status}")
            if cost is not None:
                self._validate_actual_cost(cost, state.limits)
            payload = self._base_payload(operation_id) | {
                "attempt_id": attempt_id,
                "status": AttemptStatus.UNCERTAIN,
                "uncertain": evidence.model_dump(mode="json"),
                "usage": None if usage is None else usage.model_dump(mode="json"),
                "cost": None if cost is None else cost.model_dump(mode="json"),
                "recorded_at": self._now().isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_attempt_outcome",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return self.read()

    def reconcile_response(
        self,
        *,
        attempt_id: str,
        response_bytes: bytes,
        usage: UsageRecord,
        cost: CostRecord,
        reconciliation_reference: str,
    ) -> CompletedUnitResult | BudgetSnapshot:
        return self._record_response(
            attempt_id=attempt_id,
            response_bytes=response_bytes,
            usage=usage,
            cost=cost,
            reconciled=True,
            reconciliation_reference=_portable_reference(
                reconciliation_reference, "reconciliation_reference"
            ),
        )

    def reconcile_failure(
        self,
        *,
        attempt_id: str,
        failure: FailureEvidence,
        usage: UsageRecord,
        cost: CostRecord,
        reconciliation_reference: str,
    ) -> BudgetSnapshot:
        reference = _portable_reference(reconciliation_reference, "reconciliation_reference")
        operation_id = f"reconcile:{attempt_id}"
        while True:
            state = self._replay()
            current = self._require_attempt(state, attempt_id)
            if current.status is AttemptStatus.RECONCILED_FAILURE:
                if current.failure == failure and current.usage == usage and current.cost == cost:
                    return self._snapshot(state, self._now())
                raise AttemptStateError("reconciled failure is already bound to different content")
            if current.status is not AttemptStatus.UNCERTAIN:
                raise AttemptStateError("only an uncertain attempt can be reconciled")
            self._validate_actual_cost(cost, state.limits)
            payload = self._base_payload(operation_id) | {
                "attempt_id": attempt_id,
                "status": AttemptStatus.RECONCILED_FAILURE,
                "failure": failure.model_dump(mode="json"),
                "usage": usage.model_dump(mode="json"),
                "cost": cost.model_dump(mode="json"),
                "reconciliation_reference": reference,
                "recorded_at": self._now().isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_attempt_reconciled",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return self.read()

    def completed_unit(self, unit: PlannedEvaluationUnit) -> CompletedUnitResult | None:
        state = self._replay()
        attempt_id = state.completed_units.get(_unit_digest(unit))
        return None if attempt_id is None else self._completed_result(state.attempts[attempt_id])

    def record_replan(self, *, replan_id: str) -> BudgetSnapshot:
        return self._record_control(kind="replan", control_id=replan_id)

    def record_turn(self, *, turn_id: str) -> BudgetSnapshot:
        return self._record_control(kind="turn", control_id=turn_id)

    def cancel(self, *, reason: str) -> BudgetSnapshot:
        if type(reason) is not str or not reason.strip():
            raise ValueError("cancellation reason must be a non-empty string")
        operation_id = "cancel-run"
        while True:
            state = self._replay()
            if state.cancelled:
                if state.cancel_reason == reason.strip():
                    return self._snapshot(state, self._now())
                raise AttemptStateError("run is already cancelled with a different reason")
            payload = self._base_payload(operation_id) | {
                "reason": reason.strip(),
                "cancelled_at": self._now().isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_run_cancelled",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return self.read()

    def _record_response(
        self,
        *,
        attempt_id: str,
        response_bytes: bytes,
        usage: UsageRecord,
        cost: CostRecord,
        reconciled: bool,
        reconciliation_reference: str | None,
    ) -> CompletedUnitResult | BudgetSnapshot:
        if type(response_bytes) is not bytes:
            raise TypeError("response_bytes must be immutable bytes")
        digest = hashlib.sha256(response_bytes).hexdigest()
        target = AttemptStatus.RECONCILED_RESPONSE if reconciled else AttemptStatus.RESPONSE_COMMITTED
        operation_id = f"reconcile:{attempt_id}" if reconciled else f"outcome:{attempt_id}"
        while True:
            state = self._replay()
            current = self._require_attempt(state, attempt_id)
            if current.status is target:
                if (
                    current.response_sha256 == digest
                    and current.response_bytes == response_bytes
                    and current.usage == usage
                    and current.cost == cost
                ):
                    owner = state.completed_units.get(_unit_digest(current.attempt.unit))
                    return (
                        self._completed_result(current)
                        if current.reservation.completes_unit and owner == attempt_id
                        else self._snapshot(state, self._now())
                    )
                raise AttemptStateError("response is already committed with different immutable content")
            expected = AttemptStatus.UNCERTAIN if reconciled else AttemptStatus.RESERVED
            if current.status is not expected:
                raise AttemptStateError(f"cannot commit {target} from {current.status}")
            self._validate_actual_cost(cost, state.limits)
            payload = self._base_payload(operation_id) | {
                "attempt_id": attempt_id,
                "status": target,
                "response_base64": base64.b64encode(response_bytes).decode("ascii"),
                "response_sha256": digest,
                "usage": usage.model_dump(mode="json"),
                "cost": cost.model_dump(mode="json"),
                "reconciliation_reference": reconciliation_reference,
                "recorded_at": self._now().isoformat(),
            }
            event_type = "evaluation_attempt_reconciled" if reconciled else "evaluation_attempt_outcome"
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type=event_type,
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            updated = self._replay()
            entry = updated.attempts[attempt_id]
            owner = updated.completed_units.get(_unit_digest(entry.attempt.unit))
            return (
                self._completed_result(entry)
                if entry.reservation.completes_unit and owner == attempt_id
                else self._snapshot(updated, self._now())
            )

    def _record_control(self, *, kind: Literal["replan", "turn"], control_id: str) -> BudgetSnapshot:
        if type(control_id) is not str or not control_id.strip():
            raise ValueError(f"{kind}_id must be a non-empty string")
        control_id = control_id.strip()
        operation_id = f"{kind}:{control_id}"
        while True:
            state = self._replay()
            collection = state.replans if kind == "replan" else state.turns
            if control_id in collection:
                return self._snapshot(state, self._now())
            limit = state.limits.max_replans if kind == "replan" else state.limits.max_turns
            if len(collection) >= limit:
                raise BudgetAdmissionError(f"max_{kind}s budget is exhausted")
            payload = self._base_payload(operation_id) | {
                "kind": kind,
                "control_id": control_id,
                "recorded_at": self._now().isoformat(),
            }
            try:
                self._journal.append(
                    expected_sequence=state.next_sequence,
                    event_type="evaluation_control_consumed",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return self.read()

    def _ensure_initialized(self) -> None:
        while True:
            journal = self._journal.read()
            if journal.events:
                self._replay()
                return
            now = self._now()
            payload = self._base_payload("initialize-ledger") | {
                "budget_limits": self._limits.model_dump(mode="json"),
                "budget_fingerprint": self._limits.fingerprint,
                "started_at": now.isoformat(),
                "categories": [item.value for item in LedgerCategory],
            }
            try:
                self._journal.append(
                    expected_sequence=0,
                    event_type="evaluation_budget_initialized",
                    payload=payload,
                )
            except EvaluationJournalConflict:
                continue
            return

    def _replay(self) -> _ReplayState:
        journal = self._journal.read()
        if not journal.events:
            raise AttemptStateError("evaluation budget journal is not initialized")
        first = journal.events[0]
        if first.event_type != "evaluation_budget_initialized":
            raise AttemptStateError("evaluation journal does not start with budget initialization")
        initial = dict(first.payload)
        self._validate_envelope(initial)
        limits = BudgetLimits.model_validate(initial["budget_limits"], strict=False)
        if initial.get("budget_fingerprint") != limits.fingerprint:
            raise AttemptStateError("journal budget fingerprint does not match serialized limits")
        if limits.fingerprint != self._limits.fingerprint:
            raise AttemptStateError("recovery budget limits differ from the durable journal")
        started_at = _parse_time(initial["started_at"], "started_at")
        state = _ReplayState(
            run_id=self._run_id,
            limits=limits,
            started_at=started_at,
            next_sequence=journal.next_sequence,
            attempts={},
            completed_units={},
            operations={str(initial["operation_id"])},
            replans=set(),
            turns=set(),
            cancelled=False,
            cancel_reason=None,
        )
        for event in journal.events[1:]:
            payload = dict(event.payload)
            self._validate_envelope(payload)
            operation_id = str(payload["operation_id"])
            if operation_id in state.operations:
                raise AttemptStateError("journal contains a duplicate operation_id")
            state.operations.add(operation_id)
            if event.event_type == "evaluation_attempt_reserved":
                self._apply_reserved(state, payload)
            elif event.event_type in {"evaluation_attempt_outcome", "evaluation_attempt_reconciled"}:
                self._apply_outcome(state, payload, reconciled=event.event_type.endswith("reconciled"))
            elif event.event_type == "evaluation_control_consumed":
                kind = payload.get("kind")
                if kind not in {"replan", "turn"}:
                    raise AttemptStateError("journal control kind is invalid")
                collection = state.replans if kind == "replan" else state.turns
                collection.add(str(payload["control_id"]))
            elif event.event_type == "evaluation_run_cancelled":
                if state.cancelled:
                    raise AttemptStateError("journal cancels one run more than once")
                state.cancelled = True
                state.cancel_reason = str(payload["reason"])
            else:
                raise AttemptStateError(f"unsupported evaluation journal event: {event.event_type}")
        return state

    def _apply_reserved(self, state: _ReplayState, payload: Mapping[str, Any]) -> None:
        control = RunControl.model_validate(payload["control"], strict=False)
        attempt = AttemptIdentity.model_validate(payload["attempt"], strict=False)
        reservation = AttemptReservation.model_validate(payload["reservation"], strict=False)
        approval_value = payload.get("retry_approval")
        approval = None if approval_value is None else RetryApprovalReference.model_validate(approval_value, strict=False)
        if attempt.attempt_id in state.attempts:
            raise AttemptStateError("journal reuses an attempt_id")
        if control.attempt_id != attempt.attempt_id or control.unit != attempt.unit:
            raise AttemptStateError("journal attempt does not match its RunControl")
        state.attempts[attempt.attempt_id] = _AttemptState(
            attempt=attempt,
            control=control,
            reservation=reservation,
            status=AttemptStatus.RESERVED,
            reserved_at=_parse_time(payload["reserved_at"], "reserved_at"),
            retry_approval=approval,
        )

    def _apply_outcome(self, state: _ReplayState, payload: Mapping[str, Any], *, reconciled: bool) -> None:
        attempt_id = str(payload["attempt_id"])
        current = self._require_attempt(state, attempt_id)
        status = AttemptStatus(payload["status"])
        allowed = (
            {AttemptStatus.RECONCILED_RESPONSE, AttemptStatus.RECONCILED_FAILURE}
            if reconciled
            else {AttemptStatus.RESPONSE_COMMITTED, AttemptStatus.KNOWN_FAILURE, AttemptStatus.UNCERTAIN}
        )
        if status not in allowed:
            raise AttemptStateError("journal outcome status does not match event type")
        expected = AttemptStatus.UNCERTAIN if reconciled else AttemptStatus.RESERVED
        if current.status is not expected:
            raise AttemptStateError("journal contains an invalid attempt state transition")
        current.status = status
        usage_value = payload.get("usage")
        cost_value = payload.get("cost")
        current.usage = None if usage_value is None else UsageRecord.model_validate(usage_value, strict=False)
        current.cost = None if cost_value is None else CostRecord.model_validate(cost_value, strict=False)
        if status in {AttemptStatus.RESPONSE_COMMITTED, AttemptStatus.RECONCILED_RESPONSE}:
            encoded = str(payload["response_base64"])
            try:
                response = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise AttemptStateError("journal response_base64 is invalid") from error
            digest = hashlib.sha256(response).hexdigest()
            if digest != payload["response_sha256"]:
                raise AttemptStateError("journal response bytes do not match response_sha256")
            if current.usage is None or current.cost is None:
                raise AttemptStateError("committed responses require usage and cost records")
            current.response_bytes = response
            current.response_sha256 = digest
            if current.reservation.completes_unit:
                key = _unit_digest(current.attempt.unit)
                state.completed_units.setdefault(key, attempt_id)
        elif status in {AttemptStatus.KNOWN_FAILURE, AttemptStatus.RECONCILED_FAILURE}:
            current.failure = FailureEvidence.model_validate(payload["failure"], strict=False)
            if current.usage is None or current.cost is None:
                raise AttemptStateError("known failures require explicit usage and cost records")
        else:
            current.uncertain = UncertainEvidence.model_validate(payload["uncertain"], strict=False)

    def _validate_reservation(
        self,
        state: _ReplayState,
        control: RunControl,
        attempt: AttemptIdentity,
        reservation: AttemptReservation,
        retry_approval: RetryApprovalReference | None,
        now: datetime,
    ) -> None:
        if state.cancelled:
            raise BudgetAdmissionError(f"run is cancelled: {state.cancel_reason}")
        self._validate_control_binding(state, control, attempt)
        if any(
            item.reservation.reservation_id == reservation.reservation_id
            for item in state.attempts.values()
        ):
            raise BudgetAdmissionError("reservation_id is already bound to another attempt")
        if reservation.request_timeout_seconds > state.limits.request_timeout_seconds + _EPSILON:
            raise BudgetAdmissionError("attempt timeout exceeds request_timeout_seconds")
        run_deadline = state.started_at + timedelta(seconds=state.limits.run_deadline_seconds)
        if now >= run_deadline:
            raise BudgetAdmissionError("run deadline has expired; no new requests are allowed")
        same_unit = [
            item
            for item in state.attempts.values()
            if item.attempt.unit == attempt.unit and item.reservation.category is reservation.category
        ]
        expected_sequence = len(same_unit) + 1
        if attempt.sequence != expected_sequence:
            raise BudgetAdmissionError(
                f"attempt sequence must be {expected_sequence} for this unit/category"
            )
        if attempt.sequence - 1 > state.limits.max_retries:
            raise BudgetAdmissionError("retry budget is exhausted")
        if same_unit:
            previous = max(same_unit, key=lambda item: item.attempt.sequence)
            if previous.status is AttemptStatus.RESERVED:
                raise BudgetAdmissionError("previous attempt is still active")
            if previous.status is AttemptStatus.UNCERTAIN:
                self._validate_retry_approval(previous, attempt, retry_approval)
            elif retry_approval is not None:
                raise BudgetAdmissionError("retry approval is only valid for an unresolved uncertain attempt")
        elif retry_approval is not None:
            raise BudgetAdmissionError("initial attempts must not carry retry approval")
        case_start = min(
            (item.reserved_at for item in state.attempts.values() if item.attempt.unit == attempt.unit),
            default=now,
        )
        if now >= case_start + timedelta(seconds=state.limits.case_deadline_seconds):
            raise BudgetAdmissionError("case deadline has expired; no new requests are allowed")

        snapshot = self._snapshot(state, now)
        if snapshot.spent_requests + snapshot.reserved_requests + 1 > state.limits.max_requests:
            raise BudgetAdmissionError("request budget is exhausted")
        if snapshot.spent_tokens + snapshot.reserved_tokens + reservation.max_tokens > state.limits.max_total_tokens:
            raise BudgetAdmissionError("token budget is exhausted")
        if snapshot.remaining_concurrency <= 0:
            raise BudgetAdmissionError("concurrency budget is exhausted")
        self._validate_estimate(reservation.cost_estimate, state.limits)
        if state.limits.max_cost is not None:
            estimate = reservation.cost_estimate.amount
            if estimate is None:
                raise BudgetAdmissionError("cost cap cannot be guaranteed without a trustworthy rate estimate")
            projected = snapshot.known_spent_cost + snapshot.reserved_cost + estimate
            if projected > state.limits.max_cost + _EPSILON:
                raise BudgetAdmissionError("cost budget is exhausted")

    def _validate_control_binding(
        self,
        state: _ReplayState,
        control: RunControl,
        attempt: AttemptIdentity,
    ) -> None:
        if control.run_id != self._run_id:
            raise BudgetAdmissionError("RunControl run_id does not match the shared ledger")
        if control.attempt_id != attempt.attempt_id or control.unit != attempt.unit:
            raise BudgetAdmissionError("RunControl does not match AttemptIdentity")
        if control.budget_limits.fingerprint != state.limits.fingerprint:
            raise BudgetAdmissionError("RunControl budget does not match the durable shared ledger")

    @staticmethod
    def _validate_retry_approval(
        previous: _AttemptState,
        attempt: AttemptIdentity,
        approval: RetryApprovalReference | None,
    ) -> None:
        if approval is None:
            raise BudgetAdmissionError(
                "uncertain provider receipt blocks automatic retry; explicit human approval is required"
            )
        if (
            approval.prior_attempt_id != previous.attempt.attempt_id
            or approval.next_attempt_sequence != attempt.sequence
            or approval.unit_digest != _unit_digest(attempt.unit)
        ):
            raise BudgetAdmissionError("retry approval does not authorize this exact next attempt")

    @staticmethod
    def _validate_estimate(estimate: CostEstimate, limits: BudgetLimits) -> None:
        if limits.max_cost is None:
            return
        pricing = estimate.pricing
        if estimate.amount is None or pricing is None:
            raise BudgetAdmissionError("known max_cost requires a priced reservation estimate")
        if pricing.currency != limits.currency:
            raise BudgetAdmissionError("reservation pricing currency differs from BudgetLimits")
        if pricing.basis_reference != limits.price_basis_reference:
            raise BudgetAdmissionError("reservation pricing basis differs from BudgetLimits")

    @staticmethod
    def _validate_actual_cost(cost: CostRecord, limits: BudgetLimits) -> None:
        if cost.amount is None or cost.pricing is None:
            return
        if limits.currency is not None and cost.pricing.currency != limits.currency:
            raise AttemptStateError("actual cost currency differs from BudgetLimits")

    def _snapshot(self, state: _ReplayState, now: datetime) -> BudgetSnapshot:
        category_values: dict[LedgerCategory, CategoryLedger] = {}
        all_unknown: list[str] = []
        total_spent_requests = total_reserved_requests = 0
        total_spent_tokens = total_reserved_tokens = 0
        total_known_cost = total_reserved_cost = 0.0
        active: list[str] = []
        uncertain: list[str] = []
        entries: list[AttemptLedgerEntry] = []
        for category in LedgerCategory:
            attempts = [item for item in state.attempts.values() if item.reservation.category is category]
            spent_requests = reserved_requests = spent_tokens = reserved_tokens = failures = retries = 0
            known_cost = reserved_cost = 0.0
            unknown_reasons: list[str] = []
            for item in attempts:
                retries += int(item.attempt.sequence > 1)
                if item.status is AttemptStatus.RESERVED:
                    reserved_requests += 1
                    reserved_tokens += item.reservation.max_tokens
                    if item.reservation.cost_estimate.amount is None:
                        unknown_reasons.append(item.reservation.cost_estimate.unknown_reason or "unknown estimate")
                    else:
                        reserved_cost += item.reservation.cost_estimate.amount
                    active.append(item.attempt.attempt_id)
                else:
                    spent_requests += 1
                    if item.usage is None or item.usage.total_tokens is None:
                        reserved_tokens += item.reservation.max_tokens
                    else:
                        spent_tokens += item.usage.total_tokens
                    if item.cost is None or item.cost.amount is None:
                        if item.reservation.cost_estimate.amount is None:
                            reason = (
                                item.cost.unknown_reason if item.cost is not None else None
                            ) or item.reservation.cost_estimate.unknown_reason or "unknown cost"
                            unknown_reasons.append(reason)
                        else:
                            reserved_cost += item.reservation.cost_estimate.amount
                            if item.cost is not None and item.cost.unknown_reason:
                                unknown_reasons.append(item.cost.unknown_reason)
                    else:
                        known_cost += item.cost.amount
                    if item.status in {AttemptStatus.KNOWN_FAILURE, AttemptStatus.RECONCILED_FAILURE}:
                        failures += 1
                    if item.status is AttemptStatus.UNCERTAIN:
                        uncertain.append(item.attempt.attempt_id)
                        if item.uncertain is not None and item.uncertain.provider_may_still_be_active:
                            active.append(item.attempt.attempt_id)
            total = None if unknown_reasons else known_cost + reserved_cost
            ledger = CategoryLedger(
                category=category,
                spent_requests=spent_requests,
                reserved_requests=reserved_requests,
                spent_tokens=spent_tokens,
                reserved_tokens=reserved_tokens,
                known_spent_cost=known_cost,
                reserved_cost=reserved_cost,
                total_cost=total,
                unknown_cost_reasons=tuple(unknown_reasons),
                failures=failures,
                retries=retries,
            )
            category_values[category] = ledger
            total_spent_requests += spent_requests
            total_reserved_requests += reserved_requests
            total_spent_tokens += spent_tokens
            total_reserved_tokens += reserved_tokens
            total_known_cost += known_cost
            total_reserved_cost += reserved_cost
            all_unknown.extend(unknown_reasons)
        for item in state.attempts.values():
            entries.append(
                AttemptLedgerEntry(
                    attempt=item.attempt,
                    reservation=item.reservation,
                    status=item.status,
                    reserved_at=item.reserved_at,
                    usage=item.usage,
                    cost=item.cost,
                    failure=item.failure,
                    uncertain=item.uncertain,
                    response_sha256=item.response_sha256,
                    response_bytes=item.response_bytes,
                    retry_approval=item.retry_approval,
                )
            )
        total_cost = None if all_unknown else total_known_cost + total_reserved_cost
        remaining_cost = (
            None
            if state.limits.max_cost is None
            else max(0.0, state.limits.max_cost - total_known_cost - total_reserved_cost)
        )
        stop_reason = state.cancel_reason if state.cancelled else None
        if stop_reason is None and now >= state.started_at + timedelta(seconds=state.limits.run_deadline_seconds):
            stop_reason = "run_deadline_expired"
        if stop_reason is None and total_spent_requests + total_reserved_requests >= state.limits.max_requests:
            stop_reason = "request_budget_exhausted"
        if stop_reason is None and total_spent_tokens + total_reserved_tokens >= state.limits.max_total_tokens:
            stop_reason = "token_budget_exhausted"
        if (
            stop_reason is None
            and state.limits.max_cost is not None
            and total_known_cost + total_reserved_cost >= state.limits.max_cost - _EPSILON
        ):
            stop_reason = "cost_budget_exhausted"
        return BudgetSnapshot(
            run_id=state.run_id,
            budget_limits=state.limits,
            started_at=state.started_at,
            next_sequence=state.next_sequence,
            attempts=tuple(entries),
            categories=category_values,
            spent_requests=total_spent_requests,
            reserved_requests=total_reserved_requests,
            spent_tokens=total_spent_tokens,
            reserved_tokens=total_reserved_tokens,
            known_spent_cost=total_known_cost,
            reserved_cost=total_reserved_cost,
            total_cost=total_cost,
            unknown_cost_reasons=tuple(all_unknown),
            remaining_requests=max(0, state.limits.max_requests - total_spent_requests - total_reserved_requests),
            remaining_tokens=max(0, state.limits.max_total_tokens - total_spent_tokens - total_reserved_tokens),
            remaining_cost=remaining_cost,
            remaining_concurrency=max(0, state.limits.max_concurrency - len(set(active))),
            active_attempt_ids=tuple(active),
            uncertain_attempt_ids=tuple(uncertain),
            replan_count=len(state.replans),
            turn_count=len(state.turns),
            cancelled=state.cancelled,
            stop_reason=stop_reason,
            cost_limit_guaranteed=state.limits.max_cost is not None and not all_unknown,
        )

    def _existing_reservation_result(
        self,
        state: _ReplayState,
        existing: _AttemptState,
    ) -> AttemptLease | CompletedUnitResult:
        if existing.status in {AttemptStatus.RESPONSE_COMMITTED, AttemptStatus.RECONCILED_RESPONSE}:
            if existing.reservation.completes_unit:
                return self._completed_result(existing)
            raise AttemptStateError("attempt already has a committed non-unit response")
        if existing.status is not AttemptStatus.RESERVED:
            raise AttemptStateError(f"attempt is already {existing.status}")
        deadline = min(
            state.started_at + timedelta(seconds=state.limits.run_deadline_seconds),
            existing.reserved_at + timedelta(seconds=state.limits.case_deadline_seconds),
        )
        return AttemptLease(
            disposition=ReservationDisposition.RESERVED,
            attempt=existing.attempt,
            reservation=existing.reservation,
            reserved_at=existing.reserved_at,
            deadline_at=deadline,
        )

    @staticmethod
    def _completed_result(item: _AttemptState) -> CompletedUnitResult:
        if item.response_bytes is None or item.response_sha256 is None or item.usage is None or item.cost is None:
            raise AttemptStateError("completed unit is missing immutable response accounting")
        return CompletedUnitResult(
            disposition=ReservationDisposition.CACHED,
            attempt=item.attempt,
            response_bytes=item.response_bytes,
            response_sha256=item.response_sha256,
            usage=item.usage,
            cost=item.cost,
        )

    @staticmethod
    def _require_attempt(state: _ReplayState, attempt_id: str) -> _AttemptState:
        try:
            return state.attempts[attempt_id]
        except KeyError as error:
            raise AttemptStateError(f"unknown attempt_id: {attempt_id}") from error

    def _base_payload(self, operation_id: str) -> dict[str, Any]:
        return {
            "ledger_schema": _LEDGER_SCHEMA,
            "run_id": self._run_id,
            "operation_id": operation_id,
        }

    def _validate_envelope(self, payload: Mapping[str, Any]) -> None:
        if payload.get("ledger_schema") != _LEDGER_SCHEMA:
            raise AttemptStateError("unsupported evaluation budget journal schema")
        if payload.get("run_id") != self._run_id:
            raise AttemptStateError("evaluation journal is bound to a different run_id")
        if not str(payload.get("operation_id") or "").strip():
            raise AttemptStateError("evaluation journal event requires operation_id")

    def _now(self) -> datetime:
        return _aware(self._clock(), "clock")


def _unit_digest(unit: PlannedEvaluationUnit) -> str:
    return content_digest(unit)


def _parse_time(value: Any, field_name: str) -> datetime:
    if type(value) is not str:
        raise AttemptStateError(f"{field_name} must be an ISO timestamp")
    try:
        return _aware(datetime.fromisoformat(value), field_name)
    except ValueError as error:
        raise AttemptStateError(f"{field_name} must be an ISO timestamp") from error


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _portable_reference(value: str, field_name: str) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty normalized string")
    if len(value) > 4096 or _PORTABLE_REF.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a portable non-file reference")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative strict integer")
    return value


def _non_negative_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return float(value)


__all__ = [
    "AttemptLease",
    "AttemptReservation",
    "AttemptStateError",
    "AttemptStatus",
    "BudgetAdmissionError",
    "BudgetSnapshot",
    "CategoryLedger",
    "CompletedUnitResult",
    "CostEstimate",
    "CostRecord",
    "FailureEvidence",
    "LedgerCategory",
    "PriceRate",
    "PricingMetadata",
    "ProtocolBudgetLedger",
    "ReservationDisposition",
    "RetryApprovalReference",
    "UncertainEvidence",
    "UsageRecord",
]


