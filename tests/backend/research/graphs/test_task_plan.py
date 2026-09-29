from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.research.graphs import (
    RESEARCH_DYNAMIC_AGGREGATOR_REF,
    RESEARCH_DYNAMIC_CAPABILITIES,
    RESEARCH_DYNAMIC_GATE_REFS,
    RESEARCH_DYNAMIC_GATES_BY_CAPABILITY,
    RESEARCH_DYNAMIC_OUTPUT_ROLES_BY_CAPABILITY,
    RESEARCH_DYNAMIC_OUTPUT_SCHEMA_REFS,
    RESEARCH_DYNAMIC_STAGE_ID,
    RESEARCH_DYNAMIC_TOOL_IDS,
    RESEARCH_DYNAMIC_WORKER_CONTRACT_REFS,
    RESEARCH_DYNAMIC_WORKER_REFS,
    ResearchAnalysisPlanCandidateBuilder,
    ResearchAnalysisTaskPlanStageWorker,
    build_dynamic_paper_analysis_graph_definition,
    build_paper_analysis_gate_registry,
    build_research_analysis_capability_registry,
    build_research_analysis_task_plan_aggregator,
    build_research_analysis_task_plan_policy,
    build_research_artifact_terminal_policy,
    validate_research_analysis_candidate,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskAdmissionOwner
from framework.harness.task_plan.store import (
    LogicalTaskReadiness,
    TASK_PLAN_EVENT_SCHEMA_V3,
    TaskQueueAdmissionEvidence,
)
from framework.harness.task_plan import (
    GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
    GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
    InMemoryTaskPlanStore,
    PlanBuildRequest,
    PlanCandidate,
    PlanPatch,
    PlanPatchOperation,
    PlanPatchOperationType,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskLifecycle,
    TaskPlanGateEvidence,
    TaskPlanPatchValidator,
    TaskPlanStageRequest,
    TASK_PLAN_RESULT_SCHEMA,
    TaskPlanEvent,
    TaskPlanReadyDecision,
    TaskResultRecord,
    TaskPlanScheduler,
    TaskPlanStageBinding,
    TaskPlanResultVerifier,
    TaskPlanStageIdentity,
    TaskPlanValidationContext,
    TaskPlanValidator,
    ValidatedTaskPlan,
    task_instance_for_attempt,
)
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
)
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.workers.result import HarnessWorkerResult
from framework.harness.workers.result import HarnessWorkerEvidence
from framework.harness.subagents.transcript import SubAgentTranscriptReceipt
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from tests.fixtures.task_plan import InMemoryTaskPlanGateArtifactWriter


class _BoundResearchWorker:
    worker_version = "1"
    worker_type = HarnessWorkerType.SUBAGENT

    def __init__(self, capability: str) -> None:
        self.worker_id = capability

    def execute(self, _task):
        return HarnessWorkerResult(status="succeeded", output={"candidate": {}})


class _OutlineWorker:
    def __init__(self, outline: dict[str, object]) -> None:
        self.outline = outline
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate_candidate(self, *, task: str, payload: dict[str, object], timeout_seconds=None, max_transport_attempts=None):
        assert timeout_seconds == 30.0
        assert max_transport_attempts == 1
        self.calls.append((task, payload))
        return self.outline


def test_policy_pins_existing_research_gates_workers_and_subagents() -> None:
    policy = build_research_analysis_task_plan_policy()
    gate_registry = build_paper_analysis_gate_registry()

    assert policy.pinned_capability_bindings == RESEARCH_DYNAMIC_WORKER_REFS
    assert (
        policy.required_worker_contract_refs
        == RESEARCH_DYNAMIC_WORKER_CONTRACT_REFS
    )
    assert policy.metadata["stage_aggregator_ref"] == RESEARCH_DYNAMIC_AGGREGATOR_REF
    assert all(gate_registry.bindings_for(ref) for ref in RESEARCH_DYNAMIC_GATE_REFS)
    assert set(policy.allowed_subagent_ids) == {
        "research_analysis_structure",
        "research_analysis_contribution",
        "research_analysis_experiments",
    }
    assert policy.join_policy == "wait_all"
    assert policy.serial_fallback is False
    assert policy.max_tasks_per_group == 8
    assert policy.max_waves == 8
    assert policy.capability_capacity == 3
    assert policy.available_concurrency_reservations == 3
    assert policy.side_effect_class == "READ_ONLY"
    assert policy.parent_observation_limits == {
        "max_task_summaries": 3,
        "max_summary_bytes": 1024,
        "max_diagnostics": 8,
        "max_refs": 8,
        "max_observation_bytes": 8192,
    }


