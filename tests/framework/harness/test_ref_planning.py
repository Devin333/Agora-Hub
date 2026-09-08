from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import RefAuthority, RefDescriptor, InMemoryRefResolutionPort
from framework.harness.ref_planning import HarnessPlanningRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase, SnapshotRefResolutionPort
from framework.harness.task_plan.planning_observation import (
    HarnessPlanningObservationService, PlanningObservationPolicy,
    PlanningObservationReceipt, PlanningObservationRequest,
)
from framework.tool import ToolDefinition, ToolExecutor, ToolRegistry
from infrastructure.storage.harness.planning_observation import FilesystemPlanningObservationStore
from tests.framework.harness.test_ref_snapshot_store import _snapshot, _store


def _setup(root: Path, *, parent=None, planner_turn=1, resolution=None, artifact_types=()):
    parent = parent or _snapshot()
    grants, events = _store(root)
    grants.commit(parent)
    receipts = FilesystemPlanningObservationStore(root / "receipts", input_snapshot=parent)
    authority = HarnessPlanningRefAuthority(
        grants, receipt_store=receipts, input_snapshot=parent, planner_turn=planner_turn,
        artifact_resolution=resolution, allowed_artifact_types=artifact_types,
    )
    calls = []
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="research.lookup", version="1.0.0", side_effect="read_only",
        concurrency_safe=True, input_schema={"type": "object"},
    ), lambda arguments: calls.append(arguments) or {"answer": "证据"})
    service = HarnessPlanningObservationService(
        executor=ToolExecutor(registry), registry=registry, store=receipts,
        policy=PlanningObservationPolicy(
            policy_checksum=parent.task_policy_checksum,
            allowed_tool_ids=("research.lookup@1.0.0",), max_tool_calls=1,
        ), planning_ref_authority=authority,
    )
    request = PlanningObservationRequest(
        request_id="lookup", run_id=parent.run_id, stage_id=parent.stage_id,
        planner_turn_id=authority.planner_turn_id, policy_checksum=parent.task_policy_checksum,
        correlation_id="lookup-correlation", tool_name="research.lookup",
        purpose="Read evidence", arguments={"query": "private input"},
    )
    return service, request, receipts, grants, events, calls


def _receipt(request, **kwargs):
    return PlanningObservationReceipt(
        request=request, status="SUCCEEDED", tool_call_id="tool-1",
        observation_summary="private summary", result_checksum=checksum_for("result"),
        **kwargs,
    )


def test_planning_grant_survives_restart_without_live_calls(tmp_path):
    service, request, receipts, grants, events, calls = _setup(tmp_path)
    receipt = service.observe(request)
    assert len(calls) == 1
    metadata = receipts.describe_request(request.request_checksum)
    grant = grants.find(run_id=request.run_id, binding_key=RefAuthoritySnapshot.planning_binding_key(
        service.planning_ref_authority.input_snapshot, request.request_checksum,
    ))
    assert grant.phase is RefSnapshotPhase.PLANNING_OBSERVATION
    assert grant.attempt_identity is None
    assert grant.policy.writable_refs == ()
    assert grant.source_checksum == request.request_checksum
    assert RefAuthoritySnapshot.from_dict(grant.to_dict()) == grant
    assert "private summary" not in str(metadata.to_dict())
    assert "private input" not in str(metadata.to_dict())
    high = events.get_stream_high_watermark("run:ref-run", tenant_id="control")
    reopened, _, _, _, _, reopened_calls = _setup(tmp_path)
    assert reopened.observe(request) == reopened.replay(request) == receipt
    assert reopened.validate_source_refs(
        (receipt.source_ref,), run_id=request.run_id, stage_id=request.stage_id,
        planner_turn_id=request.planner_turn_id, policy_checksum=request.policy_checksum,
    ) == (receipt,)
    assert not reopened_calls
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == high


