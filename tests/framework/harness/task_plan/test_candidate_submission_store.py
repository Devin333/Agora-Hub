from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Thread

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.store import InMemoryTaskPlanStore
from framework.harness.task_plan.submission import (
    CandidateDedupIdentity,
    CandidateSubmission,
    submissions_from_events,
)
from framework.harness.workers.result import HarnessWorkerResult
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore,
    _EventStore,
    _UnitOfWork,
    _graph_only_candidate_and_plan,
    _store,
)


ACCEPTED_AT = "2026-09-05T00:00:00Z"


class _ConcurrentUnitOfWork(_UnitOfWork):
    """Make the test event adapter enforce its production CAS transaction boundary."""

    def __enter__(self):
        self.store._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if not self.finished:
                self.rollback()
            return False
        finally:
            self.store._lock.release()

    def commit(self) -> None:
        if self.finished:
            raise RuntimeError("unit of work already finished")
        self.store._events.extend(self.pending)
        self.finished = True


class _ConcurrentEventStore(_EventStore):
    def unit_of_work(self):
        return _ConcurrentUnitOfWork(self)


class _DelayedCandidateArtifactStore(_ArtifactStore):
    """Hold one writer after its immutable candidate write, before event read."""

    def __init__(self) -> None:
        super().__init__()
        self.loser_candidate_written = Event()
        self.release_loser = Event()

    def write(self, artifact):
        written = super().write(artifact)
        if (
            artifact.artifact_type == "harness.task-plan.candidate"
            and b'"candidate_id":"candidate-2"' in artifact.content_bytes()
        ):
            self.loser_candidate_written.set()
            assert self.release_loser.wait(timeout=5)
        return written


def _identity(*, parent_turn_id: str = "turn-1") -> CandidateDedupIdentity:
    return CandidateDedupIdentity(
        run_id="submission-run",
        stage_id="dynamic_analysis_stage",
        parent_turn_id=parent_turn_id,
        action_correlation_id="action-1",
    )


def _candidate(*, candidate_id: str = "candidate-1"):
    candidate, _plan = _graph_only_candidate_and_plan(run_id="submission-run")
    return replace(candidate, candidate_id=candidate_id)


@pytest.mark.parametrize("field", ("run_id", "stage_id", "parent_turn_id", "action_correlation_id"))
def test_dedup_identity_binds_every_scope_dimension(field):
    original = _identity()
    changed = replace(original, **{field: getattr(original, field) + "-other"})
    assert changed.dedup_key != original.dedup_key
    assert CandidateDedupIdentity.from_dict(original.to_dict()) == original
    assert CandidateDedupIdentity.from_dict(changed.to_dict()) == changed


def test_dedup_identity_cannot_alias_ambiguous_concatenation():
    first = replace(_identity(), parent_turn_id="turn-a", action_correlation_id="b")
    second = replace(_identity(), parent_turn_id="turn", action_correlation_id="a-b")
    assert first.dedup_key != second.dedup_key


def test_in_memory_submission_reuses_original_time_and_rejects_different_payload():
    store = InMemoryTaskPlanStore()
    identity = _identity()
    candidate = _candidate()

    first = store.admit_candidate_submission(
        candidate,
        identity,
        accepted_at=ACCEPTED_AT,
    )
    replay = store.admit_candidate_submission(
        candidate,
        identity,
        accepted_at="2026-09-05T00:01:00Z",
    )

    assert replay == first
    assert replay.accepted_at == ACCEPTED_AT
    assert replay.plan_id.startswith("candidate-plan-")
    assert store.candidate_submission(identity) == first
    assert store.candidate_for("submission-run", "dynamic_analysis_stage", first.candidate_ref) == candidate
    assert [event.event_type for event in store.read_events("submission-run", "dynamic_analysis_stage")] == [
        "PLAN_CANDIDATE_BUILT"
    ]

    with pytest.raises(HarnessValidationError) as conflict:
        store.admit_candidate_submission(
            _candidate(candidate_id="candidate-2"),
            identity,
            accepted_at=ACCEPTED_AT,
        )
    assert conflict.value.code == "CANDIDATE_IDEMPOTENCY_CONFLICT"


