from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionIdentityMismatchError,
    ExecutionMode,
    ExecutionProfile,
    ExecutionReceipt,
    ExecutionRequest,
    ExecutionStatus,
    FakeExecutionEnvironment,
    ResourceLimits,
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


def _profile(**kwargs: object) -> ExecutionProfile:
    values = {
        "provider_id": "test-provider",
        "allowed_argv_prefixes": (("python",),),
        "require_filesystem_isolation": False,
        "require_resource_limits": False,
    }
    values.update(kwargs)
    return ExecutionProfile.sandboxed_process(**values)


def _request(profile: ExecutionProfile | None = None, **kwargs: object) -> ExecutionRequest:
    values = {
        "execution_id": "execution-1",
        "tool_id": "tool.example@1.0.0",
        "graph_identity": _identity(),
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "profile": profile or _profile(),
        "image": "python:3.12",
        "argv": ("python", "-c", "print(1)"),
    }
    values.update(kwargs)
    return ExecutionRequest(**values)


def test_trusted_profile_is_explicit_and_has_no_physical_capabilities() -> None:
    profile = ExecutionProfile.trusted_in_process()
    assert profile.mode is ExecutionMode.TRUSTED_IN_PROCESS
    assert profile.provider_id is None


@pytest.mark.parametrize("root", ["relative", "C:relative", r"C:\\tmp\\..\\escape", r"\\\\server\\share"])
def test_request_rejects_noncanonical_or_traversal_roots(root: str) -> None:
    profile = _profile(require_filesystem_isolation=True)
    with pytest.raises(ValueError):
        _request(profile, read_roots=(root,))


@pytest.mark.parametrize("name", ["PATH", "PYTHONPATH", "LD_PRELOAD", "TEMP"])
def test_request_rejects_protected_environment_overrides(name: str) -> None:
    with pytest.raises(ValueError, match="protected environment"):
        _request(environment={name: "attacker-controlled"})


def test_registry_fails_closed_when_provider_cannot_enforce_network_allowlist() -> None:
    profile = _profile(
        network_policy={"mode": "allowlist", "allowlist": [{"host": "api.example", "port": 443}]}
    )
    request = _request(profile)
    provider = FakeExecutionEnvironment(
        ExecutionCapabilityProfile(
            provider_id="test-provider",
            available=True,
            enforces_network_deny=True,
            isolates_environment=True,
            enforces_argv_policy=True,
            controls_process_tree=True,
            confirms_termination=True,
        ),
        lambda _: pytest.fail("provider must not be invoked"),
    )
    registry = ExecutionEnvironmentRegistry()
    registry.register(provider)
    with pytest.raises(ExecutionEnvironmentUnavailableError) as exc_info:
        registry.execute(request)
    assert exc_info.value.reason_code == "execution_environment_unavailable"
    assert "network_allowlist" in exc_info.value.details["missing"]


def test_registry_rejects_receipt_identity_mismatch() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    def mismatched(_: ExecutionRequest) -> object:
        now = datetime.now(UTC)
        receipt = ExecutionReceipt(
            execution_id="other-execution",
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="test-provider",
            provider_capability_checksum=capabilities.checksum,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            finished_at=now + timedelta(milliseconds=1),
            termination_confirmed=True,
            reason_code="process_exit",
        )
        from framework.execution_environment.models import ExecutionOutcome

        return ExecutionOutcome(receipt=receipt)

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, mismatched))
    with pytest.raises(ExecutionIdentityMismatchError):
        registry.execute(request)


def test_registry_rejects_receipt_capability_checksum_mismatch() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    def mismatched(_: ExecutionRequest) -> object:
        now = datetime.now(UTC)
        from framework.execution_environment.models import ExecutionOutcome

        receipt = ExecutionReceipt(
            execution_id=request.execution_id,
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="test-provider",
            provider_capability_checksum="sha256:" + "c" * 64,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            finished_at=now + timedelta(milliseconds=1),
            termination_confirmed=True,
            reason_code="process_exit",
        )
        return ExecutionOutcome(receipt=receipt)

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, mismatched))
    with pytest.raises(ExecutionIdentityMismatchError):
        registry.execute(request)


