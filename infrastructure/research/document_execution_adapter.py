"""Harness-controlled execution adapter for Research document parsers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from framework.execution_environment.errors import (
    ExecutionEnvironmentError,
    ExecutionEnvironmentUnavailableError,
    ExecutionIdentityMismatchError,
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
from framework.shared.json import stable_json_dumps
from framework.harness.graph.canonical import canonical_checksum

@runtime_checkable
class ParserExecutionEvidenceArtifactStore(Protocol):
    def publish(
        self,
        *,
        artifact_type: str,
        payload: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> str: ...


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
        max_timeout_seconds: float = 1_800.0,
        max_attempts: int = 2,
        allowed_backends: Sequence[str] = ("marker",),
        execution_evidence_store: ParserExecutionEvidenceArtifactStore | None = None,
        allowed_environment_names: Sequence[str] = (
            "TORCH_DEVICE",
            "HF_HOME",
            "MODEL_CACHE_DIR",
            "FONT_PATH",
            "MARKER_MODEL_SOURCE",
            "OPENBLAS_NUM_THREADS",
            "OMP_NUM_THREADS",
            "MKL_NUM_THREADS",
        ),
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
            else ResourceLimits(**dict(resource_limits or {"max_processes": profile.process_policy.max_processes}))
        )
        self._cancellation_grace_seconds = float(cancellation_grace_seconds)
        self._max_timeout_seconds = _positive_finite(
            max_timeout_seconds,
            "max_timeout_seconds",
        )
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 8:
            raise ValueError("max_attempts must be between 1 and 8")
        self._max_attempts = max_attempts
        self._allowed_backends = tuple(_identifier(value, "allowed_backend") for value in allowed_backends)
        if not self._allowed_backends or len(self._allowed_backends) != len(set(self._allowed_backends)):
            raise ValueError("allowed_backends must be a non-empty unique sequence")
        if execution_evidence_store is not None and not isinstance(
            execution_evidence_store, ParserExecutionEvidenceArtifactStore
        ):
            raise TypeError(
                "execution_evidence_store must implement ParserExecutionEvidenceArtifactStore"
            )
        self._execution_evidence_store = execution_evidence_store
        self.last_evidence_ref: str | None = None
        self._last_evidence_execution_id: str | None = None
        self._allowed_environment_names = tuple(
            _environment_name(value) for value in allowed_environment_names
        )
        if len(self._allowed_environment_names) != len(set(self._allowed_environment_names)):
            raise ValueError("allowed_environment_names must be unique")
        self.last_outcome: ExecutionOutcome | None = None

    def cache_identity(self) -> dict[str, Any]:
        """Return stable declared execution configuration, never run state."""
        return {
            "schema": "research-parser-execution-adapter.v1",
            "profile": self._profile.to_dict(),
            "resource_limits": self._resource_limits.to_dict(),
            "cancellation_grace_seconds": self._cancellation_grace_seconds,
            "max_timeout_seconds": self._max_timeout_seconds,
            "max_attempts": self._max_attempts,
            "allowed_backends": list(self._allowed_backends),
            "allowed_environment_names": list(self._allowed_environment_names),
            "durable_execution_evidence": self._execution_evidence_store is not None,
            # The registry has no declared immutable provider identity API. Do
            # not inspect its provider internals or dynamic capability state.
            "provider_identity": "unknown",
            "adapter_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        }

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
        normalized_backend = _identifier(backend, "backend")
        if normalized_backend not in self._allowed_backends:
            raise ExecutionPolicyViolationError(
                "Research parser backend is not admitted by the typed allowlist",
                reason_code="parser_backend_not_allowed",
                details={"backend": normalized_backend},
            )
        if identity.attempt > self._max_attempts:
            raise ExecutionPolicyViolationError(
                "Research parser attempt budget is exhausted",
                reason_code="parser_attempt_budget_exhausted",
                details={
                    "attempt": identity.attempt,
                    "max_attempts": self._max_attempts,
                },
            )
        requested_timeout = min(
            _positive_finite(timeout_seconds, "timeout_seconds"),
            self._max_timeout_seconds,
        )
        # Availability is a prerequisite, not a hardware/profile admission.
        # Preserve the registry's fail-closed unavailable-provider diagnosis
        # before inspecting parser-specific GPU flags. Available providers still
        # undergo every command policy check and full registry admission below.
        capabilities = self._execution_environment.resolve_capabilities(
            self._profile.provider_id
        )
        if not capabilities.available:
            raise ExecutionEnvironmentUnavailableError(
                "Research parser execution provider is unavailable",
                details={
                    "provider_id": capabilities.provider_id,
                    "provider_capability_checksum": capabilities.checksum,
                    "missing": ["provider_unavailable"],
                },
            )
        (
            image, argv, environment, read_roots, write_roots, container_mounts
        ) = _parse_docker_command(command)
        unexpected_environment = sorted(set(environment).difference(self._allowed_environment_names))
        if unexpected_environment:
            raise ExecutionPolicyViolationError(
                "Research parser environment contains unapproved names",
                reason_code="parser_environment_not_allowed",
                details={"environment_names": unexpected_environment},
            )
        argv = _map_container_paths(
            argv,
            input_root=read_roots[0],
            output_root=write_roots[0],
        )
        argv = _pin_parser_executable(normalized_backend, argv)
        environment = _map_container_environment(
            environment, container_mounts=container_mounts
        )
        operation_id = f"research-parser:{normalized_backend}:{paper_id}"
        execution_key = _execution_key(identity=identity, operation_id=operation_id)
        execution_id = f"exec:{normalized_backend}:{execution_key}"
        attempt_id = f"attempt:{normalized_backend}:{execution_key}"
        request = ExecutionRequest(
            execution_id=execution_id,
            tool_id=f"research.document-parser.{normalized_backend}",
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
            timeout_seconds=requested_timeout,
            cancellation_grace_seconds=self._cancellation_grace_seconds,
        )
        attempt_context = current_attempt_context()
        if attempt_context is not None:
            replay_requested = any(
                attempt_context.admission_details.get(key) is True
                for key in ("replay", "recovery_replay", "side_effects_replayed")
            )
            if replay_requested:
                raise ExecutionPolicyViolationError(
                    "Replay cannot invoke the live parser backend",
                    reason_code="parser_live_execution_forbidden_during_replay",
                    details={"status": "replay_blocked"},
                )
            if attempt_context.cancelled:
                raise ExecutionPolicyViolationError(
                    "Research parser execution was cancelled before admission",
                    reason_code="parser_execution_cancelled",
                    details={
                        "status": "cancelled",
                        "termination_confirmed": True,
                    },
                )
            remaining = attempt_context.remaining_seconds
            if remaining is not None:
                available = remaining - self._cancellation_grace_seconds
                if available <= 0:
                    raise ExecutionPolicyViolationError(
                        "Research parser has no safe execution window remaining",
                        reason_code="parser_execution_deadline_exhausted",
                        details={
                            "status": "timed_out",
                            "termination_confirmed": True,
                        },
                    )
                if request.timeout_seconds is not None and request.timeout_seconds > available:
                    request = replace(request, timeout_seconds=available)
        try:
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
        if (
            attempt_context is not None
            and outcome.receipt.status.value == "indeterminate"
        ):
            attempt_context.mark_descendant_indeterminate()
            if not outcome.receipt.termination_confirmed:
                attempt_context.mark_descendant_unconfirmed()
        evidence_artifact_ref = self._record_execution_evidence(
            request=request, outcome=outcome
        )
        if outcome.receipt.status.value != "succeeded":
            raise ExecutionEnvironmentUnavailableError(
                "Research parser external process did not complete successfully",
                details={
                    "backend": normalized_backend,
                    "status": outcome.receipt.status.value,
                    "reason_code": outcome.receipt.reason_code,
                    "execution_id": outcome.receipt.execution_id,
                    "attempt_id": outcome.receipt.attempt_id,
                    "receipt_checksum": outcome.receipt.receipt_checksum,
                    "execution_receipt_ref": (
                        f"execution-receipt://{outcome.receipt.execution_id}"
                    ),
                    **(
                        {"execution_evidence_artifact_ref": evidence_artifact_ref}
                        if evidence_artifact_ref is not None
                        else {}
                    ),
                    "termination_confirmed": outcome.receipt.termination_confirmed,
                    **_execution_failure_diagnostic(outcome),
                },
            )
        return outcome


    def evidence_ref_for(self, outcome: ExecutionOutcome) -> str | None:
        receipt = getattr(outcome, "receipt", None)
        if receipt is None or self.last_evidence_ref is None:
            return None
        if receipt.execution_id != self._last_evidence_execution_id:
            raise ExecutionIdentityMismatchError(
                "parser evidence reference belongs to another execution",
                details={
                    "receipt_execution_id": receipt.execution_id,
                    "evidence_execution_id": self._last_evidence_execution_id,
                },
            )
        return self.last_evidence_ref

    def _record_execution_evidence(
        self, *, request: ExecutionRequest, outcome: ExecutionOutcome
    ) -> str | None:
        if self._execution_evidence_store is None:
            return None
        receipt = outcome.receipt
        if not receipt.matches_request(request):
            raise ExecutionIdentityMismatchError(
                "parser receipt does not match the admitted execution request"
            )
        request_payload = _execution_request_payload(request)
        body = {
            "schema_version": "research-parser-execution-evidence/v1",
            "request": request_payload,
            "receipt": receipt.to_dict(),
        }
        checksum = canonical_checksum(body)
        evidence_ref = f"parser-execution-evidence://{checksum.removeprefix('sha256:')}"
        artifact_ref = self._execution_evidence_store.publish(
            artifact_type="parser-execution-evidence",
            payload={**body, "ref": evidence_ref, "checksum": checksum},
            metadata={
                "execution_id": receipt.execution_id,
                "receipt_checksum": receipt.receipt_checksum,
            },
        )
        self.last_evidence_ref = artifact_ref
        self._last_evidence_execution_id = receipt.execution_id
        return artifact_ref


def _execution_failure_diagnostic(outcome: ExecutionOutcome) -> dict[str, str]:
    details: dict[str, str] = {}
    if outcome.diagnostic:
        details["provider_diagnostic"] = str(outcome.diagnostic)[:1024]
    output = outcome.output
    if output is not None:
        text = (
            output.decode("utf-8", errors="replace")
            if isinstance(output, bytes)
            else str(output)
        )
        text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32).strip()
        if text:
            details["process_output_excerpt"] = text[-4096:]
    return details


def _execution_request_payload(request: ExecutionRequest) -> dict[str, Any]:
    return {
        "execution_id": request.execution_id,
        "tool_id": request.tool_id,
        "graph_identity": request.graph_identity.to_dict(),
        "operation_id": request.operation_id,
        "attempt_id": request.attempt_id,
        "profile": request.profile.to_dict(),
        "image": request.image,
        "argv": list(request.argv),
        "read_roots": list(request.read_roots),
        "write_roots": list(request.write_roots),
        "working_directory": request.working_directory,
        "environment": dict(request.environment),
        "resource_limits": request.resource_limits.to_dict(),
        "timeout_seconds": request.timeout_seconds,
        "cancellation_grace_seconds": request.cancellation_grace_seconds,
        "approval_evidence_ref": request.approval_evidence_ref,
        "budget_ref": request.budget_ref,
        "requested_at": request.requested_at.isoformat(),
    }


def _execution_key(
    *,
    identity: GraphExecutionIdentity,
    operation_id: str,
) -> str:
    payload = {
        "graph_identity": identity.to_dict(),
        "operation_id": operation_id,
    }
    return sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


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
) -> tuple[
    str, tuple[str, ...], dict[str, str], list[str], list[str], dict[str, str]
]:
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
    container_mounts: dict[str, str] = {}
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
            raise ExecutionPolicyViolationError(
                "Research parser GPU execution requires a provider with an admitted GPU capability",
                reason_code="parser_gpu_capability_unavailable",
                details={"requested_gpu": tokens[index + 1]},
            )
        if token in {"-e", "--env"}:
            if index + 1 >= len(tokens) or "=" not in tokens[index + 1]:
                raise ExecutionPolicyViolationError(
                    "parser docker environment option must be KEY=VALUE",
                    reason_code="parser_command_invalid",
                )
            name, value = tokens[index + 1].split("=", 1)
            name = _environment_name(name)
            if name in environment:
                raise ExecutionPolicyViolationError(
                    "parser docker command contains a duplicate environment name",
                    reason_code="parser_environment_not_allowed",
                    details={"environment_name": name},
                )
            if len(value) > 4096 or any(char in value for char in ("\x00", "\r", "\n")):
                raise ExecutionPolicyViolationError(
                    "parser docker environment value is invalid",
                    reason_code="parser_command_invalid",
                    details={"environment_name": name},
                )
            _validate_environment_value(name, value)
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
            host, container = _parse_volume_mount(volume)
            if container not in {"/input", "/output", "/root/.cache"}:
                raise ExecutionPolicyViolationError(
                    "parser docker volume targets an unapproved container path",
                    reason_code="parser_roots_invalid",
                    details={"container_path": container},
                )
            if container in container_mounts:
                raise ExecutionPolicyViolationError(
                    "parser docker command contains a duplicate container mount",
                    reason_code="parser_roots_invalid",
                    details={"container_path": container},
                )
            resolved_host = str(Path(host).resolve())
            if container == "/output":
                write_roots.append(resolved_host)
            else:
                read_roots.append(resolved_host)
            container_mounts[container] = resolved_host
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
    if "/input" not in container_mounts or len(write_roots) != 1:
        raise ExecutionPolicyViolationError(
            "parser command must declare exactly one input and output root",
            reason_code="parser_roots_invalid",
        )
    input_root = container_mounts["/input"]
    read_roots = [input_root, *[root for root in read_roots if root != input_root]]
    return image, tuple(argv), environment, read_roots, write_roots, container_mounts



def _parse_volume_mount(value: str) -> tuple[str, str]:
    match = re.fullmatch(r"(.+):(/[^:]+)(?::(?:ro|rw))?", str(value))
    if match is None:
        raise ExecutionPolicyViolationError(
            "parser docker volume must map a host path to an absolute container path",
            reason_code="parser_command_invalid",
        )
    host = match.group(1).strip()
    container = match.group(2).rstrip("/") or "/"
    host_path = Path(host)
    if (
        not host
        or "\x00" in host
        or host.startswith(("\\", "//"))
        or not host_path.is_absolute()
    ):
        raise ExecutionPolicyViolationError(
            "parser docker volume host path must be an absolute local path",
            reason_code="parser_command_invalid",
        )
    return host, container


def _validate_environment_value(name: str, value: str) -> None:
    if name == "TORCH_DEVICE" and value not in {"cpu", "cuda", "mps"}:
        raise ExecutionPolicyViolationError(
            "parser device environment value is not allowed",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )
    if name == "MARKER_MODEL_SOURCE" and value not in {
        "huggingface",
        "modelscope",
        "local",
    }:
        raise ExecutionPolicyViolationError(
            "parser model source is not allowed",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )
    if name == "HF_HOME" and value != "/root/.cache/huggingface":
        raise ExecutionPolicyViolationError(
            "parser cache path is not allowed",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )
    if name == "MODEL_CACHE_DIR" and value != "/root/.cache/datalab/models":
        raise ExecutionPolicyViolationError(
            "parser model cache path is not allowed",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )
    if name == "FONT_PATH" and value != "/root/.cache/static/fonts/GoNotoCurrent-Regular.ttf":
        raise ExecutionPolicyViolationError(
            "parser font path is not allowed",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )
    if name in {
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
    } and value != "1":
        raise ExecutionPolicyViolationError(
            "parser numerical runtime thread limit must be exactly one",
            reason_code="parser_environment_not_allowed",
            details={"environment_name": name},
        )


def _map_container_environment(
    environment: Mapping[str, str],
    *,
    container_mounts: Mapping[str, str],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    prefixes = sorted(container_mounts, key=len, reverse=True)
    for name, raw_value in environment.items():
        value = str(raw_value)
        for prefix in prefixes:
            if value == prefix or value.startswith(prefix + "/"):
                suffix = value[len(prefix):].lstrip("/")
                value = str(Path(container_mounts[prefix], suffix).resolve())
                break
        mapped[name] = value
    return mapped

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


def _pin_parser_executable(backend: str, argv: tuple[str, ...]) -> tuple[str, ...]:
    if backend == "marker" and argv and argv[0] == "marker_single":
        return ("/usr/local/bin/marker_single", *argv[1:])
    return argv


def _positive_finite(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a positive finite number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return numeric


def _identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 128 or re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]*", text) is None:
        raise ValueError(f"{field_name} is invalid")
    return text


def _environment_name(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > 128 or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) is None:
        raise ValueError("environment name is invalid")
    return text


__all__ = ["ParserExecutionEvidenceArtifactStore", "ResearchParserExecutionAdapter"]
