from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionRequest,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionProfile,
    ExecutionReceipt,
    ExecutionStatus,
    FakeExecutionEnvironment,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolCall, ToolDefinition, ToolExecutor, ToolPolicy, ToolRegistry, ToolStatus
from framework.tool.governance.secrets import MappingSecretProvider


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


def test_sandboxed_definition_never_falls_back_to_in_process_executor() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    definition = ToolDefinition(
        name="sample.sandboxed",
        input_schema={},
        metadata={
            "execution_profile": profile.to_dict(),
            "execution": {
                "image": "python:3.12",
                "argv": ["python", "-c", "print(1)"],
                "cancellation_grace_seconds": 0,
            },
        },
    )
    called: list[bool] = []
    captured_requests: list[ExecutionRequest] = []
    registry = ToolRegistry()
    registry.register(definition, lambda _: called.append(True))
    capabilities = ExecutionCapabilityProfile(
        provider_id="fake",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        supports_cancellation=True,
        confirms_termination=True,
    )

    def run(request):
        captured_requests.append(request)
        now = datetime.now(UTC)
        output = '{"sandbox": true}'
        receipt = ExecutionReceipt(
            execution_id=request.execution_id,
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="fake",
            provider_capability_checksum=capabilities.checksum,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            finished_at=now + timedelta(milliseconds=1),
            termination_confirmed=True,
            reason_code="process_exit",
            output_checksum="sha256:" + sha256(output.encode("utf-8")).hexdigest(),
            output_bytes=len(output.encode("utf-8")),
        )
        return ExecutionOutcome(receipt=receipt, output=output)

    environment = ExecutionEnvironmentRegistry()
    provider = FakeExecutionEnvironment(capabilities, run)
    environment.register(provider)
    observation = ToolExecutor(
        registry,
        execution_environment=environment,
        graph_identity=_identity(),
    ).execute(
        ToolCall(tool_name="sample.sandboxed", arguments={}, graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.sandboxed"]),
    )
    assert observation.status is ToolStatus.SUCCEEDED
    assert observation.result.output == {"sandbox": True}
    assert called == []
    assert captured_requests[0].cancellation_grace_seconds == 0.0
    assert len(provider.cancellation_signals) == 1
    assert provider.cancellation_signals[0] is not None


def test_sandboxed_definition_without_environment_fails_closed() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="missing",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.sandboxed",
            metadata={"execution_profile": profile.to_dict(), "execution": {"image": "python", "argv": ["python"]}},
        ),
        lambda _: {"unsafe": True},
    )
    observation = ToolExecutor(registry, graph_identity=_identity()).execute(
        ToolCall(tool_name="sample.sandboxed", graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.sandboxed"]),
    )
    assert observation.status is ToolStatus.FAILED
    assert observation.result.metadata["resolved_tool_id"] == "sample.sandboxed@1.0.0"
    assert observation.result.error_type == "execution_environment_unavailable"
    assert observation.result.metadata["reason_code"] == "execution_environment_unavailable"
    assert observation.result.metadata["execution_error_type"] == "ExecutionEnvironmentUnavailableError"
    assert observation.result.metadata["details"]["missing"] == ["execution_environment"]


def test_sandboxed_definition_rejects_duck_typed_environment_bypass() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    definition = ToolDefinition(
        name="sample.duck-typed-environment",
        metadata={
            "execution_profile": profile.to_dict(),
            "execution": {"image": "python:3.12", "argv": ["python"]},
        },
    )
    called: list[bool] = []

    class _BypassEnvironment:
        def execute(self, _request):
            called.append(True)
            raise AssertionError("capability admission must run before execute")

    registry = ToolRegistry()
    registry.register(definition, lambda _arguments: {"unsafe": True})
    observation = ToolExecutor(
        registry,
        execution_environment=_BypassEnvironment(),
        graph_identity=_identity(),
    ).execute(
        ToolCall(
            tool_name="sample.duck-typed-environment",
            graph_identity=_identity(),
        ),
        ToolPolicy(allowed_tools=["sample.duck-typed-environment"]),
    )

    assert observation.status is ToolStatus.FAILED
    assert observation.result.metadata["reason_code"] == (
        "execution_environment_unavailable"
    )
    assert observation.result.metadata["details"]["missing"] == [
        "execution_environment_registry"
    ]
    assert called == []


def test_invalid_sandbox_execution_profile_is_a_typed_denial() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.invalid-profile",
            metadata={
                "execution_profile": {"mode": "sandboxed_process", "unknown": True},
            },
        ),
        lambda _: {"unsafe": True},
    )

    observation = ToolExecutor(registry, graph_identity=_identity()).execute(
        ToolCall(tool_name="sample.invalid-profile", graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.invalid-profile"]),
    )

    assert observation.status is ToolStatus.FAILED
    assert observation.result.error_type == "runtime_profile_denied"
    assert observation.result.metadata["reason_code"] == "runtime_profile_denied"
    assert observation.result.metadata["execution_error_type"] == "RuntimeCompositionProfileError"
    assert observation.result.metadata["details"] == {
        "tool_id": "sample.invalid-profile@1.0.0",
        "profile_error_type": "ValueError",
    }


def test_sandboxed_definition_without_graph_identity_is_a_typed_denial() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.missing-identity",
            metadata={
                "execution_profile": profile.to_dict(),
                "execution": {"image": "python", "argv": ["python"]},
            },
        ),
        lambda _: {"unsafe": True},
    )

    observation = ToolExecutor(
        registry,
        execution_environment=ExecutionEnvironmentRegistry(),
    ).execute(
        ToolCall(tool_name="sample.missing-identity"),
        ToolPolicy(allowed_tools=["sample.missing-identity"]),
    )

    assert observation.status is ToolStatus.FAILED
    assert observation.result.error_type == "execution_identity_mismatch"
    assert observation.result.metadata["reason_code"] == "execution_identity_mismatch"
    assert observation.result.metadata["execution_error_type"] == "ExecutionIdentityMismatchError"
    assert observation.result.metadata["details"] == {
        "tool_id": "sample.missing-identity@1.0.0",
        "missing": ["graph_identity"],
    }


