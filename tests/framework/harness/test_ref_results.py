from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for
from framework.harness import (
    HarnessValidationError, InMemoryTaskPlanStore, SubAgentRuntime,
    TaskPlanReplayReducer, TaskPlanResultVerificationRequest, TaskPlanResultVerifier,
)
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import RefAccessPolicy, RefDescriptor
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore
from tests.framework.harness.test_ref_snapshot_store import _store
from tests.framework.harness.task_plan.test_subagent_result_lineage import (
    _fixture, _start_attempt, _worker_result_from_child,
)
from tests.fixtures.task_plan import InMemoryTaskPlanGateArtifactWriter


TENANT = "result-tenant"


def _admit(store, identity, policy_checksum):
    descriptor = RefDescriptor(
        ref="document", run_id=identity.parent_run_id, stage_id=identity.stage_id,
        tenant_id=checksum_for(TENANT), owner_id="root-owner", access_mode="READ_ONLY",
        artifact_type="graph_input", source_checksum=checksum_for("input"),
        ref_kind="input", scope="SHARED_READ_ONLY",
    )
    root = RefAuthoritySnapshot(
        execution_identity=RefAuthoritySnapshot.execution_for_attempt(identity),
        stage_id=identity.stage_id, stage_binding_checksum=identity.stage_binding_checksum,
        task_policy_checksum=policy_checksum, source_checksum=checksum_for("inputs"),
        policy=RefAccessPolicy(
            policy_id="inputs", version="1", run_id=identity.parent_run_id,
            stage_id=identity.stage_id, tenant_id=checksum_for(TENANT), owner_id="root-owner",
            allowed_refs=("document",), allowed_artifact_types=("graph_input",),
            allowed_ref_kinds=("input",), pinned_checksums={"document": descriptor.source_checksum},
        ), descriptors=(descriptor,),
    )
    store.commit(root)
    child = HarnessRefAdmissionService(store).admit_child_inputs(root, attempt_identity=identity, input_refs=("document",))
    return root, child


def _authorized_fixture(tmp_path):
    fixture = _fixture(tmp_path / "lineage")
    invocation = fixture["adapter"].build_invocation(
        plan=fixture["plan"], resolved_task=fixture["resolved"], binding=fixture["binding"],
        instance=fixture["instance"], context_pack=fixture["context_pack"],
        budget_snapshot=fixture["budget"], execution_identity=fixture["execution_identity"],
    )
    grants, events = _store(tmp_path / "grants")
    root, child = _admit(grants, invocation.attempt_identity, fixture["plan"].policy_checksum)
    authority = HarnessResultRefAuthority(grants, transcript_store=fixture["transcript_store"], tenant_id=TENANT)
    runtime = SubAgentRuntime(
        workers={invocation.subagent_spec.subagent_id: fixture["worker"]},
        transcript_store=fixture["transcript_store"], result_ref_authority=authority,
    )
    gate_artifact_owner = InMemoryTaskPlanGateArtifactWriter()
    verifier = TaskPlanResultVerifier(
        fixture["verifier"]._gates, transcript_store=fixture["transcript_store"],
        result_ref_authority=authority,
        gate_artifact_writer=gate_artifact_owner,
    )
    fixture.update(invocation=invocation, grants=grants, events=events, authority=authority,
                   runtime=runtime, verifier=verifier, root=root, child_grant=child,
                   gate_artifact_owner=gate_artifact_owner)
    return fixture


def _verify(fixture, result, **changes):
    request = TaskPlanResultVerificationRequest(
        plan=fixture["plan"], task=fixture["resolved"], instance=fixture["instance"],
        worker_result=result, execution_identity=fixture["execution_identity"],
    )
    return fixture["verifier"].verify(result, task=fixture["resolved"], request=replace(request, **changes))


def _no_payload_reads(monkeypatch, store):
    def denied(*args, **kwargs):
        pytest.fail("unauthorized payload read")
    for name in ("read", "read_context", "read_output", "verify", "find_by_identity"):
        monkeypatch.setattr(store, name, denied)


