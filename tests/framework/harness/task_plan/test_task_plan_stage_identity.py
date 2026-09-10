from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.graphs import (
    RESEARCH_DYNAMIC_INPUT_REFS,
    RESEARCH_DYNAMIC_OUTPUT_ROLES,
    RESEARCH_DYNAMIC_POLICY_REF,
    RESEARCH_DYNAMIC_STAGE_ID,
    build_dynamic_paper_analysis_graph_definition,
    build_research_analysis_task_plan_policy,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.task_plan import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    FakePlanCandidateBuilder,
    GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,
    InMemoryTaskPlanStore,
    TASK_PLAN_EVENT_SCHEMA_V3,
    PlanBuildRequest,
    TaskPlanContractKind,
    TaskCapabilityRegistry,
    TaskPlanStageBinding,
    TaskPlanStageIdentity,
    TaskPlanStageRequest,
    TaskPlanStageRunner,
    TaskPlanValidationContext,
)


def _graph_only_binding(*, graph_version: str | None = None) -> TaskPlanStageBinding:
    definition = build_dynamic_paper_analysis_graph_definition()
    if graph_version is not None:
        definition = replace(
            definition,
            graph_version=graph_version,
            definition_checksum=None,
        )
    graph = HarnessGraphCompiler().compile(definition).graph
    return TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID)


def _request(binding: TaskPlanStageBinding) -> PlanBuildRequest:
    return PlanBuildRequest(
        run_id="research-run",
        stage_binding=binding,
        context_refs={name: name for name in RESEARCH_DYNAMIC_INPUT_REFS},
        policy=build_research_analysis_task_plan_policy(),
    )


def test_stage_identity_registry_uses_v2_writer_and_keeps_v1_read_only() -> None:
    registration = next(
        item
        for item in DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.registrations
        if item.contract_kind is TaskPlanContractKind.STAGE_IDENTITY
    )

    assert registration.writer_schema == GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA
    assert registration.readable_schemas == (GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,)
    assert registration.executable_schemas == (
        GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA,
    )


def test_graph_task_plan_registries_expose_only_v2_contracts() -> None:
    registrations = {
        item.contract_kind: item
        for item in DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.registrations
    }

    candidate = registrations[TaskPlanContractKind.PLAN_CANDIDATE]
    assert candidate.writer_schema == GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA
    assert candidate.readable_schemas == (GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,)
    assert candidate.executable_schemas == (GRAPH_ONLY_PLAN_CANDIDATE_SCHEMA,)

    plan = registrations[TaskPlanContractKind.VALIDATED_PLAN]
    assert plan.writer_schema == GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
    assert plan.readable_schemas == (GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,)
    assert plan.executable_schemas == (GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA,)

    expected_runtime_contracts = {
        TaskPlanContractKind.TASK_INSTANCE: GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
        TaskPlanContractKind.TASK_PROJECTION: GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
        TaskPlanContractKind.PLAN_PROJECTION: GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    }
    for contract_kind, graph_schema in expected_runtime_contracts.items():
        registration = registrations[contract_kind]
        assert registration.writer_schema == graph_schema
        assert registration.readable_schemas == (graph_schema,)
        assert registration.executable_schemas == (graph_schema,)


def test_graph_only_stage_identity_and_plan_request_have_no_workflow_alias() -> None:
    binding = _graph_only_binding()
    identity = TaskPlanStageIdentity("research-run", binding)
    request = _request(binding)
    identity_payload = identity.to_dict()
    request_payload = request.to_dict()

    assert identity.schema_version == GRAPH_ONLY_TASK_PLAN_STAGE_IDENTITY_SCHEMA
    assert identity_payload["graph_ref"] == binding.graph.graph_ref.exact_ref
    assert identity_payload["graph_id"] == binding.graph_id
    assert identity_payload["graph_version"] == binding.graph_version
    assert identity_payload["stage_binding_checksum"] == binding.binding_checksum
    assert not {"workflow_id", "workflow_ref"}.intersection(identity_payload)
    assert request_payload["graph_ref"] == identity.graph_ref
    assert request_payload["stage_identity_checksum"] == identity.identity_checksum
    assert request_payload["condition_policy_version"] == (
        binding.graph.condition_policy_version
    )
    assert not {"workflow_id", "workflow_ref"}.intersection(request_payload)
    assert TaskPlanStageIdentity.from_dict(
        identity_payload,
        stage_binding=binding,
    ) == identity

    assert not hasattr(identity, "workflow_id")


