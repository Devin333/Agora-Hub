from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from framework.events.canonical import checksum_for, thaw_canonical_json
from framework.harness.artifacts.ports import ArtifactReferenceDescriptor
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_authority import RefAccessPolicy, RefDescriptor
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore
from tests.framework.harness.test_ref_snapshot_store import _store as _snapshot_store
from framework.harness import (
    FakeSubAgentWorker,
    HarnessValidationError,
    HarnessWorkerResult,
    SubAgentContextBuilder,
    SubAgentInvocation,
    SubAgentRuntime,
    SubAgentSpec,
    SubAgentStatus,
)
from framework.harness.control_plane.graph_application import (
    HarnessGraphControlPlaneRuntime,
)
from framework.harness.control_plane.policy import (
    HarnessBudget,
    HarnessBudgetSnapshot,
)
from framework.harness.context.models import (
    CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
    ContextEnvelope,
    ContextGraphIdentity,
    ContextTaskExecutionIdentity,
)
from framework.harness.runtime import (
    GraphArtifactResultError,
    GraphArtifactResultErrorCode,
    HarnessGraphResultRuntime,
    PersistenceMode,
)
from framework.harness.runtime.subagent_result_adapter import (
    HarnessSubAgentActivityRuntime,
    HarnessSubAgentResultAdapter,
    VerifiedSubAgentMaterializedBundle,
    verify_subagent_materialized_bundle,
)
from framework.harness.subagents.models import SubAgentHandoff
from framework.harness.subagents.models import SUBAGENT_INVOCATION_SCHEMA_V3
from framework.harness.subagents.transcript import (
    SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3,
    SubAgentAttemptIdentity,
)
from framework.shared.json import stable_json_dumps
from tests.framework.harness.runtime.test_graph_result_runtime import (
    NOW,
    TENANT_ID,
    TENANT_SCOPE_REF,
    _dispatched,
)
from tests.framework.harness.runtime.test_materializer import (
    RecordingArtifactPort,
    RecordingAttempts,
    RecordingCache,
    RecordingCatalog,
    RecordingQuota,
    _materializer,
)


def _spec() -> SubAgentSpec:
    return SubAgentSpec(
        subagent_id="critic",
        role="critic",
        purpose="Review a candidate.",
        input_schema={
            "required": ["input_refs"],
            "properties": {"input_refs": {"type": "array"}},
        },
        output_schema={
            "required": ["result"],
            "properties": {"result": {"type": "string"}},
        },
        allowed_tools=("search.read",),
        allowed_memory_namespaces=("research.public",),
        budget={"max_turns": 2, "max_tool_calls": 1, "max_memory_ops": 1},
    )