def test_result_grant_reopens_without_worker_and_preserves_input_grants(tmp_path):
    f = _authorized_fixture(tmp_path)
    child = f["runtime"].invoke(f["invocation"])
    assert child.status.value == "succeeded"
    assert f["worker"].calls == 1
    record = _verify(f, _worker_result_from_child(child))
    assert record.subagent_output_ref == child.transcript_receipt.output_ref
    bound = f["authority"].for_attempt(f["invocation"].attempt_identity)
    receipt = child.transcript_receipt
    assert bound.read(transcript_ref=receipt.transcript_ref).identity == bound.identity
    assert bound.read_context(context_ref=receipt.context_ref).identity == bound.identity
    assert bound.read_output(output_ref=receipt.output_ref).identity == bound.identity
    assert bound.refs_for_parent(bound.identity.parent_run_id, limit=1) == (receipt.transcript_ref,)
    with pytest.raises(HarnessValidationError):
        bound.refs_for_parent(bound.identity.parent_run_id, limit=True)
    reopened, events = _store(tmp_path / "grants")
    raw = FilesystemSubAgentTranscriptStore(f["transcript_store"].root)
    authority = HarnessResultRefAuthority(reopened, transcript_store=raw, tenant_id=TENANT)
    runtime = SubAgentRuntime(workers={}, transcript_store=raw, result_ref_authority=authority)
    recovered = runtime.invoke(f["invocation"])
    assert recovered.transcript_receipt == child.transcript_receipt
    assert recovered.output == child.output
    assert f["worker"].calls == 1
    for grant in (f["root"], f["child_grant"]):
        assert reopened.get(run_id=grant.run_id, snapshot_ref=grant.snapshot_ref) == grant
    assert events.get_stream_high_watermark(f"run:{f['plan'].run_id}", tenant_id="control") == 3


def test_bundle_committed_before_grant_is_recovered_before_read_without_worker(tmp_path, monkeypatch):
    f = _authorized_fixture(tmp_path)
    original = f["grants"].commit
    def crash(snapshot):
        if snapshot.phase is RefSnapshotPhase.RESULT_ACCEPTANCE:
            raise RuntimeError("crash before result grant")
        return original(snapshot)
    monkeypatch.setattr(f["grants"], "commit", crash)
    with pytest.raises(RuntimeError, match="crash before"):
        f["runtime"].invoke(f["invocation"])
    assert f["worker"].calls == 1
    identity = f["invocation"].attempt_identity
    assert f["transcript_store"].describe_attempt(identity) is not None
    with monkeypatch.context() as guarded:
        _no_payload_reads(guarded, f["transcript_store"])
        with pytest.raises(HarnessValidationError) as error:
            f["authority"].for_attempt(identity).find_by_identity(identity)
        assert error.value.code == "REF_SNAPSHOT_MISSING"
    monkeypatch.setattr(f["grants"], "commit", original)
    receipt = f["runtime"].invoke(f["invocation"]).transcript_receipt
    assert receipt is not None and f["worker"].calls == 1