def test_durable_submission_reopens_and_validates_candidate_artifact():
    artifacts = _ArtifactStore()
    event_store = _ConcurrentEventStore()
    candidate = _candidate()
    identity = _identity()
    first = _store(event_store, artifacts).admit_candidate_submission(
        candidate,
        identity,
        accepted_at=ACCEPTED_AT,
    )

    reopened = _store(event_store, artifacts)
    assert reopened.candidate_submission(identity) == first
    assert reopened.submissions_for("submission-run", "dynamic_analysis_stage") == (first,)
    assert reopened.candidate_for("submission-run", "dynamic_analysis_stage", first.candidate_ref) == candidate

    candidate_path = next(
        path
        for run_id, path in artifacts._content
        if run_id == "submission-run" and "/candidate/" in path
    )
    artifacts._content[("submission-run", candidate_path)] = b"{}"

    with pytest.raises(HarnessValidationError) as corrupt:
        _store(event_store, artifacts).candidate_submission(identity)
    assert corrupt.value.code == "task_plan_artifact_checksum_mismatch"


def test_durable_submission_concurrent_same_payload_is_one_durable_event():
    artifacts = _ArtifactStore()
    event_store = _ConcurrentEventStore()
    candidate = _candidate()
    identity = _identity()
    barrier = Barrier(2)

    def admit():
        store = _store(event_store, artifacts)
        barrier.wait()
        return store.admit_candidate_submission(
            candidate,
            identity,
            accepted_at=ACCEPTED_AT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(lambda _unused: admit(), range(2)))

    assert first == second
    events = _store(event_store, artifacts).read_events("submission-run", "dynamic_analysis_stage")
    assert len(events) == 1
    assert events[0].payload["submission"]["record_checksum"] == first.record_checksum


def test_durable_submission_concurrent_different_payload_fails_closed():
    artifacts = _ArtifactStore()
    event_store = _ConcurrentEventStore()
    identity = _identity()
    barrier = Barrier(2)

    def admit(candidate_id: str):
        store = _store(event_store, artifacts)
        barrier.wait()
        return store.admit_candidate_submission(
            _candidate(candidate_id=candidate_id),
            identity,
            accepted_at=ACCEPTED_AT,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, value) for value in ("candidate-1", "candidate-2")]
        outcomes = [future.exception() or future.result() for future in futures]

    assert sum(isinstance(value, CandidateSubmission) for value in outcomes) == 1
    errors = [value for value in outcomes if isinstance(value, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], HarnessValidationError)
    assert errors[0].code == "CANDIDATE_IDEMPOTENCY_CONFLICT"
    assert len(_store(event_store, artifacts).read_events("submission-run", "dynamic_analysis_stage")) == 1


def test_durable_submission_rechecks_dedup_after_candidate_artifact_interleaving():
    artifacts = _DelayedCandidateArtifactStore()
    event_store = _EventStore()
    identity = _identity()
    loser_error: list[BaseException] = []

    def submit_loser() -> None:
        try:
            _store(event_store, artifacts).admit_candidate_submission(
                _candidate(candidate_id="candidate-2"),
                identity,
                accepted_at=ACCEPTED_AT,
            )
        except BaseException as exc:
            loser_error.append(exc)

    loser = Thread(target=submit_loser)
    loser.start()
    assert artifacts.loser_candidate_written.wait(timeout=5)
    winner = _store(event_store, artifacts).admit_candidate_submission(
        _candidate(candidate_id="candidate-1"),
        identity,
        accepted_at=ACCEPTED_AT,
    )
    artifacts.release_loser.set()
    loser.join(timeout=5)

    assert not loser.is_alive()
    assert len(loser_error) == 1
    assert isinstance(loser_error[0], HarnessValidationError)
    assert loser_error[0].code == "CANDIDATE_IDEMPOTENCY_CONFLICT"
    reopened = _store(event_store, artifacts)
    assert reopened.candidate_submission(identity) == winner
    assert len(reopened.read_events("submission-run", "dynamic_analysis_stage")) == 1


def test_durable_candidate_ref_can_be_referenced_by_multiple_parent_turns():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    candidate = _candidate()
    first = _store(event_store, artifacts).admit_candidate_submission(
        candidate,
        _identity(parent_turn_id="turn-1"),
        accepted_at=ACCEPTED_AT,
    )
    second = _store(event_store, artifacts).admit_candidate_submission(
        candidate,
        _identity(parent_turn_id="turn-2"),
        accepted_at=ACCEPTED_AT,
    )

    reopened = _store(event_store, artifacts)
    assert reopened.candidate_for("submission-run", "dynamic_analysis_stage", first.candidate_ref) == candidate
    assert reopened.submissions_for("submission-run", "dynamic_analysis_stage") == tuple(
        sorted((first, second), key=lambda item: item.submission_id)
    )
    assert len(reopened.read_events("submission-run", "dynamic_analysis_stage")) == 2


