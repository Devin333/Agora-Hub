from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from backend.research.graphs import ResearchAnalysisPlanCandidateBuilder
from framework.events.runtime.models import StreamReadRequest
from framework.harness import FakeArtifactPort
from framework.harness.control_plane.activity_execution import HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY, HarnessGraphActivityTaskContext
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_snapshot import RefSnapshotPhase
from framework.harness.ref_snapshot_store import REF_SNAPSHOT_EVENT_TYPE
from framework.memory.models import MemoryRecord
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from interfaces.composition.harness_memory import build_harness_ref_admission_service
from tests.backend.research.integration.test_dynamic_paper_analysis_task_plan import _DynamicTaskPlanFactory, _analyze
from tests.framework.harness.test_ref_snapshot_store import _store


class _ScopedMemoryAdmission(HarnessRefAdmissionService):
    """Test composition supplies a revision scoped to the actual Graph actor."""

    def __init__(self, store, namespace_root, *, shared=True, mismatch=None):
        namespaces = FilesystemMemoryNamespaceStore(namespace_root)
        super().__init__(store, memory_namespaces=namespaces)
        self.shared = shared
        self.mismatch = mismatch
        self.task = self.snapshot = self.metadata = self.publisher = None

    def admit_graph_inputs(self, task, **kwargs):
        self.task = deepcopy(task)
        if not self.memory_namespace_refs:
            context = HarnessGraphActivityTaskContext.from_dict(task[HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY])
            scope = dict(namespace="research.analysis", tenant_id=context.activity.tenant_scope_ref,
                         owner_id=context.activity.identity_scope_ref)
            if self.mismatch:
                scope[self.mismatch] = "another-scope"
            self.publisher = MemoryNamespacePublisher(
                self.memory_namespaces, **scope, shared_read_only=self.shared, policy=MemoryPolicy(),
            )
            self.metadata = self.publisher.publish((MemoryRecord(
                memory_id="verified-note", content="An accepted evidence-bound memory note.",
                namespace=scope["namespace"], tenant_id=scope["tenant_id"], actor=scope["owner_id"],
                refs={"evidence_id": "evidence-1"},
            ),))
            self.memory_namespace_refs = (self.metadata.exact_ref,)
        self.snapshot = super().admit_graph_inputs(task, **kwargs)
        return self.snapshot


def _request_memory(monkeypatch):
    original = ResearchAnalysisPlanCandidateBuilder._task_from_outline
    monkeypatch.setattr(ResearchAnalysisPlanCandidateBuilder, "_task_from_outline", staticmethod(
        lambda value, *, policy: replace(original(value, policy=policy), requested_memory_namespaces=("research.analysis",))
    ))


