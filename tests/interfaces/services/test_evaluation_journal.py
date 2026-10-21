from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime

import pytest

from backend.research.ports.evaluation_journal import EvaluationJournalConflict
from backend.research.rag.evaluation.protocol_budget import (
    AttemptReservation,
    CostEstimate,
    CostRecord,
    LedgerCategory,
    ProtocolBudgetLedger,
    ReservationDisposition,
    UsageRecord,
)
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
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
from interfaces.services.evaluation_journal import PaperRagEvaluationJournal
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore

D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
NOW = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)


def _limits() -> BudgetLimits:
    return BudgetLimits(
        max_requests=3,
        max_total_tokens=500,
        max_cost=None,
        currency=None,
        unknown_price=True,
        unknown_price_reason="provider pricing unavailable",
        price_basis_reference=None,
        request_timeout_seconds=30.0,
        case_deadline_seconds=120.0,
        run_deadline_seconds=600.0,
        max_concurrency=2,
        max_retries=1,
        max_replans=1,
        max_turns=4,
    )


def _attempt(sequence: int = 1) -> AttemptIdentity:
    return AttemptIdentity(
        attempt_id=f"attempt-{sequence}",
        unit=PlannedEvaluationUnit(
            dataset_version="dataset-v1",
            case_id="case-1",
            track=EvaluationTrack.END_TO_END,
            variant_fingerprint=D2,
            repetition=1,
        ),
        sequence=sequence,
        external_request_id=f"client://request/{sequence}",
    )


def _control(attempt: AttemptIdentity, limits: BudgetLimits) -> RunControl:
    source = ScopedPaper(paper_id="paper-1", document_version="source-v1", source_sha256=D1)
    scope = CaseScopeProjection.create(
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
        run_id="run-native",
        unit=attempt.unit,
        attempt_id=attempt.attempt_id,
        options_fingerprint=D3,
        budget_fingerprint=limits.fingerprint,
        namespace_fingerprint=namespaces.fingerprint,
        case_scope=scope,
        namespaces=namespaces,
        budget_limits=limits,
        source_access_grants=(
            SourceAccessGrant(
                paper_id="paper-1",
                document_version="source-v1",
                source_sha256=D1,
                authorization_reference="authorization://evaluation/source-v1",
            ),
        ),
    )


def _reservation() -> AttemptReservation:
    return AttemptReservation(
        reservation_id="reservation-native",
        category=LedgerCategory.REQUEST,
        max_tokens=100,
        cost_estimate=CostEstimate(
            amount=None,
            unknown_reason="provider pricing unavailable",
            pricing=None,
            estimate_reference="estimate://provider/unknown",
        ),
        request_timeout_seconds=20.0,
        completes_unit=True,
    )


def _usage() -> UsageRecord:
    return UsageRecord(
        input_tokens=7,
        output_tokens=5,
        total_tokens=12,
        unknown_reason=None,
        evidence_reference="provider://usage/native-1",
    )


def _cost() -> CostRecord:
    return CostRecord(
        amount=None,
        unknown_reason="provider omitted billing data",
        pricing=None,
        evidence_reference="provider://billing/native-1",
    )


def test_adapter_uses_native_hashed_actor_scoped_journal_file(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    adapter = PaperRagEvaluationJournal(
        store,
        run_id="run-native",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    adapter.append(expected_sequence=0, event_type="evaluation-test", payload={"value": 1})

    expected_name = f"journal-{hashlib.sha256(adapter.session_id.encode('utf-8')).hexdigest()}.json"
    path = tmp_path / expected_name
    assert path == store.journal_path_for(adapter.session_id)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["actor_scope"] == adapter.actor_scope
    assert raw["actor_scope"]["memory_namespace"].startswith("research:evaluation:bookkeeping:")
    assert raw["journal_checksum"].startswith("sha256:")
    assert adapter.read().events[0].payload == {"value": 1}

    other_actor = PaperRagEvaluationJournal(
        store,
        run_id="run-native",
        tenant_id="tenant-1",
        user_id="other-user",
    )
    with pytest.raises(PermissionError):
        other_actor.read()


def test_adapter_converts_native_atomic_sequence_race_to_optimistic_conflict(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    first = PaperRagEvaluationJournal(
        store, run_id="run-race", tenant_id="tenant-1", user_id="user-1"
    )
    second = PaperRagEvaluationJournal(
        store, run_id="run-race", tenant_id="tenant-1", user_id="user-1"
    )
    barrier = threading.Barrier(2)
    successes: list[str] = []
    conflicts: list[EvaluationJournalConflict] = []

    def append(adapter: PaperRagEvaluationJournal, writer: str) -> None:
        barrier.wait()
        try:
            adapter.append(
                expected_sequence=0,
                event_type="evaluation-race",
                payload={"writer": writer},
            )
        except EvaluationJournalConflict as error:
            conflicts.append(error)
        else:
            successes.append(writer)

    threads = [
        threading.Thread(target=append, args=(first, "first")),
        threading.Thread(target=append, args=(second, "second")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].expected_sequence == 0
    assert conflicts[0].actual_sequence == 1
    assert first.read().next_sequence == 1


def test_native_journal_replay_restores_spend_and_completed_response_cache(tmp_path) -> None:
    store = PaperRagTranscriptFileStore(tmp_path)
    adapter = PaperRagEvaluationJournal(
        store,
        run_id="run-native",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    limits = _limits()
    ledger = ProtocolBudgetLedger(adapter, run_id="run-native", budget_limits=limits, clock=lambda: NOW)
    attempt = _attempt()
    ledger.reserve_attempt(
        control=_control(attempt, limits),
        attempt=attempt,
        reservation=_reservation(),
    )
    ledger.commit_response(
        attempt_id=attempt.attempt_id,
        response_bytes=b"native durable response bytes",
        usage=_usage(),
        cost=_cost(),
    )

    recovered_adapter = PaperRagEvaluationJournal(
        PaperRagTranscriptFileStore(tmp_path),
        run_id="run-native",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    recovered = ProtocolBudgetLedger(
        recovered_adapter,
        run_id="run-native",
        budget_limits=limits,
        clock=lambda: NOW,
    )
    assert recovered.read().spent_requests == 1
    assert recovered.read().spent_tokens == 12
    next_attempt = _attempt(2)
    cached = recovered.reserve_attempt(
        control=_control(next_attempt, limits),
        attempt=next_attempt,
        reservation=AttemptReservation(
            **{
                **_reservation().model_dump(mode="python"),
                "reservation_id": "reservation-replay",
            }
        ),
    )
    assert cached.disposition is ReservationDisposition.CACHED
    assert cached.response_bytes == b"native durable response bytes"
    assert recovered.read().spent_requests == 1
    native = store.load_journal(adapter.session_id, actor_scope=adapter.actor_scope)
    assert [event["event_type"] for event in native["events"]] == [
        "evaluation_budget_initialized",
        "evaluation_attempt_reserved",
        "evaluation_attempt_outcome",
    ]
