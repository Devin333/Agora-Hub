from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from framework.events import (
    ENVELOPE_SCHEMA_V2,
    EventRuntime,
    EventSchemaCatalog,
    TransactionalStateSnapshot,
    thaw_canonical_json,
)
from framework.events.runtime import RUNTIME_EVENT_SCHEMA_V1
from framework.governance.budget import BUDGET_EVENT_SCHEMA_VERSION, BUDGET_SCHEMA_VERSION
from framework.execution_environment import (
    EXECUTION_PROFILE_SCHEMA,
    EXECUTION_RECEIPT_SCHEMA,
    ExecutionProfile,
    ExecutionReceipt,
    ExecutionRequest,
    ExecutionStatus,
)
from framework.harness.artifacts import (
    GRAPH_TERMINAL_MANIFEST_SCHEMA,
    GRAPH_TERMINAL_MANIFEST_V2_SCHEMA,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.runtime.tool_result_adapter import (
    HARNESS_BOUND_TOOL_RECEIPT_SCHEMA,
    TOOL_SIDE_EFFECT_EVIDENCE_SCHEMA,
)
from framework.harness.side_effects import (
    SIDE_EFFECT_DECISION_SCHEMA_VERSION,
    SIDE_EFFECT_INTENT_SCHEMA_VERSION,
    SIDE_EFFECT_OUTCOME_SCHEMA_VERSION,
)
from framework.harness.subagents.supervisor import (
    ChildAgentHandle,
    ChildAgentSpawnRequest,
    ChildAgentState,
    ChildAgentSupervisor,
    ChildAgentSupervisorError,
    ChildAgentTerminalReceipt,
)
from framework.harness.subagents.supervisor_store import (
    CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
    CHILD_AGENT_LIFECYCLE_STATE_SCHEMA,
    DurableChildAgentEventLog,
)
from framework.harness.subagents.transcript import (
    SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3,
    SUBAGENT_CONTEXT_SCHEMA_V3,
    SUBAGENT_OUTPUT_SCHEMA_V3,
)
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.runtime_contract import (
    HARNESS_RUNTIME_CONTRACT_VERSION,
    runtime_contract_binding,
    validate_history_read_contract,
    validate_parallel_dispatch_contract,
    validate_task_result_owner_contract,
)
from framework.harness.task_plan.parallel import PARENT_OBSERVATION_SCHEMA, ParallelAgentCoordinator
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.store import TASK_PLAN_RESULT_SCHEMA_V3, TaskPlanEvent, TaskResultRecord
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.models.result_envelope import (
    TOOL_RESULT_ENVELOPE_SCHEMA,
    TOOL_SIDE_EFFECT_RECEIPT_SCHEMA,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
)


def test_runtime_binding_is_derived_from_existing_owner_schemas() -> None:
    binding = runtime_contract_binding()
    assert binding.schema_version == HARNESS_RUNTIME_CONTRACT_VERSION
    assert binding.schema_version == "newsroom.harness-runtime-contract/v2"
    assert binding.owners["execution_profile"] == EXECUTION_PROFILE_SCHEMA
    assert binding.owners["validated_task_plan"] == _accepted_parallel_plan().schema_version
    assert binding.owners["child_agent_handle"] == ChildAgentHandle.CONTRACT_SCHEMA_VERSION
    assert binding.owners["child_lifecycle_state"] == CHILD_AGENT_LIFECYCLE_STATE_SCHEMA
    assert "child_agent_terminal_receipt" not in binding.owners
    assert binding.projection_owners["child_agent_terminal_receipt"] == "child_lifecycle_state"
    assert binding.owners["execution_receipt"] == EXECUTION_RECEIPT_SCHEMA
    assert binding.owners["event_envelope"] == ENVELOPE_SCHEMA_V2
    assert binding.owners["runtime_event_data"] == RUNTIME_EVENT_SCHEMA_V1
    assert binding.owners["budget_policy"] == BUDGET_SCHEMA_VERSION
    assert binding.owners["budget_snapshot"] == BUDGET_SCHEMA_VERSION
    assert binding.owners["budget_event"] == BUDGET_EVENT_SCHEMA_VERSION
    assert binding.owners["subagent_attempt_identity"] == SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3
    assert binding.owners["subagent_context"] == SUBAGENT_CONTEXT_SCHEMA_V3
    assert binding.owners["subagent_output"] == SUBAGENT_OUTPUT_SCHEMA_V3
    assert binding.owners["task_result"] == TASK_PLAN_RESULT_SCHEMA_V3
    assert binding.owners["parent_observation"] == PARENT_OBSERVATION_SCHEMA
    assert binding.owners["graph_terminal"] == GRAPH_TERMINAL_MANIFEST_SCHEMA
    assert binding.owners["artifact_manifest"] == GRAPH_TERMINAL_MANIFEST_V2_SCHEMA
    assert binding.owners["side_effect_intent"] == SIDE_EFFECT_INTENT_SCHEMA_VERSION
    assert binding.owners["side_effect_decision"] == SIDE_EFFECT_DECISION_SCHEMA_VERSION
    assert binding.owners["side_effect_outcome"] == SIDE_EFFECT_OUTCOME_SCHEMA_VERSION
    assert binding.owners["tool_result_envelope"] == TOOL_RESULT_ENVELOPE_SCHEMA
    assert binding.owners["tool_side_effect_receipt"] == TOOL_SIDE_EFFECT_RECEIPT_SCHEMA
    assert binding.owners["harness_bound_tool_receipt"] == HARNESS_BOUND_TOOL_RECEIPT_SCHEMA
    assert binding.owners["tool_side_effect_evidence"] == TOOL_SIDE_EFFECT_EVIDENCE_SCHEMA
    with pytest.raises(TypeError):
        binding.owners["execution_profile"] = "newsroom.invalid/v1"  # type: ignore[index]
    with pytest.raises(TypeError):
        binding.projection_owners["child_agent_terminal_receipt"] = "invalid"  # type: ignore[index]


def test_child_lifecycle_reader_rejects_snapshot_from_another_schema(tmp_path) -> None:
    database = tmp_path / "runtime-contract-child-lifecycle.sqlite3"
    store = SQLiteEventStore(database)
    runtime = EventRuntime(
        store=store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    state_key = "binding-test"
    log = DurableChildAgentEventLog(
        state_runtime=runtime,
        state_reader=store,
        state_key=state_key,
    )
    log.acquire_owner("owner-1")
    snapshot = store.load_transactional_state(
        CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
        state_key,
    )
    assert snapshot is not None
    payload = thaw_canonical_json(snapshot.payload)
    payload["schema_version"] = "newsroom.harness-child-lifecycle-state/v999"
    corrupt = TransactionalStateSnapshot.create(
        namespace=snapshot.namespace,
        key=snapshot.key,
        revision=snapshot.revision + 1,
        payload=payload,
    )
    runtime.compare_and_swap_transactional_state(
        corrupt,
        expected_revision=snapshot.revision,
        expected_checksum=snapshot.checksum,
    )

    with pytest.raises(ChildAgentSupervisorError) as exc_info:
        log.read_events()

    assert exc_info.value.code == "child_event_store_corrupt"


@pytest.mark.parametrize(
    ("field_name", "owner_key"),
    [
        ("RUNTIME_EVENT_DATA_SCHEMA", "runtime_event_data"),
        ("BUDGET_EVENT_DATA_SCHEMA", "budget_event"),
    ],
)
def test_runtime_binding_rejects_canonical_event_registry_drift(
    monkeypatch: pytest.MonkeyPatch,
    field_name: str,
    owner_key: str,
) -> None:
    from framework.events.schema import catalog

    monkeypatch.setattr(catalog, field_name, f"newsroom.invalid-{owner_key}/v1")

    with pytest.raises(HarnessValidationError) as exc_info:
        runtime_contract_binding()

    assert exc_info.value.code == "RUNTIME_CONTRACT_OWNER_DRIFT"


def test_execution_profile_requires_the_versioned_contract() -> None:
    profile = ExecutionProfile.trusted_in_process()
    assert profile.to_dict()["schema_version"] == EXECUTION_PROFILE_SCHEMA
    with pytest.raises(ValueError, match="schema_version"):
        ExecutionProfile.from_dict({key: value for key, value in profile.to_dict().items() if key != "schema_version"})
    with pytest.raises(ValueError, match="schema_version"):
        ExecutionProfile.from_dict({**profile.to_dict(), "schema_version": "newsroom.execution-profile/v2"})


def _receipt_identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="receipt-run",
        graph_id="receipt-graph",
        graph_version="1.0.0",
        graph_ref="receipt-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="receipt-node",
        node_instance_id="receipt-node-1",
        activity_id="receipt-activity",
        attempt=1,
    )


def _execution_receipt() -> ExecutionReceipt:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="receipt-provider",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    request = ExecutionRequest(
        execution_id="receipt-execution",
        tool_id="tool.receipt@1.0.0",
        graph_identity=_receipt_identity(),
        operation_id="receipt-operation",
        attempt_id="receipt-attempt",
        profile=profile,
        image="python:3.12",
        argv=("python", "-c", "print(1)"),
    )
    completed_at = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)
    receipt = ExecutionReceipt(
        execution_id=request.execution_id,
        tool_id=request.tool_id,
        graph_identity=request.graph_identity,
        operation_id=request.operation_id,
        attempt_id=request.attempt_id,
        provider_id="receipt-provider",
        provider_capability_checksum="sha256:" + "b" * 64,
        status=ExecutionStatus.SUCCEEDED,
        started_at=completed_at,
        finished_at=completed_at,
        termination_confirmed=True,
        reason_code="process_exit",
        exit_code=0,
    )
    return receipt