def test_stage_worker_rejects_same_ref_policy_with_changed_tool_boundary() -> None:
    policy = build_research_analysis_task_plan_policy()
    changed = replace(policy, allowed_tool_ids=("retrieval.untrusted",))

    with pytest.raises(HarnessValidationError) as exc_info:
        _test_stage_worker(policy=changed)

    assert exc_info.value.code == "research_task_plan_policy_mismatch"


def test_stage_worker_rejects_changed_research_publication_policy() -> None:
    terminal_policy = build_research_artifact_terminal_policy()
    definition = replace(
        build_dynamic_paper_analysis_graph_definition(),
        terminal_side_effect_policy=replace(
            terminal_policy,
            retry_limit=terminal_policy.retry_limit + 1,
        ),
        definition_checksum=None,
    )
    graph = HarnessGraphCompiler().compile(definition).graph

    with pytest.raises(HarnessValidationError) as exc_info:
        _test_stage_worker(
            stage_binding=TaskPlanStageBinding(
                graph,
                RESEARCH_DYNAMIC_STAGE_ID,
            )
        )

    assert exc_info.value.code == (
        "research_task_plan_publication_policy_required"
    )


def test_stage_worker_rejects_capacity_below_pinned_parallel_policy() -> None:
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
    )
    with pytest.raises(HarnessValidationError) as exc_info:
        _test_stage_worker(
            parallel_coordinator=coordinator,
            child_agent_supervisor=supervisor,
        )

    assert exc_info.value.code == "research_task_plan_parallel_capacity_invalid"


def test_capability_registry_requires_every_exact_subagent_binding() -> None:
    bindings = _worker_bindings()
    registry = build_research_analysis_capability_registry(bindings)
    policy = build_research_analysis_task_plan_policy()

    for capability in RESEARCH_DYNAMIC_CAPABILITIES:
        resolved = registry.resolve(capability, policy)
        assert resolved.worker_ref == RESEARCH_DYNAMIC_WORKER_REFS[capability]
        assert resolved.worker_contract_ref == (
            RESEARCH_DYNAMIC_WORKER_CONTRACT_REFS[capability]
        )
        assert resolved.subagent_spec is not None
        assert resolved.subagent_spec.metadata["gate_refs"] == list(
            RESEARCH_DYNAMIC_GATES_BY_CAPABILITY[capability]
        )

    missing = dict(bindings)
    missing.pop(RESEARCH_DYNAMIC_CAPABILITIES[0])
    with pytest.raises(HarnessValidationError) as exc_info:
        build_research_analysis_capability_registry(missing)
    assert exc_info.value.code == (
        "research_task_plan_capability_bindings_incomplete"
    )


def test_candidate_builder_accepts_only_outline_and_pins_control_fields() -> None:
    worker = _OutlineWorker(_valid_outline())
    policy = build_research_analysis_task_plan_policy()
    builder = ResearchAnalysisPlanCandidateBuilder(worker)
    request = replace(
        _plan_build_request(policy),
        metadata={
            "parent_raw_messages": ["controller-private"],
            "hidden_prompt": "controller-only",
        },
    )

    candidate = builder.build_candidate(request)

    assert worker.calls[0][0] == "candidate_task_plan"
    assert "controller-private" not in repr(worker.calls[0][1])
    assert "controller-only" not in repr(worker.calls[0][1])
    assert candidate.generated_by == "research.task-plan-builder@1"
    assert candidate.schema_version == GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA
    assert candidate.matches_stage_identity(request.stage_identity)
    assert not {"workflow_id", "workflow_ref"}.intersection(
        candidate.to_dict()
    )
    assert PlanCandidate.from_dict(candidate.to_dict()) == candidate
    assert set(candidate.required_output_roles) == set(policy.required_output_roles)
    for task in candidate.tasks:
        capability = task.worker_capability
        assert task.output_contract.output_role == (
            RESEARCH_DYNAMIC_OUTPUT_ROLES_BY_CAPABILITY[capability]
        )
        assert task.output_contract.schema_ref == (
            RESEARCH_DYNAMIC_OUTPUT_SCHEMA_REFS[capability]
        )
        assert task.acceptance_criteria.gate_refs == (
            RESEARCH_DYNAMIC_GATES_BY_CAPABILITY[capability]
        )
        assert task.requested_tools == RESEARCH_DYNAMIC_TOOL_IDS
        assert task.requested_memory_namespaces == ()
        assert task.retry_policy.max_attempts == policy.max_task_attempts

    forbidden = _valid_outline()
    forbidden["quality_passed"] = True
    with pytest.raises(HarnessValidationError) as exc_info:
        ResearchAnalysisPlanCandidateBuilder(_OutlineWorker(forbidden)).build_candidate(
            _plan_build_request(policy)
        )
    assert exc_info.value.code == "research_task_plan_builder_output_invalid"


