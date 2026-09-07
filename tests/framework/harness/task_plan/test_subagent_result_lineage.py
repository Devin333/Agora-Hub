from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.research.graphs import (
    RESEARCH_DYNAMIC_STAGE_ID,
    build_dynamic_paper_analysis_graph_definition,
)
from framework.agent.artifacts import FilesystemArtifactStore
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness import (
    ContextEnvelope,
    DurableTaskPlanStore,
    FakePlanCandidateBuilder,
    HarnessBudget,
    HarnessBudgetSnapshot,
    HarnessValidationError,
    HarnessWorkerResult,
    InMemoryTaskPlanStore,
    PlanBuildBudget,
    PlanCandidate,
    ResolvedSubAgentTaskAdapter,
    SubAgentRuntime,
    SubAgentSpec,
    SubAgentStatus,
    SubAgentContextEvidence,
    SubAgentOutputDocument,
    SubAgentTranscript,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskLifecycle,
    TaskOutputContract,
    TaskPlanEvent,
    TaskPlanGateRegistry,
    TaskPlanPolicy,
    TaskPlanReadyDecision,
    TaskPlanReplayReducer,
    TaskPlanResultVerificationRequest,
    TaskPlanResultVerifier,
    TaskPlanScheduler,
    TaskPlanStageRequest,
    TaskPlanStageBinding,
    TaskPlanStageIdentity,
    TaskPlanStageRunner,
    TaskPlanValidationContext,
    TaskPlanValidator,
    TaskResultRecord,
    TaskRetryPolicy,
    TaskSpec,
    canonical_payload_checksum,
    subagent_attempt_evidence,
    task_plan_context_identities,
    task_instance_for_attempt,
)
from framework.harness.graph import HarnessGraphCompiler, HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
)
from framework.harness.subagents.transcript import (
    SUBAGENT_CONTEXT_SCHEMA_V3,
    SUBAGENT_OUTPUT_SCHEMA_V3,
    SUBAGENT_RECEIPT_SCHEMA_V3,
    SUBAGENT_TRANSCRIPT_SCHEMA_V3,
    subagent_evidence_schemas,
)
from framework.harness.subagents.models import SUBAGENT_INVOCATION_SCHEMA_V3
from framework.harness.task_plan import task_plan_subagent_attempt_identity
from framework.harness.task_plan import TASK_PLAN_REPLAY_REDUCER_VERSION_V3
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore
from infrastructure.storage.events import SQLiteEventStore
from tests.fixtures.task_plan import build_task_plan_stage_binding


ACCEPTED_AT = "2026-08-13T00:00:00Z"


def _execution_identity(
    plan,
    instance,
    *,
    node_id: str | None = None,
) -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id=plan.run_id,
        graph_id=plan.graph_id,
        graph_version=plan.graph_version,
        graph_ref=plan.graph_ref,
        graph_checksum=plan.graph_checksum,
        node_id=node_id or f"node-{plan.stage_id}",
        node_instance_id=f"node-instance-{instance.task_instance_id}",
        activity_id=f"activity-{instance.task_instance_id}",
        attempt=1,
    )