def _invocation(fixture, spec: SubAgentSpec) -> SubAgentInvocation:
    budget = HarnessBudgetSnapshot.from_budget(HarnessBudget.safe_default())
    child_run_id = f"{fixture.activity.run_id}:{fixture.activity.node_id}:task-1"
    stage_binding_checksum = checksum_for({"stage": fixture.activity.node_id})
    stage_identity_checksum = checksum_for(
        {
            "schema_version": CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
            "run_id": fixture.activity.run_id,
            "graph_schema_version": fixture.graph.schema_version,
            "compiler_version": fixture.graph.compiler_version,
            "condition_policy_version": fixture.graph.condition_policy_version,
            "graph_id": fixture.graph.graph_id,
            "graph_version": fixture.graph.graph_version,
            "graph_checksum": fixture.graph.checksum,
            "stage_id": fixture.activity.node_id,
            "stage_binding_checksum": stage_binding_checksum,
            "graph_ref": fixture.graph.graph_ref.exact_ref,
        }
    )
    graph_identity = ContextGraphIdentity(
        run_id=fixture.activity.run_id,
        graph_id=fixture.graph.graph_id,
        graph_version=fixture.graph.graph_version,
        graph_ref=fixture.graph.graph_ref.exact_ref,
        graph_schema_version=fixture.graph.schema_version,
        compiler_version=fixture.graph.compiler_version,
        condition_policy_version=fixture.graph.condition_policy_version,
        graph_checksum=fixture.graph.checksum,
        stage_id=fixture.activity.node_id,
        stage_binding_checksum=stage_binding_checksum,
        stage_identity_schema=CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
        stage_identity_checksum=stage_identity_checksum,
        node_id=fixture.activity.node_id,
        node_instance_id=fixture.activity.node_instance_id,
        activity_id=fixture.activity.activity_id,
        activity_attempt=fixture.activity.attempt,
    )
    task_identity = ContextTaskExecutionIdentity(
        plan_id="adapter-test-plan",
        plan_version=1,
        plan_checksum=checksum_for({"plan": "adapter-test-plan"}),
        task_id="task-1",
        task_definition_checksum=checksum_for({"task": "task-1"}),
        task_instance_id="task-instance-1",
        attempt=1,
    )
    context_pack = ContextEnvelope.for_graph(
        envelope_id="context://subagent-result",
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id=spec.subagent_id,
        worker_type="subagent",
        token_estimate=10,
    )
    context = SubAgentContextBuilder().build(
        parent_run_id=fixture.activity.run_id,
        child_run_id=child_run_id,
        spec=spec,
        context_pack=context_pack,
        input_refs=("artifact://research/input-1",),
        memory_context_refs=(),
        budget_snapshot=budget,
    )
    attempt_identity = SubAgentAttemptIdentity(
        invocation_id=f"invocation://{child_run_id}",
        parent_run_id=fixture.activity.run_id,
        child_run_id=child_run_id,
        graph_id=graph_identity.graph_id,
        graph_version=graph_identity.graph_version,
        graph_ref=graph_identity.graph_ref,
        graph_schema_version=graph_identity.graph_schema_version,
        compiler_version=graph_identity.compiler_version,
        condition_policy_version=graph_identity.condition_policy_version,
        graph_checksum=graph_identity.graph_checksum,
        stage_id=graph_identity.stage_id,
        stage_binding_checksum=graph_identity.stage_binding_checksum,
        stage_identity_schema=graph_identity.stage_identity_schema,
        stage_identity_checksum=graph_identity.stage_identity_checksum,
        plan_id=task_identity.plan_id,
        plan_version=task_identity.plan_version,
        plan_checksum=task_identity.plan_checksum,
        task_id=task_identity.task_id,
        task_definition_checksum=task_identity.task_definition_checksum,
        context_envelope_id=context_pack.envelope_id,
        context_envelope_checksum=context_pack.checksum,
        node_id=fixture.activity.node_id,
        node_instance_id=fixture.activity.node_instance_id,
        activity_id=fixture.activity.activity_id,
        activity_attempt=fixture.activity.attempt,
        task_instance_id=task_identity.task_instance_id,
        attempt=task_identity.attempt,
        subagent_id=spec.subagent_id,
        schema_version=SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3,
    )
    return SubAgentInvocation(
        invocation_id=f"invocation://{child_run_id}",
        parent_run_id=fixture.activity.run_id,
        child_run_id=child_run_id,
        stage_id=fixture.activity.node_id,
        task_id="task-1",
        task_instance_id="task-instance-1",
        attempt=1,
        observed_at=NOW,
        subagent_spec=spec,
        input_refs=("artifact://research/input-1",),
        context_envelope=context,
        budget_snapshot=budget,
        attempt_identity=attempt_identity,
        metadata={"input_refs": ["artifact://research/input-1"]},
        schema_version=SUBAGENT_INVOCATION_SCHEMA_V3,
    )


class _DescriptorCatalog(RecordingCatalog):
    def __init__(self, identity):
        super().__init__()
        # Fixture-owned source metadata, independently of the worker's response.
        self.source = ArtifactReferenceDescriptor(
            ref="artifact://research/analysis-1", run_id=identity.parent_run_id,
            tenant_id=TENANT_ID, graph_id=identity.graph_id, node_id=identity.node_id,
            attempt_id=identity.result_attempt_id, artifact_type="analysis",
            checksum=checksum_for("analysis fixture"), byte_size=16,
            media_type="application/json",
        )

    def describe_artifact_ref(self, ref, *, expected_run_id, expected_tenant_id=None):
        if ref == self.source.ref:
            descriptor = self.source
        else:
            record = next(item.record for item in self.requests if item.record.ref == ref)
            descriptor = ArtifactReferenceDescriptor(
                ref=record.ref, run_id=record.run_id, tenant_id=record.tenant_id,
                graph_id=record.graph_id, node_id=record.node_id, attempt_id=record.attempt_id,
                artifact_type=record.artifact_type, checksum=record.content_checksum,
                byte_size=record.byte_size, media_type=record.media_type,
            )
        assert descriptor.run_id == expected_run_id
        assert descriptor.tenant_id == expected_tenant_id
        return descriptor