def test_candidate_requires_both_isolated_research_input_refs() -> None:
    policy = build_research_analysis_task_plan_policy()
    outline = _valid_outline()
    outline["tasks"][0]["input_refs"] = ["document"]

    with pytest.raises(HarnessValidationError) as exc_info:
        ResearchAnalysisPlanCandidateBuilder(_OutlineWorker(outline)).build_candidate(
            _plan_build_request(policy)
        )

    assert exc_info.value.code == "research_task_plan_required_input_missing"
    assert exc_info.value.details == {"missing": ["evidence_pack"]}


def test_research_candidate_contract_rejects_dropped_tool_policy() -> None:
    policy = build_research_analysis_task_plan_policy()
    candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(_plan_build_request(policy))
    first = candidate.tasks[0]
    forged = replace(
        candidate,
        candidate_id="research-plan-with-dropped-tool-policy",
        tasks=(replace(first, requested_tools=()), *candidate.tasks[1:]),
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_research_analysis_candidate(forged)

    assert exc_info.value.code == "research_task_plan_candidate_contract_mismatch"
    assert exc_info.value.details["violations"] == [
        {"task_id": first.task_id, "reason": "tool_policy_mismatch"}
    ]


def test_research_candidate_rejects_capability_gate_substitution() -> None:
    policy = build_research_analysis_task_plan_policy()
    candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(_plan_build_request(policy))
    structure = next(
        task
        for task in candidate.tasks
        if task.worker_capability == "research.analysis.structure"
    )
    substituted = replace(
        structure,
        acceptance_criteria=TaskAcceptanceCriteria(
            ("BenchmarkEvidenceLineageGate@1",)
        ),
    )
    altered = replace(
        candidate,
        candidate_id="altered-research-analysis-plan",
        tasks=tuple(
            substituted if task.task_id == structure.task_id else task
            for task in candidate.tasks
        ),
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_research_analysis_candidate(altered)
    assert exc_info.value.code == "research_task_plan_candidate_contract_mismatch"


def test_graph_only_candidate_validates_to_graph_only_plan() -> None:
    policy = build_research_analysis_task_plan_policy()
    request = _plan_build_request(policy)
    candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(request)
    context = TaskPlanValidationContext(
        run_id=request.run_id,
        stage_binding=request.stage_binding,
        available_input_refs=tuple(request.context_refs.values()),
        registered_gate_refs=policy.allowed_gate_refs,
    )

    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        build_research_analysis_capability_registry(_worker_bindings()),
        context=context,
        accepted_at="2026-08-17T00:00:00Z",
    )
    payload = plan.to_dict()

    assert plan.schema_version == GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
    assert plan.matches_stage_identity(request.stage_identity)
    assert plan.stage_identity_checksum == candidate.stage_identity_checksum
    assert plan.stage_binding_checksum == request.stage_binding.binding_checksum
    assert not {"workflow_id", "workflow_ref"}.intersection(payload)
    assert ValidatedTaskPlan.from_dict(payload) == plan

    with pytest.raises(HarnessValidationError) as alias_error:
        ValidatedTaskPlan.from_dict(
            {**payload, "workflow_id": request.stage_identity.graph_id}
        )
    assert alias_error.value.code == "invalid_task_plan_payload_fields"

    with pytest.raises(HarnessValidationError) as checksum_error:
        ValidatedTaskPlan.from_dict(
            {**payload, "plan_checksum": "sha256:" + "0" * 64}
        )
    assert checksum_error.value.code == "task_plan_checksum_mismatch"

    dependent = next(task for task in plan.tasks if task.depends_on)
    patch = PlanPatch.for_plan(
        plan,
        patch_id="graph-only-plan-patch",
        reason_code="replan",
        source_candidate_ref="candidate://graph-only-plan-patch",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY,
                target_task_id=dependent.task_id,
                depends_on=dependent.depends_on,
            ),
        ),
    )
    store = InMemoryTaskPlanStore()
    assert store.append_candidate(candidate) == candidate.candidate_checksum
    assert store.accept_plan(plan) == plan.plan_checksum
    projection = store.load_projection(plan.run_id, plan.stage_id)
    next_plan = TaskPlanPatchValidator().apply(
        plan,
        patch,
        projection,
        policy,
        build_research_analysis_capability_registry(_worker_bindings()),
        accepted_at="2026-08-17T00:01:00Z",
        available_input_refs=tuple(request.context_refs.values()),
    )

    assert next_plan.schema_version == GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
    assert next_plan.matches_stage_identity(request.stage_identity)
    assert next_plan.stage_identity_checksum == plan.stage_identity_checksum

    events = store.read_events(candidate.run_id, candidate.stage_id)
    assert [event.event_type for event in events] == [
        "PLAN_CANDIDATE_BUILT",
        "PLAN_ACCEPTED",
    ]
    assert all(
        event.schema_version == TASK_PLAN_EVENT_SCHEMA_V3 for event in events
    )
    assert all(event.graph_id == request.stage_identity.graph_id for event in events)
    assert all(
        event.matches_contract_identity(candidate if index == 0 else plan)
        for index, event in enumerate(events)
    )
    assert all("workflow_id" not in event.to_dict() for event in events)
    assert all(TaskPlanEvent.from_dict(event.to_dict()) == event for event in events)