def test_graph_only_contexts_share_the_frozen_stage_identity() -> None:
    binding = _graph_only_binding()
    policy = build_research_analysis_task_plan_policy()
    validation_context = TaskPlanValidationContext(
        run_id="research-run",
        stage_binding=binding,
        available_input_refs=RESEARCH_DYNAMIC_INPUT_REFS,
    )
    stage_request = TaskPlanStageRequest(
        run_id="research-run",
        stage_binding=binding,
        context_refs={name: name for name in RESEARCH_DYNAMIC_INPUT_REFS},
        policy=policy,
        accepted_at="2026-08-17T00:00:00Z",
    )

    assert validation_context.stage_identity == stage_request.stage_identity
    assert validation_context.graph_id == binding.graph_id
    assert validation_context.graph_version == binding.graph_version
    assert validation_context.graph_ref == binding.graph.graph_ref.exact_ref
    assert stage_request.graph_ref == validation_context.graph_ref

    for context in (validation_context, stage_request):
        assert not hasattr(context, "workflow_id")


def test_graph_only_preplan_failure_records_v3_halt_without_workflow_alias() -> None:
    binding = _graph_only_binding()
    policy = build_research_analysis_task_plan_policy()
    store = InMemoryTaskPlanStore()
    request = TaskPlanStageRequest(
        run_id="research-run",
        stage_binding=binding,
        context_refs={name: name for name in RESEARCH_DYNAMIC_INPUT_REFS},
        policy=policy,
        accepted_at="2026-08-17T00:00:00Z",
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(
            HarnessValidationError("planner unavailable", code="planner_unavailable")
        ),
        capability_registry=TaskCapabilityRegistry(),
        store=store,
    )

    result = runner.run(request)
    events = store.read_events(request.run_id, request.stage_id)

    assert result.status.value == "blocked"
    assert result.diagnostics["reason_code"] == "planner_unavailable"
    assert [event.event_type for event in events] == [
        "PLAN_BUILD_INTENT", "PLAN_BUILD_RECEIPT", "TASK_PLAN_HALTED",
    ]
    assert events[0].payload["build_attempt"]["status"] == "STARTED"
    assert events[1].payload["build_attempt"]["status"] == "FAILED"
    assert events[1].payload["build_attempt"]["retryable"] is False
    assert events[0].payload["build_attempt"]["request_checksum"] == events[1].payload["build_attempt"]["request_checksum"]
    for event in events:
        assert event.schema_version == TASK_PLAN_EVENT_SCHEMA_V3
        assert event.is_graph_only is True
        assert not hasattr(event, "workflow_id")
        assert "workflow_id" not in event.to_dict()


def test_graph_only_stage_identity_rejects_alias_tamper_and_cross_graph_restore() -> None:
    first_binding = _graph_only_binding()
    second_binding = _graph_only_binding(graph_version="2")
    identity = TaskPlanStageIdentity("research-run", first_binding)
    payload = identity.to_dict()

    with pytest.raises(HarnessValidationError) as alias_error:
        TaskPlanStageIdentity.from_dict(
            {**payload, "workflow_id": first_binding.graph_id},
            stage_binding=first_binding,
        )
    assert alias_error.value.code == "task_plan_stage_identity_projection_invalid"

    with pytest.raises(HarnessValidationError) as checksum_error:
        TaskPlanStageIdentity.from_dict(
            {**payload, "identity_checksum": "sha256:" + "0" * 64},
            stage_binding=first_binding,
        )
    assert checksum_error.value.code == "task_plan_stage_identity_checksum_invalid"

    with pytest.raises(HarnessValidationError) as graph_error:
        TaskPlanStageIdentity.from_dict(
            payload,
            stage_binding=second_binding,
        )
    assert graph_error.value.code == "task_plan_stage_identity_checksum_invalid"


def test_stage_identity_rejects_schema_binding_mixing() -> None:
    binding = _graph_only_binding()

    with pytest.raises(HarnessValidationError) as error:
        TaskPlanStageIdentity(
            "research-run",
            binding,
            schema_version="newsroom.harness-task-plan-stage-identity/v1",
        )

    assert error.value.code == "unsupported_task_plan_schema"

    with pytest.raises(HarnessValidationError) as empty_error:
        TaskPlanStageIdentity(
            "research-run",
            binding,
            schema_version="",
        )

    assert empty_error.value.code == "unsupported_task_plan_schema"