@pytest.mark.parametrize("store_factory", [InMemoryTaskPlanStore])
def test_submission_does_not_treat_empty_action_checksum_as_missing(store_factory):
    store = store_factory()
    with pytest.raises(HarnessValidationError) as invalid:
        store.admit_candidate_submission(
            _candidate(),
            _identity(),
            accepted_at=ACCEPTED_AT,
            candidate_checksum="",
        )
    assert invalid.value.code == "task_plan_required_field"


def test_initial_plan_must_bind_the_exact_submission_not_first_matching_candidate():
    candidate, plan = _graph_only_candidate_and_plan(run_id="submission-run")
    identity = _identity()
    store = InMemoryTaskPlanStore()
    submission = store.admit_candidate_submission(
        candidate,
        identity,
        accepted_at=ACCEPTED_AT,
    )

    with pytest.raises(HarnessValidationError) as conflict:
        store.accept_plan(replace(plan, accepted_at=ACCEPTED_AT))
    assert conflict.value.code == "task_plan_submission_binding_conflict"

    with pytest.raises(HarnessValidationError) as unrelated:
        store.accept_plan(replace(plan, source_candidate_ref="sha256:" + "a" * 64))
    assert unrelated.value.code == "task_plan_submission_binding_conflict"

    bound_plan = replace(
        plan,
        plan_id=submission.plan_id,
        accepted_at=submission.accepted_at,
    )
    assert store.accept_plan(bound_plan) == bound_plan.plan_checksum

    other_submission = store.admit_candidate_submission(
        candidate,
        _identity(parent_turn_id="turn-2"),
        accepted_at=ACCEPTED_AT,
    )
    assert other_submission.candidate_ref == submission.candidate_ref
    other_plan = replace(
        plan,
        plan_id=other_submission.plan_id,
        accepted_at=other_submission.accepted_at,
    )
    with pytest.raises(HarnessValidationError) as version_conflict:
        store.accept_plan(other_plan)
    assert version_conflict.value.code == "task_plan_version_conflict"


@pytest.mark.parametrize("durable", (False, True))
def test_pre_plan_rejection_cannot_be_converted_to_accepted_execution(durable):
    candidate, plan = _graph_only_candidate_and_plan(run_id="submission-run")
    store = _store(_EventStore(), _ArtifactStore()) if durable else InMemoryTaskPlanStore()
    submission = store.admit_candidate_submission(candidate, _identity(), accepted_at=ACCEPTED_AT)
    rejected = HarnessWorkerResult(status="blocked", error="candidate_rejected",
        diagnostics={"reason_code": "candidate_rejected"})
    built = store.read_events(candidate.run_id, candidate.stage_id)[0]
    halt = replace(built, event_type="TASK_PLAN_HALTED", sequence=2, input_checksum=None,
        reason_code="candidate_rejected", payload={
            "submission_key": submission.identity.dedup_key,
            "terminal_result": rejected.to_dict(),
            "terminal_result_checksum": rejected.candidate_result_ref,
        })
    store.append_event(halt)
    before = store.read_events(candidate.run_id, candidate.stage_id)
    bound = replace(plan, plan_id=submission.plan_id, accepted_at=submission.accepted_at)
    with pytest.raises(HarnessValidationError) as refused:
        store.accept_plan(bound)
    assert refused.value.code == "task_plan_submission_binding_conflict"
    assert store.plan(candidate.run_id, candidate.stage_id) is None
    assert store.read_events(candidate.run_id, candidate.stage_id) == before
    assert store.candidate_submission(_identity()) == submission


@pytest.mark.parametrize("durable", (False, True))
def test_submission_success_requires_causal_plan_acceptance(durable):
    candidate = _candidate()
    store = _store(_EventStore(), _ArtifactStore()) if durable else InMemoryTaskPlanStore()
    submission = store.admit_candidate_submission(candidate, _identity(), accepted_at=ACCEPTED_AT)
    result = HarnessWorkerResult(status="succeeded", output={"aggregate_checksum": "sha256:" + "0" * 64})
    before = store.read_events(candidate.run_id, candidate.stage_id)
    event = replace(before[0], event_type="TASK_PLAN_VERIFIED", sequence=2,
        input_checksum=result.output["aggregate_checksum"], payload={
            "submission_key": submission.identity.dedup_key,
            "terminal_result": result.to_dict(),
            "terminal_result_checksum": result.candidate_result_ref,
        })
    with pytest.raises(HarnessValidationError) as refused:
        store.append_event(event)
    assert refused.value.code == "task_plan_submission_result_invalid"
    assert store.read_events(candidate.run_id, candidate.stage_id) == before