def test_graph_only_task_result_contract_is_strict_and_lifecycle_bound() -> None:
    policy = build_research_analysis_task_plan_policy()
    request = _plan_build_request(policy)
    candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(request)
    context = TaskPlanValidationContext(
        run_id=request.run_id,
        stage_binding=request.stage_binding,
        available_input_refs=tuple(request.context_refs.values()),
        registered_gate_refs=policy.allowed_gate_refs,
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        build_research_analysis_capability_registry(_worker_bindings()),
        context=context,
        accepted_at="2026-08-17T00:00:00Z",
    )
    definition = plan.tasks[0]
    instance = task_instance_for_attempt(plan, definition.task_id, 1)
    result_ref = f"result://{definition.task_id}"
    result = TaskResultRecord.for_plan(
        plan,
        task_id=definition.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=1,
        status=TaskLifecycle.SUCCEEDED,
        result_ref=result_ref,
        output_refs=(f"artifact://{definition.task_id}",),
        output_roles=(definition.output_role,),
        output_schema_ref=definition.task.output_contract.schema_ref,
        usage={"turns": 1},
        transcript_ref=f"transcript://{definition.task_id}",
        transcript_checksum="sha256:" + "1" * 64,
        subagent_output_ref=result_ref,
        subagent_output_checksum="sha256:" + "2" * 64,
    )
    payload = result.to_dict()

    assert result.schema_version == TASK_PLAN_RESULT_SCHEMA
    assert result.matches_plan_identity(plan)
    assert result.worker_ref == definition.worker_ref
    assert result.task_checksum == definition.task_definition_checksum
    assert result.binding_checksum == definition.binding_checksum
    assert result.graph_checksum == plan.graph_checksum
    assert not {"workflow_id", "workflow_ref"}.intersection(payload)
    assert TaskResultRecord.from_dict(payload) == result

    with pytest.raises(HarnessValidationError) as retired_schema:
        TaskResultRecord.from_dict(
            {
                **payload,
                "schema_version": "newsroom.harness-task-plan-result/v3",
            }
        )
    assert retired_schema.value.code == "task_plan_result_schema_unsupported"

    with pytest.raises(HarnessValidationError) as alias_error:
        TaskResultRecord.from_dict(
            {**payload, "workflow_id": request.stage_identity.graph_id}
        )
    assert alias_error.value.code == "invalid_task_plan_payload_fields"

    with pytest.raises(HarnessValidationError) as identity_error:
        TaskResultRecord.from_dict(
            {
                **payload,
                "stage_identity_checksum": "sha256:" + "0" * 64,
            }
        )
    assert identity_error.value.code == "task_plan_stage_identity_checksum_invalid"

    with pytest.raises(HarnessValidationError) as checksum_error:
        TaskResultRecord.from_dict(
            {**payload, "result_checksum": "sha256:" + "0" * 64}
        )
    assert checksum_error.value.code == "task_plan_checksum_mismatch"

    with pytest.raises(HarnessValidationError) as unknown_task_error:
        TaskResultRecord.for_plan(
            plan,
            task_id="outside-plan",
            task_instance_id="outside-plan-attempt-1",
            attempt=1,
            status=TaskLifecycle.FAILED,
            error_code="worker_failed",
        )
    assert unknown_task_error.value.code == "task_plan_unknown_task"

    other_request = _plan_build_request(policy, graph_version="2")
    other_candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(other_request)
    other_plan = TaskPlanValidator().accept(
        other_candidate,
        policy,
        build_research_analysis_capability_registry(_worker_bindings()),
        context=TaskPlanValidationContext(
            run_id=other_request.run_id,
            stage_binding=other_request.stage_binding,
            available_input_refs=tuple(other_request.context_refs.values()),
            registered_gate_refs=policy.allowed_gate_refs,
        ),
        accepted_at="2026-08-17T00:00:00Z",
    )
    assert not result.matches_plan_identity(other_plan)

    gate_owner = InMemoryTaskPlanGateArtifactWriter()
    receipt = SubAgentTranscriptReceipt(
        transcript_ref=result.transcript_ref,
        transcript_checksum=result.transcript_checksum,
        transcript_id="research-contract-transcript",
        invocation_id="research-contract-invocation",
        parent_run_id=plan.run_id,
        child_run_id="research-contract-child",
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        context_ref="subagent-context://research-contract",
        context_checksum="sha256:" + "3" * 64,
        output_ref=result.subagent_output_ref,
        output_checksum=result.subagent_output_checksum,
        storage_revision="research-contract-revision",
        committed_at=datetime(2026, 8, 17, tzinfo=UTC),
        identity_checksum="sha256:" + "4" * 64,
    )
    worker_result = HarnessWorkerResult(
        status="succeeded",
        artifacts=result.output_refs,
        metrics={"turns": 1},
        evidence=(
            HarnessWorkerEvidence(
                evidence_type="subagent_attempt",
                payload=receipt.to_dict(),
            ),
        ),
    )
    input_checksum = canonical_payload_checksum(
        {
            "instance": instance.checksum_projection(),
            "worker_result": worker_result.candidate_payload(),
        }
    )
    evidences = tuple(
        TaskPlanGateEvidence(
            gate_ref=gate_ref,
            input_checksum=input_checksum,
            result_checksum=canonical_payload_checksum(
                worker_result.candidate_payload()
            ),
            passed=True,
        )
        for gate_ref in definition.gate_refs
    )
    refs = gate_owner.persist_gate_verification(
        plan,
        instance,
        worker_result,
        evidences,
    )
    result = replace(
        result,
        verified_gate_refs=definition.gate_refs,
        gate_evidence_refs=refs.evidence_checksums,
    )
    store = InMemoryTaskPlanStore(gate_evidence_reader=gate_owner)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    scheduler = TaskPlanScheduler()
    projection = store.load_projection(plan.run_id, plan.stage_id)
    decision = TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,))
    projection = scheduler.reserve_ready_tasks(
        projection,
        decision,
    )
    sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
    readiness = LogicalTaskReadiness(
        task_id=instance.task_id,
        task_definition_checksum=instance.task_definition_checksum,
        logical_ready_order=decision.logical_ready_task_ids,
    )
    projection = replace(projection, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_READY",
        plan,
        task_id=instance.task_id,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
        payload={"logical_readiness": readiness.to_dict()},
    ), projection)

    before_admission = projection
    projection = scheduler.mark_admitted(
        before_admission,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            before_admission.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            projection.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    sequence += 1
    projection = replace(projection, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_QUEUE_ADMITTED",
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
        payload={"queue_admission": admission.to_dict()},
    ), projection)
    for event_type, transition in (
        (
            "TASK_DISPATCHED",
            lambda value: scheduler.mark_dispatched(value, instance),
        ),
        (
            "TASK_STARTED",
            lambda value: scheduler.mark_started(value, instance),
        ),
    ):
        projection = transition(projection)
        sequence += 1
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

    assert store.append_result(result) == result.result_checksum
    events = store.read_events(plan.run_id, plan.stage_id)
    assert [event.event_type for event in events[-6:]] == [
        "TASK_READY",
        "TASK_QUEUE_ADMITTED",
        "TASK_DISPATCHED",
        "TASK_STARTED",
        "TASK_RESULT_ACCEPTED",
        "TASK_COMPLETED",
    ]
    assert all(event.schema_version == TASK_PLAN_EVENT_SCHEMA_V3 for event in events)
    assert store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == (result,)


