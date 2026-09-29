from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.ref_snapshot_store import REF_SNAPSHOT_EVENT_TYPE
from framework.events.runtime.models import StreamReadRequest
from tests.backend.research.integration.test_dynamic_paper_analysis_task_plan import _DynamicTaskPlanFactory, _analyze
from tests.framework.harness.test_ref_snapshot_store import _store
from framework.harness import FakeArtifactPort, TaskPlanReplayReducer


class _RecordingAdmission(HarnessRefAdmissionService):
    def __init__(self, store):
        super().__init__(store)
        self.task = None
        self.snapshot = None

    def admit_graph_inputs(self, task, **kwargs):
        self.task = deepcopy(task)
        self.snapshot = super().admit_graph_inputs(task, **kwargs)
        return self.snapshot


@pytest.mark.parametrize("authorize_results", (False, True))
def test_real_graph_inputs_and_children_use_persisted_read_only_grants(tmp_path, monkeypatch, authorize_results):
    store, events = _store(tmp_path)
    admission = _RecordingAdmission(store)
    factory = _DynamicTaskPlanFactory(
        ref_admission_service=admission, transcript_root=tmp_path / "transcripts",
        authorize_results=authorize_results,
    )
    result = _analyze(
        "research-ref-authority", dynamic=True,
        dynamic_factory=factory, artifact_port=FakeArtifactPort(),
    )
    assert result.status == "succeeded", result
    root = admission.snapshot
    assert root is not None
    assert root.policy.allowed_refs == ("document", "evidence_pack")
    assert root.phase is RefSnapshotPhase.INPUT_ADMISSION
    committed = events.read_stream(StreamReadRequest(
        stream_id=f"run:{root.run_id}", tenant_id="control",
        event_types=frozenset({REF_SNAPSHOT_EVENT_TYPE}),
    )).events
    assert len(committed) == (7 if authorize_results else 4)
    reopened, _ = _store(tmp_path)
    restored_admission = HarnessRefAdmissionService(reopened)
    stage = factory.stage_workers[0]
    assert restored_admission.admit_graph_inputs(
        admission.task, stage_binding=stage._stage_binding, task_policy=stage._policy,
    ) == root
    grants = tuple(reopened.get(run_id=root.run_id, snapshot_ref=event.payload["snapshot_ref"]) for event in committed)
    children = tuple(grant for grant in grants if grant.phase is RefSnapshotPhase.CHILD_INPUT)
    assert len(children) == 3
    assert len({grant.policy.owner_id for grant in children}) == 3
    for grant in children:
        assert grant.policy.owner_id == grant.attempt_identity.child_run_id
        assert grant.policy.tenant_id == root.policy.tenant_id
        assert grant.parent_snapshot_ref == root.snapshot_ref
        assert grant.policy.shared_read_only_refs == ("document", "evidence_pack")
        assert grant.policy.writable_refs == ()
        assert grant.descriptors == root.descriptors
        assert RefAuthoritySnapshot.from_dict(grant.to_dict()) == grant
    with pytest.raises(HarnessValidationError) as error:
        restored_admission.admit_child_inputs(
            root, attempt_identity=children[0].attempt_identity,
            input_refs=("document", "sibling-private"),
        )
    assert error.value.code == "REF_UNAUTHORIZED"
    with pytest.raises(HarnessValidationError) as error:
        restored_admission.admit_graph_inputs(
            admission.task, stage_binding=stage._stage_binding,
            task_policy=replace(stage._policy, metadata={**stage._policy.metadata, "revision": "changed"}),
        )
    assert error.value.code == "REF_SNAPSHOT_CONFLICT"
    with pytest.raises(HarnessValidationError) as error:
        restored_admission.admit_graph_inputs(
            {}, stage_binding=stage._stage_binding, task_policy=stage._policy,
        )
    assert error.value.code == "REF_INPUT_INVALID"
    calls_before = factory.outline_workers[0].calls
    child_calls_before = sum(len(worker.calls) for worker in factory.subagent_workers[0].values())
    tampered = deepcopy(admission.task)
    tampered["inputs"]["document"] = {"unauthorized": "replacement"}
    with pytest.raises(HarnessValidationError) as error:
        factory.stage_workers[0].run(tampered)
    assert error.value.code == "REF_INPUT_CHECKSUM_MISMATCH"
    assert factory.outline_workers[0].calls == calls_before
    assert sum(len(worker.calls) for worker in factory.subagent_workers[0].values()) == child_calls_before
    assert events.get_stream_high_watermark(f"run:{root.run_id}", tenant_id="control") == len(committed)
    if authorize_results:
        results = tuple(grant for grant in grants if grant.phase is RefSnapshotPhase.RESULT_ACCEPTANCE)
        assert len(results) == 3
        assert {grant.parent_snapshot_ref for grant in results} == {grant.snapshot_ref for grant in children}
        assert all(grant.policy.shared_read_only_refs == () and grant.policy.writable_refs == () for grant in results)
        assert all({item.artifact_type for item in grant.descriptors} == {"subagent_context", "subagent_output", "subagent_transcript"} for grant in results)
    else:
        # The explicit test-store fixture exercises input admission in isolation.
        # Its successful online candidate is not authority for a later replay.
        transcript_store = factory.transcript_stores[0]

        def unexpected_read(*args, **kwargs):
            pytest.fail("ungranted replay must reject before reading result payload")

        for name in ("read", "read_context", "read_output", "verify", "find_by_identity"):
            monkeypatch.setattr(transcript_store, name, unexpected_read)
        plan_store = factory.stores[0]
        plan = plan_store.plan(root.run_id, root.stage_id)
        assert plan is not None
        records = plan_store.results_for(root.run_id, root.stage_id, plan.plan_id, plan.version)
        assert len(records) == 3
        with pytest.raises(HarnessValidationError) as replay_error:
                TaskPlanReplayReducer(
                    transcript_store,
                    gate_evidence_reader=plan_store,
                ).replay(
                    (plan,),
                    plan_store.read_events(root.run_id, root.stage_id),
                    results=records,
                )
        assert replay_error.value.code == "task_plan_result_ref_authority_required"
