from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import subprocess
from threading import Event

import pytest

from framework.execution_environment import (
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionStatus,
    ResourceLimits,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.execution_environment import docker as docker_module
from infrastructure.execution_environment.docker import DockerExecutionEnvironment


def _request() -> ExecutionRequest:
    return ExecutionRequest(
        execution_id="cleanup-qualification",
        tool_id="qualification.docker@1.0.0",
        graph_identity=GraphExecutionIdentity(
            run_id="docker-cleanup-run",
            graph_id="docker-cleanup",
            graph_version="1.0.0",
            graph_ref="docker-cleanup@1.0.0",
            graph_checksum="sha256:" + "c" * 64,
            node_id="execute",
            node_instance_id="execute-1",
            activity_id="docker-provider",
            attempt=1,
        ),
        operation_id="operation-cleanup-qualification",
        attempt_id="attempt-cleanup-qualification",
        profile=ExecutionProfile.sandboxed_process(
            provider_id="docker",
            allowed_argv_prefixes=(("true",),),
            require_filesystem_isolation=False,
            require_resource_limits=False,
        ),
        image="qualification-image",
        argv=("true",),
    )


class _CompletedWait:
    returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        return b"0", b""


def _provider(monkeypatch: pytest.MonkeyPatch) -> DockerExecutionEnvironment:
    provider = object.__new__(DockerExecutionEnvironment)
    provider._docker = "docker"
    provider._probe_timeout_seconds = 1.0
    provider._available = True
    monkeypatch.setattr(provider, "_canonical_mounts", lambda _request: ([], {}, None))
    monkeypatch.setattr(
        provider,
        "_build_run_command",
        lambda *_args, **_kwargs: ["docker", "run"],
    )
    monkeypatch.setattr(
        docker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _CompletedWait(),
    )
    return provider


def test_cleanup_nonzero_exit_returns_indeterminate_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1 if command[1] == "rm" else 0,
            stdout=b"" if command[1] != "logs" else b"completed",
            stderr=b"cleanup failed" if command[1] == "rm" else b"",
        )

    monkeypatch.setattr(provider, "_run", run)

    outcome = provider.execute(_request())

    container_name = provider._container_name("cleanup-qualification")
    assert ["docker", "rm", "-f", "-v", container_name] in commands
    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.termination_confirmed is False
    assert outcome.receipt.reason_code == "termination_unconfirmed"
    assert outcome.diagnostic == "container cleanup could not be confirmed"


def test_cleanup_command_exception_returns_indeterminate_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1] == "rm":
            raise ExecutionEnvironmentUnavailableError("Docker cleanup unavailable")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"" if command[1] != "logs" else b"completed",
            stderr=b"",
        )

    monkeypatch.setattr(provider, "_run", run)

    outcome = provider.execute(_request())

    container_name = provider._container_name("cleanup-qualification")
    assert ["docker", "rm", "-f", "-v", container_name] in commands
    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.termination_confirmed is False
    assert outcome.receipt.reason_code == "termination_unconfirmed"
    assert outcome.diagnostic == "container cleanup could not be confirmed"


def test_active_cancellation_stops_container_and_quarantines_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    cancellation = Event()
    launched = Event()
    stopped = Event()
    commands: list[list[str]] = []

    class _CancellableWait:
        returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if not stopped.is_set():
                raise subprocess.TimeoutExpired(["docker", "wait"], timeout)
            self.returncode = 0
            return b"143", b""

        def kill(self) -> None:
            self.returncode = -9

    wait = _CancellableWait()
    monkeypatch.setattr(
        docker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: wait,
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        action = command[1]
        if action == "run":
            launched.set()
        elif action == "stop":
            stopped.set()
        elif action == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout=b"false\n", stderr=b"")
        elif action == "logs":
            pytest.fail("cancelled output must remain quarantined")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider, "_run", run)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider.execute, _request(), cancellation=cancellation)
        assert launched.wait(1.0)
        cancellation.set()
        outcome = future.result(timeout=5.0)

    container_name = provider._container_name("cleanup-qualification")
    assert ["docker", "stop", "--time", "5", container_name] in commands
    assert ["docker", "rm", "-f", "-v", container_name] in commands
    assert outcome.receipt.status is ExecutionStatus.CANCELLED
    assert outcome.receipt.reason_code == "cancelled"
    assert outcome.receipt.exit_code == 143
    assert outcome.receipt.termination_confirmed is True
    assert outcome.output is None
    assert outcome.diagnostic == "cancelled execution output quarantined"


