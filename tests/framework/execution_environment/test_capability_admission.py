from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from framework.execution_environment import (
    CAPABILITY_DENIAL_CODE_VERSION,
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionRequest,
    FakeExecutionEnvironment,
    ResourceLimits,
    capability_denial_code,
)
from framework.shared.graph_identity import GraphExecutionIdentity


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-1",
        graph_id="graph",
        graph_version="1.0.0",
        graph_ref="graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="node",
        node_instance_id="node-1",
        activity_id="activity",
        attempt=1,
    )


def _request() -> ExecutionRequest:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="test-provider",
        allowed_argv_prefixes=(("python",),),
        network_policy={
            "mode": "allowlist",
            "allowlist": [{"host": "api.example", "port": 443}],
        },
        require_filesystem_isolation=False,
        require_resource_limits=True,
    )
    return ExecutionRequest(
        execution_id="execution-1",
        tool_id="tool.example@1.0.0",
        graph_identity=_identity(),
        operation_id="operation-1",
        attempt_id="attempt-1",
        profile=profile,
        image="python:3.12",
        argv=("python", "-c", "print(1)"),
        secret_handles=("vault/key",),
        resource_limits=ResourceLimits(max_memory_bytes=1 << 20),
    )


def test_capability_diagnostics_expose_stable_codes_without_request_material() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["status"] == "rejected"
    assert diagnostics["denial_code_version"] == CAPABILITY_DENIAL_CODE_VERSION
    assert diagnostics["provider_capability_checksum"] == capabilities.checksum
    assert diagnostics["missing"] == [
        "network_allowlist",
        "memory_limits",
        "secret_handle_injection",
    ]
    assert diagnostics["denials"] == [
        {
            "capability": "network_allowlist",
            "denial_code": "execution_network_policy_unsupported",
        },
        {
            "capability": "memory_limits",
            "denial_code": "execution_resource_limits_unsupported",
        },
        {
            "capability": "secret_handle_injection",
            "denial_code": "execution_secret_handles_unsupported",
        },
    ]
    assert "api.example" not in str(diagnostics)
    assert "vault/key" not in str(diagnostics)