def _stack(fixture, worker_result: HarnessWorkerResult, tmp_path):
    spec = _spec()
    invocation = _invocation(fixture, spec)
    identity = invocation.attempt_identity
    store = FilesystemSubAgentTranscriptStore(tmp_path / "transcripts")
    grants, _events = _snapshot_store(tmp_path / "grants")
    descriptors = tuple(RefDescriptor(
        ref=ref, run_id=identity.parent_run_id, stage_id=identity.stage_id,
        tenant_id=TENANT_SCOPE_REF, owner_id="fixture-owner", access_mode="READ_ONLY",
        artifact_type="graph_input", source_checksum=checksum_for(ref),
        ref_kind="input", scope="SHARED_READ_ONLY",
    ) for ref in invocation.input_refs)
    root = RefAuthoritySnapshot(
        execution_identity=RefAuthoritySnapshot.execution_for_attempt(identity),
        stage_id=identity.stage_id, stage_binding_checksum=identity.stage_binding_checksum,
        task_policy_checksum=checksum_for("fixture-policy"), source_checksum=checksum_for("inputs"),
        policy=RefAccessPolicy(
            policy_id="fixture-inputs", version="1", run_id=identity.parent_run_id,
            stage_id=identity.stage_id, tenant_id=TENANT_SCOPE_REF, owner_id="fixture-owner",
            allowed_refs=invocation.input_refs, allowed_artifact_types=("graph_input",),
            allowed_ref_kinds=("input",),
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
        ), descriptors=descriptors,
    )
    grants.commit(root)
    HarnessRefAdmissionService(grants).admit_child_inputs(
        root, attempt_identity=identity, input_refs=invocation.input_refs,
    )
    catalog = _DescriptorCatalog(identity)
    authority = HarnessResultRefAuthority(
        grants, transcript_store=store, artifact_descriptors=catalog, tenant_id=TENANT_ID,
    )
    runtime = SubAgentRuntime(
        workers={spec.subagent_id: FakeSubAgentWorker((worker_result,))},
        transcript_store=store,
        result_ref_authority=authority,
    )
    artifact = RecordingArtifactPort()
    attempts = RecordingAttempts()
    materializer = _materializer(
        artifact=artifact,
        attempts=attempts,
        cache=RecordingCache(),
        catalog=catalog,
        quota=RecordingQuota(),
    )
    graph_runtime = HarnessGraphControlPlaneRuntime(fixture.port)
    adapter = HarnessSubAgentResultAdapter(
        materializer=materializer,
        graph_result_runtime=HarnessGraphResultRuntime(graph_runtime),
        transcript_store=store,
        result_ref_authority=authority,
        clock=lambda: NOW,
    )
    return (
        HarnessSubAgentActivityRuntime(runtime=runtime, adapter=adapter),
        adapter,
        runtime,
        store,
        artifact,
        attempts,
        catalog,
        invocation,
    )


def _execute(activity_runtime, fixture, invocation, **kwargs):
    return activity_runtime.execute_and_accept(
        invocation=invocation,
        activity=fixture.activity,
        graph=fixture.graph,
        tenant_id=TENANT_ID,
        tenant_scope_ref=TENANT_SCOPE_REF,
        run_spec_checksum=fixture.run_spec_checksum,
        occurred_at=NOW + timedelta(minutes=1),
        **kwargs,
    )


def _stored_bundle(artifact, result) -> dict:
    ref = result.outcome.materialization.envelope.materialized_refs[0].ref
    stored = artifact.read_artifact(ref)["payload"]
    return stored["value"]


def _lineage(result, fixture) -> dict:
    node = next(
        item
        for item in result.graph_state.node_instances
        if item.instance_id == fixture.activity.node_instance_id
    )
    return thaw_canonical_json(node.output_refs["activity_result_lineage"])


