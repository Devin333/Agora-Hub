from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.execution_environment import EXECUTION_PROFILE_SCHEMA, ExecutionProfile
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.runtime_contract import (
    HARNESS_RUNTIME_CONTRACT_VERSION,
    runtime_contract_binding,
    validate_history_read_contract,
    validate_parallel_dispatch_contract,
)
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
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
