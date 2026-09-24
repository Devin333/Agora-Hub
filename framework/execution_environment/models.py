"""Immutable, provider-neutral physical execution contracts.

The framework owns the requested boundary and receipt identity.  Providers own
platform mechanics and must advertise only controls they can actually enforce.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import math
import re
from types import MappingProxyType
from typing import Any

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps
from framework.shared.time import format_datetime


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,255}\Z")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_WINDOWS_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:($|[^\\/])")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PATH = re.compile(r"^(?:\\\\|//)")
_SECRET_LIKE_ENVIRONMENT_NAME = re.compile(
    r"(?:secret|token|password|credential|private[_-]?key|api[_-]?key)",
    re.IGNORECASE,
)
_PROTECTED_ENVIRONMENT_NAME = re.compile(
    r"^(?:PATH|PATHEXT|PYTHONPATH|PYTHONHOME|LD_PRELOAD|LD_LIBRARY_PATH|"
    r"DYLD_LIBRARY_PATH|DYLD_INSERT_LIBRARIES|COMSPEC|SYSTEMROOT|WINDIR|"
    r"TEMP|TMP|TMPDIR)$",
    re.IGNORECASE,
)


class ExecutionMode(StrEnum):
    TRUSTED_IN_PROCESS = "trusted_in_process"
    SANDBOXED_PROCESS = "sandboxed_process"
    EXTERNAL_PROCESS = "external_process"


class NetworkPolicyMode(StrEnum):
    DENY = "deny"
    ALLOWLIST = "allowlist"


class ExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"


# The mapping is part of the execution boundary contract.  Keep capability
# names stable for provider implementations while exposing a coarser denial
# vocabulary to operators and callers.
CAPABILITY_DENIAL_CODE_VERSION = "newsroom.execution-capability-denials/v1"
EXECUTION_PROFILE_SCHEMA = "newsroom.execution-profile/v1"
_CAPABILITY_DENIAL_CODES = MappingProxyType({
    "provider_unavailable": "execution_provider_unavailable",
    "filesystem_roots": "execution_filesystem_isolation_unsupported",
    "network_deny": "execution_network_policy_unsupported",
    "network_allowlist": "execution_network_policy_unsupported",
    "environment_isolation": "execution_environment_isolation_unsupported",
    "argv_policy": "execution_argv_policy_unsupported",
    "process_tree_control": "execution_process_tree_unsupported",
    "child_process_allowlist": "execution_child_process_policy_unsupported",
    "resource_limits": "execution_resource_limits_unsupported",
    "memory_limits": "execution_resource_limits_unsupported",
    "cpu_limits": "execution_resource_limits_unsupported",
    "process_limits": "execution_resource_limits_unsupported",
    "timeout": "execution_timeout_unsupported",
    "cancellation": "execution_cancellation_unsupported",
    "termination_confirmation": "execution_termination_confirmation_unsupported",
    "secret_handle_injection": "execution_secret_handles_unsupported",
})


def capability_denial_code(capability: str) -> str:
    """Return the stable operator-facing denial code for one capability."""

    normalized = str(capability).strip()
    return _CAPABILITY_DENIAL_CODES.get(
        normalized,
        "execution_capability_unsupported",
    )


@dataclass(frozen=True, slots=True)
class NetworkEndpoint:
    host: str
    port: int

    def __post_init__(self) -> None:
        host = str(self.host).strip().casefold()
        if not host or len(host) > 253 or any(character.isspace() for character in host):
            raise ValueError("network endpoint host is invalid")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("network endpoint port must be between 1 and 65535")
        object.__setattr__(self, "host", host)

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port}


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    mode: NetworkPolicyMode | str = NetworkPolicyMode.DENY
    allowlist: tuple[NetworkEndpoint | Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        mode = NetworkPolicyMode(self.mode)
        endpoints = tuple(
            endpoint
            if isinstance(endpoint, NetworkEndpoint)
            else NetworkEndpoint(**dict(endpoint))
            for endpoint in self.allowlist
        )
        if mode is NetworkPolicyMode.DENY and endpoints:
            raise ValueError("network deny policy cannot contain an allowlist")
        if mode is NetworkPolicyMode.ALLOWLIST and not endpoints:
            raise ValueError("network allowlist policy requires at least one endpoint")
        if len(endpoints) > 64:
            raise ValueError("network allowlist exceeds its bounded item limit")
        unique = {(item.host, item.port) for item in endpoints}
        if len(unique) != len(endpoints):
            raise ValueError("network allowlist contains duplicate endpoints")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "allowlist",
            tuple(sorted(endpoints, key=lambda item: (item.host, item.port))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "allowlist": [item.to_dict() for item in self.allowlist],
        }


@dataclass(frozen=True, slots=True)
class ProcessPolicy:
    """Initial command admission and bounded child-process policy."""

    allowed_argv_prefixes: tuple[tuple[str, ...], ...] = ()
    allowed_child_argv_prefixes: tuple[tuple[str, ...], ...] = ()
    max_processes: int = 1
    require_child_process_allowlist: bool = False

    def __post_init__(self) -> None:
        prefixes: list[tuple[str, ...]] = []
        for raw_prefix in self.allowed_argv_prefixes:
            if isinstance(raw_prefix, (str, bytes)) or not isinstance(raw_prefix, Sequence):
                raise TypeError("allowed_argv_prefixes must contain argv sequences")
            prefix = tuple(_command_token(value, "allowed argv prefix") for value in raw_prefix)
            if not prefix:
                raise ValueError("allowed argv prefix cannot be empty")
            prefixes.append(prefix)
        if len(prefixes) > 32:
            raise ValueError("allowed_argv_prefixes exceeds its bounded item limit")
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("allowed_argv_prefixes contains duplicates")
        child_prefixes: list[tuple[str, ...]] = []
        for raw_prefix in self.allowed_child_argv_prefixes:
            if isinstance(raw_prefix, (str, bytes)) or not isinstance(raw_prefix, Sequence):
                raise TypeError("allowed_child_argv_prefixes must contain argv sequences")
            prefix = tuple(_command_token(value, "allowed child argv prefix") for value in raw_prefix)
            if not prefix:
                raise ValueError("allowed child argv prefix cannot be empty")
            child_prefixes.append(prefix)
        if len(child_prefixes) > 32:
            raise ValueError("allowed_child_argv_prefixes exceeds its bounded item limit")
        if len(set(child_prefixes)) != len(child_prefixes):
            raise ValueError("allowed_child_argv_prefixes contains duplicates")
        if isinstance(self.max_processes, bool) or not isinstance(self.max_processes, int):
            raise TypeError("max_processes must be an integer")
        if not 1 <= self.max_processes <= 256:
            raise ValueError("max_processes must be between 1 and 256")
        if self.max_processes > 1 and not self.require_child_process_allowlist:
            raise ValueError(
                "max_processes above one requires an explicit child process allowlist"
            )
        if self.require_child_process_allowlist and not child_prefixes:
            raise ValueError("child process allowlist requires explicit child argv prefixes")
        object.__setattr__(self, "allowed_argv_prefixes", tuple(sorted(prefixes)))
        object.__setattr__(self, "allowed_child_argv_prefixes", tuple(sorted(child_prefixes)))

    def admits(self, argv: Sequence[str]) -> bool:
        supplied = tuple(argv)
        return any(supplied[: len(prefix)] == prefix for prefix in self.allowed_argv_prefixes)

    def admits_child(self, argv: Sequence[str]) -> bool:
        supplied = tuple(argv)
        return any(
            supplied[: len(prefix)] == prefix
            for prefix in self.allowed_child_argv_prefixes
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_argv_prefixes": [list(item) for item in self.allowed_argv_prefixes],
            "allowed_child_argv_prefixes": [list(item) for item in self.allowed_child_argv_prefixes],
            "max_processes": self.max_processes,
            "require_child_process_allowlist": self.require_child_process_allowlist,
        }


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_memory_bytes: int | None = None
    max_cpu_seconds: float | None = None
    max_processes: int | None = None

    def __post_init__(self) -> None:
        if self.max_memory_bytes is not None:
            if isinstance(self.max_memory_bytes, bool) or not isinstance(self.max_memory_bytes, int):
                raise TypeError("max_memory_bytes must be an integer or None")
            if not 1_048_576 <= self.max_memory_bytes <= 1 << 50:
                raise ValueError("max_memory_bytes is outside the supported range")
        if self.max_cpu_seconds is not None:
            _positive_finite(self.max_cpu_seconds, "max_cpu_seconds")
        if self.max_processes is not None:
            if isinstance(self.max_processes, bool) or not isinstance(self.max_processes, int):
                raise TypeError("max_processes must be an integer or None")
            if not 1 <= self.max_processes <= 256:
                raise ValueError("max_processes must be between 1 and 256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_memory_bytes": self.max_memory_bytes,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_processes": self.max_processes,
        }


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """The static, versioned physical requirements of one tool class."""

    mode: ExecutionMode | str
    provider_id: str | None = None
    network_policy: NetworkPolicy | Mapping[str, Any] = field(default_factory=NetworkPolicy)
    process_policy: ProcessPolicy | Mapping[str, Any] = field(default_factory=ProcessPolicy)
    require_filesystem_isolation: bool = False
    require_environment_isolation: bool = False
    require_process_tree_control: bool = False
    require_resource_limits: bool = False
    require_termination_confirmation: bool = False
    schema_version: str = EXECUTION_PROFILE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_PROFILE_SCHEMA:
            raise ValueError(f"unsupported execution profile schema: {self.schema_version}")
        mode = ExecutionMode(self.mode)
        provider_id = _optional_identifier(self.provider_id, "provider_id")
        network_policy = (
            self.network_policy
            if isinstance(self.network_policy, NetworkPolicy)
            else NetworkPolicy(**dict(self.network_policy))
        )
        process_policy = (
            self.process_policy
            if isinstance(self.process_policy, ProcessPolicy)
            else ProcessPolicy(**dict(self.process_policy))
        )
        requirement_values = (
            self.require_filesystem_isolation,
            self.require_environment_isolation,
            self.require_process_tree_control,
            self.require_resource_limits,
            self.require_termination_confirmation,
        )
        if any(not isinstance(value, bool) for value in requirement_values):
            raise TypeError("execution profile requirements must be booleans")
        if mode is ExecutionMode.TRUSTED_IN_PROCESS:
            if provider_id is not None:
                raise ValueError("trusted_in_process profile cannot select a provider")
            if any(requirement_values):
                raise ValueError("trusted_in_process profile cannot request physical isolation")
            if network_policy.mode is not NetworkPolicyMode.DENY or network_policy.allowlist:
                raise ValueError("trusted_in_process profile cannot declare network access")
            if (
                process_policy.allowed_argv_prefixes
                or process_policy.allowed_child_argv_prefixes
                or process_policy.max_processes != 1
                or process_policy.require_child_process_allowlist
            ):
                raise ValueError("trusted_in_process profile cannot declare a process policy")
        else:
            if provider_id is None:
                raise ValueError(
                    f"{mode.value} profile requires an explicit provider_id"
                )
            if not (
                self.require_environment_isolation
                and self.require_process_tree_control
                and self.require_termination_confirmation
            ):
                raise ValueError(
                    f"{mode.value} profile must require environment isolation, "
                    "process-tree control, and termination confirmation"
                )
            if not process_policy.allowed_argv_prefixes:
                raise ValueError(
                    f"{mode.value} profile requires allowed argv prefixes"
                )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "network_policy", network_policy)
        object.__setattr__(self, "process_policy", process_policy)

    @classmethod
    def trusted_in_process(cls) -> "ExecutionProfile":
        return cls(mode=ExecutionMode.TRUSTED_IN_PROCESS)

    @classmethod
    def sandboxed_process(
        cls,
        *,
        provider_id: str,
        allowed_argv_prefixes: Sequence[Sequence[str]],
        network_policy: NetworkPolicy | Mapping[str, Any] | None = None,
        require_filesystem_isolation: bool = True,
        require_resource_limits: bool = True,
        max_processes: int = 1,
        require_child_process_allowlist: bool = False,
        allowed_child_argv_prefixes: Sequence[Sequence[str]] = (),
    ) -> "ExecutionProfile":
        return cls(
            mode=ExecutionMode.SANDBOXED_PROCESS,
            provider_id=provider_id,
            network_policy=network_policy or NetworkPolicy(),
            process_policy=ProcessPolicy(
                allowed_argv_prefixes=tuple(tuple(item) for item in allowed_argv_prefixes),
                allowed_child_argv_prefixes=tuple(tuple(item) for item in allowed_child_argv_prefixes),
                max_processes=max_processes,
                require_child_process_allowlist=require_child_process_allowlist,
            ),
            require_filesystem_isolation=require_filesystem_isolation,
            require_environment_isolation=True,
            require_process_tree_control=True,
            require_resource_limits=require_resource_limits,
            require_termination_confirmation=True,
        )

    @classmethod
    def external_process(
        cls,
        *,
        provider_id: str,
        allowed_argv_prefixes: Sequence[Sequence[str]],
        network_policy: NetworkPolicy | Mapping[str, Any] | None = None,
        require_filesystem_isolation: bool = True,
        require_resource_limits: bool = True,
        max_processes: int = 1,
        require_child_process_allowlist: bool = False,
        allowed_child_argv_prefixes: Sequence[Sequence[str]] = (),
    ) -> "ExecutionProfile":
        """Declare an external process with the same fail-closed controls.

        ``external_process`` is intentionally a distinct profile identity so
        callers cannot silently reinterpret a parser, sidecar, or child
        process as a generic sandboxed tool.  Physical admission remains
        provider-owned and uses the same capability contract.
        """

        return cls(
            mode=ExecutionMode.EXTERNAL_PROCESS,
            provider_id=provider_id,
            network_policy=network_policy or NetworkPolicy(),
            process_policy=ProcessPolicy(
                allowed_argv_prefixes=tuple(tuple(item) for item in allowed_argv_prefixes),
                allowed_child_argv_prefixes=tuple(
                    tuple(item) for item in allowed_child_argv_prefixes
                ),
                max_processes=max_processes,
                require_child_process_allowlist=require_child_process_allowlist,
            ),
            require_filesystem_isolation=require_filesystem_isolation,
            require_environment_isolation=True,
            require_process_tree_control=True,
            require_resource_limits=require_resource_limits,
            require_termination_confirmation=True,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionProfile":
        if not isinstance(value, Mapping):
            raise TypeError("execution_profile must be an object")
        expected = {
            "schema_version",
            "mode",
            "provider_id",
            "network_policy",
            "process_policy",
            "require_filesystem_isolation",
            "require_environment_isolation",
            "require_process_tree_control",
            "require_resource_limits",
            "require_termination_confirmation",
        }
        unknown = sorted(set(value) - expected)
        if unknown:
            raise ValueError(f"execution_profile contains unknown fields: {unknown}")
        if value.get("schema_version") != EXECUTION_PROFILE_SCHEMA:
            raise ValueError(
                "execution_profile schema_version must be "
                f"{EXECUTION_PROFILE_SCHEMA!r}"
            )
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "provider_id": self.provider_id,
            "network_policy": self.network_policy.to_dict(),
            "process_policy": self.process_policy.to_dict(),
            "require_filesystem_isolation": self.require_filesystem_isolation,
            "require_environment_isolation": self.require_environment_isolation,
            "require_process_tree_control": self.require_process_tree_control,
            "require_resource_limits": self.require_resource_limits,
            "require_termination_confirmation": self.require_termination_confirmation,
        }


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """One admitted sandboxed-process invocation without raw secret material."""

    execution_id: str
    tool_id: str
    graph_identity: GraphExecutionIdentity | Mapping[str, Any]
    operation_id: str
    attempt_id: str
    profile: ExecutionProfile | Mapping[str, Any]
    image: str
    argv: tuple[str, ...]
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    working_directory: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    secret_handles: tuple[str, ...] = ()
    resource_limits: ResourceLimits | Mapping[str, Any] = field(default_factory=ResourceLimits)
    timeout_seconds: float | None = None
    cancellation_grace_seconds: float = 5.0
    approval_evidence_ref: str | None = None
    budget_ref: str | None = None
    requested_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name in ("execution_id", "tool_id", "operation_id", "attempt_id"):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        identity = self.graph_identity
        if not isinstance(identity, GraphExecutionIdentity):
            identity = GraphExecutionIdentity.from_dict(identity)
        profile = self.profile
        if not isinstance(profile, ExecutionProfile):
            profile = ExecutionProfile.from_dict(profile)
        if profile.mode not in {
            ExecutionMode.SANDBOXED_PROCESS,
            ExecutionMode.EXTERNAL_PROCESS,
        }:
            raise ValueError(
                "ExecutionRequest requires a sandboxed_process or external_process profile"
            )
        image = str(self.image).strip()
        if not image or len(image) > 512 or any(character.isspace() for character in image):
            raise ValueError("sandbox image is invalid")
        argv = tuple(_command_token(value, "argv") for value in self.argv)
        if not argv:
            raise ValueError("sandbox execution requires argv")
        if not profile.process_policy.admits(argv):
            raise ValueError("argv is not admitted by the execution profile")
        read_roots = _root_paths(self.read_roots, "read_roots")
        write_roots = _root_paths(self.write_roots, "write_roots")
        if set(read_roots).intersection(write_roots):
            raise ValueError("read_roots and write_roots must not overlap")
        if profile.require_filesystem_isolation and not (read_roots or write_roots):
            raise ValueError("sandbox profile requires at least one declared filesystem root")
        working_directory = (
            _root_path(self.working_directory, "working_directory")
            if self.working_directory is not None
            else None
        )
        if working_directory is not None and working_directory not in {*read_roots, *write_roots}:
            raise ValueError("working_directory must be one declared root")
        environment = _environment(self.environment)
        secret_handles = tuple(
            _identifier(value, "secret_handle") for value in self.secret_handles
        )
        if len(secret_handles) > 32 or len(set(secret_handles)) != len(secret_handles):
            raise ValueError("secret_handles must be unique and bounded")
        resource_limits = (
            self.resource_limits
            if isinstance(self.resource_limits, ResourceLimits)
            else ResourceLimits(**dict(self.resource_limits))
        )
        if profile.require_resource_limits and resource_limits == ResourceLimits():
            raise ValueError("sandbox profile requires explicit resource limits")
        if resource_limits.max_processes is not None and resource_limits.max_processes > profile.process_policy.max_processes:
            raise ValueError("resource max_processes exceeds process policy")
        timeout_seconds = self.timeout_seconds
        if timeout_seconds is not None:
            _positive_finite(timeout_seconds, "timeout_seconds")
            timeout_seconds = float(timeout_seconds)
        _positive_finite(self.cancellation_grace_seconds, "cancellation_grace_seconds", allow_zero=True)
        requested_at = _utc_time(self.requested_at, "requested_at")
        object.__setattr__(self, "graph_identity", identity)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "image", image)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "read_roots", read_roots)
        object.__setattr__(self, "write_roots", write_roots)
        object.__setattr__(self, "working_directory", working_directory)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "secret_handles", secret_handles)
        object.__setattr__(self, "resource_limits", resource_limits)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "cancellation_grace_seconds", float(self.cancellation_grace_seconds))
        object.__setattr__(
            self,
            "approval_evidence_ref",
            _optional_identifier(self.approval_evidence_ref, "approval_evidence_ref"),
        )
        object.__setattr__(self, "budget_ref", _optional_identifier(self.budget_ref, "budget_ref"))
        object.__setattr__(self, "requested_at", requested_at)

    def to_operator_projection(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "tool_id": self.tool_id,
            "graph_identity": self.graph_identity.to_dict(),
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "profile": self.profile.to_dict(),
            "image_ref": _checksum(self.image),
            "argv_checksum": _checksum(list(self.argv)),
            "read_root_count": len(self.read_roots),
            "write_root_count": len(self.write_roots),
            "working_directory_declared": self.working_directory is not None,
            "environment_names": sorted(self.environment),
            "secret_handle_count": len(self.secret_handles),
            "resource_limits": self.resource_limits.to_dict(),
            "timeout_seconds": self.timeout_seconds,
            "cancellation_grace_seconds": self.cancellation_grace_seconds,
            "approval_evidence_ref": self.approval_evidence_ref,
            "budget_ref": self.budget_ref,
            "requested_at": format_datetime(self.requested_at),
        }


@dataclass(frozen=True, slots=True)
class ExecutionCapabilityProfile:
    provider_id: str
    available: bool
    enforces_filesystem_roots: bool = False
    enforces_network_deny: bool = False
    enforces_network_allowlist: bool = False
    isolates_environment: bool = False
    enforces_argv_policy: bool = False
    controls_process_tree: bool = False
    enforces_child_process_allowlist: bool = False
    enforces_resource_limits: bool = False
    enforces_memory_limits: bool = False
    enforces_cpu_limits: bool = False
    enforces_process_limits: bool = False
    confirms_termination: bool = False
    supports_secret_handles: bool = False
    enforces_timeout: bool = False
    supports_cancellation: bool = False
    version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _identifier(self.provider_id, "provider_id"))
        object.__setattr__(self, "version", _identifier(self.version, "version"))
        for field_name in (
            "available",
            "enforces_filesystem_roots",
            "enforces_network_deny",
            "enforces_network_allowlist",
            "isolates_environment",
            "enforces_argv_policy",
            "controls_process_tree",
            "enforces_child_process_allowlist",
            "enforces_resource_limits",
            "enforces_memory_limits",
            "enforces_cpu_limits",
            "enforces_process_limits",
            "enforces_timeout",
            "supports_cancellation",
            "confirms_termination",
            "supports_secret_handles",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be boolean")

    @property
    def checksum(self) -> str:
        return _checksum(self.to_dict())

    def missing_for(self, request: ExecutionRequest) -> tuple[str, ...]:
        missing: list[str] = []
        profile = request.profile
        if not self.available:
            missing.append("provider_unavailable")
        if profile.require_filesystem_isolation and not self.enforces_filesystem_roots:
            missing.append("filesystem_roots")
        if profile.network_policy.mode is NetworkPolicyMode.DENY and not self.enforces_network_deny:
            missing.append("network_deny")
        if profile.network_policy.mode is NetworkPolicyMode.ALLOWLIST and not self.enforces_network_allowlist:
            missing.append("network_allowlist")
        if profile.require_environment_isolation and not self.isolates_environment:
            missing.append("environment_isolation")
        if not self.enforces_argv_policy:
            missing.append("argv_policy")
        if profile.require_process_tree_control and not self.controls_process_tree:
            missing.append("process_tree_control")
        # A provider must prove child executable admission whenever the
        # process tree can contain more than the initial process.  The
        # initial argv prefix alone cannot constrain a spawned shell or child
        # executable, so accepting this profile would silently widen the
        # physical boundary.
        if (
            profile.process_policy.max_processes > 1
            or profile.process_policy.require_child_process_allowlist
        ) and not self.enforces_child_process_allowlist:
            missing.append("child_process_allowlist")
        if profile.require_resource_limits:
            limits = request.resource_limits
            if limits.max_memory_bytes is not None and not self.enforces_memory_limits:
                missing.append("memory_limits")
            if limits.max_cpu_seconds is not None and not self.enforces_cpu_limits:
                missing.append("cpu_limits")
            if limits.max_processes is not None and not self.enforces_process_limits:
                missing.append("process_limits")
            if (
                limits.max_memory_bytes is None
                and limits.max_cpu_seconds is None
                and limits.max_processes is None
                and not self.enforces_resource_limits
            ):
                missing.append("resource_limits")
        if profile.require_termination_confirmation and not self.confirms_termination:
            missing.append("termination_confirmation")
        # A timeout is only safe when the provider can both enforce the
        # deadline and interrupt the running process.  The request's default
        # cancellation grace remains a request policy; it does not itself
        # make cancellation a required capability for unbounded executions.
        if request.timeout_seconds is not None:
            if not self.enforces_timeout:
                missing.append("timeout")
            if not self.supports_cancellation:
                missing.append("cancellation")
        if request.secret_handles and not self.supports_secret_handles:
            missing.append("secret_handle_injection")
        return tuple(missing)

    def admission_diagnostics(self, request: ExecutionRequest) -> dict[str, Any]:
        """Describe capability admission without exposing request contents.

        The returned shape is deliberately stable and suitable for operator
        projections.  ``missing`` remains the low-level capability vocabulary;
        ``denials`` provides versioned, coarser denial codes for callers.
        """

        missing = self.missing_for(request)
        denials = [
            {
                "capability": capability,
                "denial_code": capability_denial_code(capability),
            }
            for capability in missing
        ]
        diagnostics: dict[str, Any] = {
            "status": "admitted" if not missing else "rejected",
            "denial_code_version": CAPABILITY_DENIAL_CODE_VERSION,
            "provider_id": self.provider_id,
            "provider_capability_version": self.version,
            "provider_capability_checksum": self.checksum,
            "missing": list(missing),
            "denials": denials,
        }
        if denials:
            # Provider availability is the primary admission failure: other
            # advertised controls are not actionable until a provider exists.
            if "provider_unavailable" in missing:
                diagnostics["denial_code"] = capability_denial_code(
                    "provider_unavailable"
                )
            elif len(denials) == 1:
                diagnostics["denial_code"] = denials[0]["denial_code"]
            else:
                diagnostics["denial_code"] = "execution_capability_admission_denied"
        return diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "available": self.available,
            "enforces_filesystem_roots": self.enforces_filesystem_roots,
            "enforces_network_deny": self.enforces_network_deny,
            "enforces_network_allowlist": self.enforces_network_allowlist,
            "isolates_environment": self.isolates_environment,
            "enforces_argv_policy": self.enforces_argv_policy,
            "controls_process_tree": self.controls_process_tree,
            "enforces_child_process_allowlist": self.enforces_child_process_allowlist,
            "enforces_resource_limits": self.enforces_resource_limits,
            "enforces_memory_limits": self.enforces_memory_limits,
            "enforces_cpu_limits": self.enforces_cpu_limits,
            "enforces_process_limits": self.enforces_process_limits,
            "enforces_timeout": self.enforces_timeout,
            "supports_cancellation": self.supports_cancellation,
            "confirms_termination": self.confirms_termination,
            "supports_secret_handles": self.supports_secret_handles,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    execution_id: str
    tool_id: str
    graph_identity: GraphExecutionIdentity | Mapping[str, Any]
    operation_id: str
    attempt_id: str
    provider_id: str
    provider_capability_checksum: str
    status: ExecutionStatus | str
    started_at: datetime
    finished_at: datetime
    termination_confirmed: bool
    reason_code: str
    exit_code: int | None = None
    output_checksum: str | None = None
    output_bytes: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "execution_id",
            "tool_id",
            "operation_id",
            "attempt_id",
            "provider_id",
            "reason_code",
        ):
            object.__setattr__(self, field_name, _identifier(getattr(self, field_name), field_name))
        identity = self.graph_identity
        if not isinstance(identity, GraphExecutionIdentity):
            identity = GraphExecutionIdentity.from_dict(identity)
        checksum = str(self.provider_capability_checksum).strip().lower()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum):
            raise ValueError("provider_capability_checksum must be a sha256 checksum")
        status = ExecutionStatus(self.status)
        started_at = _utc_time(self.started_at, "started_at")
        finished_at = _utc_time(self.finished_at, "finished_at")
        if finished_at < started_at:
            raise ValueError("finished_at cannot precede started_at")
        if not isinstance(self.termination_confirmed, bool):
            raise TypeError("termination_confirmed must be boolean")
        if self.exit_code is not None and (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)):
            raise TypeError("exit_code must be an integer or None")
        if status in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.REJECTED,
            ExecutionStatus.TIMED_OUT,
            ExecutionStatus.CANCELLED,
        } and not self.termination_confirmed:
            raise ValueError(f"{status.value} receipt requires termination_confirmed")
        if status is ExecutionStatus.INDETERMINATE and self.termination_confirmed:
            raise ValueError("indeterminate receipt cannot claim confirmed termination")
        output_checksum = _optional_checksum(self.output_checksum, "output_checksum")
        if self.output_bytes is not None:
            if isinstance(self.output_bytes, bool) or not isinstance(self.output_bytes, int):
                raise TypeError("output_bytes must be an integer or None")
            if not 0 <= self.output_bytes <= 128 * 1024 * 1024:
                raise ValueError("output_bytes is outside the bounded limit")
            if output_checksum is None:
                raise ValueError("output_bytes requires output_checksum")
        object.__setattr__(self, "graph_identity", identity)
        object.__setattr__(self, "provider_capability_checksum", checksum)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "finished_at", finished_at)
        object.__setattr__(self, "output_checksum", output_checksum)

    def matches_request(self, request: ExecutionRequest) -> bool:
        return (
            self.execution_id == request.execution_id
            and self.tool_id == request.tool_id
            and self.graph_identity == request.graph_identity
            and self.operation_id == request.operation_id
            and self.attempt_id == request.attempt_id
            and self.provider_id == request.profile.provider_id
        )

    @property
    def receipt_checksum(self) -> str:
        """Checksum of the complete operator-visible receipt projection."""
        return _checksum(self.checksum_projection())

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "tool_id": self.tool_id,
            "graph_identity": self.graph_identity.to_dict(),
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "provider_id": self.provider_id,
            "provider_capability_checksum": self.provider_capability_checksum,
            "status": self.status.value,
            "started_at": format_datetime(self.started_at),
            "finished_at": format_datetime(self.finished_at),
            "termination_confirmed": self.termination_confirmed,
            "reason_code": self.reason_code,
            "exit_code": self.exit_code,
            "output_checksum": self.output_checksum,
            "output_bytes": self.output_bytes,
        }

    def to_operator_projection(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "receipt_checksum": self.receipt_checksum,
        }


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """In-memory provider result; only its receipt is safe for ordinary events."""

    receipt: ExecutionReceipt
    output: bytes | str | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ExecutionReceipt):
            raise TypeError("receipt must be ExecutionReceipt")
        if self.output is not None and not isinstance(self.output, (bytes, str)):
            raise TypeError("execution output must be bytes, string, or None")
        if self.diagnostic is not None:
            diagnostic = str(self.diagnostic).strip()
            if len(diagnostic) > 1024:
                diagnostic = diagnostic[:1024]
            object.__setattr__(self, "diagnostic", diagnostic or None)


def _identifier(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized or _IDENTIFIER.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _command_token(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 4096 or "\x00" in normalized:
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _root_paths(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{field_name} must be a sequence")
    roots = tuple(_root_path(value, field_name) for value in values)
    if len(roots) > 32:
        raise ValueError(f"{field_name} exceeds its bounded item limit")
    if len(set(roots)) != len(roots):
        raise ValueError(f"{field_name} contains duplicate paths")
    return tuple(sorted(roots))


def _root_path(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must contain strings")
    normalized = value.strip()
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{field_name} contains an invalid path")
    if _UNC_PATH.match(normalized) or _WINDOWS_DRIVE_RELATIVE.match(normalized):
        raise ValueError(f"{field_name} contains a forbidden path form")
    if ".." in normalized.replace("\\", "/").split("/"):
        raise ValueError(f"{field_name} cannot contain traversal")
    if not (normalized.startswith("/") or _WINDOWS_ABSOLUTE.match(normalized)):
        raise ValueError(f"{field_name} must contain absolute roots")
    return normalized.rstrip("\\/") or normalized


def _environment(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("environment must be an object")
    normalized: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = str(raw_name).strip()
        if _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError("environment contains an invalid variable name")
        if _SECRET_LIKE_ENVIRONMENT_NAME.search(name):
            raise ValueError("secret-like environment variables must use named secret handles")
        if _PROTECTED_ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError("protected environment variables cannot be overridden")
        if not isinstance(raw_value, str) or "\x00" in raw_value or len(raw_value) > 8192:
            raise ValueError("environment contains an invalid variable value")
        normalized[name] = raw_value
    if len(normalized) > 64:
        raise ValueError("environment exceeds its bounded item limit")
    return dict(sorted(normalized.items()))


def _positive_finite(value: Any, field_name: str, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field_name} must be finite")
    threshold = 0 if allow_zero else 0
    if float(value) < threshold or (not allow_zero and float(value) == 0):
        raise ValueError(f"{field_name} must be positive")


def _utc_time(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def _optional_checksum(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise ValueError(f"{field_name} must be a sha256 checksum")
    return normalized


__all__ = [
    "CAPABILITY_DENIAL_CODE_VERSION",
    "EXECUTION_PROFILE_SCHEMA",
    "ExecutionCapabilityProfile",
    "ExecutionMode",
    "ExecutionOutcome",
    "ExecutionProfile",
    "ExecutionReceipt",
    "ExecutionRequest",
    "ExecutionStatus",
    "capability_denial_code",
    "NetworkEndpoint",
    "NetworkPolicy",
    "NetworkPolicyMode",
    "ProcessPolicy",
    "ResourceLimits",
]