@pytest.mark.parametrize("path", ("compose", "execute", "recover", "adapter"))
@pytest.mark.parametrize("fault", ("missing", "non_durable", "different_owner"))
def test_graph_authority_is_required_before_worker_or_evidence_access(tmp_path, monkeypatch, path, fault):
    fixture = _dispatched("run-subagent-authority")
    activity, adapter, runtime, store, artifact, attempts, _catalog, invocation = _stack(
        fixture, HarnessWorkerResult(status="succeeded", output={"result": "ok"}), tmp_path,
    )
    authority = runtime.result_ref_authority

    def unexpected(*args, **kwargs):
        pytest.fail("invalid Graph composition must fail before payload or worker access")

    for method in ("read", "read_context", "read_output", "verify", "find_by_identity"):
        monkeypatch.setattr(store, method, unexpected)
    monkeypatch.setattr(runtime.workers[invocation.subagent_spec.subagent_id], "execute", unexpected)
    if fault == "missing":
        adapter.result_ref_authority = None
    elif fault == "non_durable":
        monkeypatch.setattr(authority.store, "is_durable", False)
    else:
        adapter.result_ref_authority = HarnessResultRefAuthority(
            authority.store, transcript_store=store,
            artifact_descriptors=_DescriptorCatalog(invocation.attempt_identity), tenant_id=TENANT_ID,
        )
    with pytest.raises(HarnessValidationError) as error:
        if path == "compose":
            HarnessSubAgentActivityRuntime(runtime=runtime, adapter=adapter)
        elif path == "execute":
            _execute(activity, fixture, invocation)
        elif path == "adapter":
            adapter.binding_for_activity(
                activity=fixture.activity, graph=fixture.graph, tenant_id=TENANT_ID,
                tenant_scope_ref=TENANT_SCOPE_REF, run_spec_checksum=fixture.run_spec_checksum,
                invocation=invocation,
            )
        else:
            activity.recover_and_accept(
                invocation=invocation, activity=fixture.activity, graph=fixture.graph,
                tenant_id=TENANT_ID, tenant_scope_ref=TENANT_SCOPE_REF,
                run_spec_checksum=fixture.run_spec_checksum, occurred_at=NOW,
            )
    assert error.value.code == (
        "REF_SNAPSHOT_BINDING_MISMATCH" if fault == "different_owner" else "REF_SNAPSHOT_MISSING"
    )
    assert artifact.write_count == attempts.put_count == 0
    assert fixture.port.recover_graph(fixture.activity.run_id).activity_result_commits == ()


def test_runtime_cannot_use_a_separate_authority_from_graph_acceptance(tmp_path):
    fixture = _dispatched("run-subagent-separated-authority")
    _activity, adapter, runtime, store, _artifact, _attempts, _catalog, _invocation_value = _stack(
        fixture, HarnessWorkerResult(status="succeeded", output={"result": "ok"}), tmp_path,
    )
    authority = runtime.result_ref_authority
    runtime.result_ref_authority = HarnessResultRefAuthority(
        authority.store, transcript_store=store, artifact_descriptors=authority.artifact_descriptors,
        tenant_id=TENANT_ID,
    )
    with pytest.raises(HarnessValidationError) as error:
        HarnessSubAgentActivityRuntime(runtime=runtime, adapter=adapter)
    assert error.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"


def test_verified_bundle_materializes_all_documents_and_projects_only_lineage(tmp_path) -> None:
    fixture = _dispatched("run-subagent-materialized")
    large_result = "x" * (48 * 1024)
    (
        activity_runtime,
        _adapter,
        _runtime,
        _store,
        artifact,
        _attempts,
        catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(
            status="succeeded",
            output={"result": large_result},
            artifacts=("artifact://research/analysis-1",),
        ),
        tmp_path,
    )
    handoff = SubAgentHandoff(
        handoff_id="handoff-1",
        from_subagent_id="critic",
        to_subagent_id="aggregator",
        parent_run_id=fixture.activity.run_id,
        payload={"claim": "bounded"},
        payload_schema={
            "required": ["claim"],
            "properties": {"claim": {"type": "string"}},
        },
        input_refs=("artifact://research/input-1",),
        artifact_refs=("artifact://research/analysis-1",),
        created_at=NOW,
    )

    result = _execute(
        activity_runtime,
        fixture,
        invocation,
        handoff=handoff,
    )
    envelope = result.outcome.materialization.envelope
    stored = _stored_bundle(artifact, result)
    verified = verify_subagent_materialized_bundle(
        stored,
        expected_binding=envelope.binding,
    )
    lineage = _lineage(result, fixture)

    assert envelope.persistence_decision.mode is PersistenceMode.ARTIFACT
    assert verified.output.output["result"] == large_result
    assert verified.context.input_refs == invocation.input_refs
    assert verified.transcript.output_checksum == verified.output.output_checksum
    assert verified.handoff == handoff
    assert len(catalog.requests) == 1
    projection = lineage["inline_projection"]
    assert projection["subagent_status"] == "succeeded"
    assert projection["transcript_checksum"] == verified.transcript.transcript_checksum
    assert projection["handoff"]["handoff_id"] == "handoff-1"
    assert projection["result_summary"] == "x" * 2048
    graph_payload = stable_json_dumps(result.graph_state.to_dict())
    assert "x" * 2049 not in graph_payload
    assert "transcript_body" not in graph_payload
    assert "context_evidence" not in graph_payload