def _child_terminal_receipt() -> ChildAgentTerminalReceipt:
    supervisor = ChildAgentSupervisor()
    handle = supervisor.spawn(
        ChildAgentSpawnRequest(
            parent_graph_identity=_receipt_identity(),
            stage_id="receipt-stage",
            task_id="receipt-task",
            task_instance_id="receipt-task-1",
            attempt=1,
            allowed_tools=("tool.read",),
            allowed_memory_namespaces=("research",),
            budget={"turns": 1},
            operation_id="receipt-child-operation",
        )
    )
    receipt = ChildAgentTerminalReceipt(
        child_id=handle.child_id,
        operation_id=handle.operation_id,
        parent_graph_identity=handle.parent_graph_identity,
        status=ChildAgentState.SUCCEEDED,
        reason_code="worker_completed",
        result_ref="result://receipt-child",
        result_checksum="sha256:" + "c" * 64,
        termination_confirmed=True,
        completed_at=datetime(2026, 9, 29, 8, 1, tzinfo=UTC),
    )
    return receipt


def test_execution_receipt_reader_accepts_canonical_receipt() -> None:
    receipt = _execution_receipt()

    assert ExecutionReceipt.from_dict(receipt.to_dict()) == receipt


@pytest.mark.parametrize("mutation", ["missing", "unknown", "schema", "checksum"])
def test_execution_receipt_reader_rejects_noncanonical_payload(mutation: str) -> None:
    receipt = _execution_receipt()
    payload = receipt.to_dict()
    if mutation == "missing":
        payload.pop("operation_id")
    elif mutation == "unknown":
        payload["unexpected"] = True
    elif mutation == "schema":
        payload["schema_version"] = "newsroom.execution-receipt/v999"
    else:
        payload["receipt_checksum"] = "sha256:" + "f" * 64

    with pytest.raises(ValueError):
        ExecutionReceipt.from_dict(payload)


