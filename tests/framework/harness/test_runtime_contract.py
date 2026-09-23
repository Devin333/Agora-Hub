from __future__ import annotations

from dataclasses import replace

import pytest

from framework.execution_environment import EXECUTION_PROFILE_SCHEMA, ExecutionProfile
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.runtime_contract import (
    HARNESS_RUNTIME_CONTRACT_VERSION,
    runtime_contract_binding,
    validate_history_read_contract,
    validate_parallel_dispatch_contract,
)
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


def test_history_contract_rejects_unknown_event_schema_before_replay() -> None:
    with pytest.raises(HarnessValidationError, match="unsupported event"):
        validate_history_read_contract((), (type("Event", (), {"schema_version": "newsroom.invalid/v1"})(),), ())