def test_online_repairs_missing_grant_before_payload_but_offline_never_commits(tmp_path, monkeypatch):
    service, request, receipts, grants, events, calls = _setup(tmp_path)
    receipt = _receipt(request)
    receipts.save(receipt)
    reads = []
    original = receipts.by_source_ref

    def read(source_ref):
        reads.append(source_ref)
        assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 2
        return original(source_ref)

    monkeypatch.setattr(receipts, "by_source_ref", read)
    with pytest.raises(HarnessValidationError) as error:
        service.replay(request)
    assert error.value.code == "REF_SNAPSHOT_MISSING"
    assert reads == [] and calls == []
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    assert service.observe(request) == receipt
    assert reads == [receipt.source_ref] and calls == []


@pytest.mark.parametrize("field,value", [
    ("run_id", "other-run"), ("stage_id", "other-stage"),
    ("planner_turn_id", "candidate-chosen-turn"), ("policy_checksum", checksum_for("other-policy")),
])
def test_foreign_request_is_denied_before_any_payload_read(tmp_path, monkeypatch, field, value):
    service, request, receipts, _, _, calls = _setup(tmp_path)
    monkeypatch.setattr(receipts, "by_source_ref", lambda *_: pytest.fail("unauthorized payload read"))
    for method in (service.observe, service.replay):
        with pytest.raises(HarnessValidationError):
            method(replace(request, **{field: value}))
    assert calls == []


def test_other_physical_execution_and_turn_cannot_consume_a_receipt(tmp_path, monkeypatch):
    service, request, receipts, grants, _, _ = _setup(tmp_path)
    receipt = service.observe(request)
    parent = service.planning_ref_authority.input_snapshot
    with pytest.raises(HarnessValidationError):
        service.planning_ref_authority.require_execution(
            replace(parent.execution_identity, activity_id="another-activity"),
            parent.stage_binding_checksum, parent.task_policy_checksum,
        )
    other = HarnessPlanningRefAuthority(grants, receipt_store=receipts, input_snapshot=parent, planner_turn=2)
    monkeypatch.setattr(receipts, "by_source_ref", lambda *_: pytest.fail("foreign turn payload read"))
    with pytest.raises(HarnessValidationError) as error:
        other.reader().by_source_ref(receipt.source_ref)
    assert error.value.code == "REF_POLICY_SCOPE_MISMATCH"


def test_deleted_committed_receipt_never_reexecutes_tool(tmp_path):
    service, request, receipts, _, _, calls = _setup(tmp_path)
    service.observe(request)
    payload, metadata, _ = receipts._paths(request.request_checksum)
    payload.unlink()
    metadata.unlink()
    with pytest.raises(HarnessValidationError) as error:
        service.observe(request)
    assert error.value.code == "REF_CHECKSUM_MISMATCH"
    assert len(calls) == 1


def test_artifact_metadata_must_match_owner_type_and_pinned_checksum(tmp_path, monkeypatch):
    parent = _snapshot()
    artifact = RefDescriptor(
        ref="artifact://planning/result", run_id=parent.run_id, stage_id=parent.stage_id,
        tenant_id=parent.policy.tenant_id, owner_id=parent.policy.owner_id,
        access_mode="READ_ONLY", artifact_type="planning_evidence",
        source_checksum=checksum_for("artifact"), ref_kind="result",
    )
    service, request, receipts, _, _, _ = _setup(
        tmp_path, resolution=InMemoryRefResolutionPort((artifact,)),
        artifact_types=("planning_evidence",),
    )
    receipt = _receipt(request, artifact_refs=(artifact.ref,))
    receipts.save(receipt)
    assert service.observe(request) == receipt
    monkeypatch.setattr(receipts, "by_source_ref", lambda *_: pytest.fail("changed artifact payload read"))
    for changed in (
        replace(artifact, owner_id="sibling"), replace(artifact, tenant_id="foreign"),
        replace(artifact, artifact_type="private_transcript"),
        replace(artifact, source_checksum=checksum_for("changed")),
    ):
        service.planning_ref_authority._artifact_resolution = InMemoryRefResolutionPort((changed,))
        with pytest.raises(HarnessValidationError):
            service.replay(request)