def test_registry_preserves_reason_code_and_adds_specific_single_denial_code() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_resource_limits=True,
        enforces_memory_limits=True,
        confirms_termination=True,
        supports_secret_handles=True,
    )
    registry = ExecutionEnvironmentRegistry()
    registry.register(
        FakeExecutionEnvironment(capabilities, lambda _: pytest.fail("must not execute"))
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        registry.resolve(request)

    assert raised.value.reason_code == "execution_environment_unavailable"
    assert raised.value.details["denial_code"] == "execution_network_policy_unsupported"
    assert raised.value.details["denial_code_version"] == CAPABILITY_DENIAL_CODE_VERSION
    assert raised.value.details["denials"] == [
        {
            "capability": "network_allowlist",
            "denial_code": "execution_network_policy_unsupported",
        }
    ]


def test_registry_reports_unregistered_provider_as_structured_denial() -> None:
    request = _request()
    registry = ExecutionEnvironmentRegistry()

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        registry.resolve(request)

    assert raised.value.reason_code == "execution_environment_unavailable"
    assert raised.value.details["denial_code"] == "execution_provider_unavailable"
    assert raised.value.details["missing"] == ["provider"]
    assert raised.value.details["denials"] == [
        {
            "capability": "provider_unavailable",
            "denial_code": "execution_provider_unavailable",
        }
    ]


def test_registry_capability_lookup_reports_typed_unregistered_provider_denial() -> None:
    registry = ExecutionEnvironmentRegistry()

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        registry.resolve_capabilities("missing-provider")

    assert raised.value.reason_code == "execution_environment_unavailable"
    assert raised.value.details == {
        "provider_id": "missing-provider",
        "missing": ["provider"],
        "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
        "denial_code": "execution_provider_unavailable",
        "denials": [
            {
                "capability": "provider_unavailable",
                "denial_code": "execution_provider_unavailable",
            }
        ],
    }


def test_docker_direct_execute_uses_typed_capability_denial_without_request_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = object.__new__(DockerExecutionEnvironment)
    provider._available = False
    request = _request()
    monkeypatch.setattr(
        provider,
        "_canonical_mounts",
        lambda _request: pytest.fail("rejected capability request must not execute"),
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        provider.execute(request)

    details = raised.value.details
    assert details["provider_id"] == "docker"
    assert details["provider_capability_version"] == "docker-v2"
    assert details["provider_capability_checksum"] == provider.capabilities.checksum
    assert details["denial_code_version"] == CAPABILITY_DENIAL_CODE_VERSION
    assert details["denial_code"] == "execution_provider_unavailable"
    assert details["missing"]
    assert details["denials"]
    assert "api.example" not in str(details)
    assert "vault/key" not in str(details)


def test_unavailable_provider_keeps_provider_code_primary() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=False,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )
    registry = ExecutionEnvironmentRegistry()
    registry.register(
        FakeExecutionEnvironment(capabilities, lambda _: pytest.fail("must not execute"))
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        registry.resolve(request)

    assert raised.value.details["denial_code"] == "execution_provider_unavailable"
    assert raised.value.details["denials"][0] == {
        "capability": "provider_unavailable",
        "denial_code": "execution_provider_unavailable",
    }


def test_resource_capability_checks_requested_dimensions_independently() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_resource_limits=True,
        confirms_termination=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["missing"] == ["network_allowlist", "memory_limits", "secret_handle_injection"]
    assert {
        item["denial_code"] for item in diagnostics["denials"]
    } == {
        "execution_network_policy_unsupported",
        "execution_resource_limits_unsupported",
        "execution_secret_handles_unsupported",
    }


def test_declared_filesystem_roots_require_provider_enforcement(tmp_path: Path) -> None:
    request = replace(_request(), read_roots=(str(tmp_path),))
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_allowlist=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_memory_limits=True,
        confirms_termination=True,
        supports_secret_handles=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["missing"] == ["filesystem_roots"]
    assert diagnostics["denial_code"] == "execution_filesystem_isolation_unsupported"


def test_explicit_environment_requires_provider_isolation() -> None:
    request = replace(_request(), environment={"QUALIFICATION_VALUE": "admitted"})
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_allowlist=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_memory_limits=True,
        confirms_termination=True,
        supports_secret_handles=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["missing"] == ["environment_isolation"]
    assert diagnostics["denial_code"] == "execution_environment_isolation_unsupported"


def test_process_policy_and_explicit_process_limit_require_enforcement() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="test-provider",
        allowed_argv_prefixes=(("python",),),
        allowed_child_argv_prefixes=(("python",),),
        max_processes=2,
        require_child_process_allowlist=True,
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    request = ExecutionRequest(
        execution_id="execution-1",
        tool_id="tool.example@1.0.0",
        graph_identity=_identity(),
        operation_id="operation-1",
        attempt_id="attempt-1",
        profile=profile,
        image="python:3.12",
        argv=("python", "-c", "print(1)"),
        resource_limits=ResourceLimits(max_processes=1),
    )
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_child_process_allowlist=True,
        confirms_termination=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["missing"] == ["process_limits"]
    assert diagnostics["denial_code"] == "execution_resource_limits_unsupported"


def test_unknown_capability_uses_versioned_generic_code() -> None:
    assert capability_denial_code("future_capability") == "execution_capability_unsupported"


def test_timeout_admission_requires_timeout_and_cancellation_capabilities() -> None:
    request = replace(_request(), timeout_seconds=10.0)
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    diagnostics = capabilities.admission_diagnostics(request)

    assert diagnostics["missing"] == [
        "network_allowlist",
        "memory_limits",
        "timeout",
        "cancellation",
        "secret_handle_injection",
    ]
    assert diagnostics["denials"][2:4] == [
        {"capability": "timeout", "denial_code": "execution_timeout_unsupported"},
        {"capability": "cancellation", "denial_code": "execution_cancellation_unsupported"},
    ]
    assert diagnostics["denial_code"] == "execution_capability_admission_denied"
    assert capabilities.to_dict()["enforces_timeout"] is False
    assert capabilities.to_dict()["supports_cancellation"] is False


def test_timeout_admission_does_not_require_capabilities_without_timeout() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_allowlist=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        enforces_memory_limits=True,
        confirms_termination=True,
        supports_secret_handles=True,
    )

    assert capabilities.missing_for(request) == ()
