"""Harness-controlled execution adapter for Research document parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from framework.execution_environment.errors import (
    ExecutionEnvironmentError,
    ExecutionEnvironmentUnavailableError,
    ExecutionPolicyViolationError,
)
from framework.execution_environment.models import (
    ExecutionMode,
    ExecutionOutcome,
    ExecutionProfile,
    ExecutionRequest,
    ResourceLimits,
)
from framework.execution_environment.registry import ExecutionEnvironmentRegistry
from framework.shared.attempts import current_attempt_context
from framework.shared.graph_identity import GraphExecutionIdentity


class ResearchParserExecutionAdapter:
    """Translate a parser Docker command into one admitted execution request.

    The parser owns neither a process handle nor provider selection.  Docker
    CLI syntax is accepted only as a compatibility command description; the
    registered provider receives the image, container argv, declared roots,
    environment, timeout, and exact Graph activity identity.
    """

    def __init__(
        self,
        *,
        execution_environment: ExecutionEnvironmentRegistry,
        profile: ExecutionProfile,
        resource_limits: ResourceLimits | Mapping[str, Any] | None = None,
        cancellation_grace_seconds: float = 5.0,
    ) -> None:
        if not isinstance(execution_environment, ExecutionEnvironmentRegistry):
            raise TypeError("execution_environment must be ExecutionEnvironmentRegistry")
        if not isinstance(profile, ExecutionProfile):
            raise TypeError("profile must be ExecutionProfile")
        if profile.mode not in {
            ExecutionMode.SANDBOXED_PROCESS,
            ExecutionMode.EXTERNAL_PROCESS,
        }:
            raise ValueError("Research parser adapter requires an external process profile")
        if profile.provider_id is None:
            raise ValueError("Research parser profile requires provider_id")
        self._execution_environment = execution_environment
        self._profile = profile
        self._resource_limits = (
            resource_limits
            if isinstance(resource_limits, ResourceLimits)
            else ResourceLimits(**dict(resource_limits or {"max_processes": 1}))
        )
        self._cancellation_grace_seconds = float(cancellation_grace_seconds)
        self.last_outcome: ExecutionOutcome | None = None

    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: int,
        execution_identity: GraphExecutionIdentity | Mapping[str, Any] | None,
        paper_id: str,
        backend: str,
    ) -> ExecutionOutcome:
        identity = _coerce_execution_identity(execution_identity)
        image, argv, environment, read_roots, write_roots = _parse_docker_command(command)
        argv = _map_container_paths(
            argv,
            input_root=read_roots[0],
            output_root=write_roots[0],
        )
        operation_id = f"research-parser:{backend}:{paper_id}"
        execution_id = f"exec:{identity.run_id}:{backend}:{paper_id}"
        attempt_id = f"{operation_id}:attempt-{identity.attempt}"
        request = ExecutionRequest(
            execution_id=execution_id,
            tool_id=f"research.document-parser.{backend}",
            graph_identity=identity,
            operation_id=operation_id,
            attempt_id=attempt_id,
            profile=self._profile,
            image=image,
            argv=argv,
            read_roots=tuple(read_roots),
            write_roots=tuple(write_roots),
            environment=environment,
            resource_limits=self._resource_limits,
            timeout_seconds=float(timeout_seconds),
            cancellation_grace_seconds=self._cancellation_grace_seconds,
        )
        try:
            attempt_context = current_attempt_context()
            outcome = self._execution_environment.execute(
                request,
                cancellation=(
                    attempt_context.cancel_event
                    if attempt_context is not None
                    else None
                ),
            )
        except ExecutionEnvironmentError:
            raise
        if not isinstance(outcome, ExecutionOutcome):
            raise TypeError("execution environment returned an invalid parser outcome")
        self.last_outcome = outcome
        if outcome.receipt.status.value != "succeeded":
            raise ExecutionEnvironmentUnavailableError(
                "Research parser external process did not complete successfully",
                details={
                    "backend": backend,
                    "status": outcome.receipt.status.value,
                    "reason_code": outcome.receipt.reason_code,
                    "execution_id": outcome.receipt.execution_id,
                    "termination_confirmed": outcome.receipt.termination_confirmed,
                },
            )
        return outcome


def _coerce_execution_identity(
    value: GraphExecutionIdentity | Mapping[str, Any] | None,
) -> GraphExecutionIdentity:
    if value is None:
        raise ExecutionPolicyViolationError(
            "Research parser execution requires an exact Graph identity",
            reason_code="graph_identity_required",
        )
    if isinstance(value, GraphExecutionIdentity):
        return value
    return GraphExecutionIdentity.from_dict(value)


def _parse_docker_command(
    command: Sequence[str],
) -> tuple[str, tuple[str, ...], dict[str, str], list[str], list[str]]:
    tokens = tuple(str(item) for item in command)
    if len(tokens) < 4 or tokens[0] != "docker" or tokens[1] != "run" or "--rm" not in tokens[2:]:
        raise ExecutionPolicyViolationError(
            "parser command must be a bounded docker run description",
            reason_code="parser_command_invalid",
        )
    image: str | None = None
    argv: list[str] = []
    environment: dict[str, str] = {}
    read_roots: list[str] = []
    write_roots: list[str] = []
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token == "--rm":
            index += 1
            continue
        if token == "--gpus":
            if index + 1 >= len(tokens):
                raise ExecutionPolicyViolationError(
                    "parser docker command has an incomplete gpu option",
                    reason_code="parser_command_invalid",
                )
            index += 2
            continue
        if token in {"-e", "--env"}:
            if index + 1 >= len(tokens) or "=" not in tokens[index + 1]:
                raise ExecutionPolicyViolationError(
                    "parser docker environment option must be KEY=VALUE",
                    reason_code="parser_command_invalid",
                )
            name, value = tokens[index + 1].split("=", 1)
            environment[name] = value
            index += 2
            continue
        if token in {"-v", "--volume"}:
            if index + 1 >= len(tokens):
                raise ExecutionPolicyViolationError(
                    "parser docker volume option is incomplete",
                    reason_code="parser_command_invalid",
                )
            volume = tokens[index + 1]
            host: str | None = None
            container: str | None = None
            for expected_container in ("/input", "/output"):
                suffix = f":{expected_container}"
                if volume.endswith(suffix):
                    host = volume[: -len(suffix)]
                    container = expected_container
                    break
            if container == "/input" and host:
                read_roots.append(str(Path(host).resolve()))
            elif container == "/output" and host:
                write_roots.append(str(Path(host).resolve()))
            # Cache/config mounts are deliberately ignored.  They cannot
            # widen the admitted input/output roots or become hidden writes.
            index += 2
            continue
        if token.startswith("-"):
            raise ExecutionPolicyViolationError(
                "parser docker command contains an unsupported host option",
                reason_code="parser_command_invalid",
                details={"option": token},
            )
        image = token
        argv = list(tokens[index + 1 :])
        break
    if not image or not argv:
        raise ExecutionPolicyViolationError(
            "parser docker command must include image and container argv",
            reason_code="parser_command_invalid",
        )
    if len(read_roots) != 1 or len(write_roots) != 1:
        raise ExecutionPolicyViolationError(
            "parser command must declare exactly one input and output root",
            reason_code="parser_roots_invalid",
        )
    return image, tuple(argv), environment, read_roots, write_roots


def _map_container_paths(
    argv: Sequence[str],
    *,
    input_root: str,
    output_root: str,
) -> tuple[str, ...]:
    """Make provider mount translation effective for parser container paths."""

    mapped: list[str] = []
    for token in argv:
        if token == "/input" or token.startswith("/input/"):
            mapped.append(input_root + token[len("/input") :])
        elif token == "/output" or token.startswith("/output/"):
            mapped.append(output_root + token[len("/output") :])
        else:
            mapped.append(token)
    return tuple(mapped)


__all__ = ["ResearchParserExecutionAdapter"]