def test_submission_event_parser_rejects_tampered_record_and_duplicate_key():
    candidate = _candidate()
    identity = _identity()
    store = InMemoryTaskPlanStore()
    submission = store.admit_candidate_submission(candidate, identity, accepted_at=ACCEPTED_AT)
    event = store.read_events("submission-run", "dynamic_analysis_stage")[0]

    duplicate = replace(event, sequence=2)
    with pytest.raises(HarnessValidationError) as repeated:
        submissions_from_events((event, duplicate))
    assert repeated.value.code == "CANDIDATE_IDEMPOTENCY_CONFLICT"

    tampered = submission.to_dict()
    tampered["record_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(HarnessValidationError) as checksum_error:
        CandidateSubmission.from_dict(tampered)
    assert checksum_error.value.code == "candidate_submission_checksum_mismatch"
    assert submission.record_checksum == event.payload["submission"]["record_checksum"]


@pytest.mark.parametrize(
    "factory, raw_factory, removed_key",
    [
        (CandidateDedupIdentity.from_dict, CandidateDedupIdentity.to_dict, "parent_turn_id"),
        (CandidateSubmission.from_dict, CandidateSubmission.to_dict, "accepted_at"),
    ],
)
def test_submission_contract_rejects_missing_strict_fields(factory, raw_factory, removed_key):
    candidate = _candidate()
    identity = _identity()
    submission = CandidateSubmission(
        identity=identity,
        candidate_checksum=candidate.candidate_checksum,
        candidate_ref=candidate.candidate_checksum,
        accepted_at=ACCEPTED_AT,
        admission_id="writer-1",
    )
    raw = raw_factory(identity if raw_factory is CandidateDedupIdentity.to_dict else submission)
    raw.pop(removed_key)

    with pytest.raises(HarnessValidationError) as invalid:
        factory(raw)
    assert invalid.value.code == "invalid_task_plan_payload_fields"


@pytest.mark.parametrize("exclusive", (False, True))
def test_concurrent_admission_has_exactly_one_execution_owner(exclusive):
    artifacts, events = _ArtifactStore(), _ConcurrentEventStore()
    barrier = Barrier(2)

    def submit():
        store = _store(events, artifacts)
        barrier.wait()
        return store.submit_candidate(_candidate(), _identity(), accepted_at=ACCEPTED_AT,
                                      exclusive_stage=exclusive)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: submit(), range(2)))
    assert sum(item.created for item in outcomes) == 1
    assert outcomes[0].submission == outcomes[1].submission
    assert len(_store(events, artifacts).read_events("submission-run", "dynamic_analysis_stage")) == 1


def test_exclusive_stage_admission_rechecks_scope_after_artifact_interleaving():
    artifacts, events = _DelayedCandidateArtifactStore(), _ConcurrentEventStore()
    errors = []

    def losing_submit():
        try:
            _store(events, artifacts).submit_candidate(
                _candidate(candidate_id="candidate-2"), _identity(parent_turn_id="other-turn"),
                accepted_at=ACCEPTED_AT, exclusive_stage=True,
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=losing_submit)
    thread.start()
    try:
        assert artifacts.loser_candidate_written.wait(5)
        winner = _store(events, artifacts).submit_candidate(
            _candidate(), _identity(), accepted_at=ACCEPTED_AT, exclusive_stage=True,
        )
        assert winner.created
    finally:
        artifacts.release_loser.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], HarnessValidationError)
    assert errors[0].code == "task_plan_submission_scope_unavailable"
    assert _store(events, artifacts).submissions_for("submission-run", "dynamic_analysis_stage") == (winner.submission,)


def test_submission_schema_pins_writer_nonce_without_changing_logical_identity():
    candidate = _candidate()
    first = CandidateSubmission(identity=_identity(), candidate_checksum=candidate.candidate_checksum,
        candidate_ref=candidate.candidate_checksum, accepted_at=ACCEPTED_AT, admission_id="writer-1")
    other = replace(first, admission_id="writer-2")
    assert first.record_checksum != other.record_checksum
    assert first.submission_id == other.submission_id and first.plan_id == other.plan_id
    assert CandidateSubmission.from_dict(first.to_dict()) == first
    old = first.to_dict()
    old["schema_version"] = "newsroom.harness-candidate-submission/v1"
    with pytest.raises(HarnessValidationError) as unsupported:
        CandidateSubmission.from_dict(old)
    assert unsupported.value.code == "candidate_submission_schema_invalid"