class _CountingSubAgentWorker:
    worker_id = "lineage-subagent"
    worker_version = "1"
    worker_type = HarnessWorkerType.SUBAGENT

    def __init__(
        self,
        *,
        status: str = "succeeded",
        artifacts: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.artifacts = artifacts
        self.calls = 0

    def execute(self, _task):
        self.calls += 1
        return HarnessWorkerResult(
            status=self.status,
            output={"result": "durable candidate"},
            artifacts=self.artifacts,
            error=None if self.status == "succeeded" else "worker failed",
        )


class _RecordingArtifactVerifier:
    def __init__(self, valid_refs: tuple[str, ...] = ()) -> None:
        self.valid_refs = set(valid_refs)
        self.calls: list[tuple[str, str]] = []

    def verify_artifact_ref(self, ref: str, *, expected_run_id: str) -> None:
        self.calls.append((ref, expected_run_id))
        if expected_run_id != "lineage-run" or ref not in self.valid_refs:
            raise ValueError("artifact evidence is not owned by the accepted run")


def _fixture(
    tmp_path: Path,
    *,
    worker_status: str = "succeeded",
    worker_artifacts: tuple[str, ...] = (),
    artifact_reference_verifier=None,
):
    spec = SubAgentSpec(
        subagent_id="lineage-subagent",
        role="analysis.lineage",
        purpose="Produce one durable candidate for TaskPlan lineage tests.",
        input_schema={
            "required": ["input_refs", "task_id", "task_definition_checksum"],
        },
        output_schema={
            "required": ["result"],
            "properties": {"result": {"type": "string"}},
        },
        allowed_tools=("research.read",),
        allowed_memory_namespaces=("research.public",),
        context_policy={"allow_sibling_history": False},
        budget={"max_turns": 2, "max_tool_calls": 1, "max_memory_ops": 1},
    )
    worker = _CountingSubAgentWorker(
        status=worker_status,
        artifacts=worker_artifacts,
    )
    registration = TaskCapabilityRegistration(
        capability="research.lineage",
        worker_binding=HarnessWorkerBinding(
            HarnessContractReference(
                HarnessContractKind.WORKER,
                worker.worker_id,
                worker.worker_version,
            ),
            HarnessWorkerType.SUBAGENT,
            worker,
        ),
        worker_contract_ref="lineage-worker-contract@1",
        input_schema_ref="lineage-input@1",
        output_schema_ref="lineage-output@1",
        subagent_spec=spec,
    )
    registry = TaskCapabilityRegistry((registration,))
    policy = TaskPlanPolicy(
        policy_id="lineage.task-plan",
        version="1",
        stage_id="dynamic_stage",
        allowed_worker_capabilities=("research.lineage",),
        allowed_subagent_ids=(spec.subagent_id,),
        allowed_tool_ids=("research.read",),
        allowed_memory_namespaces=("research.public",),
        allowed_input_refs=("document",),
        allowed_output_roles=("analysis.lineage",),
        required_output_roles=("analysis.lineage",),
        allowed_output_schema_refs=("lineage-output@1",),
        allowed_gate_refs=("LineageGate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={
            "research.lineage": registration.worker_ref,
        },
        required_worker_contract_refs={
            "research.lineage": registration.worker_contract_ref,
        },
        max_tasks=2,
        max_depth=2,
        max_parallelism=1,
        max_replans=0,
        max_task_attempts=1,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(
            max_turns=2,
            max_tool_calls=1,
            max_memory_ops=1,
            max_output_tokens=128,
        ),
        aggregate_task_budget=TaskBudget(
            max_turns=2,
            max_tool_calls=1,
            max_memory_ops=1,
            max_output_tokens=128,
        ),
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="lineage-graph",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        input_keys=("document",),
    )
    task = TaskSpec(
        task_id="lineage-task",
        objective="Verify durable subagent lineage.",
        worker_capability="research.lineage",
        input_refs=("document",),
        output_contract=TaskOutputContract(
            "lineage-output@1",
            "analysis.lineage",
        ),
        acceptance_criteria=TaskAcceptanceCriteria(("LineageGate@1",)),
        requested_tools=("research.read",),
        requested_memory_namespaces=("research.public",),
        budget_request=policy.per_task_budget,
        retry_policy=TaskRetryPolicy(max_attempts=1),
    )
    stage_identity = TaskPlanStageIdentity(
        "lineage-run",
        stage_binding,
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=stage_identity,
        candidate_id="lineage-candidate",
        input_context_refs=("document",),
        tasks=(task,),
        required_output_roles=("analysis.lineage",),
        generated_by="lineage-planner@1",
        requested_plan_budget=PlanBuildBudget(max_builder_calls=1, max_turns=1),
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=TaskPlanValidationContext(
            run_id=candidate.run_id,
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=policy.allowed_gate_refs,
        ),
        accepted_at=ACCEPTED_AT,
    )
    transcript_store = FilesystemSubAgentTranscriptStore(tmp_path / "artifacts")
    runtime = SubAgentRuntime(
        workers={spec.subagent_id: worker},
        transcript_store=transcript_store,
    )
    adapter = ResolvedSubAgentTaskAdapter(runtime)
    gates = TaskPlanGateRegistry()
    gates.register("LineageGate@1", lambda _request: True, deterministic=True)
    verifier = TaskPlanResultVerifier(
        gates,
        transcript_store=transcript_store,
        artifact_reference_verifier=artifact_reference_verifier,
    )
    binding = registry.resolve(task.worker_capability, policy)
    resolved = plan.tasks[0]
    instance = task_instance_for_attempt(plan, task.task_id, 1)
    execution_identity = _execution_identity(
        plan,
        instance,
        node_id=stage_binding.node_id,
    )
    graph_identity, task_identity = task_plan_context_identities(
        plan,
        instance,
        execution_identity=execution_identity,
    )
    context_pack = ContextEnvelope.for_graph(
        envelope_id="lineage-context",
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id="lineage-task-plan",
        worker_type=HarnessWorkerType.TASK_PLAN.value,
        dynamic_tail={"input_refs": ["document"]},
    )
    budget = HarnessBudgetSnapshot.from_budget(HarnessBudget.safe_default())

    def invoke():
        child = adapter.invoke(
            plan=plan,
            resolved_task=resolved,
            binding=binding,
            instance=instance,
            context_pack=context_pack,
            budget_snapshot=budget,
            execution_identity=execution_identity,
        )
        return _worker_result_from_child(child)

    def recover(_binding, active_instance, active_execution):
        assert active_instance == instance
        assert active_execution == execution_identity
        child = adapter.recover(
            plan=plan,
            resolved_task=resolved,
            binding=binding,
            instance=instance,
            context_pack=context_pack,
            budget_snapshot=budget,
            execution_identity=execution_identity,
        )
        return None if child is None else _worker_result_from_child(child, recovered=True)

    request = TaskPlanStageRequest(
        run_id=plan.run_id,
        stage_binding=stage_binding,
        context_refs={"document": "document"},
        policy=policy,
        policy_ref=policy.exact_ref,
        accepted_at=ACCEPTED_AT,
        candidate=candidate,
        execution_identity=execution_identity,
    )
    return {
        "adapter": adapter,
        "binding": binding,
        "budget": budget,
        "candidate": candidate,
        "context_pack": context_pack,
        "plan": plan,
        "policy": policy,
        "registry": registry,
        "worker": worker,
        "transcript_store": transcript_store,
        "artifact_reference_verifier": artifact_reference_verifier,
        "verifier": verifier,
        "instance": instance,
        "execution_identity": execution_identity,
        "resolved": resolved,
        "invoke": invoke,
        "recover": recover,
        "request": request,
    }


def _worker_result_from_child(child, *, recovered: bool = False) -> HarnessWorkerResult:
    succeeded = child.status is SubAgentStatus.SUCCEEDED
    return HarnessWorkerResult(
        status="succeeded" if succeeded else "failed",
        output=child.output,
        artifacts=child.artifact_refs,
        diagnostics={"subagent_id": child.subagent_id, "recovered": recovered},
        evidence=(subagent_attempt_evidence(child.transcript_receipt),)
        if child.transcript_receipt is not None
        else (),
        error=None if succeeded else "subagent attempt failed",
    )


def _graph_only_candidate_plan_for_fixture(fixture):
    policy = replace(fixture["policy"], stage_id=RESEARCH_DYNAMIC_STAGE_ID)
    definition = build_dynamic_paper_analysis_graph_definition()
    task_plan_binding = replace(
        definition.task_plan_stage_bindings[0],
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
    )
    graph = HarnessGraphCompiler().compile(
        replace(
            definition,
            task_plan_stage_bindings=(task_plan_binding,),
            definition_checksum=None,
        )
    ).graph
    stage_binding = TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID)
    stage_identity = TaskPlanStageIdentity(
        fixture["plan"].run_id,
        stage_binding,
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=stage_identity,
        candidate_id="graph-lineage-candidate",
        input_context_refs=fixture["candidate"].input_context_refs,
        tasks=fixture["candidate"].tasks,
        required_output_roles=fixture["candidate"].required_output_roles,
        generated_by=fixture["candidate"].generated_by,
        requested_plan_budget=fixture["candidate"].requested_plan_budget,
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        fixture["registry"],
        context=TaskPlanValidationContext(
            run_id=candidate.run_id,
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=policy.allowed_gate_refs,
        ),
        accepted_at=fixture["plan"].accepted_at,
    )
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    return candidate, plan, instance