def test_child_terminal_receipt_reader_accepts_nested_projection() -> None:
    receipt = _child_terminal_receipt()

    assert ChildAgentTerminalReceipt.from_dict(receipt.to_dict()) == receipt


@pytest.mark.parametrize("mutation", ["missing", "unknown", "checksum"])
def test_child_terminal_receipt_reader_rejects_noncanonical_payload(mutation: str) -> None:
    receipt = _child_terminal_receipt()
    payload = receipt.to_dict()
    if mutation == "missing":
        payload.pop("operation_id")
    elif mutation == "unknown":
        payload["unexpected"] = True
    else:
        payload["receipt_checksum"] = "sha256:" + "f" * 64

    with pytest.raises(ValueError):
        ChildAgentTerminalReceipt.from_dict(payload)


def test_dispatch_contract_rejects_tampered_plan_checksum_before_admission() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    instance = request.task_instances[0]
    object.__setattr__(instance, "plan_checksum", "sha256:" + "f" * 64)
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"


def test_dispatch_contract_rejects_duck_typed_request() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    forged = SimpleNamespace(**{
        field: getattr(request, field)
        for field in request.__dataclass_fields__
    })
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(forged)
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


def test_dispatch_contract_rejects_duck_typed_task_instance() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    instance = request.task_instances[0]
    forged = SimpleNamespace(**{
        field: getattr(instance, field)
        for field in instance.__dataclass_fields__
    })
    object.__setattr__(request, "task_instances", (forged,))
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


def test_dispatch_contract_rejects_task_instance_schema_drift() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    object.__setattr__(request.task_instances[0], "schema_version", "newsroom.invalid/v1")
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


def test_dispatch_contract_rejects_task_instance_scope_drift() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    object.__setattr__(request.task_instances[0], "run_id", "another-run")
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCOPE_MISMATCH"