def test_halted_attempt_is_materialized_with_failed_gate_evidence(tmp_path) -> None:
    fixture = _dispatched("run-subagent-halted")
    (
        activity_runtime,
        _adapter,
        _runtime,
        _store,
        artifact,
        _attempts,
        _catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(
            status="succeeded",
            output={"result": "ok", "requested_tools": ["admin.write"]},
        ),
        tmp_path,
    )

    result = _execute(activity_runtime, fixture, invocation)
    stored = verify_subagent_materialized_bundle(
        _stored_bundle(artifact, result),
        expected_binding=result.outcome.materialization.envelope.binding,
    )
    projection = _lineage(result, fixture)["inline_projection"]

    assert result.outcome.result.status is SubAgentStatus.HALTED
    assert result.outcome.materialization.envelope.status.value == "halted"
    assert projection["failed_gate_count"] == 1
    assert any(gate["passed"] is False for gate in stored.transcript.gate_results)


def test_restart_reuses_transcript_and_attempt_without_invoking_worker(tmp_path) -> None:
    fixture = _dispatched("run-subagent-restart")
    (
        _activity_runtime,
        adapter,
        runtime,
        store,
        artifact,
        attempts,
        _catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(status="succeeded", output={"result": "ok"}),
        tmp_path,
    )
    binding = adapter.binding_for_activity(
        activity=fixture.activity,
        graph=fixture.graph,
        tenant_id=TENANT_ID,
        tenant_scope_ref=TENANT_SCOPE_REF,
        run_spec_checksum=fixture.run_spec_checksum,
        invocation=invocation,
    )
    first_result = runtime.invoke(invocation)
    first = adapter.materialize(
        first_result,
        invocation=invocation,
        binding=binding,
        created_at=NOW + timedelta(minutes=1),
    )

    class ExplodingWorker:
        calls = 0

        def execute(self, _task):
            self.calls += 1
            raise AssertionError("recovery must not invoke a worker")

    worker = ExplodingWorker()
    restarted_runtime = SubAgentRuntime(
        workers={invocation.subagent_spec.subagent_id: worker},
        transcript_store=store,
        result_ref_authority=runtime.result_ref_authority,
    )
    restarted_adapter = HarnessSubAgentResultAdapter(
        materializer=_materializer(
            artifact=artifact,
            attempts=attempts,
            cache=RecordingCache(),
            catalog=runtime.result_ref_authority.artifact_descriptors,
            quota=RecordingQuota(),
        ),
        graph_result_runtime=HarnessGraphResultRuntime(
            HarnessGraphControlPlaneRuntime(fixture.port)
        ),
        transcript_store=store,
        result_ref_authority=runtime.result_ref_authority,
        clock=lambda: NOW,
    )
    restarted = HarnessSubAgentActivityRuntime(
        runtime=restarted_runtime,
        adapter=restarted_adapter,
    ).recover_and_accept(
        invocation=invocation,
        activity=fixture.activity,
        graph=fixture.graph,
        tenant_id=TENANT_ID,
        tenant_scope_ref=TENANT_SCOPE_REF,
        run_spec_checksum=fixture.run_spec_checksum,
        occurred_at=NOW + timedelta(minutes=2),
    )

    assert restarted.outcome.recovered is True
    assert restarted.outcome.materialization.envelope == first.materialization.envelope
    assert worker.calls == 0
    assert artifact.write_count == 1
    assert attempts.put_count == 1