@pytest.mark.parametrize("change", ("activity", "policy", "receipt", "sibling", "sibling_ref", "type", "tenant", "metadata"))
def test_wrong_authority_is_denied_before_payload_reads(tmp_path, monkeypatch, change):
    f = _authorized_fixture(tmp_path)
    child = f["runtime"].invoke(f["invocation"])
    result = _worker_result_from_child(child)
    identity = f["invocation"].attempt_identity
    _no_payload_reads(monkeypatch, f["transcript_store"])
    with pytest.raises(HarnessValidationError):
        if change == "activity":
            _verify(f, result, execution_identity=replace(f["execution_identity"], activity_id="other-activity"))
        elif change == "policy":
            f["authority"].accepted_attempt(execution=f["execution_identity"], stage_id=identity.stage_id,
                stage_binding_checksum=identity.stage_binding_checksum, task_instance_id=identity.task_instance_id,
                attempt=identity.attempt, task_policy_checksum=checksum_for("wrong"))
        elif change == "receipt":
            f["authority"].for_attempt(identity).verify(replace(child.transcript_receipt, output_checksum=checksum_for("wrong")))
        elif change == "sibling":
            f["authority"].for_attempt(identity).find_by_identity(replace(identity, child_run_id="sibling-child"))
        elif change == "sibling_ref":
            f["authority"].for_attempt(identity).read(child.transcript_receipt.transcript_ref.replace(identity.transcript_id, "sat_" + "a" * 64))
        elif change == "type":
            f["authority"].for_attempt(identity).read_output(child.transcript_receipt.transcript_ref)
        elif change == "metadata":
            metadata = f["transcript_store"].describe_attempt(identity)
            monkeypatch.setattr(f["transcript_store"], "describe_attempt", lambda _identity: replace(metadata, bundle_checksum=checksum_for("changed")))
            f["authority"].for_attempt(identity).read(child.transcript_receipt.transcript_ref)
        else:
            HarnessResultRefAuthority(f["grants"], transcript_store=f["transcript_store"], tenant_id="other").for_attempt(identity).read(child.transcript_receipt.transcript_ref)


def test_replay_uses_committed_result_grants_without_new_events_or_worker(tmp_path):
    f = _authorized_fixture(tmp_path)
    child = f["runtime"].invoke(f["invocation"])
    record = _verify(f, _worker_result_from_child(child))
    store = InMemoryTaskPlanStore(
        gate_evidence_reader=f["gate_artifact_owner"],
    )
    store.append_candidate(f["candidate"])
    store.accept_plan(f["plan"])
    _start_attempt(store, f["plan"], f["instance"])
    store.append_result(record)
    replay = TaskPlanReplayReducer(
        transcript_store=f["transcript_store"], result_ref_authority=f["authority"],
        execution_identity=f["execution_identity"],
        gate_evidence_reader=f["gate_artifact_owner"],
    ).replay((f["plan"],), store.read_events(f["plan"].run_id, f["plan"].stage_id), results=(record,))
    assert replay.projection.tasks[0].status.value == "succeeded"
    assert f["worker"].calls == 1
    assert f["events"].get_stream_high_watermark(f"run:{f['plan'].run_id}", tenant_id="control") == 3


def test_unadmitted_runtime_does_not_invoke_worker(tmp_path):
    f = _authorized_fixture(tmp_path)
    empty, _ = _store(tmp_path / "empty")
    authority = HarnessResultRefAuthority(empty, transcript_store=f["transcript_store"], tenant_id=TENANT)
    runtime = SubAgentRuntime(workers={f["invocation"].subagent_spec.subagent_id: f["worker"]},
                              transcript_store=f["transcript_store"], result_ref_authority=authority)
    with pytest.raises(HarnessValidationError) as error:
        runtime.invoke(f["invocation"])
    assert error.value.code == "REF_SNAPSHOT_MISSING"
    assert f["worker"].calls == 0


def test_worker_selected_artifact_cannot_create_owner_authority(tmp_path):
    from framework.harness.artifacts.ports import ArtifactReferenceDescriptor

    f = _authorized_fixture(tmp_path)
    identity = f["invocation"].attempt_identity
    f["worker"].artifacts = ("artifact://lineage-run/sibling-result",)
    class SiblingCatalog:
        def describe_artifact_ref(self, ref, *, expected_run_id, expected_tenant_id=None):
            return ArtifactReferenceDescriptor(
                ref=ref, run_id=expected_run_id, tenant_id=expected_tenant_id,
                artifact_type="graph-result", checksum=checksum_for("sibling content"),
                byte_size=1, media_type="application/json", graph_id=identity.graph_id,
                node_id=identity.node_id, attempt_id="subagent_" + "a" * 64,
            )
    authority = HarnessResultRefAuthority(f["grants"], transcript_store=f["transcript_store"],
                                          artifact_descriptors=SiblingCatalog(), tenant_id=TENANT)
    runtime = SubAgentRuntime(workers={identity.subagent_id: f["worker"]}, transcript_store=f["transcript_store"], result_ref_authority=authority)
    for _ in range(2):
        with pytest.raises(HarnessValidationError) as error:
            runtime.invoke(f["invocation"])
        assert error.value.code == "REF_OWNER_MISMATCH"
    assert f["worker"].calls == 1
    assert authority._grant(identity, RefSnapshotPhase.RESULT_ACCEPTANCE) is None


