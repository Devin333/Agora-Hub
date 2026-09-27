from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.execution_environment import EXECUTION_PROFILE_SCHEMA, ExecutionProfile
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.runtime_contract import (
    HARNESS_RUNTIME_CONTRACT_VERSION,
    runtime_contract_binding,
    validate_history_read_contract,
    validate_parallel_dispatch_contract,
)
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.store import TaskPlanEvent, TaskResultRecord
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
)


def test_runtime_binding_is_derived_from_existing_owner_schemas() -> None:
    binding = runtime_contract_binding()
    assert binding.schema_version == HARNESS_RUNTIME_CONTRACT_VERSION
    assert binding.owners["execution_profile"] == EXECUTION_PROFILE_SCHEMA
    assert binding.owners["validated_task_plan"] == _accepted_parallel_plan().schema_version
    with pytest.raises(TypeError):
        binding.owners["execution_profile"] = "newsroom.invalid/v1"  # type: ignore[index]


def test_execution_profile_requires_the_versioned_contract() -> None:
    profile = ExecutionProfile.trusted_in_process()
    assert profile.to_dict()["schema_version"] == EXECUTION_PROFILE_SCHEMA
    with pytest.raises(ValueError, match="schema_version"):
        ExecutionProfile.from_dict({key: value for key, value in profile.to_dict().items() if key != "schema_version"})
    with pytest.raises(ValueError, match="schema_version"):
        ExecutionProfile.from_dict({**profile.to_dict(), "schema_version": "newsroom.execution-profile/v2"})


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