def test_late_cancellation_preserves_already_completed_process_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    cancellation = Event()
    commands: list[list[str]] = []

    class _CompletedBeforeStopWait:
        returncode = 0
        calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            self.calls += 1
            if self.calls == 1:
                cancellation.set()
                raise subprocess.TimeoutExpired(["docker", "wait"], timeout)
            return b"0", b""

    monkeypatch.setattr(
        docker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _CompletedBeforeStopWait(),
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[1] == "logs":
            return subprocess.CompletedProcess(command, 0, stdout=b"completed", stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider, "_run", run)

    outcome = provider.execute(_request(), cancellation=cancellation)

    assert not any(command[1] == "stop" for command in commands)
    assert outcome.receipt.status is ExecutionStatus.SUCCEEDED
    assert outcome.receipt.reason_code == "process_exit"
    assert outcome.output == b"completed"


def test_active_cancellation_stop_failure_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    cancellation = Event()
    launched = Event()
    commands: list[list[str]] = []

    class _UnconfirmedWait:
        returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if self.returncode == -9:
                return b"", b""
            raise subprocess.TimeoutExpired(["docker", "wait"], timeout)

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(
        docker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: _UnconfirmedWait(),
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        action = command[1]
        if action == "run":
            launched.set()
        elif action == "stop":
            raise ExecutionEnvironmentUnavailableError("daemon unavailable")
        elif action == "inspect":
            return subprocess.CompletedProcess(command, 0, stdout=b"false\n", stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider, "_run", run)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(provider.execute, _request(), cancellation=cancellation)
        assert launched.wait(1.0)
        cancellation.set()
        outcome = future.result(timeout=5.0)

    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.reason_code == "termination_unconfirmed"
    assert outcome.receipt.termination_confirmed is False
    assert outcome.receipt.exit_code is None
    assert outcome.output is None
    assert not any(command[1] in {"logs", "rm"} for command in commands)


def test_wait_protocol_failure_reaps_cli_without_claiming_container_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    commands: list[list[str]] = []

    class _BrokenWait:
        returncode: int | None = None
        killed = False

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if self.killed:
                return b"", b""
            raise OSError("wait pipe failed")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    wait = _BrokenWait()
    monkeypatch.setattr(
        docker_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: wait,
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(provider, "_run", run)

    outcome = provider.execute(_request())

    assert wait.killed is True
    assert outcome.receipt.status is ExecutionStatus.INDETERMINATE
    assert outcome.receipt.reason_code == "termination_unconfirmed"
    assert outcome.receipt.termination_confirmed is False
    assert not any(command[1] in {"logs", "rm"} for command in commands)


def test_unsupported_cpu_limit_is_rejected_before_docker_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(monkeypatch)
    request = _request()
    request = ExecutionRequest(
        execution_id=request.execution_id,
        tool_id=request.tool_id,
        graph_identity=request.graph_identity,
        operation_id=request.operation_id,
        attempt_id=request.attempt_id,
        profile=request.profile,
        image=request.image,
        argv=request.argv,
        resource_limits=ResourceLimits(max_cpu_seconds=1.0),
    )
    monkeypatch.setattr(
        provider,
        "_canonical_mounts",
        lambda _request: pytest.fail("unsupported resource request must not launch"),
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        provider.execute(request)

    assert raised.value.details["missing"] == ["cpu_limits"]
    assert raised.value.details["denial_code"] == "execution_resource_limits_unsupported"