def test_materializer_appends_separate_grant_and_recovery_reuses_it(tmp_path):
    from framework.harness.control_plane.graph_application import HarnessGraphControlPlaneRuntime
    from framework.harness.runtime.graph_result_runtime import HarnessGraphResultRuntime
    from framework.harness.runtime.result_models import NodeResultBinding
    from framework.harness.runtime.subagent_result_adapter import HarnessSubAgentResultAdapter
    from infrastructure.storage.artifacts.catalog_local_json import LocalJsonArtifactCatalog
    from tests.framework.harness.runtime.test_graph_result_runtime import _dispatched
    from tests.framework.harness.runtime.test_materializer import _materializer

    f = _authorized_fixture(tmp_path)
    catalog = LocalJsonArtifactCatalog(tmp_path / "catalog")
    authority = HarnessResultRefAuthority(f["grants"], transcript_store=f["transcript_store"],
                                          artifact_descriptors=catalog, tenant_id=TENANT)
    runtime = SubAgentRuntime(workers={f["invocation"].subagent_spec.subagent_id: f["worker"]},
                              transcript_store=f["transcript_store"], result_ref_authority=authority)
    result = runtime.invoke(f["invocation"])
    identity = f["invocation"].attempt_identity
    original, _ = authority.result_grant(identity)
    graph = _dispatched("materializer-fixture")
    adapter = HarnessSubAgentResultAdapter(
        materializer=_materializer(catalog=catalog),
        graph_result_runtime=HarnessGraphResultRuntime(HarnessGraphControlPlaneRuntime(graph.port)),
        transcript_store=f["transcript_store"], result_ref_authority=authority,
    )
    binding = NodeResultBinding(
        tenant_id=TENANT, tenant_scope_ref=checksum_for(TENANT), run_id=identity.parent_run_id,
        graph_id=identity.graph_id, graph_version=identity.graph_ref, node_id=identity.node_id,
        attempt_id=identity.result_attempt_id, parent_checkpoint_ref=checksum_for("checkpoint"),
    )
    first = adapter.materialize(result, invocation=f["invocation"], binding=binding, created_at=f["invocation"].observed_at)
    restored = adapter.recover_materialization(invocation=f["invocation"], binding=binding, created_at=f["invocation"].observed_at)
    assert restored.materialization.envelope == first.materialization.envelope
    grant = authority._grant(identity, RefSnapshotPhase.MATERIALIZED_RESULT)
    assert grant.parent_snapshot_ref == original.snapshot_ref
    assert grant.source_checksum == checksum_for(first.materialization.envelope.to_dict())
    assert authority.result_grant(identity)[0] == original
    assert f["events"].get_stream_high_watermark(f"run:{identity.parent_run_id}", tenant_id="control") == 4
    refs = tuple(item.ref for item in first.materialization.envelope.materialized_refs)
    authority.for_attempt(identity).authorize_artifacts(refs, include_materialized=True)
    with pytest.raises(HarnessValidationError):
        authority.for_attempt(identity).authorize_artifacts(refs)
    with pytest.raises(HarnessValidationError):
        authority.for_attempt(identity).authorize_artifacts((), include_materialized=True)
    reopened, _ = _store(tmp_path / "grants")
    assert reopened.get(run_id=identity.parent_run_id, snapshot_ref=grant.snapshot_ref) == grant
    assert f["worker"].calls == 1