def test_real_graph_admits_exact_memory_and_children_inherit_only_read_only_refs(tmp_path, monkeypatch):
    _request_memory(monkeypatch)
    snapshots, events = _store(tmp_path)
    admission = _ScopedMemoryAdmission(snapshots, tmp_path / "namespaces")
    factory = _DynamicTaskPlanFactory(ref_admission_service=admission, transcript_root=tmp_path / "transcripts", authorize_results=True)
    result = _analyze("memory-admission", dynamic=True, dynamic_factory=factory, artifact_port=FakeArtifactPort())
    assert result.status == "succeeded", result
    root = admission.snapshot
    ref = admission.metadata.exact_ref
    assert root.policy.allowed_memory_namespaces == ("research.analysis",)
    assert root.policy.pinned_checksums[ref] == admission.metadata.source_checksum
    stream = events.read_stream(StreamReadRequest(
        stream_id=f"run:{root.run_id}", tenant_id="control", event_types=frozenset({REF_SNAPSHOT_EVENT_TYPE}),
    )).events
    grants = tuple(snapshots.get(run_id=root.run_id, snapshot_ref=item.payload["snapshot_ref"]) for item in stream)
    children = tuple(grant for grant in grants if grant.phase is RefSnapshotPhase.CHILD_INPUT)
    assert len(children) == 3
    for child in children:
        assert child.policy.allowed_memory_namespaces == ("research.analysis",)
        assert ref in child.policy.shared_read_only_refs
        assert not child.policy.writable_refs
        reader = admission.memory_reader(child, execution_identity=root.execution_identity, attempt_identity=child.attempt_identity)
        assert reader.read(ref).records()[0].memory_id == "verified-note"
        result_grant = next(grant for grant in grants if grant.phase is RefSnapshotPhase.RESULT_ACCEPTANCE and grant.attempt_identity == child.attempt_identity)
        context_ref = next(item.ref for item in result_grant.descriptors if item.artifact_type == "subagent_context")
        context = factory.transcript_stores[0].read_context(context_ref)
        assert context.memory_context_refs == (ref,)

    # A subsequently published revision is not an update to an admitted grant.
    previous = admission.memory_namespaces.read(ref).records()[0]
    newer = admission.publisher.publish((replace(previous, content="New revision"),))
    assert newer.exact_ref != ref
    reopened, _ = _store(tmp_path)
    restored = build_harness_ref_admission_service(
        snapshot_store=reopened, namespace_root=tmp_path / "namespaces", namespace_refs=(ref,),
    )
    # Restoring the original admission does not scan a live namespace catalog.
    monkeypatch.setattr(restored.memory_namespaces, "describe", lambda _ref: pytest.fail("recovery must reuse recorded descriptors"))
    stage = factory.stage_workers[0]
    assert restored.admit_graph_inputs(admission.task, stage_binding=stage._stage_binding, task_policy=stage._policy) == root
    changed = build_harness_ref_admission_service(
        snapshot_store=reopened, namespace_root=tmp_path / "namespaces", namespace_refs=(newer.exact_ref,),
    )
    with pytest.raises(HarnessValidationError) as error:
        changed.admit_graph_inputs(admission.task, stage_binding=stage._stage_binding, task_policy=stage._policy)
    assert error.value.code == "REF_SNAPSHOT_CONFLICT"
    assert events.get_stream_high_watermark(f"run:{root.run_id}", tenant_id="control") == len(stream)


@pytest.mark.parametrize("mismatch", ("tenant_id", "owner_id"))
def test_graph_memory_metadata_cannot_fabricate_actor_scope(tmp_path, mismatch):
    snapshots, events = _store(tmp_path)
    admission = _ScopedMemoryAdmission(snapshots, tmp_path / "namespaces", mismatch=mismatch)
    factory = _DynamicTaskPlanFactory(ref_admission_service=admission)
    result = _analyze("denied-memory", dynamic=True, dynamic_factory=factory, artifact_port=FakeArtifactPort())
    assert result.status != "succeeded"
    assert not any(worker.calls for group in factory.subagent_workers for worker in group.values())
    assert events.get_stream_high_watermark("run:denied-memory", tenant_id="control") is None


def test_private_namespace_cannot_be_shared_with_children(tmp_path, monkeypatch):
    _request_memory(monkeypatch)
    snapshots, _ = _store(tmp_path)
    admission = _ScopedMemoryAdmission(snapshots, tmp_path / "namespaces", shared=False)
    factory = _DynamicTaskPlanFactory(ref_admission_service=admission)
    result = _analyze("private-memory", dynamic=True, dynamic_factory=factory, artifact_port=FakeArtifactPort())
    assert result.status != "succeeded"
    assert not any(worker.calls for group in factory.subagent_workers for worker in group.values())


def test_allowlisted_namespace_name_without_admitted_revision_cannot_start_children(tmp_path, monkeypatch):
    _request_memory(monkeypatch)
    snapshots, _ = _store(tmp_path)
    admission = build_harness_ref_admission_service(snapshot_store=snapshots, namespace_root=tmp_path / "namespaces")
    factory = _DynamicTaskPlanFactory(ref_admission_service=admission)
    result = _analyze("unbound-memory", dynamic=True, dynamic_factory=factory, artifact_port=FakeArtifactPort())
    assert result.status != "succeeded"
    assert not any(worker.calls for group in factory.subagent_workers for worker in group.values())