def test_dispatch_contract_rejects_forged_task_reference() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    instance = request.task_instances[0]
    object.__setattr__(instance, "task_id", "unknown-task")
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_REFERENCE_MISMATCH"


def test_dispatch_contract_rejects_forged_capability_binding() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    instance = request.task_instances[0]
    object.__setattr__(instance, "worker_ref", "forged-worker@1")
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_CAPABILITY_MISMATCH"


def test_dispatch_contract_rejects_forged_instance_checksum() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    object.__setattr__(request.task_instances[0], "instance_checksum", "sha256:" + "f" * 64)
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request)
    assert exc_info.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"


def test_dispatch_contract_rejects_group_from_another_request_policy() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    coordinator = ParallelAgentCoordinator(max_workers=1, allow_test_executor=True)
    group = coordinator.create_group(request, check_capacity=False)
    object.__setattr__(request, "max_waves", request.max_waves + 1)
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request, group)
    assert exc_info.value.code == "RUNTIME_CONTRACT_POLICY_MISMATCH"


def test_dispatch_contract_rejects_forged_group_identity() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    coordinator = ParallelAgentCoordinator(max_workers=1, allow_test_executor=True)
    group = coordinator.create_group(request, check_capacity=False)
    object.__setattr__(group, "group_id", "dg_" + "f" * 32)
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request, group)
    assert exc_info.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"


def test_dispatch_contract_rejects_group_before_admission_transition() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    coordinator = ParallelAgentCoordinator(max_workers=1, allow_test_executor=True)
    group = replace(coordinator._group_definition(request), state="PLANNED")
    with pytest.raises(HarnessValidationError) as exc_info:
        validate_parallel_dispatch_contract(request, group)
    assert exc_info.value.code == "RUNTIME_CONTRACT_TRANSITION_INVALID"


def test_history_contract_rejects_unknown_event_schema_before_replay() -> None:
    with pytest.raises(HarnessValidationError, match="unsupported event"):
        validate_history_read_contract((), (type("Event", (), {"schema_version": "newsroom.invalid/v1"})(),), ())


def test_history_contract_rejects_unknown_task_result_schema_before_replay() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    result = _history_result(plan)
    object.__setattr__(result, "schema_version", "newsroom.invalid/v1")

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract((plan,), (), (result,))

    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


def _history_result(plan):
    task = plan.tasks[0]
    instance = task_instance_for_attempt(plan, task.task_id, 1)
    return TaskResultRecord.for_plan(
        plan,
        task_id=task.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=1,
        status="succeeded",
        result_ref=f"result://{task.task_id}",
        output_refs=(f"artifact://{task.task_id}",),
        output_roles=(task.output_role,),
        output_schema_ref=task.task.output_contract.schema_ref,
        verified_gate_refs=task.gate_refs,
        gate_evidence_refs=(f"evidence://{task.task_id}",),
    )


def test_history_contract_accepts_canonical_plan_event_and_result() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    event = TaskPlanEvent.for_plan(
        "TASK_RESULT_ACCEPTED",
        plan,
        sequence=1,
        task_id=plan.tasks[0].task_id,
        payload={"result_checksum": _history_result(plan).result_checksum},
    )

    validate_history_read_contract((plan,), (event,), (_history_result(plan),))


def test_task_result_owner_contract_accepts_canonical_result() -> None:
    validate_task_result_owner_contract(_history_result(_accepted_parallel_plan(("task-1",))))


def test_task_result_owner_contract_rejects_tampered_checksum() -> None:
    result = _history_result(_accepted_parallel_plan(("task-1",)))
    object.__setattr__(result, "result_checksum", "sha256:" + "f" * 64)

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_task_result_owner_contract(result)

    assert exc_info.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"


def test_task_result_owner_contract_rejects_duck_typed_result() -> None:
    result = _history_result(_accepted_parallel_plan(("task-1",)))
    forged = SimpleNamespace(**{
        field: getattr(result, field) for field in result.__dataclass_fields__
    })

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_task_result_owner_contract(forged)

    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


def test_history_contract_accepts_planless_event_with_matching_scope() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    payload = TaskPlanEvent.for_plan(
        "PLAN_BUILD_INTENT",
        plan,
        sequence=1,
        payload={},
    ).to_dict()
    payload["plan_id"] = None
    payload["plan_version"] = None
    payload.pop("event_checksum")
    event = TaskPlanEvent(**payload)

    validate_history_read_contract((plan,), (event,), ())


