from __future__ import annotations

import subprocess

import pytest

from framework.execution_environment import (
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionStatus,
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
