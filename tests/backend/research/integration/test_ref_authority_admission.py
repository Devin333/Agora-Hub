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
from framework.harness import FakeArtifactPort


class _RecordingAdmission(HarnessRefAdmissionService):
    def __init__(self, store):
        super().__init__(store)
        self.task = None
        self.snapshot = None

    def admit_graph_inputs(self, task, **kwargs):
        self.task = deepcopy(task)
        self.snapshot = super().admit_graph_inputs(task, **kwargs)
        return self.snapshot


def test_real_graph_inputs_and_children_use_persisted_read_only_grants(tmp_path):
    store, events = _store(tmp_path)
    admission = _RecordingAdmission(store)
    factory = _DynamicTaskPlanFactory(
        ref_admission_service=admission, transcript_root=tmp_path / "transcripts",
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
    assert len(committed) == 4
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
    assert events.get_stream_high_watermark(f"run:{root.run_id}", tenant_id="control") == 4
