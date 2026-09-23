from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest

from framework.execution_environment import (
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionRequest,
    ExecutionStatus,
    NetworkPolicy,
    ResourceLimits,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.execution_environment.docker import DockerExecutionEnvironment


_RUN_DOCKER_INTEGRATION = os.environ.get("NEWSROOM_DOCKER_INTEGRATION") == "1"
pytestmark = pytest.mark.skipif(
    not _RUN_DOCKER_INTEGRATION,
    reason="real Docker execution integration is explicit; set NEWSROOM_DOCKER_INTEGRATION=1",
)


def _image_ref() -> str:
    configured = os.environ.get("NEWSROOM_DOCKER_TEST_IMAGE", "redis:7-alpine")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", configured],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"Docker qualification image could not be inspected: {exc}")
    if result.returncode != 0 or not result.stdout.strip().startswith("sha256:"):
        pytest.fail(f"Docker qualification image is unavailable: {configured}")
    return result.stdout.strip()


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="docker-qualification-run",
        graph_id="docker-qualification",
        graph_version="1.0.0",
        graph_ref="docker-qualification@1.0.0",
        graph_checksum="sha256:" + "d" * 64,
        node_id="execute",
        node_instance_id="execute-1",
        activity_id="docker-provider",
        attempt=1,
    )


def _profile(
    argv_prefix: str,
    *,
    network_policy: NetworkPolicy | None = None,
) -> ExecutionProfile:
    return ExecutionProfile.sandboxed_process(
        provider_id="docker",
        allowed_argv_prefixes=((argv_prefix,),),
        network_policy=network_policy,
    )


def _request(
    *,
    execution_id: str,
    profile: ExecutionProfile,
    argv: tuple[str, ...],
    read_roots: tuple[str, ...] = (),
    write_roots: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
) -> ExecutionRequest:
    unique_execution_id = f"{execution_id}-{uuid4().hex}"
    return ExecutionRequest(
        execution_id=unique_execution_id,
        tool_id="qualification.docker@1.0.0",
        graph_identity=_identity(),
        operation_id=f"operation-{unique_execution_id}",
        attempt_id=f"attempt-{unique_execution_id}",
        profile=profile,
        image=_image_ref(),
        argv=argv,
        read_roots=read_roots,
        write_roots=write_roots,
        environment=environment or {},
        resource_limits=ResourceLimits(
            max_memory_bytes=64 * 1024 * 1024,
            max_processes=1,
        ),
        timeout_seconds=timeout_seconds,
        cancellation_grace_seconds=1.0,
    )


@pytest.fixture
def docker_registry() -> ExecutionEnvironmentRegistry:
    provider = DockerExecutionEnvironment(probe_timeout_seconds=10.0)
    if not provider.capabilities.available:
        pytest.fail("Docker integration was requested but the daemon is unavailable")
    registry = ExecutionEnvironmentRegistry()
    registry.register(provider)
    return registry


def test_real_docker_enforces_read_only_and_writable_roots(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
) -> None:
    read_root = tmp_path / "read"
    write_root = tmp_path / "write"
    read_root.mkdir()
    write_root.mkdir()
    source = read_root / "input.txt"
    destination = write_root / "output.txt"
    source.write_text("qualified", encoding="utf-8")

    copied = docker_registry.execute(
        _request(
            execution_id="filesystem-copy",
            profile=_profile("cp"),
            argv=("cp", str(source), str(destination)),
            read_roots=(str(read_root),),
            write_roots=(str(write_root),),
        )
    )
    assert copied.receipt.status is ExecutionStatus.SUCCEEDED
    assert copied.receipt.termination_confirmed is True
    assert destination.read_text(encoding="utf-8") == "qualified"

    denied = docker_registry.execute(
        _request(
            execution_id="filesystem-readonly",
            profile=_profile("touch"),
            argv=("touch", str(read_root / "forbidden.txt")),
            read_roots=(str(read_root),),
        )
    )
    assert denied.receipt.status is ExecutionStatus.FAILED
    assert denied.receipt.termination_confirmed is True
    assert not (read_root / "forbidden.txt").exists()