def test_new_request_obeys_durable_planning_budget(tmp_path):
    service, request, _, _, _, calls = _setup(tmp_path)
    service.observe(request)
    denied = service.observe(replace(request, request_id="next", correlation_id="next"))
    assert denied.reason_code == "planning_tool_budget_exhausted"
    assert len(calls) == 1
    assert service.replay(denied.request) == denied


def test_accepted_stage_recovery_revalidates_planning_refs_without_executing(tmp_path, monkeypatch):
    from framework.harness.task_plan import (
        FakePlanCandidateBuilder, InMemoryTaskPlanStore, TaskPlanStageRequest, TaskPlanStageRunner,
    )
    from tests.framework.harness.task_plan.test_task_plan_runtime import (
        _setup as stage_setup, _candidate, _execution_identity, _task, _AcceptingResultVerifier,
    )

    binding, policy, registry = stage_setup()
    policy = replace(policy, allowed_tool_ids=("research.lookup",))
    candidate = _candidate(binding, (_task("analysis"),))
    parent = _snapshot()
    descriptor = replace(parent.descriptors[0], run_id=candidate.run_id, stage_id=binding.stage_id)
    parent = replace(
        parent, execution_identity=_execution_identity(candidate), stage_id=binding.stage_id,
        stage_binding_checksum=binding.binding_checksum, task_policy_checksum=policy.policy_checksum,
        descriptors=(descriptor,), policy=replace(parent.policy, run_id=candidate.run_id, stage_id=binding.stage_id),
    )
    service, request, receipts, grants, events, calls = _setup(tmp_path, parent=parent)
    receipt = service.observe(request)
    candidate = replace(candidate, source_observation_refs=(receipt.source_ref,), metadata={"planner_turn_id": request.planner_turn_id})
    stage = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate), capability_registry=registry,
        store=InMemoryTaskPlanStore(), planning_observation_port=service,
        result_verifier=_AcceptingResultVerifier(),
        worker_executor=lambda *_: pytest.fail("recovery must not execute a worker"),
    )
    stage_request = TaskPlanStageRequest(
        run_id=candidate.run_id, stage_binding=binding, context_refs={"document": "document"},
        policy=policy, accepted_at="2026-09-08T00:00:00Z", execution_identity=parent.execution_identity,
        ref_authority=RefAuthority(), ref_policy=parent.policy,
        ref_resolution=SnapshotRefResolutionPort(parent),
    )
    plan = stage._ensure_plan(stage_request)
    assert grants.get(run_id=parent.run_id, snapshot_ref=parent.snapshot_ref) == parent
    assert parent.policy.allowed_refs == ("document",)
    original_events = stage.store.read_events(candidate.run_id, binding.stage_id)
    assert stage._ensure_plan(stage_request) == plan
    stage._replay_history(stage_request, plan)
    assert stage.store.read_events(candidate.run_id, binding.stage_id) == original_events
    assert len(calls) == 1

    original_find = grants.find
    missing_binding = RefAuthoritySnapshot.planning_binding_key(parent, request.request_checksum)
    monkeypatch.setattr(grants, "find", lambda **kwargs: None if kwargs["binding_key"] == missing_binding else original_find(**kwargs))
    monkeypatch.setattr(receipts, "by_source_ref", lambda *_: pytest.fail("missing grant must precede payload read"))
    for recover in (lambda: stage._ensure_plan(stage_request), lambda: stage._replay_history(stage_request, plan)):
        with pytest.raises(HarnessValidationError) as error:
            recover()
        assert error.value.code == "REF_SNAPSHOT_MISSING"
    assert stage.store.read_events(candidate.run_id, binding.stage_id) == original_events
    assert len(calls) == 1