def test_graph_only_candidate_and_plan_readers_fail_closed() -> None:
    policy = build_research_analysis_task_plan_policy()
    request = _plan_build_request(policy)
    candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(request)
    candidate_payload = candidate.to_dict()

    with pytest.raises(HarnessValidationError) as alias_error:
        PlanCandidate.from_dict(
            {**candidate_payload, "workflow_id": request.stage_identity.graph_id}
        )
    assert alias_error.value.code == "invalid_task_plan_payload_fields"

    with pytest.raises(HarnessValidationError) as identity_error:
        PlanCandidate.from_dict(
            {
                **candidate_payload,
                "stage_identity_checksum": "sha256:" + "0" * 64,
            }
        )
    assert identity_error.value.code == (
        "task_plan_stage_identity_checksum_invalid"
    )

    with pytest.raises(HarnessValidationError) as checksum_error:
        PlanCandidate.from_dict(
            {**candidate_payload, "candidate_checksum": "sha256:" + "0" * 64}
        )
    assert checksum_error.value.code == "task_plan_checksum_mismatch"

    other_request = _plan_build_request(policy, graph_version="2")
    assert not candidate.matches_stage_identity(other_request.stage_identity)
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    event = store.read_events(candidate.run_id, candidate.stage_id)[0]
    event_payload = event.to_dict()
    with pytest.raises(HarnessValidationError) as event_alias_error:
        TaskPlanEvent.from_dict(
            {**event_payload, "workflow_id": request.stage_identity.graph_id}
        )
    assert event_alias_error.value.code == "invalid_task_plan_payload_fields"
    with pytest.raises(HarnessValidationError) as event_identity_error:
        TaskPlanEvent.from_dict(
            {
                **event_payload,
                "stage_identity_checksum": "sha256:" + "0" * 64,
            }
        )
    assert event_identity_error.value.code == (
        "task_plan_stage_identity_checksum_invalid"
    )
    other_candidate = ResearchAnalysisPlanCandidateBuilder(
        _OutlineWorker(_valid_outline())
    ).build_candidate(other_request)
    assert not event.matches_contract_identity(other_candidate)
    with pytest.raises(HarnessValidationError) as graph_error:
        TaskPlanStageRequest(
            run_id=other_request.run_id,
            stage_binding=other_request.stage_binding,
            context_refs=other_request.context_refs,
            policy=policy,
            candidate=candidate,
            accepted_at="2026-08-17T00:00:00Z",
        )
    assert graph_error.value.code == "task_plan_candidate_scope_mismatch"


