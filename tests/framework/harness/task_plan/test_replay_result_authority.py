from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness import (
    HarnessValidationError,
    InMemoryTaskPlanStore,
    TaskPlanRecoveryService,
    TaskPlanReplayReducer,
)
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from tests.framework.harness.test_ref_results import (
    TENANT,
    _authorized_fixture,
    _no_payload_reads,
    _start_attempt,
    _verify,
    _worker_result_from_child,
)
from tests.framework.harness.test_ref_snapshot_store import _store as _ref_snapshot_store


class _EmptyQueueReader:
    def read_task_plan_queue(self, *, queue_name, task_instance_ids):
        return ()


def _committed_history(tmp_path):
    fixture = _authorized_fixture(tmp_path)
    child = fixture["runtime"].invoke(fixture["invocation"])
    record = _verify(fixture, _worker_result_from_child(child))
    store = InMemoryTaskPlanStore(
        gate_evidence_reader=fixture["gate_artifact_owner"],
    )
    store.append_candidate(fixture["candidate"])
    store.accept_plan(fixture["plan"])
    _start_attempt(store, fixture["plan"], fixture["instance"])
    store.append_result(record)
    events = store.read_events(fixture["plan"].run_id, fixture["plan"].stage_id)
    assert fixture["plan"].is_graph_only is True
    assert fixture["authority"].is_durable is True
    return fixture, record, events


def _replay(fixture, record, events, **changes):
    options = {
        "transcript_store": fixture["transcript_store"],
        "result_ref_authority": fixture["authority"],
        "execution_identity": fixture["execution_identity"],
        "gate_evidence_reader": fixture["gate_artifact_owner"],
    }
    options.update(changes)
    return TaskPlanReplayReducer(**options).replay(
        (fixture["plan"],),
        events,
        results=(record,),
    )


def test_graph_subagent_replay_and_recovery_share_committed_result_authority(
    tmp_path,
) -> None:
    fixture, record, events = _committed_history(tmp_path)
    event_high_watermark = fixture["events"].get_stream_high_watermark(
        f"run:{fixture['plan'].run_id}",
        tenant_id="control",
    )

    report = _replay(fixture, record, events)
    recovery = TaskPlanRecoveryService(
        queue_reader=_EmptyQueueReader(),
        transcript_store=fixture["transcript_store"],
        result_ref_authority=fixture["authority"],
        execution_identity=fixture["execution_identity"],
        gate_evidence_reader=fixture["gate_artifact_owner"],
    ).recover(
        (fixture["plan"],),
        events,
        results=(record,),
    )

    assert report.projection.tasks[0].status.value == "succeeded"
    assert recovery.report.replay_checksum == report.replay_checksum
    assert recovery.missing_queue_projections == ()
    assert fixture["worker"].calls == 1
    assert fixture["events"].get_stream_high_watermark(
        f"run:{fixture['plan'].run_id}",
        tenant_id="control",
    ) == event_high_watermark


def test_graph_subagent_replay_requires_authority_before_payload_read(
    tmp_path,
    monkeypatch,
) -> None:
    fixture, record, events = _committed_history(tmp_path)
    _no_payload_reads(monkeypatch, fixture["transcript_store"])

    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanReplayReducer(
            fixture["transcript_store"],
            gate_evidence_reader=fixture["gate_artifact_owner"],
        ).replay(
            (fixture["plan"],),
            events,
            results=(record,),
        )

    assert captured.value.code == "task_plan_result_ref_authority_required"
    assert fixture["worker"].calls == 1


def test_graph_subagent_replay_requires_the_callers_recorded_execution(tmp_path, monkeypatch):
    fixture, record, events = _committed_history(tmp_path)
    _no_payload_reads(monkeypatch, fixture["transcript_store"])
    with pytest.raises(HarnessValidationError) as error:
        _replay(fixture, record, events, execution_identity=None)
    assert error.value.code == "task_plan_execution_identity_required"