@pytest.mark.parametrize("kind", ["plan", "event", "result"])
def test_history_contract_rejects_duck_typed_owner(kind: str) -> None:
    plan = _accepted_parallel_plan(("task-1",))
    result = _history_result(plan)
    event = TaskPlanEvent.for_plan("PLAN_ACCEPTED", plan, sequence=1)
    value = {"plan": plan, "event": event, "result": result}[kind]
    forged = SimpleNamespace(**{
        field: getattr(value, field) for field in value.__dataclass_fields__
    })
    arguments = {
        "plan": ((forged,), (), ()),
        "event": ((plan,), (forged,), ()),
        "result": ((plan,), (), (forged,)),
    }[kind]

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract(*arguments)
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCHEMA_MISMATCH"


@pytest.mark.parametrize("kind", ["plan", "event", "result"])
def test_history_contract_rejects_tampered_owner_checksum(kind: str) -> None:
    plan = _accepted_parallel_plan(("task-1",))
    result = _history_result(plan)
    event = TaskPlanEvent.for_plan("PLAN_ACCEPTED", plan, sequence=1)
    value = {"plan": plan, "event": event, "result": result}[kind]
    checksum_field = {
        "plan": "plan_checksum",
        "event": "event_checksum",
        "result": "result_checksum",
    }[kind]
    object.__setattr__(value, checksum_field, "sha256:" + "f" * 64)
    arguments = {
        "plan": ((plan,), (), ()),
        "event": ((plan,), (event,), ()),
        "result": ((plan,), (), (result,)),
    }[kind]

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract(*arguments)
    assert exc_info.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"


def test_history_contract_rejects_event_from_unknown_plan() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    event = TaskPlanEvent.for_plan("PLAN_ACCEPTED", plan, sequence=1)
    object.__setattr__(event, "plan_id", "unknown-plan")
    object.__setattr__(
        event,
        "event_checksum",
        TaskPlanEvent(**{
            key: value
            for key, value in event.to_dict().items()
            if key != "event_checksum"
        }).event_checksum,
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract((plan,), (event,), ())
    assert exc_info.value.code == "RUNTIME_CONTRACT_REFERENCE_MISMATCH"


def test_history_contract_accepts_event_from_future_plan_version_with_matching_scope() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    event = TaskPlanEvent.for_plan("PLAN_ACCEPTED", plan, sequence=1)
    future_event = replace(
        event,
        plan_id="replacement-plan",
        plan_version=plan.version + 1,
    )

    validate_history_read_contract((plan,), (future_event,), ())


def test_history_contract_rejects_future_plan_version_from_another_scope() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    event = TaskPlanEvent.for_plan("PLAN_ACCEPTED", plan, sequence=1)
    other_stage_id = "other-stage"
    other_stage_identity_checksum = canonical_payload_checksum(
        {
            "schema_version": plan.stage_identity_schema,
            "run_id": plan.run_id,
            "graph_schema_version": plan.graph_schema_version,
            "compiler_version": plan.compiler_version,
            "condition_policy_version": plan.condition_policy_version,
            "graph_id": plan.graph_id,
            "graph_version": plan.graph_version,
            "graph_checksum": plan.graph_checksum,
            "stage_id": other_stage_id,
            "stage_binding_checksum": plan.stage_binding_checksum,
            "graph_ref": plan.graph_ref,
        }
    )
    future_event = replace(
        event,
        plan_id="replacement-plan",
        plan_version=plan.version + 1,
        stage_id=other_stage_id,
        stage_identity_checksum=other_stage_identity_checksum,
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract((plan,), (future_event,), ())
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCOPE_MISMATCH"


def test_history_contract_rejects_result_scope_drift() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    other_stage_id = "other-stage"
    other_stage_identity_checksum = canonical_payload_checksum(
        {
            "schema_version": plan.stage_identity_schema,
            "run_id": plan.run_id,
            "graph_schema_version": plan.graph_schema_version,
            "compiler_version": plan.compiler_version,
            "condition_policy_version": plan.condition_policy_version,
            "graph_id": plan.graph_id,
            "graph_version": plan.graph_version,
            "graph_checksum": plan.graph_checksum,
            "stage_id": other_stage_id,
            "stage_binding_checksum": plan.stage_binding_checksum,
            "graph_ref": plan.graph_ref,
        }
    )
    other_plan = replace(
        plan,
        stage_id=other_stage_id,
        stage_identity_checksum=other_stage_identity_checksum,
    )
    result = _history_result(other_plan)

    with pytest.raises(HarnessValidationError) as exc_info:
        validate_history_read_contract((plan,), (), (result,))
    assert exc_info.value.code == "RUNTIME_CONTRACT_SCOPE_MISMATCH"