def test_real_docker_clears_host_environment_and_admits_explicit_values(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEWSROOM_HOST_ONLY_SECRET", "must-not-cross-boundary")
    outcome = docker_registry.execute(
        _request(
            execution_id="environment-isolation",
            profile=_profile("env"),
            argv=("env",),
            read_roots=(str(tmp_path),),
            environment={"QUALIFICATION_VALUE": "admitted"},
        )
    )
    output = (outcome.output or b"").decode("utf-8")
    assert outcome.receipt.status is ExecutionStatus.SUCCEEDED
    assert "QUALIFICATION_VALUE=admitted" in output
    assert "NEWSROOM_HOST_ONLY_SECRET" not in output
    assert "must-not-cross-boundary" not in output


def test_real_docker_network_deny_blocks_external_connection(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
) -> None:
    outcome = docker_registry.execute(
        _request(
            execution_id="network-deny",
            profile=_profile("sh"),
            argv=(
                "sh",
                "-c",
                "printf 'NETWORK_PROBE_STARTED\\n'; "
                "exec wget -T 1 -O - http://1.1.1.1",
            ),
            read_roots=(str(tmp_path),),
        )
    )
    output = (outcome.output or b"").lower()
    assert outcome.receipt.status is ExecutionStatus.FAILED
    assert outcome.receipt.termination_confirmed is True
    assert outcome.receipt.exit_code not in (None, 126, 127)
    assert b"network_probe_started" in output
    assert b"network unreachable" in output


def test_real_docker_removes_image_anonymous_volume_with_container(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
) -> None:
    request = _request(
        execution_id="anonymous-volume-cleanup",
        profile=_profile("sleep"),
        argv=("sleep", "30"),
        read_roots=(str(tmp_path),),
        timeout_seconds=3.0,
    )
    container_name = DockerExecutionEnvironment._container_name(request.execution_id)
    volume_name: str | None = None

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(docker_registry.execute, request)
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not future.done():
                inspect = subprocess.run(
                    [
                        "docker",
                        "container",
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        container_name,
                    ],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=10,
                )
                if inspect.returncode == 0:
                    mounts = json.loads(inspect.stdout)
                    volume_name = next(
                        (
                            mount["Name"]
                            for mount in mounts
                            if mount.get("Type") == "volume"
                            and mount.get("Destination") == "/data"
                        ),
                        None,
                    )
                    if volume_name is not None:
                        break
                time.sleep(0.05)
            outcome = future.result(timeout=15)

        assert volume_name is not None, "Redis /data anonymous volume was not observed"
        assert outcome.receipt.status is ExecutionStatus.TIMED_OUT
        assert outcome.receipt.termination_confirmed is True
        volume_inspect = subprocess.run(
            ["docker", "volume", "inspect", volume_name],
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert volume_inspect.returncode != 0
    finally:
        subprocess.run(
            ["docker", "rm", "-f", "-v", container_name],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if volume_name is not None:
            subprocess.run(
                ["docker", "volume", "rm", "-f", volume_name],
                capture_output=True,
                check=False,
                timeout=10,
            )


def test_real_docker_timeout_confirms_termination_and_removes_container(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
) -> None:
    request = _request(
        execution_id="timeout-termination",
        profile=_profile("sleep"),
        argv=("sleep", "30"),
        read_roots=(str(tmp_path),),
        timeout_seconds=0.2,
    )
    outcome = docker_registry.execute(request)

    assert outcome.receipt.status is ExecutionStatus.TIMED_OUT
    assert outcome.receipt.termination_confirmed is True
    container_name = DockerExecutionEnvironment._container_name(request.execution_id)
    inspect = subprocess.run(
        ["docker", "container", "inspect", container_name],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert inspect.returncode != 0


def test_real_docker_rejects_unenforceable_network_allowlist_before_launch(
    docker_registry: ExecutionEnvironmentRegistry,
    tmp_path: Path,
) -> None:
    request = _request(
        execution_id="unsupported-allowlist",
        profile=_profile(
            "wget",
            network_policy=NetworkPolicy(
                mode="allowlist",
                allowlist=({"host": "example.com", "port": 443},),
            ),
        ),
        argv=("wget", "https://example.com"),
        read_roots=(str(tmp_path),),
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        docker_registry.execute(request)
    assert raised.value.details["missing"] == ["network_allowlist"]
    container_name = DockerExecutionEnvironment._container_name(request.execution_id)
    inspect = subprocess.run(
        ["docker", "container", "inspect", container_name],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert inspect.returncode != 0