def test_registry_rejects_receipt_output_checksum_mismatch() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    def mismatched(_: ExecutionRequest) -> object:
        now = datetime.now(UTC)
        from framework.execution_environment.models import ExecutionOutcome

        receipt = ExecutionReceipt(
            execution_id=request.execution_id,
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="test-provider",
            provider_capability_checksum=capabilities.checksum,
            status=ExecutionStatus.SUCCEEDED,
            started_at=now,
            finished_at=now + timedelta(milliseconds=1),
            termination_confirmed=True,
            reason_code="process_exit",
            output_checksum="sha256:" + "f" * 64,
            output_bytes=4,
        )
        return ExecutionOutcome(receipt=receipt, output="safe")

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, mismatched))
    with pytest.raises(ExecutionIdentityMismatchError):
        registry.execute(request)


def test_receipt_distinguishes_indeterminate_from_confirmed_cancellation() -> None:
    now = datetime.now(UTC)
    common = {
        "execution_id": "execution-1",
        "tool_id": "tool.example@1.0.0",
        "graph_identity": _identity(),
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "provider_id": "test-provider",
        "provider_capability_checksum": "sha256:" + "b" * 64,
        "started_at": now,
        "finished_at": now + timedelta(milliseconds=1),
        "reason_code": "termination_unconfirmed",
    }
    receipt = ExecutionReceipt(status=ExecutionStatus.INDETERMINATE, termination_confirmed=False, **common)
    assert receipt.status is ExecutionStatus.INDETERMINATE
    with pytest.raises(ValueError):
        ExecutionReceipt(status=ExecutionStatus.CANCELLED, termination_confirmed=False, **common)


def test_terminal_receipt_requires_termination_confirmation_and_has_checksum() -> None:
    now = datetime.now(UTC)
    common = {
        "execution_id": "execution-1",
        "tool_id": "tool.example@1.0.0",
        "graph_identity": _identity(),
        "operation_id": "operation-1",
        "attempt_id": "attempt-1",
        "provider_id": "test-provider",
        "provider_capability_checksum": "sha256:" + "b" * 64,
        "started_at": now,
        "finished_at": now + timedelta(milliseconds=1),
        "reason_code": "process_exit",
    }
    with pytest.raises(ValueError):
        ExecutionReceipt(status=ExecutionStatus.SUCCEEDED, termination_confirmed=False, **common)
    receipt = ExecutionReceipt(status=ExecutionStatus.SUCCEEDED, termination_confirmed=True, **common)
    assert receipt.receipt_checksum.startswith("sha256:")
    assert receipt.to_operator_projection()["receipt_checksum"] == receipt.receipt_checksum


def test_registry_rejects_inconsistent_empty_output_receipt() -> None:
    request = _request()
    capabilities = ExecutionCapabilityProfile(
        provider_id="test-provider",
        available=True,
        enforces_network_deny=True,
        isolates_environment=True,
        enforces_argv_policy=True,
        controls_process_tree=True,
        confirms_termination=True,
    )

    def mismatched(_: ExecutionRequest) -> object:
        now = datetime.now(UTC)
        from framework.execution_environment.models import ExecutionOutcome

        return ExecutionOutcome(
            receipt=ExecutionReceipt(
                execution_id=request.execution_id,
                tool_id=request.tool_id,
                graph_identity=request.graph_identity,
                operation_id=request.operation_id,
                attempt_id=request.attempt_id,
                provider_id="test-provider",
                provider_capability_checksum=capabilities.checksum,
                status=ExecutionStatus.SUCCEEDED,
                started_at=now,
                finished_at=now + timedelta(milliseconds=1),
                termination_confirmed=True,
                reason_code="process_exit",
                output_checksum="sha256:" + "f" * 64,
                output_bytes=4,
            ),
            output="",
        )

    registry = ExecutionEnvironmentRegistry()
    registry.register(FakeExecutionEnvironment(capabilities, mismatched))
    with pytest.raises(ExecutionIdentityMismatchError):
        registry.execute(request)


def test_docker_provider_rejects_symlinked_declared_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = object.__new__(DockerExecutionEnvironment)
    request = _request(_profile(require_filesystem_isolation=True), read_roots=(str(link),))
    with pytest.raises(Exception) as exc_info:
        provider._canonical_mounts(request)
    assert getattr(exc_info.value, "reason_code", None) == "filesystem_root_invalid"


def test_docker_provider_rejects_nested_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = object.__new__(DockerExecutionEnvironment)
    request = _request(_profile(require_filesystem_isolation=True), read_roots=(str(root),))
    with pytest.raises(Exception) as exc_info:
        provider._canonical_mounts(request)
    assert getattr(exc_info.value, "reason_code", None) == "filesystem_root_invalid"