def test_invalid_sandbox_execution_spec_is_a_typed_denial() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.invalid-spec",
            metadata={
                "execution_profile": profile.to_dict(),
                "execution": None,
            },
        ),
        lambda _: {"unsafe": True},
    )

    observation = ToolExecutor(
        registry,
        execution_environment=ExecutionEnvironmentRegistry(),
        graph_identity=_identity(),
    ).execute(
        ToolCall(tool_name="sample.invalid-spec", graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.invalid-spec"]),
    )

    assert observation.status is ToolStatus.FAILED
    assert observation.result.error_type == "execution_policy_violation"
    assert observation.result.metadata["reason_code"] == "execution_policy_violation"
    assert observation.result.metadata["execution_error_type"] == "ExecutionPolicyViolationError"
    assert observation.result.metadata["details"] == {
        "tool_id": "sample.invalid-spec@1.0.0",
        "violation": "sandbox_execution_spec_invalid",
        "missing": ["execution"],
    }


def test_sandbox_never_places_secret_values_in_argv() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    definition = ToolDefinition(
        name="sample.secret-sandbox",
        input_schema={},
        required_secret_names=["API_TOKEN"],
        metadata={
            "execution_profile": profile.to_dict(),
            "execution": {
                "image": "python:3.12",
                "argv": ["python", "{arguments}"],
                "secret_handles": ["API_TOKEN"],
            },
        },
    )
    registry = ToolRegistry()
    registry.register(definition, lambda _: {"unsafe": True})
    observation = ToolExecutor(
        registry,
        secret_provider=MappingSecretProvider({"API_TOKEN": "super-secret"}),
        execution_environment=ExecutionEnvironmentRegistry(),
        graph_identity=_identity(),
    ).execute(
        ToolCall(tool_name="sample.secret-sandbox", arguments={"value": "ok"}, graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.secret-sandbox"]),
    )
    assert observation.status is ToolStatus.FAILED
    assert "raw arguments" in (observation.result.error_message or "")


def test_tool_executor_rejects_provider_output_checksum_tampering() -> None:
    profile = ExecutionProfile.sandboxed_process(
        provider_id="fake",
        allowed_argv_prefixes=(("python",),),
        require_filesystem_isolation=False,
        require_resource_limits=False,
    )
    definition = ToolDefinition(
        name="sample.tampered-output",
        metadata={
            "execution_profile": profile.to_dict(),
            "execution": {"image": "python", "argv": ["python"]},
        },
    )
    capabilities = ExecutionCapabilityProfile(
        provider_id="fake",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    def run(request):
        now = datetime.now(UTC)
        return ExecutionOutcome(
            receipt=ExecutionReceipt(
                execution_id=request.execution_id,
                tool_id=request.tool_id,
                graph_identity=request.graph_identity,
                operation_id=request.operation_id,
                attempt_id=request.attempt_id,
                provider_id="fake",
                provider_capability_checksum=capabilities.checksum,
                status=ExecutionStatus.SUCCEEDED,
                started_at=now,
                finished_at=now + timedelta(milliseconds=1),
                termination_confirmed=True,
                reason_code="process_exit",
                output_checksum="sha256:" + "f" * 64,
                output_bytes=4,
            ),
            output="safe",
        )

    environment = ExecutionEnvironmentRegistry()
    environment.register(FakeExecutionEnvironment(capabilities, run))
    tool_registry = ToolRegistry()
    tool_registry.register(definition, lambda _: {"unsafe": True})
    executor = ToolExecutor(
        registry=tool_registry,
        execution_environment=environment,
        graph_identity=_identity(),
    )
    result = executor.execute(
        ToolCall(tool_name="sample.tampered-output", graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.tampered-output"]),
    )
    assert result.status is ToolStatus.FAILED


def test_production_mode_can_require_explicit_execution_profile() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="sample.unclassified"), lambda _: {"ok": True})
    observation = ToolExecutor(
        registry,
        graph_identity=_identity(),
        require_explicit_execution_profile=True,
    ).execute(
        ToolCall(tool_name="sample.unclassified", graph_identity=_identity()),
        ToolPolicy(allowed_tools=["sample.unclassified"]),
    )
    assert observation.status is ToolStatus.FAILED
    assert "explicitly declare" in (observation.result.error_message or "")