def test_research_materialized_results_replay_exact_authorized_ref_union(tmp_path):
    from backend.research.application.graph_result_committer import ResearchTaskPlanResultMaterializer
    from framework.harness.control_plane.graph_application import HarnessGraphControlPlaneRuntime
    from framework.harness.runtime.graph_result_runtime import HarnessGraphResultRuntime
    from framework.harness.runtime.subagent_result_adapter import HarnessSubAgentResultAdapter
    from infrastructure.storage.artifacts.catalog_local_json import LocalJsonArtifactCatalog
    from tests.framework.harness.runtime.test_graph_result_runtime import _dispatched
    from tests.framework.harness.runtime.test_materializer import RecordingArtifactPort, _materializer
    from framework.harness.runtime.result_policy import GraphArtifactPersistenceConfig, GraphArtifactRolloutMode

    f = _authorized_fixture(tmp_path)
    catalog = LocalJsonArtifactCatalog(tmp_path / "catalog")
    artifacts = RecordingArtifactPort()
    authority = HarnessResultRefAuthority(f["grants"], transcript_store=f["transcript_store"], artifact_descriptors=catalog, tenant_id=TENANT)
    graph = _dispatched("materializer-fixture")
    config = GraphArtifactPersistenceConfig(mode=GraphArtifactRolloutMode.ENFORCE)
    adapter = HarnessSubAgentResultAdapter(
        materializer=_materializer(catalog=catalog, artifact=artifacts, config=config),
        graph_result_runtime=HarnessGraphResultRuntime(HarnessGraphControlPlaneRuntime(graph.port)),
        transcript_store=f["transcript_store"], result_ref_authority=authority,
    )
    class ArtifactVerifier:
        def verify_artifact_ref(self, ref, *, expected_run_id):
            descriptor = catalog.describe_artifact_ref(ref, expected_run_id=expected_run_id, expected_tenant_id=TENANT)
            assert checksum_for(artifacts.read_artifact(ref)["payload"]["value"]) == descriptor.checksum
    artifact_verifier = ArtifactVerifier()
    verifier = TaskPlanResultVerifier(
        f["verifier"]._gates,
        transcript_store=f["transcript_store"],
        result_ref_authority=authority,
        artifact_reference_verifier=artifact_verifier,
        gate_artifact_writer=f["verifier"].gate_artifact_writer,
    )
    f["verifier"] = ResearchTaskPlanResultMaterializer(
        verifier=verifier, adapter=adapter, config=config, tenant_id=TENANT,
        tenant_scope_ref=checksum_for(TENANT), invocation_factory=lambda *_args: f["invocation"],
    )
    worker_result = _worker_result_from_child(f["runtime"].invoke(f["invocation"]))
    record = _verify(f, worker_result)
    assert len(record.output_refs) == 1 and worker_result.artifacts == ()
    assert _verify(f, worker_result) == record
    store = InMemoryTaskPlanStore(
        gate_evidence_reader=f["gate_artifact_owner"],
    )
    store.append_candidate(f["candidate"])
    store.accept_plan(f["plan"])
    _start_attempt(store, f["plan"], f["instance"])
    store.append_result(record)
    replay = TaskPlanReplayReducer(transcript_store=f["transcript_store"], result_ref_authority=authority,
                                   execution_identity=f["execution_identity"], artifact_reference_verifier=artifact_verifier,
                                   gate_evidence_reader=f["gate_artifact_owner"])
    report = replay.replay((f["plan"],), store.read_events(f["plan"].run_id, f["plan"].stage_id), results=(record,))
    assert report.projection.tasks[0].status.value == "succeeded"
    assert f["worker"].calls == 1
    assert artifacts.write_count == 1
    assert f["events"].get_stream_high_watermark(f"run:{f['plan'].run_id}", tenant_id="control") == 4