def test_docker_provider_probe_fails_closed_when_executable_is_missing() -> None:
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = DockerExecutionEnvironment(
        docker_executable="__newsroom_missing_docker_executable__",
    )
    assert provider.capabilities.available is False


def test_docker_launch_uncertainty_returns_indeterminate_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = object.__new__(DockerExecutionEnvironment)
    provider._docker = "docker"
    provider._probe_timeout_seconds = 0.1
    provider._available = True
    request = _request()
    monkeypatch.setattr(provider, "_canonical_mounts", lambda _request: ([], {}, None))
    monkeypatch.setattr(provider, "_build_run_command", lambda *_args, **_kwargs: ["docker", "run"])
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ExecutionEnvironmentUnavailableError("daemon connection lost")
        ),
    )

    outcome = provider.execute(request)

    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.termination_confirmed is False
    assert outcome.receipt.reason_code == "termination_unconfirmed"
    assert "reconciliation" in (outcome.diagnostic or "")


def test_docker_wait_process_failure_returns_indeterminate_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    from infrastructure.execution_environment import docker as docker_module
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment
    import subprocess

    provider = object.__new__(DockerExecutionEnvironment)
    provider._docker = "docker"
    provider._probe_timeout_seconds = 0.1
    provider._available = True
    request = _request()
    monkeypatch.setattr(provider, "_canonical_mounts", lambda _request: ([], {}, None))
    monkeypatch.setattr(provider, "_build_run_command", lambda *_args, **_kwargs: ["docker", "run"])
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["docker", "run"],
            0,
            stdout=b"container-id",
            stderr=b"",
        ),
    )

    def fail_wait(*_args: object, **_kwargs: object) -> object:
        raise OSError("wait executable unavailable")

    monkeypatch.setattr(docker_module.subprocess, "Popen", fail_wait)
    outcome = provider.execute(request)

    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.termination_confirmed is False
    assert outcome.receipt.reason_code == "termination_unconfirmed"


def test_docker_command_uses_valid_mount_and_env_argv_contract(tmp_path: Path) -> None:
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment

    provider = object.__new__(DockerExecutionEnvironment)
    provider._docker = "docker"
    request = _request(
        _profile(allowed_argv_prefixes=(("cp",),)),
        read_roots=(str(tmp_path),),
        environment={"QUALIFICATION_VALUE": "admitted"},
        argv=("cp", "input", "output"),
    )
    mounts, path_map, working_directory = provider._canonical_mounts(request)
    command = provider._build_run_command(
        request,
        mounts=mounts,
        path_map=path_map,
        working_directory=working_directory,
    )

    mount_values = [value for index, value in enumerate(command) if command[index - 1] == "--mount"]
    assert mount_values
    assert all(",rw" not in value for value in mount_values)
    assert "QUALIFICATION_VALUE=admitted" in command
    assert command[-3:] == ["cp", "input", "output"]


def test_docker_collects_logs_without_unsupported_stream_selector_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infrastructure.execution_environment import docker as docker_module
    from infrastructure.execution_environment.docker import DockerExecutionEnvironment
    import subprocess

    provider = object.__new__(DockerExecutionEnvironment)
    provider._docker = "docker"
    provider._probe_timeout_seconds = 1.0
    provider._available = True
    request = _request()
    monkeypatch.setattr(provider, "_canonical_mounts", lambda _request: ([], {}, None))
    monkeypatch.setattr(provider, "_build_run_command", lambda *_args, **_kwargs: ["docker", "run"])
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1] == "run":
            return subprocess.CompletedProcess(command, 0, stdout=b"container", stderr=b"")
        if command[1] == "logs":
            return subprocess.CompletedProcess(command, 0, stdout=b"output", stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    class _Wait:
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"0", b""

    monkeypatch.setattr(provider, "_run", run)
    monkeypatch.setattr(docker_module.subprocess, "Popen", lambda *_args, **_kwargs: _Wait())

    outcome = provider.execute(request)

    assert outcome.receipt.status is ExecutionStatus.SUCCEEDED
    assert outcome.output == b"output"
    logs_command = next(command for command in commands if command[1] == "logs")
    assert logs_command == ["docker", "logs", provider._container_name(request.execution_id)]
