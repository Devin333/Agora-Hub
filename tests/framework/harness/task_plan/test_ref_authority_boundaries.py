from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_PLANNING,
    REF_KIND_RESULT,
    InMemoryRefResolutionPort,
    RefAccessMode,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
)
from framework.harness.subagents.transcript import SubAgentTranscriptReceipt
from framework.harness.task_plan import (
    TaskLifecycle,
    TaskPlanGateRegistry,
    TaskPlanResultVerificationRequest,
    TaskPlanResultVerifier,
    TaskPlanValidationContext,
    TaskPlanValidator,
    task_instance_for_attempt,
)
from framework.harness.task_plan.planning_observation import (
    HarnessPlanningObservationService,
    InMemoryPlanningObservationStore,
    PlanningObservationReceipt,
)
from framework.harness.workers.result import HarnessWorkerResult
from tests.framework.harness.task_plan.test_planning_observation import (
    _request as planning_request,
)
from tests.framework.harness.task_plan.test_planning_observation import (
    _service as planning_service,
)
from tests.framework.harness.task_plan.test_subagent_result_lineage import _fixture
from tests.framework.harness.task_plan.test_task_plan_runtime import (
    _candidate,
    _setup,
    _task,
)
from tests.fixtures.task_plan import InMemoryTaskPlanGateArtifactWriter


class _RecordingAuthority(RefAuthority):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def authorize_ref(self, ref, policy, **kwargs):
        self._events.append(f"authorize:{ref}")
        return super().authorize_ref(ref, policy, **kwargs)


class _RecordingTranscriptStore:
    def __init__(self, delegate, events: list[str]) -> None:
        self._delegate = delegate
        self._events = events

    def write(self, context, output, transcript):
        return self._delegate.write(context, output, transcript)

    def read(self, transcript_ref):
        self._events.append(f"transcript:read:{transcript_ref}")
        return self._delegate.read(transcript_ref)

    def read_context(self, context_ref):
        self._events.append(f"transcript:read-context:{context_ref}")
        return self._delegate.read_context(context_ref)

    def read_output(self, output_ref):
        self._events.append(f"transcript:read-output:{output_ref}")
        return self._delegate.read_output(output_ref)

    def verify(self, receipt):
        self._events.append(f"transcript:verify:{receipt.transcript_ref}")
        return self._delegate.verify(receipt)

    def refs_for_parent(self, parent_run_id, *, limit=256):
        return self._delegate.refs_for_parent(parent_run_id, limit=limit)

    def find_by_identity(self, identity):
        return self._delegate.find_by_identity(identity)


class _RecordingArtifactVerifier:
    def __init__(self, expected_ref: str, events: list[str]) -> None:
        self._expected_ref = expected_ref
        self._events = events

    def verify_artifact_ref(self, ref: str, *, expected_run_id: str) -> None:
        self._events.append(f"artifact-owner:{ref}")
        if ref != self._expected_ref or expected_run_id != "lineage-run":
            raise ValueError("unexpected artifact authority request")


def _descriptor(
    ref: str,
    source_checksum: str,
    *,
    run_id: str,
    stage_id: str,
    ref_kind: str = REF_KIND_RESULT,
    artifact_type: str,
) -> RefDescriptor:
    return RefDescriptor(
        ref=ref,
        run_id=run_id,
        stage_id=stage_id,
        tenant_id="tenant-1",
        owner_id="owner-1",
        access_mode=RefAccessMode.READ_ONLY,
        artifact_type=artifact_type,
        source_checksum=source_checksum,
        ref_kind=ref_kind,
    )


def _policy(
    descriptors: tuple[RefDescriptor, ...],
    *,
    run_id: str,
    stage_id: str,
) -> RefAccessPolicy:
    return RefAccessPolicy(
        policy_id="refs.boundary",
        version="1",
        run_id=run_id,
        stage_id=stage_id,
        tenant_id="tenant-1",
        owner_id="owner-1",
        allowed_refs=tuple(item.ref for item in descriptors),
        allowed_artifact_types=tuple(
            sorted({item.artifact_type for item in descriptors})
        ),
        allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
        pinned_checksums={
            item.ref: item.source_checksum
            for item in descriptors
        },
    )