def test_self_consistent_existing_grant_cannot_authorize_sibling_payload(tmp_path, monkeypatch):
    f = _authorized_fixture(tmp_path)
    original_commit = f["grants"].commit
    expected = []
    def crash(snapshot):
        if snapshot.phase is RefSnapshotPhase.RESULT_ACCEPTANCE:
            expected.append(snapshot)
            raise RuntimeError("interrupted grant commit")
        return original_commit(snapshot)
    monkeypatch.setattr(f["grants"], "commit", crash)
    with pytest.raises(RuntimeError, match="interrupted"):
        f["runtime"].invoke(f["invocation"])
    monkeypatch.setattr(f["grants"], "commit", original_commit)
    snapshot = expected[0]
    ref = f"subagent-transcript://v3/{snapshot.run_id}/sat_" + "a" * 64
    descriptor = replace(snapshot.descriptors[0], ref=ref, artifact_type="subagent_transcript")
    forged = replace(snapshot, descriptors=(descriptor,), policy=replace(
        snapshot.policy, allowed_refs=(ref,), allowed_artifact_types=(descriptor.artifact_type,),
        pinned_checksums={ref: descriptor.source_checksum},
    ))
    assert forged.source_checksum == snapshot.source_checksum and forged.binding_key == snapshot.binding_key
    original_commit(forged)
    _no_payload_reads(monkeypatch, f["transcript_store"])
    with pytest.raises(HarnessValidationError) as error:
        f["authority"].for_attempt(f["invocation"].attempt_identity).read(ref)
    assert error.value.code == "REF_SNAPSHOT_CONFLICT"


def test_failed_replay_authorizes_original_artifacts_before_payload(tmp_path, monkeypatch):
    from framework.harness.artifacts.ports import ArtifactReferenceDescriptor
    from framework.harness.ref_results import AttemptResultStore

    f = _authorized_fixture(tmp_path)
    identity = f["invocation"].attempt_identity
    f["worker"].status = "failed"
    f["worker"].artifacts = (f"artifact://{identity.parent_run_id}/failed-evidence",)
    class Catalog:
        def describe_artifact_ref(self, ref, *, expected_run_id, expected_tenant_id=None):
            return ArtifactReferenceDescriptor(
                ref=ref, run_id=expected_run_id, tenant_id=expected_tenant_id,
                artifact_type="graph-result", checksum=checksum_for("evidence"),
                byte_size=1, media_type="application/json", graph_id=identity.graph_id,
                node_id=identity.node_id, attempt_id=identity.result_attempt_id,
            )
        def verify_artifact_ref(self, ref, *, expected_run_id):
            assert ref == f["worker"].artifacts[0] and expected_run_id == identity.parent_run_id
    catalog = Catalog()
    authority = HarnessResultRefAuthority(f["grants"], transcript_store=f["transcript_store"], artifact_descriptors=catalog, tenant_id=TENANT)
    runtime = SubAgentRuntime(workers={identity.subagent_id: f["worker"]}, transcript_store=f["transcript_store"], result_ref_authority=authority)
    f["verifier"] = TaskPlanResultVerifier(
        f["verifier"]._gates,
        transcript_store=f["transcript_store"],
        result_ref_authority=authority,
        artifact_reference_verifier=catalog,
        gate_artifact_writer=f["verifier"].gate_artifact_writer,
    )
    record = _verify(f, _worker_result_from_child(runtime.invoke(f["invocation"])))
    assert record.status.value == "failed" and record.output_refs == ()
    store = InMemoryTaskPlanStore()
    store.append_candidate(f["candidate"])
    store.accept_plan(f["plan"])
    _start_attempt(store, f["plan"], f["instance"])
    store.append_result(record)
    calls = []
    original = AttemptResultStore.authorize_original_artifacts
    def authorize(bound):
        original(bound)
        calls.append("authorized")
    monkeypatch.setattr(AttemptResultStore, "authorize_original_artifacts", authorize)
    original_read = f["transcript_store"].read
    def read(ref):
        assert calls == ["authorized"]
        return original_read(ref)
    monkeypatch.setattr(f["transcript_store"], "read", read)
    report = TaskPlanReplayReducer(transcript_store=f["transcript_store"], result_ref_authority=authority,
                                   execution_identity=f["execution_identity"], artifact_reference_verifier=catalog).replay(
        (f["plan"],), store.read_events(f["plan"].run_id, f["plan"].stage_id), results=(record,),
    )
    assert report.projection.tasks[0].status.value == "failed"
    assert calls == ["authorized"] and f["worker"].calls == 1