@pytest.mark.parametrize("change", ("durability", "transcript_owner"))
def test_replay_rechecks_authority_before_each_payload_ingress(tmp_path, monkeypatch, change):
    fixture, record, events = _committed_history(tmp_path)
    reducer = TaskPlanReplayReducer(
        transcript_store=fixture["transcript_store"],
        result_ref_authority=fixture["authority"],
        execution_identity=fixture["execution_identity"],
        gate_evidence_reader=fixture["gate_artifact_owner"],
    )
    _no_payload_reads(monkeypatch, fixture["transcript_store"])
    if change == "durability":
        monkeypatch.setattr(fixture["grants"], "is_durable", False)
        expected = "task_plan_result_ref_authority_not_durable"
    else:
        from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore

        replacement = FilesystemSubAgentTranscriptStore(tmp_path / "foreign-transcripts")
        _no_payload_reads(monkeypatch, replacement)
        monkeypatch.setattr(fixture["authority"], "transcript_store", replacement)
        expected = "REF_SNAPSHOT_BINDING_MISMATCH"
    with pytest.raises(HarnessValidationError) as error:
        reducer.replay((fixture["plan"],), events, results=(record,))
    assert error.value.code == expected


def test_graph_subagent_replay_rejects_wrong_execution_before_payload_read(
    tmp_path,
    monkeypatch,
) -> None:
    fixture, record, events = _committed_history(tmp_path)
    _no_payload_reads(monkeypatch, fixture["transcript_store"])

    with pytest.raises(HarnessValidationError) as captured:
        _replay(
            fixture,
            record,
            events,
            execution_identity=replace(
                fixture["execution_identity"],
                activity_id="different-recorded-activity",
            ),
        )

    assert captured.value.code == "REF_SNAPSHOT_MISSING"
    assert fixture["worker"].calls == 1


def test_graph_subagent_replay_rejects_missing_result_grant_before_payload_read(
    tmp_path,
    monkeypatch,
) -> None:
    fixture, record, events = _committed_history(tmp_path / "source")
    snapshots, _ = _ref_snapshot_store(tmp_path / "missing-result-grant")
    snapshots.commit(fixture["root"])
    snapshots.commit(fixture["child_grant"])
    authority = HarnessResultRefAuthority(
        snapshots,
        transcript_store=fixture["transcript_store"],
        tenant_id=TENANT,
    )
    _no_payload_reads(monkeypatch, fixture["transcript_store"])

    with pytest.raises(HarnessValidationError) as captured:
        _replay(
            fixture,
            record,
            events,
            result_ref_authority=authority,
        )

    assert captured.value.code == "REF_SNAPSHOT_MISSING"
    assert fixture["worker"].calls == 1


def test_graph_subagent_replay_rejects_conflicting_result_grant_before_payload_read(
    tmp_path,
    monkeypatch,
) -> None:
    fixture, record, events = _committed_history(tmp_path)
    identity = fixture["invocation"].attempt_identity
    result_binding_key = RefAuthoritySnapshot.attempt_binding_key(
        identity,
        RefSnapshotPhase.RESULT_ACCEPTANCE,
    )
    original_find = fixture["grants"].find
    committed = original_find(
        run_id=identity.parent_run_id,
        binding_key=result_binding_key,
    )
    assert committed is not None
    conflicting = replace(
        committed,
        source_checksum=checksum_for("conflicting-result-descriptor"),
    )

    def find(*, run_id, binding_key):
        if binding_key == result_binding_key:
            return conflicting
        return original_find(run_id=run_id, binding_key=binding_key)

    monkeypatch.setattr(fixture["grants"], "find", find)
    _no_payload_reads(monkeypatch, fixture["transcript_store"])

    with pytest.raises(HarnessValidationError) as captured:
        _replay(fixture, record, events)

    assert captured.value.code == "REF_SNAPSHOT_CONFLICT"
    assert fixture["worker"].calls == 1


def test_graph_subagent_recovery_requires_explicit_authority_before_payload_read(
    tmp_path,
    monkeypatch,
) -> None:
    fixture, record, events = _committed_history(tmp_path)
    _no_payload_reads(monkeypatch, fixture["transcript_store"])

    with pytest.raises(TypeError):
        TaskPlanRecoveryService(
            TaskPlanReplayReducer(),
            queue_reader=_EmptyQueueReader(),
        )

    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanRecoveryService(
            queue_reader=_EmptyQueueReader(),
            transcript_store=fixture["transcript_store"],
            gate_evidence_reader=fixture["gate_artifact_owner"],
        ).recover(
            (fixture["plan"],),
            events,
            results=(record,),
        )

    assert captured.value.code == "task_plan_result_ref_authority_required"
    assert fixture["worker"].calls == 1