def test_research_aggregator_produces_existing_analysis_branch_contract() -> None:
    policy = build_research_analysis_task_plan_policy()
    aggregator = build_research_analysis_task_plan_aggregator()

    aggregate = aggregator.aggregate(_accepted_results(), policy)

    assert aggregator.registry.refs == (RESEARCH_DYNAMIC_AGGREGATOR_REF,)
    assert aggregate.branch_refs == (
        {
            "role": "analysis.structure",
            "output_ref": "result://structure",
            "producer_node_id": "analyze_structure",
            "output_key": "structure_candidate",
        },
        {
            "role": "analysis.contribution",
            "output_ref": "result://contribution",
            "producer_node_id": "analyze_contribution",
            "output_key": "contribution_candidate",
        },
        {
            "role": "analysis.experiments",
            "output_ref": "result://experiments",
            "producer_node_id": "analyze_experiments",
            "output_key": "experiment_candidate",
        },
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        aggregator.aggregate(_accepted_results()[:-1], policy)
    assert exc_info.value.code == "task_plan_missing_required_role"


def _worker_bindings() -> dict[str, HarnessWorkerBinding]:
    return {
        capability: HarnessWorkerBinding(
            HarnessContractReference(
                HarnessContractKind.WORKER,
                capability,
                "1",
            ),
            HarnessWorkerType.SUBAGENT,
            _BoundResearchWorker(capability),
        )
        for capability in RESEARCH_DYNAMIC_CAPABILITIES
    }


def _test_stage_worker(
    *,
    policy=None,
    stage_binding: TaskPlanStageBinding | None = None,
    parallel_coordinator: ParallelAgentCoordinator | None = None,
    child_agent_supervisor: ChildAgentSupervisor | None = None,
) -> ResearchAnalysisTaskPlanStageWorker:
    actual_policy = policy or build_research_analysis_task_plan_policy()
    if stage_binding is None:
        graph = HarnessGraphCompiler().compile(
            build_dynamic_paper_analysis_graph_definition()
        ).graph
        stage_binding = TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID)
    return ResearchAnalysisTaskPlanStageWorker(
        stage_binding=stage_binding,
        accepted_at="2026-08-17T00:00:00Z",
        candidate_builder=ResearchAnalysisPlanCandidateBuilder(
            _OutlineWorker(_valid_outline())
        ),
        capability_registry=build_research_analysis_capability_registry(
            _worker_bindings()
        ),
        store=InMemoryTaskPlanStore(),
        worker_executor=lambda *_args: HarnessWorkerResult(status="succeeded"),
        result_verifier=TaskPlanResultVerifier(),
        policy=actual_policy,
        parallel_coordinator=parallel_coordinator,
        child_agent_supervisor=child_agent_supervisor,
        allow_test_store=True,
    )