@pytest.mark.parametrize(
    ("worker_status", "gate_passes", "expected_status", "expects_gate"),
    (
        ("succeeded", True, TaskLifecycle.SUCCEEDED, True),
        ("succeeded", False, TaskLifecycle.FAILED, True),
        ("failed", True, TaskLifecycle.FAILED, False),
    ),
)
def test_result_verifier_authorizes_subagent_refs_before_transcript_and_gate(
    tmp_path: Path,
    worker_status: str,
    gate_passes: bool,
    expected_status: TaskLifecycle,
    expects_gate: bool,
) -> None:
    artifact_ref = "artifact://lineage-run/authority-boundary"
    fixture = _fixture(
        tmp_path,
        worker_status=worker_status,
        worker_artifacts=(artifact_ref,),
    )
    result = fixture["invoke"]()
    receipt = SubAgentTranscriptReceipt.from_dict(result.evidence[0].payload)
    artifact_checksum = checksum_for({"artifact_ref": artifact_ref})
    descriptors = (
        _descriptor(
            receipt.output_ref,
            receipt.output_checksum,
            run_id=fixture["plan"].run_id,
            stage_id=fixture["plan"].stage_id,
            artifact_type="subagent_output",
        ),
        _descriptor(
            receipt.transcript_ref,
            receipt.transcript_checksum,
            run_id=fixture["plan"].run_id,
            stage_id=fixture["plan"].stage_id,
            artifact_type="subagent_transcript",
        ),
        _descriptor(
            artifact_ref,
            artifact_checksum,
            run_id=fixture["plan"].run_id,
            stage_id=fixture["plan"].stage_id,
            artifact_type="artifact",
        ),
    )
    events: list[str] = []
    gates = TaskPlanGateRegistry()

    def evaluate_gate(_request):
        events.append("gate")
        return gate_passes

    gates.register("LineageGate@1", evaluate_gate, deterministic=True)
    verifier = TaskPlanResultVerifier(
        gates,
        transcript_store=_RecordingTranscriptStore(
            fixture["transcript_store"],
            events,
        ),
        artifact_reference_verifier=_RecordingArtifactVerifier(
            artifact_ref,
            events,
        ),
        ref_authority=_RecordingAuthority(events),
        ref_policy=_policy(
            descriptors,
            run_id=fixture["plan"].run_id,
            stage_id=fixture["plan"].stage_id,
        ),
        ref_descriptors={item.ref: item for item in descriptors},
        gate_artifact_writer=InMemoryTaskPlanGateArtifactWriter(),
    )

    record = verifier.verify(
        result,
        task=fixture["resolved"],
        request=TaskPlanResultVerificationRequest(
            plan=fixture["plan"],
            task=fixture["resolved"],
            instance=fixture["instance"],
            worker_result=result,
            execution_identity=fixture["execution_identity"],
        ),
    )

    assert record.status is expected_status
    boundary_events = [
        index
        for index, event in enumerate(events)
        if event.startswith("transcript:") or event == "gate"
    ]
    assert boundary_events
    first_boundary = min(boundary_events)
    for ref in (receipt.output_ref, receipt.transcript_ref, artifact_ref):
        assert events.index(f"authorize:{ref}") < first_boundary
    assert ("gate" in events) is expects_gate

    events.clear()
    denied_descriptors = (replace(descriptors[0], owner_id="sibling-owner"), *descriptors[1:])
    denied_verifier = TaskPlanResultVerifier(
        gates,
        transcript_store=_RecordingTranscriptStore(fixture["transcript_store"], events),
        artifact_reference_verifier=_RecordingArtifactVerifier(artifact_ref, events),
        ref_authority=_RecordingAuthority(events),
        ref_policy=_policy(descriptors, run_id=fixture["plan"].run_id, stage_id=fixture["plan"].stage_id),
        ref_descriptors={item.ref: item for item in denied_descriptors},
    )
    with pytest.raises(HarnessValidationError) as error:
        denied_verifier.verify(result, task=fixture["resolved"], request=TaskPlanResultVerificationRequest(
            plan=fixture["plan"], task=fixture["resolved"], instance=fixture["instance"],
            worker_result=result, execution_identity=fixture["execution_identity"],
        ))
    assert error.value.code == "REF_UNAUTHORIZED"
    assert events == [f"authorize:{receipt.output_ref}"]