def _graph_only_plan_for_fixture(fixture):
    _, plan, instance = _graph_only_candidate_plan_for_fixture(fixture)
    return plan, instance


def _graph_context_for_attempt(plan, instance, *, envelope_id: str) -> ContextEnvelope:
    graph_identity, task_identity = task_plan_context_identities(
        plan,
        instance,
        execution_identity=_execution_identity(plan, instance),
    )
    return ContextEnvelope.for_graph(
        envelope_id=envelope_id,
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id="lineage-task-plan",
        worker_type=HarnessWorkerType.TASK_PLAN.value,
        dynamic_tail={"input_refs": ["document"]},
    )


def _write_graph_only_attempt(
    fixture,
    plan,
    instance,
    *,
    worker_status: str = "succeeded",
    identity=None,
):
    context_pack = _graph_context_for_attempt(
        plan,
        instance,
        envelope_id=f"lineage-evidence-context-{instance.task_instance_id}",
    )
    resolved_identity = identity or task_plan_subagent_attempt_identity(
        plan,
        instance,
        invocation_id=f"invocation://{instance.task_instance_id}",
        child_run_id=f"{plan.run_id}:{plan.stage_id}:{instance.task_instance_id}",
        subagent_id=plan.tasks[0].subagent_id,
        context_pack=context_pack,
    )
    schemas = subagent_evidence_schemas(resolved_identity)
    context = SubAgentContextEvidence(
        identity=resolved_identity,
        context_envelope_ref=f"context://{instance.task_instance_id}",
        input_refs=("document",),
        memory_context_refs=(),
        schema_version=schemas.context,
    )
    output = SubAgentOutputDocument(
        identity=resolved_identity,
        status=worker_status,
        output={"result": "durable graph candidate"},
        error_code=None if worker_status == "succeeded" else "worker_failed",
        schema_version=schemas.output,
    )
    transcript = SubAgentTranscript(
        identity=resolved_identity,
        context_envelope_ref=context.context_envelope_ref,
        input_refs=context.input_refs,
        output_ref=output.ref,
        output_checksum=output.output_checksum,
        observed_at=plan.accepted_at,
        schema_version=schemas.transcript,
    )
    receipt = fixture["transcript_store"].write(context, output, transcript)
    result = HarnessWorkerResult(
        status=worker_status,
        output=output.output,
        diagnostics={"subagent_id": resolved_identity.subagent_id},
        evidence=(subagent_attempt_evidence(receipt),),
        error=None if worker_status == "succeeded" else "worker failed",
    )
    return resolved_identity, receipt, result


