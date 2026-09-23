"""Docker-backed physical execution provider.

The provider deliberately reports only controls implemented by Docker itself.
When the Docker daemon is unavailable, capability admission fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Any

from framework.execution_environment.errors import (
    ExecutionEnvironmentUnavailableError,
    ExecutionPolicyViolationError,
)
from framework.execution_environment.models import (
    ExecutionCapabilityProfile,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionRequest,
    ExecutionReceipt,
    ExecutionStatus,
)


_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_CONTAINER_ROOT = "/newsroom"


class DockerExecutionEnvironment:
    """Run one admitted request in a Docker container.

    Docker provides the physical network/filesystem/process boundary here. The
    provider intentionally does not claim network allowlists, secret-handle
    injection, child executable allowlists, or CPU-time enforcement.
    """

    def __init__(
        self,
        *,
        docker_executable: str = "docker",
        probe_timeout_seconds: float = 2.0,
    ) -> None:
        self._docker = str(docker_executable).strip() or "docker"
        self._probe_timeout_seconds = float(probe_timeout_seconds)
        self._available = self._probe_docker()

    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return ExecutionCapabilityProfile(
            provider_id="docker",
            available=self._available,
            enforces_filesystem_roots=True,
            enforces_network_deny=True,
            enforces_network_allowlist=False,
            isolates_environment=True,
            enforces_argv_policy=True,
            controls_process_tree=True,
            enforces_child_process_allowlist=False,
            enforces_resource_limits=False,
            enforces_memory_limits=True,
            enforces_cpu_limits=False,
            enforces_process_limits=True,
            confirms_termination=True,
            supports_secret_handles=False,
            version="docker-v1",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionOutcome:
        missing = self.capabilities.missing_for(request)
        if missing:
            raise ExecutionEnvironmentUnavailableError(
                "requested Docker execution capabilities are unavailable",
                details={"provider_id": "docker", "missing": list(missing)},
            )
        if request.profile.network_policy.mode.value != "deny":
            raise ExecutionEnvironmentUnavailableError(
                "Docker provider only supports network deny",
                details={"provider_id": "docker", "missing": ["network_allowlist"]},
            )

        mounts, path_map, working_directory = self._canonical_mounts(request)
        command = self._build_run_command(
            request,
            mounts=mounts,
            path_map=path_map,
            working_directory=working_directory,
        )
        container_name = self._container_name(request.execution_id)
        started_at = datetime.now(UTC)
        run_command = [*command[:2], "--name", container_name, *command[2:]]
        try:
            launch = self._run(run_command, timeout=self._probe_timeout_seconds)
        except ExecutionEnvironmentUnavailableError:
            # ``docker run --detach`` may have handed the request to the
            # daemon before the CLI connection failed.  Do not turn that
            # ambiguity into a retryable exception with no receipt.
            return self._indeterminate_outcome(
                request,
                started_at=started_at,
                diagnostic="container launch could not be confirmed; reconciliation required",
            )
        if launch.returncode != 0:
            raise ExecutionEnvironmentUnavailableError(
                "Docker could not start the admitted container",
                details={"provider_id": "docker", "reason": "container_start_failed"},
            )

        try:
            wait_process = subprocess.Popen(
                [self._docker, "wait", container_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, ValueError):
            # The container may still be running even when the local wait
            # process cannot be created.  Preserve an auditable, non-retryable
            # outcome and leave the container for reconciliation.
            return self._indeterminate_outcome(
                request,
                started_at=started_at,
                diagnostic="container wait process could not be started; reconciliation required",
            )
        status = ExecutionStatus.SUCCEEDED
        reason_code = "process_exit"
        termination_confirmed = True
        exit_code: int | None = None
        diagnostics: str | None = None
        output: bytes | None = None
        try:
            try:
                wait_stdout, wait_stderr = wait_process.communicate(
                    timeout=request.timeout_seconds,
                )
                if wait_process.returncode != 0:
                    raise ExecutionEnvironmentUnavailableError(
                        "Docker wait did not return a container exit status",
                        details={"provider_id": "docker", "reason": "wait_failed"},
                    )
                try:
                    exit_code = int(wait_stdout.decode("ascii", errors="strict").strip())
                except (UnicodeDecodeError, ValueError):
                    raise ExecutionEnvironmentUnavailableError(
                        "Docker returned an invalid container exit status",
                        details={"provider_id": "docker", "reason": "invalid_exit_status"},
                    ) from None
                if exit_code:
                    status = ExecutionStatus.FAILED
                    reason_code = "process_exit_nonzero"
            except subprocess.TimeoutExpired:
                status = ExecutionStatus.TIMED_OUT
                reason_code = "timeout"
                try:
                    stop_result = self._run(
                        [
                            self._docker,
                            "stop",
                            "--time",
                            str(max(0, int(request.cancellation_grace_seconds))),
                            container_name,
                        ],
                        timeout=request.cancellation_grace_seconds + 2.0,
                    )
                except ExecutionEnvironmentUnavailableError:
                    stop_result = None
                try:
                    wait_process.communicate(timeout=request.cancellation_grace_seconds + 2.0)
                except subprocess.TimeoutExpired:
                    # The Docker CLI itself may remain blocked after a daemon
                    # failure.  Kill only this waiter; the container remains
                    # for reconciliation when termination cannot be proven.
                    try:
                        wait_process.kill()
                        wait_process.communicate()
                    except OSError:
                        pass
                except OSError:
                    pass
                try:
                    stopped = self._is_stopped(container_name)
                except ExecutionEnvironmentUnavailableError:
                    stopped = False
                termination_confirmed = (
                    stop_result is not None
                    and stop_result.returncode == 0
                    and stopped
                )
                if not termination_confirmed:
                    status = ExecutionStatus.INDETERMINATE
                    reason_code = "termination_unconfirmed"
            except (ExecutionEnvironmentUnavailableError, OSError, ValueError) as exc:
                # A daemon/wait protocol failure leaves process termination
                # unknown. Preserve a typed receipt for reconciliation rather
                # than raising before the audit record can be emitted.
                status = ExecutionStatus.INDETERMINATE
                reason_code = "termination_unconfirmed"
                termination_confirmed = False
                diagnostics = f"container wait could not be confirmed: {type(exc).__name__}"
            try:
                # ``docker logs`` returns both streams by default.  It does
                # not provide ``--stdout``/``--stderr`` selector flags.
                logs = self._run(
                    [self._docker, "logs", container_name],
                    timeout=self._probe_timeout_seconds,
                )
                if logs.returncode != 0:
                    raise ExecutionEnvironmentUnavailableError(
                        "Docker could not collect container output",
                        details={"provider_id": "docker", "reason": "logs_failed"},
                    )
                output = logs.stdout + logs.stderr
                if len(output) > _MAX_OUTPUT_BYTES:
                    output = output[:_MAX_OUTPUT_BYTES]
                    diagnostics = "container output exceeded the bounded receipt limit"
            except ExecutionEnvironmentUnavailableError:
                # A daemon failure after a timeout must still produce an
                # auditable receipt; never replace it with an uncaught CLI
                # exception that hides termination uncertainty.
                output = None
                termination_confirmed = False
                status = ExecutionStatus.INDETERMINATE
                reason_code = "termination_unconfirmed"
                diagnostics = "container output could not be collected"
        finally:
            if termination_confirmed:
                try:
                    cleanup = self._run(
                        [self._docker, "rm", "-f", "-v", container_name],
                        timeout=self._probe_timeout_seconds,
                    )
                except ExecutionEnvironmentUnavailableError:
                    cleanup = None
                if cleanup is None or cleanup.returncode != 0:
                    termination_confirmed = False
                    status = ExecutionStatus.INDETERMINATE
                    reason_code = "termination_unconfirmed"
                    diagnostics = "container cleanup could not be confirmed"

        finished_at = datetime.now(UTC)
        receipt = ExecutionReceipt(
            execution_id=request.execution_id,
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="docker",
            provider_capability_checksum=self.capabilities.checksum,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            termination_confirmed=termination_confirmed,
            reason_code=reason_code,
            exit_code=exit_code,
            output_checksum=_sha256(output) if output is not None else None,
            output_bytes=len(output) if output is not None else None,
        )
        return ExecutionOutcome(receipt=receipt, output=output, diagnostic=diagnostics)

    def _probe_docker(self) -> bool:
        try:
            result = self._run(
                [self._docker, "info", "--format", "{{.ServerVersion}}"],
                timeout=self._probe_timeout_seconds,
            )
        except (OSError, ExecutionEnvironmentUnavailableError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())

    def _indeterminate_outcome(
        self,
        request: ExecutionRequest,
        *,
        started_at: datetime,
        diagnostic: str,
    ) -> ExecutionOutcome:
        finished_at = datetime.now(UTC)
        receipt = ExecutionReceipt(
            execution_id=request.execution_id,
            tool_id=request.tool_id,
            graph_identity=request.graph_identity,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            provider_id="docker",
            provider_capability_checksum=self.capabilities.checksum,
            status=ExecutionStatus.INDETERMINATE,
            started_at=started_at,
            finished_at=max(started_at, finished_at),
            termination_confirmed=False,
            reason_code="termination_unconfirmed",
        )
        return ExecutionOutcome(receipt=receipt, diagnostic=diagnostic)

    def _run(self, command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(0.1, float(timeout)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionEnvironmentUnavailableError(
                "Docker command could not be completed",
                details={"provider_id": "docker", "reason": "docker_command_failed"},
            ) from exc

    def _canonical_mounts(
        self,
        request: ExecutionRequest,
    ) -> tuple[list[tuple[Path, str, bool]], dict[str, str], str | None]:
        entries: list[tuple[Path, str, bool]] = []
        path_map: dict[str, str] = {}
        canonical_roots: list[tuple[Path, bool]] = []
        for roots, writable in ((request.read_roots, False), (request.write_roots, True)):
            for root in roots:
                candidate = Path(root)
                try:
                    canonical = candidate.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ExecutionPolicyViolationError(
                        "declared filesystem root cannot be canonicalized",
                        reason_code="filesystem_root_invalid",
                    ) from exc
                if not canonical.is_dir() or _is_link_or_junction(candidate) or _is_link_or_junction(canonical):
                    raise ExecutionPolicyViolationError(
                        "declared filesystem root is not a real directory",
                        reason_code="filesystem_root_invalid",
                    )
                _reject_nested_links(canonical)
                canonical_roots.append((canonical, writable))
        for index, (canonical, writable) in enumerate(canonical_roots):
            for other, _ in canonical_roots[index + 1 :]:
                if _contains(canonical, other) or _contains(other, canonical):
                    raise ExecutionPolicyViolationError(
                        "declared filesystem roots overlap after canonicalization",
                        reason_code="filesystem_root_overlap",
                    )
            target = f"{_CONTAINER_ROOT}/{'write' if writable else 'read'}/{index}"
            entries.append((canonical, target, writable))
            path_map[_normal_path(str(canonical))] = target

        for root in request.read_roots:
            canonical = Path(root).resolve(strict=True)
            path_map[_normal_path(root)] = next(
                target for item, target, writable in entries if item == canonical and not writable
            )
        for root in request.write_roots:
            canonical = Path(root).resolve(strict=True)
            path_map[_normal_path(root)] = next(
                target for item, target, writable in entries if item == canonical and writable
            )

        working_directory = None
        if request.working_directory is not None:
            canonical_workdir = Path(request.working_directory).resolve(strict=True)
            for root, target, _ in entries:
                if canonical_workdir == root:
                    working_directory = target
                    break
            if working_directory is None:
                raise ExecutionPolicyViolationError(
                    "working directory is outside declared filesystem roots",
                    reason_code="working_directory_invalid",
                )
        return entries, path_map, working_directory

    def _build_run_command(
        self,
        request: ExecutionRequest,
        *,
        mounts: Sequence[tuple[Path, str, bool]],
        path_map: Mapping[str, str],
        working_directory: str | None,
    ) -> list[str]:
        command = [self._docker, "run", "--detach", "--network", "none"]
        if working_directory:
            command.extend(["--workdir", working_directory])
        limits = request.resource_limits
        if limits.max_memory_bytes is not None:
            command.extend(["--memory", str(limits.max_memory_bytes)])
        # ProcessPolicy is part of the admitted profile even when callers do
        # not request a separate resource limit.  Always carry its bound into
        # Docker so a default max_processes=1 cannot silently degrade to an
        # unrestricted process tree.
        process_limit = request.profile.process_policy.max_processes
        if limits.max_processes is not None:
            process_limit = min(process_limit, limits.max_processes)
        command.extend(["--pids-limit", str(process_limit)])
        for source, target, writable in mounts:
            mount = f"type=bind,source={source},target={target}"
            if not writable:
                mount += ",readonly"
            command.extend(["--mount", mount])
        command.extend(["--entrypoint", "/usr/bin/env", request.image, "-i"])
        command.extend(f"{name}={value}" for name, value in request.environment.items())
        # BusyBox ``env`` does not accept GNU's optional ``--`` separator.
        # The executable is already constrained by ProcessPolicy, so the
        # separator adds no admission protection here.
        command.extend(_translate_argument(token, path_map) for token in request.argv)
        return command

    def _is_stopped(self, container_name: str) -> bool:
        result = self._run(
            [self._docker, "inspect", "--format", "{{.State.Running}}", container_name],
            timeout=self._probe_timeout_seconds,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == b"false"

    @staticmethod
    def _container_name(execution_id: str) -> str:
        digest = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[:24]
        return f"newsroom-exec-{digest}"


def _normal_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value))


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return child != parent


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _reject_nested_links(root: Path) -> None:
    """Reject link entries inside a mounted root to prevent escape races."""
    def on_error(error: OSError) -> None:
        raise ExecutionPolicyViolationError(
            "declared filesystem root could not be fully inspected",
            reason_code="filesystem_root_invalid",
        ) from error

    for current, directories, files in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=on_error,
    ):
        current_path = Path(current)
        linked_directories = [name for name in directories if _is_link_or_junction(current_path / name)]
        linked_files = [name for name in files if _is_link_or_junction(current_path / name)]
        if linked_directories or linked_files:
            raise ExecutionPolicyViolationError(
                "declared filesystem root contains a symlink or junction",
                reason_code="filesystem_root_invalid",
            )


def _translate_argument(value: str, path_map: Mapping[str, str]) -> str:
    normalized = _normal_path(value)
    for source, target in sorted(path_map.items(), key=lambda item: len(item[0]), reverse=True):
        if not target:
            continue
        if normalized == source:
            return target
        if normalized.startswith(source + os.sep) or normalized.startswith(source + "/"):
            suffix = normalized[len(source) :].replace("\\", "/")
            return target + suffix
    return value


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


__all__ = ["DockerExecutionEnvironment"]