def test_result_verifier_binds_ordinary_candidate_ref_to_candidate_payload() -> None:
    stage_binding, task_policy, registry = _setup()
    candidate = _candidate(stage_binding, (_task("task-1"),))
    plan = TaskPlanValidator().accept(
        candidate,
        task_policy,
        registry,
        context=TaskPlanValidationContext(
            run_id=candidate.run_id,
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=task_policy.allowed_gate_refs,
        ),
        accepted_at="2026-09-08T00:00:00Z",
    )
    task = plan.tasks[0]
    instance = task_instance_for_attempt(plan, task.task_id, 1)
    result = HarnessWorkerResult(
        status="succeeded",
        output={"value": "candidate"},
    )
    wrong_checksum = checksum_for({"value": "different"})
    descriptor = _descriptor(
        result.candidate_result_ref,
        wrong_checksum,
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        artifact_type="worker_candidate",
    )
    gate_calls = 0
    gates = TaskPlanGateRegistry()

    def gate(_request):
        nonlocal gate_calls
        gate_calls += 1
        return True

    gates.register("gate@1", gate, deterministic=True)
    verifier = TaskPlanResultVerifier(
        gates,
        ref_authority=RefAuthority(),
        ref_policy=_policy(
            (descriptor,),
            run_id=plan.run_id,
            stage_id=plan.stage_id,
        ),
        ref_descriptors={descriptor.ref: descriptor},
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        verifier.verify(
            result,
            task=task,
            request=TaskPlanResultVerificationRequest(
                plan=plan,
                task=task,
                instance=instance,
                worker_result=result,
            ),
        )

    assert exc_info.value.code == "REF_CHECKSUM_MISMATCH"
    assert gate_calls == 0


class _RecordingPlanningStore(InMemoryPlanningObservationStore):
    def __init__(self):
        super().__init__()
        self.read_calls = 0

    def by_source_ref(self, source_ref):
        self.read_calls += 1
        return super().by_source_ref(source_ref)


def _planning_receipt_with_artifact():
    base_service, _base_store, planning_policy = planning_service()
    request = planning_request(planning_policy.policy_checksum)
    artifact_ref = "artifact://planning/run-1/evidence"
    receipt = PlanningObservationReceipt(
        request=request,
        status="SUCCEEDED",
        tool_call_id="planning-call-1",
        observation_summary="read-only evidence",
        artifact_refs=(artifact_ref,),
        result_checksum=checksum_for({"result": "read-only evidence"}),
        elapsed_ms=1,
    )
    store = _RecordingPlanningStore()
    store.save(receipt)
    return base_service, store, planning_policy, request, receipt, artifact_ref


def test_planning_source_authority_binds_actual_receipt_and_artifacts() -> None:
    (
        base_service,
        store,
        planning_policy,
        request,
        receipt,
        artifact_ref,
    ) = _planning_receipt_with_artifact()
    descriptors = (
        _descriptor(
            receipt.source_ref,
            receipt.receipt_checksum,
            run_id=request.run_id,
            stage_id=request.stage_id,
            ref_kind=REF_KIND_PLANNING,
            artifact_type="planning_receipt",
        ),
        _descriptor(
            artifact_ref,
            checksum_for({"artifact_ref": artifact_ref}),
            run_id=request.run_id,
            stage_id=request.stage_id,
            artifact_type="planning_artifact",
        ),
    )
    events: list[str] = []
    service = HarnessPlanningObservationService(
        executor=base_service._executor,
        registry=base_service._registry,
        store=store,
        policy=planning_policy,
        ref_authority=_RecordingAuthority(events),
        ref_policy=_policy(
            descriptors,
            run_id=request.run_id,
            stage_id=request.stage_id,
        ),
        ref_descriptors={item.ref: item for item in descriptors},
    )

    assert service.validate_source_refs(
        (receipt.source_ref,),
        run_id=request.run_id,
        stage_id=request.stage_id,
        planner_turn_id=request.planner_turn_id,
        policy_checksum=request.policy_checksum,
    ) == (receipt,)
    assert events == [
        f"authorize:{receipt.source_ref}",
        f"authorize:{artifact_ref}",
    ]
    assert store.read_calls == 1
    denied_descriptor = replace(descriptors[0], owner_id="sibling-owner")
    denied_service = HarnessPlanningObservationService(
        executor=base_service._executor, registry=base_service._registry,
        store=store, policy=planning_policy,
        ref_authority=RefAuthority(),
        ref_policy=_policy(descriptors, run_id=request.run_id, stage_id=request.stage_id),
        ref_descriptors={denied_descriptor.ref: denied_descriptor},
    )
    with pytest.raises(HarnessValidationError) as error:
        denied_service.validate_source_refs((receipt.source_ref,), run_id=request.run_id,
            stage_id=request.stage_id, planner_turn_id=request.planner_turn_id,
            policy_checksum=request.policy_checksum)
    assert error.value.code == "REF_UNAUTHORIZED"
    assert store.read_calls == 1


def test_planning_source_authority_rejects_descriptor_not_bound_to_receipt() -> None:
    (
        base_service,
        store,
        planning_policy,
        request,
        receipt,
        _artifact_ref,
    ) = _planning_receipt_with_artifact()
    wrong_descriptor = _descriptor(
        receipt.source_ref,
        checksum_for({"receipt": "different"}),
        run_id=request.run_id,
        stage_id=request.stage_id,
        ref_kind=REF_KIND_PLANNING,
        artifact_type="planning_receipt",
    )
    service = HarnessPlanningObservationService(
        executor=base_service._executor,
        registry=base_service._registry,
        store=store,
        policy=planning_policy,
        ref_authority=RefAuthority(),
        ref_policy=_policy(
            (wrong_descriptor,),
            run_id=request.run_id,
            stage_id=request.stage_id,
        ),
        ref_descriptors={wrong_descriptor.ref: wrong_descriptor},
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        service.validate_source_refs(
            (receipt.source_ref,),
            run_id=request.run_id,
            stage_id=request.stage_id,
            planner_turn_id=request.planner_turn_id,
            policy_checksum=request.policy_checksum,
        )

    assert exc_info.value.code == "REF_CHECKSUM_MISMATCH"


@pytest.mark.parametrize(
    "orphan_kwargs",
    (
        {"ref_resolution": InMemoryRefResolutionPort()},
        {
            "ref_descriptors": {
                "result://orphan": _descriptor(
                    "result://orphan",
                    checksum_for({"orphan": True}),
                    run_id="run-1",
                    stage_id="stage-1",
                    artifact_type="worker_candidate",
                )
            }
        },
    ),
)
def test_reference_boundary_rejects_orphan_resolution_inputs(
    orphan_kwargs,
) -> None:
    base_service, _store, planning_policy = planning_service()
    with pytest.raises(HarnessValidationError) as planning_error:
        HarnessPlanningObservationService(
            executor=base_service._executor,
            registry=base_service._registry,
            store=InMemoryPlanningObservationStore(),
            policy=planning_policy,
            **orphan_kwargs,
        )
    assert planning_error.value.code == "REF_AUTHORITY_REQUIRED"

    with pytest.raises(HarnessValidationError) as verifier_error:
        TaskPlanResultVerifier(**orphan_kwargs)
    assert verifier_error.value.code == "REF_AUTHORITY_REQUIRED"


def test_reference_boundary_rejects_orphan_policy() -> None:
    descriptor = _descriptor(
        "result://orphan",
        checksum_for({"orphan": True}),
        run_id="run-1",
        stage_id="stage-1",
        artifact_type="worker_candidate",
    )
    policy = _policy((descriptor,), run_id="run-1", stage_id="stage-1")
    base_service, _store, planning_policy = planning_service()

    with pytest.raises(HarnessValidationError) as planning_error:
        HarnessPlanningObservationService(
            executor=base_service._executor,
            registry=base_service._registry,
            store=InMemoryPlanningObservationStore(),
            policy=planning_policy,
            ref_policy=policy,
        )
    assert planning_error.value.code == "REF_AUTHORITY_REQUIRED"

    with pytest.raises(HarnessValidationError) as verifier_error:
        TaskPlanResultVerifier(ref_policy=policy)
    assert verifier_error.value.code == "REF_AUTHORITY_REQUIRED"