def _start_attempt(store, plan, instance) -> None:
    scheduler = TaskPlanScheduler()
    projection = scheduler.reserve_ready_tasks(
        store.load_projection(plan.run_id, plan.stage_id),
        TaskPlanReadyDecision((instance,)),
    )
    transitions = (
        ("TASK_READY", lambda value: value),
        ("TASK_DISPATCHED", lambda value: scheduler.mark_dispatched(value, instance)),
        ("TASK_STARTED", lambda value: scheduler.mark_started(value, instance)),
    )
    for event_type, transition in transitions:
        projection = transition(projection)
        sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
        projection = replace(projection, last_sequence=sequence)
        store.commit_event(
            TaskPlanEvent.for_plan(
                event_type,
                plan,
                task_id=instance.task_id,
                task_instance_id=instance.task_instance_id,
                attempt=instance.attempt,
                input_checksum=instance.task_definition_checksum,
                sequence=sequence,
            ),
            projection,
        )


def _committed_lineage(
    tmp_path: Path,
    *,
    worker_status: str = "succeeded",
    worker_artifacts: tuple[str, ...] = (),
    artifact_reference_verifier=None,
):
    fixture = _fixture(
        tmp_path,
        worker_status=worker_status,
        worker_artifacts=worker_artifacts,
        artifact_reference_verifier=artifact_reference_verifier,
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(fixture["candidate"])
    store.accept_plan(fixture["plan"])
    _start_attempt(store, fixture["plan"], fixture["instance"])
    worker_result = fixture["invoke"]()
    record = fixture["verifier"].verify(
        worker_result,
        task=fixture["resolved"],
        request=TaskPlanResultVerificationRequest(
            plan=fixture["plan"],
            task=fixture["resolved"],
            instance=fixture["instance"],
            worker_result=worker_result,
            execution_identity=fixture["execution_identity"],
        ),
    )
    store.append_result(record)
    fixture.update(store=store, worker_result=worker_result, record=record)
    return fixture


def test_graph_only_subagent_invocation_uses_accepted_plan_identity_and_recovery(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    policy = replace(fixture["policy"], stage_id=RESEARCH_DYNAMIC_STAGE_ID)
    binding = fixture["registry"].resolve(
        plan.tasks[0].task.worker_capability,
        policy,
    )
    graph_identity, task_identity = task_plan_context_identities(
        plan,
        instance,
        execution_identity=_execution_identity(plan, instance),
    )
    context_pack = ContextEnvelope.for_graph(
        envelope_id="lineage-graph-context",
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id="lineage-task-plan",
        worker_type=HarnessWorkerType.TASK_PLAN.value,
        dynamic_tail={"input_refs": ["document"]},
    )
    invocation = fixture["adapter"].build_invocation(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    expected_identity = task_plan_subagent_attempt_identity(
        plan,
        instance,
        invocation_id=invocation.invocation_id,
        child_run_id=invocation.child_run_id,
        subagent_id=invocation.subagent_spec.subagent_id,
        context_pack=context_pack,
    )
    payload = invocation.to_dict()

    assert invocation.schema_version == SUBAGENT_INVOCATION_SCHEMA_V3
    assert invocation.attempt_identity == expected_identity
    assert set(payload) == {
        "attempt_identity",
        "budget_snapshot",
        "context_envelope",
        "input_refs",
        "metadata",
        "observed_at",
        "schema_version",
        "subagent_spec",
    }
    assert "workflow_id" not in payload
    assert "workflow_id" not in payload["attempt_identity"]
    assert payload["attempt_identity"]["context_envelope_id"] == context_pack.envelope_id
    assert payload["attempt_identity"]["context_envelope_checksum"] == context_pack.checksum
    context_payload = payload["context_envelope"]["context_pack"]
    assert context_payload["schema_version"] == "newsroom.context-envelope/v2"
    assert not {"workflow_id", "workflow_ref", "step_id"}.intersection(
        context_payload
    )
    assert canonical_payload_checksum(payload) == (
        "sha256:b41fc6685afa3d120dcf457dd6082c6ecdc4fbb6876983aa974ecc903f5d3651"
    )

    for invalid_context in (
        context_pack.to_dict(),
        ContextEnvelope.for_graph(
            envelope_id="lineage-wrong-attempt-context",
            graph_identity=graph_identity,
            task_execution_identity=replace(task_identity, attempt=2),
            phase="EXECUTE",
            worker_id="lineage-task-plan",
            worker_type=HarnessWorkerType.TASK_PLAN.value,
        ),
    ):
        with pytest.raises(HarnessValidationError) as context_error:
            fixture["adapter"].invoke(
                plan=plan,
                resolved_task=plan.tasks[0],
                binding=binding,
                instance=instance,
                context_pack=invalid_context,
                budget_snapshot=fixture["budget"],
                execution_identity=_execution_identity(plan, instance),
            )
        assert context_error.value.code == "task_plan_result_identity_mismatch"
        assert fixture["worker"].calls == 0

    result = fixture["adapter"].invoke(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    assert result.status is SubAgentStatus.SUCCEEDED
    assert fixture["worker"].calls == 1
    assert result.transcript_receipt is not None
    receipt = result.transcript_receipt
    assert receipt.schema_version == SUBAGENT_RECEIPT_SCHEMA_V3
    assert receipt.identity_checksum == expected_identity.identity_checksum
    assert (
        fixture["transcript_store"].read_context(receipt.context_ref).schema_version
        == SUBAGENT_CONTEXT_SCHEMA_V3
    )
    assert (
        fixture["transcript_store"].read_output(receipt.output_ref).schema_version
        == SUBAGENT_OUTPUT_SCHEMA_V3
    )
    assert (
        fixture["transcript_store"].read(receipt.transcript_ref).schema_version
        == SUBAGENT_TRANSCRIPT_SCHEMA_V3
    )

    recovered = fixture["adapter"].recover(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    assert recovered is not None
    assert recovered.invocation_id == result.invocation_id
    assert recovered.child_run_id == result.child_run_id
    assert recovered.status is result.status
    assert recovered.output == result.output
    assert recovered.transcript_receipt == result.transcript_receipt
    assert recovered.metadata["recovered"] is True
    assert fixture["worker"].calls == 1

    with pytest.raises(HarnessValidationError) as invocation_error:
        replace(invocation, schema_version="newsroom.subagent-invocation/v2")
    assert invocation_error.value.code == "subagent_invocation_schema_unsupported"


def test_success_and_failure_events_carry_complete_typed_lineage(tmp_path: Path) -> None:
    succeeded = _committed_lineage(tmp_path / "succeeded")
    failed = _committed_lineage(tmp_path / "failed", worker_status="failed")

    for fixture, result_event_type, terminal_event_type in (
        (succeeded, "TASK_RESULT_ACCEPTED", "TASK_COMPLETED"),
        (failed, "TASK_RESULT_REJECTED", "TASK_FAILED"),
    ):
        record = fixture["record"]
        assert record.transcript_ref is not None
        assert record.transcript_checksum is not None
        assert record.subagent_output_ref is not None
        assert record.subagent_output_checksum is not None
        if record.status is TaskLifecycle.SUCCEEDED:
            assert record.result_ref == record.subagent_output_ref
        else:
            assert record.result_ref is None
        events = fixture["store"].read_events(
            fixture["plan"].run_id,
            fixture["plan"].stage_id,
        )
        result_event = next(item for item in events if item.event_type == result_event_type)
        terminal_event = next(item for item in events if item.event_type == terminal_event_type)
        for event in (result_event, terminal_event):
            assert event.payload["transcript_ref"] == record.transcript_ref
            assert event.payload["transcript_checksum"] == record.transcript_checksum
            assert event.payload["subagent_output_ref"] == record.subagent_output_ref
            assert event.payload["subagent_output_checksum"] == record.subagent_output_checksum
        transcript = fixture["transcript_store"].read(record.transcript_ref)
        output = fixture["transcript_store"].read_output(record.subagent_output_ref)
        assert transcript.identity.task_instance_id == record.task_instance_id
        assert transcript.identity.attempt == record.attempt
        assert output.output_checksum == record.subagent_output_checksum


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("plan_id", "forged-plan"),
        ("plan_version", 2),
        ("plan_checksum", "sha256:" + "f" * 64),
        ("task_definition_checksum", "sha256:" + "e" * 64),
    ),
)
def test_graph_only_subagent_invocation_rejects_forged_task_plan_identity_before_worker_call(
    tmp_path: Path,
    field_name: str,
    forged_value: str | int,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    policy = replace(fixture["policy"], stage_id=RESEARCH_DYNAMIC_STAGE_ID)
    binding = fixture["registry"].resolve(
        plan.tasks[0].task.worker_capability,
        policy,
    )
    graph_identity, task_identity = task_plan_context_identities(
        plan,
        instance,
        execution_identity=_execution_identity(plan, instance),
    )
    context_pack = ContextEnvelope.for_graph(
        envelope_id="lineage-direct-invocation-context",
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id="lineage-task-plan",
        worker_type=HarnessWorkerType.TASK_PLAN.value,
    )
    invocation = fixture["adapter"].build_invocation(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    assert invocation.attempt_identity is not None

    with pytest.raises(HarnessValidationError) as captured:
        replace(
            invocation,
            attempt_identity=replace(
                invocation.attempt_identity,
                **{field_name: forged_value},
            ),
        )

    assert captured.value.code == "subagent_invocation_identity_mismatch"
    assert fixture["worker"].calls == 0


def test_graph_only_subagent_invocation_rejects_non_v3_schema(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    policy = replace(fixture["policy"], stage_id=RESEARCH_DYNAMIC_STAGE_ID)
    binding = fixture["registry"].resolve(
        plan.tasks[0].task.worker_capability,
        policy,
    )
    graph_identity, task_identity = task_plan_context_identities(
        plan,
        instance,
        execution_identity=_execution_identity(plan, instance),
    )
    context_pack = ContextEnvelope.for_graph(
        envelope_id="lineage-non-v3-schema-context",
        graph_identity=graph_identity,
        task_execution_identity=task_identity,
        phase="EXECUTE",
        worker_id="lineage-task-plan",
        worker_type=HarnessWorkerType.TASK_PLAN.value,
    )
    invocation = fixture["adapter"].build_invocation(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    with pytest.raises(HarnessValidationError) as captured:
        replace(
            invocation,
            schema_version="newsroom.subagent-invocation/v2",
        )

    assert captured.value.code == "subagent_invocation_schema_unsupported"
    assert fixture["worker"].calls == 0


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    (
        ("dynamic_tail", {"input_refs": ["forged-document"]}),
        ("worker_id", "forged-task-plan"),
    ),
)
def test_graph_only_subagent_invocation_rejects_context_content_tampering_before_worker_call(
    tmp_path: Path,
    field_name: str,
    forged_value: dict[str, list[str]] | str,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    policy = replace(fixture["policy"], stage_id=RESEARCH_DYNAMIC_STAGE_ID)
    binding = fixture["registry"].resolve(
        plan.tasks[0].task.worker_capability,
        policy,
    )
    context_pack = _graph_context_for_attempt(
        plan,
        instance,
        envelope_id="lineage-context-content-bound",
    )
    invocation = fixture["adapter"].build_invocation(
        plan=plan,
        resolved_task=plan.tasks[0],
        binding=binding,
        instance=instance,
        context_pack=context_pack,
        budget_snapshot=fixture["budget"],
        execution_identity=_execution_identity(plan, instance),
    )
    forged_context = replace(
        context_pack,
        checksum=None,
        **{field_name: forged_value},
    )

    with pytest.raises(HarnessValidationError) as captured:
        replace(
            invocation,
            context_envelope=replace(
                invocation.context_envelope,
                context_pack=forged_context,
            ),
        )

    assert captured.value.code == "subagent_invocation_identity_mismatch"
    assert fixture["worker"].calls == 0


@pytest.mark.parametrize("worker_status", ("succeeded", "failed"))
def test_graph_only_verifier_binds_v3_transcript_to_v3_result(
    tmp_path: Path,
    worker_status: str,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    _, receipt, worker_result = _write_graph_only_attempt(
        fixture,
        plan,
        instance,
        worker_status=worker_status,
    )

    record = fixture["verifier"].verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
            execution_identity=_execution_identity(plan, instance),
        ),
    )

    assert record.is_graph_only is True
    assert record.matches_plan_identity(plan)
    assert record.status is (
        TaskLifecycle.SUCCEEDED
        if worker_status == "succeeded"
        else TaskLifecycle.FAILED
    )
    assert record.transcript_ref == receipt.transcript_ref
    assert record.subagent_output_ref == receipt.output_ref
    assert record.transcript_ref.startswith("subagent-transcript://v3/")
    assert "workflow_id" not in record.to_dict()


def test_graph_only_offline_replay_verifies_v3_transcript_without_worker_call(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    candidate, plan, instance = _graph_only_candidate_plan_for_fixture(fixture)
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    _start_attempt(store, plan, instance)
    _, _, worker_result = _write_graph_only_attempt(fixture, plan, instance)
    record = fixture["verifier"].verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
            execution_identity=_execution_identity(plan, instance),
        ),
    )
    store.append_result(record)
    events = store.read_events(plan.run_id, plan.stage_id)
    worker_calls = fixture["worker"].calls

    report = TaskPlanReplayReducer(fixture["transcript_store"]).replay(
        (plan,),
        events,
        results=(record,),
    )

    assert report.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V3
    assert report.projection.matches_plan_identity(plan)
    assert report.projection.tasks[0].status is TaskLifecycle.SUCCEEDED
    assert fixture["worker"].calls == worker_calls == 0

    other_graph_id = "research.other.dynamic"
    other_graph_ref = f"{other_graph_id}@{record.graph_version}"
    other_stage_identity_checksum = canonical_payload_checksum(
        {
            "schema_version": record.stage_identity_schema,
            "run_id": record.run_id,
            "graph_schema_version": record.graph_schema_version,
            "compiler_version": record.compiler_version,
            "condition_policy_version": record.condition_policy_version,
            "graph_id": other_graph_id,
            "graph_version": record.graph_version,
            "graph_checksum": record.graph_checksum,
            "stage_id": record.stage_id,
            "stage_binding_checksum": record.stage_binding_checksum,
            "graph_ref": other_graph_ref,
        }
    )
    cross_graph_result = replace(
        record,
        graph_id=other_graph_id,
        graph_ref=other_graph_ref,
        stage_identity_checksum=other_stage_identity_checksum,
    )
    with pytest.raises(HarnessValidationError) as result_identity_error:
        TaskPlanReplayReducer(fixture["transcript_store"]).replay(
            (plan,),
            events,
            results=(cross_graph_result,),
        )
    assert result_identity_error.value.code == "task_plan_replay_result_mismatch"
    assert fixture["worker"].calls == worker_calls


def test_graph_only_verifier_rejects_cross_graph_transcript_identity(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    context_pack = _graph_context_for_attempt(
        plan,
        instance,
        envelope_id="lineage-cross-graph-context",
    )
    identity = task_plan_subagent_attempt_identity(
        plan,
        instance,
        invocation_id=f"invocation://{instance.task_instance_id}",
        child_run_id=f"{plan.run_id}:{plan.stage_id}:{instance.task_instance_id}",
        subagent_id=plan.tasks[0].subagent_id,
        context_pack=context_pack,
    )
    other_graph_identity = replace(
        identity,
        graph_id="research.other.dynamic",
        graph_ref=f"research.other.dynamic@{identity.graph_version}",
    )
    _, _, worker_result = _write_graph_only_attempt(
        fixture,
        plan,
        instance,
        identity=other_graph_identity,
    )

    with pytest.raises(HarnessValidationError) as captured:
        fixture["verifier"].verify(
            worker_result,
            task=plan.tasks[0],
            request=TaskPlanResultVerificationRequest(
                plan=plan,
                task=plan.tasks[0],
                instance=instance,
                worker_result=worker_result,
                execution_identity=_execution_identity(plan, instance),
            ),
        )

    assert captured.value.code == "task_plan_subagent_evidence_mismatch"


def test_verification_request_rejects_forged_task_instance_identity(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    plan, instance = _graph_only_plan_for_fixture(fixture)
    worker_result = HarnessWorkerResult(
        status="succeeded",
        output={"result": "candidate"},
    )

    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=replace(instance, task_instance_id="forged-instance"),
            worker_result=worker_result,
        )

    assert captured.value.code == "task_plan_task_instance_mismatch"


def test_offline_replay_verifies_transcript_and_rejects_event_lineage_mismatch(
    tmp_path: Path,
) -> None:
    fixture = _committed_lineage(tmp_path)
    events = fixture["store"].read_events(
        fixture["plan"].run_id,
        fixture["plan"].stage_id,
    )
    worker_calls = fixture["worker"].calls

    report = TaskPlanReplayReducer(fixture["transcript_store"]).replay(
        (fixture["plan"],),
        events,
        results=(fixture["record"],),
    )

    assert report.projection.tasks[0].status is TaskLifecycle.SUCCEEDED
    assert report.accepted_output_refs == (fixture["record"].subagent_output_ref,)
    assert fixture["worker"].calls == worker_calls == 1

    result_event_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "TASK_RESULT_ACCEPTED"
    )
    payload = dict(events[result_event_index].payload)
    payload["subagent_output_checksum"] = "sha256:" + "f" * 64
    tampered_events = list(events)
    tampered_events[result_event_index] = replace(
        events[result_event_index],
        payload=payload,
    )
    with pytest.raises(HarnessValidationError) as captured:
        TaskPlanReplayReducer(fixture["transcript_store"]).replay(
            (fixture["plan"],),
            tuple(tampered_events),
            results=(fixture["record"],),
        )
    assert captured.value.code == "task_plan_replay_result_mismatch"
    assert fixture["worker"].calls == worker_calls


def test_subagent_artifact_refs_require_the_canonical_owner(tmp_path: Path) -> None:
    artifact_ref = "artifact://lineage-run/candidate"
    missing = _fixture(
        tmp_path / "missing-verifier",
        worker_artifacts=(artifact_ref,),
    )
    missing_result = missing["invoke"]()
    with pytest.raises(HarnessValidationError) as missing_error:
        missing["verifier"].verify(
            missing_result,
            task=missing["resolved"],
            request=TaskPlanResultVerificationRequest(
                plan=missing["plan"],
                task=missing["resolved"],
                instance=missing["instance"],
                worker_result=missing_result,
                execution_identity=missing["execution_identity"],
            ),
        )
    assert missing_error.value.code == "task_plan_subagent_artifact_verifier_required"

    rejecting_verifier = _RecordingArtifactVerifier()
    fabricated = _fixture(
        tmp_path / "fabricated",
        worker_artifacts=(artifact_ref,),
        artifact_reference_verifier=rejecting_verifier,
    )
    fabricated_result = fabricated["invoke"]()
    with pytest.raises(HarnessValidationError) as fabricated_error:
        fabricated["verifier"].verify(
            fabricated_result,
            task=fabricated["resolved"],
            request=TaskPlanResultVerificationRequest(
                plan=fabricated["plan"],
                task=fabricated["resolved"],
                instance=fabricated["instance"],
                worker_result=fabricated_result,
                execution_identity=fabricated["execution_identity"],
            ),
        )
    assert fabricated_error.value.code == "task_plan_subagent_artifact_unverified"
    assert fabricated_error.value.details == {"artifact_index": 0}

    accepting_verifier = _RecordingArtifactVerifier((artifact_ref,))
    accepted = _committed_lineage(
        tmp_path / "accepted",
        worker_artifacts=(artifact_ref,),
        artifact_reference_verifier=accepting_verifier,
    )
    assert accepted["record"].output_refs == (artifact_ref,)
    assert accepting_verifier.calls == [(artifact_ref, "lineage-run")]


def test_offline_replay_revalidates_artifact_refs_without_live_worker_call(
    tmp_path: Path,
) -> None:
    artifact_ref = "artifact://lineage-run/candidate"
    artifact_verifier = _RecordingArtifactVerifier((artifact_ref,))
    fixture = _committed_lineage(
        tmp_path,
        worker_artifacts=(artifact_ref,),
        artifact_reference_verifier=artifact_verifier,
    )
    events = fixture["store"].read_events(
        fixture["plan"].run_id,
        fixture["plan"].stage_id,
    )
    worker_calls = fixture["worker"].calls

    replay = TaskPlanReplayReducer(
        fixture["transcript_store"],
        artifact_reference_verifier=artifact_verifier,
    ).replay(
        (fixture["plan"],),
        events,
        results=(fixture["record"],),
    )

    assert replay.verified is True
    assert fixture["worker"].calls == worker_calls == 1
    assert artifact_verifier.calls == [
        (artifact_ref, "lineage-run"),
        (artifact_ref, "lineage-run"),
    ]

    artifact_verifier.valid_refs.clear()
    with pytest.raises(HarnessValidationError) as changed_error:
        TaskPlanReplayReducer(
            fixture["transcript_store"],
            artifact_reference_verifier=artifact_verifier,
        ).replay(
            (fixture["plan"],),
            events,
            results=(fixture["record"],),
        )
    assert changed_error.value.code == "task_plan_subagent_artifact_unverified"
    assert fixture["worker"].calls == worker_calls

    with pytest.raises(HarnessValidationError) as missing_error:
        TaskPlanReplayReducer(fixture["transcript_store"]).replay(
            (fixture["plan"],),
            events,
            results=(fixture["record"],),
        )
    assert missing_error.value.code == "task_plan_subagent_artifact_verifier_required"


def test_artifact_verification_failure_halts_parent_without_task_result(
    tmp_path: Path,
) -> None:
    artifact_ref = "artifact://lineage-run/fabricated"
    artifact_verifier = _RecordingArtifactVerifier()
    fixture = _fixture(
        tmp_path,
        worker_artifacts=(artifact_ref,),
        artifact_reference_verifier=artifact_verifier,
    )
    database = tmp_path / "task-plan-events.sqlite3"
    # Keep immutable TaskPlan document paths below the Windows legacy path limit.
    task_plan_artifacts = tmp_path.parent / "halt-artifacts"
    event_store = SQLiteEventStore(database)
    store = DurableTaskPlanStore(
        EventRuntime(
            store=event_store,
            schema_catalog=default_event_schema_catalog(),
        ),
        event_store,
        artifact_store=FilesystemArtifactStore(task_plan_artifacts),
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(fixture["candidate"]),
        capability_registry=fixture["registry"],
        store=store,
        result_verifier=fixture["verifier"],
        worker_executor=lambda *_args: fixture["invoke"](),
    )

    result = runner.run(fixture["request"])

    assert result.status.value == "blocked"
    assert result.diagnostics["reason_code"] == "task_plan_subagent_artifact_unverified"
    reopened_event_store = SQLiteEventStore(database)
    reopened = DurableTaskPlanStore(
        EventRuntime(
            store=reopened_event_store,
            schema_catalog=default_event_schema_catalog(),
        ),
        reopened_event_store,
        artifact_store=FilesystemArtifactStore(task_plan_artifacts),
    )
    plan = reopened.plan(fixture["plan"].run_id, fixture["plan"].stage_id)
    assert plan is not None
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == ()
    events = reopened.read_events(plan.run_id, plan.stage_id)
    assert events[-1].event_type == "TASK_PLAN_HALTED"
    assert events[-1].reason_code == "task_plan_subagent_artifact_unverified"
    assert not {
        "TASK_RESULT_ACCEPTED",
        "TASK_RESULT_REJECTED",
        "TASK_COMPLETED",
        "TASK_FAILED",
    }.intersection(event.event_type for event in events)


def test_receipt_before_task_result_is_recovered_without_live_worker_call(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    store = InMemoryTaskPlanStore()
    store.append_candidate(fixture["candidate"])
    store.accept_plan(fixture["plan"])
    _start_attempt(store, fixture["plan"], fixture["instance"])

    first = fixture["invoke"]()
    assert len(first.evidence) == 1
    assert fixture["worker"].calls == 1
    assert store.results_for(
        fixture["plan"].run_id,
        fixture["plan"].stage_id,
        fixture["plan"].plan_id,
        fixture["plan"].version,
    ) == ()

    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(fixture["candidate"]),
        capability_registry=fixture["registry"],
        store=store,
        result_verifier=fixture["verifier"],
        worker_executor=lambda *_args: pytest.fail("live worker must not be called"),
        worker_result_recovery=fixture["recover"],
    )
    result = runner.run(fixture["request"])

    assert result.status.value == "succeeded"
    assert fixture["worker"].calls == 1
    records = store.results_for(
        fixture["plan"].run_id,
        fixture["plan"].stage_id,
        fixture["plan"].plan_id,
        fixture["plan"].version,
    )
    assert len(records) == 1
    assert records[0].transcript_ref == first.evidence[0].payload["transcript_ref"]
    event_types = [
        event.event_type
        for event in store.read_events(
            fixture["plan"].run_id,
            fixture["plan"].stage_id,
        )
    ]
    assert event_types[-4:] == [
        "TASK_RESULT_ACCEPTED",
        "TASK_COMPLETED",
        "STAGE_OUTPUT_AGGREGATED",
        "TASK_PLAN_VERIFIED",
    ]