def test_conflicting_same_attempt_fails_without_second_artifact_or_graph_commit(tmp_path) -> None:
    fixture = _dispatched("run-subagent-conflict")
    (
        _activity_runtime,
        adapter,
        runtime,
        _store,
        artifact,
        _attempts,
        _catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(status="succeeded", output={"result": "first"}),
        tmp_path,
    )
    binding = adapter.binding_for_activity(
        activity=fixture.activity,
        graph=fixture.graph,
        tenant_id=TENANT_ID,
        tenant_scope_ref=TENANT_SCOPE_REF,
        run_spec_checksum=fixture.run_spec_checksum,
        invocation=invocation,
    )
    result = runtime.invoke(invocation)
    adapter.materialize(
        result,
        invocation=invocation,
        binding=binding,
        created_at=NOW + timedelta(minutes=1),
    )
    conflicting_handoff = SubAgentHandoff(
        handoff_id="handoff-conflict",
        from_subagent_id="critic",
        to_subagent_id="aggregator",
        parent_run_id=fixture.activity.run_id,
        payload={"claim": "different"},
        payload_schema={"required": ["claim"]},
        created_at=NOW,
    )

    with pytest.raises(GraphArtifactResultError) as captured:
        adapter.materialize(
            result,
            invocation=invocation,
            binding=binding,
            handoff=conflicting_handoff,
            created_at=NOW + timedelta(minutes=2),
        )

    assert captured.value.error_code is GraphArtifactResultErrorCode.RESULT_IDENTITY_CONFLICT
    assert artifact.write_count == 1
    assert fixture.port.recover_graph(fixture.activity.run_id).activity_result_commits == ()


@pytest.mark.parametrize(
    ("tenant_id", "tenant_scope_ref", "parent_run_id", "expected_code"),
    (
        (
            "tenant-other",
            TENANT_SCOPE_REF,
            "run-subagent-cross-scope",
            "graph_result_lineage_scope_mismatch",
        ),
        (
            TENANT_ID,
            checksum_for("tenant-other"),
            "run-subagent-cross-scope",
            "graph_result_lineage_scope_mismatch",
        ),
        (
            TENANT_ID,
            TENANT_SCOPE_REF,
            "run-other",
            "subagent_result_scope_mismatch",
        ),
    ),
)
def test_cross_tenant_or_run_is_rejected_before_materialization(
    tenant_id,
    tenant_scope_ref,
    parent_run_id,
    expected_code,
    tmp_path,
) -> None:
    fixture = _dispatched("run-subagent-cross-scope")
    (
        _activity_runtime,
        adapter,
        _runtime,
        _store,
        artifact,
        attempts,
        _catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(status="succeeded", output={"result": "ok"}),
        tmp_path,
    )
    if parent_run_id != fixture.activity.run_id:
        with pytest.raises(HarnessValidationError) as identity_error:
            replace(
                invocation,
                parent_run_id=parent_run_id,
                child_run_id=f"{parent_run_id}:analyze:task-1",
                invocation_id=f"invocation://{parent_run_id}:analyze:task-1",
            )
        assert identity_error.value.code == "subagent_invocation_identity_mismatch"
        assert artifact.write_count == 0
        assert attempts.put_count == 0
        return

    with pytest.raises(HarnessValidationError) as captured:
        adapter.binding_for_activity(
            activity=fixture.activity,
            graph=fixture.graph,
            tenant_id=tenant_id,
            tenant_scope_ref=tenant_scope_ref,
            run_spec_checksum=fixture.run_spec_checksum,
            invocation=invocation,
        )

    assert captured.value.code == expected_code
    assert artifact.write_count == 0
    assert attempts.put_count == 0


def test_bundle_parser_rejects_scope_tampering(tmp_path) -> None:
    fixture = _dispatched("run-subagent-tamper")
    (
        _activity_runtime,
        adapter,
        runtime,
        _store,
        _artifact,
        _attempts,
        _catalog,
        invocation,
    ) = _stack(
        fixture,
        HarnessWorkerResult(status="succeeded", output={"result": "ok"}),
        tmp_path,
    )
    binding = adapter.binding_for_activity(
        activity=fixture.activity,
        graph=fixture.graph,
        tenant_id=TENANT_ID,
        tenant_scope_ref=TENANT_SCOPE_REF,
        run_spec_checksum=fixture.run_spec_checksum,
        invocation=invocation,
    )
    request, bundle = adapter.request_from_verified_result(
        runtime.invoke(invocation),
        invocation=invocation,
        binding=binding,
        created_at=NOW,
    )
    payload = bundle.to_dict()
    payload["graph_binding"]["run_id"] = "run-other"
    payload["bundle_checksum"] = checksum_for(
        {key: value for key, value in payload.items() if key != "bundle_checksum"}
    )

    with pytest.raises(HarnessValidationError) as captured:
        VerifiedSubAgentMaterializedBundle.from_dict(payload)

    assert captured.value.code == "subagent_result_scope_mismatch"
    assert request.binding == binding