def _plan_build_request(
    policy,
    *,
    graph_version: str | None = None,
) -> PlanBuildRequest:
    definition = build_dynamic_paper_analysis_graph_definition()
    if graph_version is not None:
        definition = replace(
            definition,
            graph_version=graph_version,
            definition_checksum=None,
        )
    graph = HarnessGraphCompiler().compile(definition).graph
    return PlanBuildRequest(
        run_id="research-run",
        stage_binding=TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID),
        context_refs={"document": "document", "evidence_pack": "evidence_pack"},
        policy=policy,
    )


def _valid_outline() -> dict[str, object]:
    tasks = []
    for index, capability in enumerate(RESEARCH_DYNAMIC_CAPABILITIES):
        tasks.append(
            {
                "task_id": f"analysis-task-{index + 1}",
                "objective": f"Produce {capability} from accepted evidence.",
                "worker_capability": capability,
                "input_refs": ["document", "evidence_pack"],
                "depends_on": [] if index < 2 else ["analysis-task-1"],
                "priority": 10 - index,
            }
        )
    return {"tasks": tasks, "requested_max_parallelism": 3}


def _accepted_results() -> tuple[TaskResultRecord, ...]:
    graph = HarnessGraphCompiler().compile(
        build_dynamic_paper_analysis_graph_definition()
    ).graph
    stage_binding = TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID)
    stage_identity = TaskPlanStageIdentity("research-run", stage_binding)
    results = []
    for capability in RESEARCH_DYNAMIC_CAPABILITIES:
        suffix = capability.rsplit(".", 1)[-1]
        results.append(
            TaskResultRecord(
                run_id="research-run",
                graph_checksum=stage_identity.graph_checksum,
                graph_id=stage_identity.graph_id,
                graph_version=stage_identity.graph_version,
                graph_ref=stage_identity.graph_ref,
                graph_schema_version=stage_identity.graph_schema_version,
                compiler_version=stage_identity.compiler_version,
                condition_policy_version=stage_identity.condition_policy_version,
                stage_binding_checksum=stage_identity.stage_binding_checksum,
                stage_identity_schema=stage_identity.schema_version,
                stage_identity_checksum=stage_identity.identity_checksum,
                stage_id="dynamic_analysis_stage",
                plan_id="research-plan",
                plan_version=1,
                task_id=f"task-{suffix}",
                task_instance_id=f"task-{suffix}-attempt-1",
                attempt=1,
                worker_ref=RESEARCH_DYNAMIC_WORKER_REFS[capability],
                task_checksum=canonical_payload_checksum(
                    {"task": suffix, "kind": "definition"}
                ),
                binding_checksum=canonical_payload_checksum(
                    {"task": suffix, "kind": "binding"}
                ),
                status=TaskLifecycle.SUCCEEDED,
                result_ref=f"result://{suffix}",
                output_roles=(
                    RESEARCH_DYNAMIC_OUTPUT_ROLES_BY_CAPABILITY[capability],
                ),
                output_schema_ref=RESEARCH_DYNAMIC_OUTPUT_SCHEMA_REFS[capability],
                usage=TaskBudget(max_turns=1).to_dict(),
            )
        )
    return tuple(results)
