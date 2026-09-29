from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from framework.tool.models.definition import ToolDefinition
from framework.tool.models.status import ToolSideEffect


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: list[str] = field(default_factory=list)
    blocked_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    require_approval_for: list[str] = field(default_factory=list)
    allow_network_access: bool = True
    max_result_bytes: int = 1_000_000
    default_timeout_seconds: float = 30.0
    allow_mcp_tools: bool = False
    max_tool_calls_per_iteration: int = 3
    max_tool_calls_per_agent: int = 20
    require_explicit_allowlist: bool = True
    allow_dangerous_tools: bool = False
    require_approval_for_side_effects: bool = True
    max_result_chars_inline: int = 8000
    spill_large_results_to_artifact: bool = True
    timeout_seconds_default: float | None = 30.0
    min_start_window_seconds: float = 0.0
    max_attempts_default: int = 1
    cancellation_grace_seconds: float = 0.1
    completion_reserve_seconds: float = 0.0
    max_total_retries: int | None = None

    def __post_init__(self) -> None:
        blocked = sorted({*self.blocked_tools, *self.denied_tools})
        object.__setattr__(self, "blocked_tools", blocked)
        object.__setattr__(self, "denied_tools", blocked)
        if self.timeout_seconds_default is None:
            object.__setattr__(self, "timeout_seconds_default", self.default_timeout_seconds)
        elif self.default_timeout_seconds == 30.0:
            object.__setattr__(self, "default_timeout_seconds", float(self.timeout_seconds_default))
        for name in (
            "min_start_window_seconds",
            "cancellation_grace_seconds",
            "completion_reserve_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.timeout_seconds_default is not None and (
            isinstance(self.timeout_seconds_default, bool)
            or not isinstance(self.timeout_seconds_default, (int, float))
            or not math.isfinite(float(self.timeout_seconds_default))
            or self.timeout_seconds_default <= 0
        ):
            raise ValueError("timeout_seconds_default must be finite and positive")
        if (
            self.timeout_seconds_default is not None
            and self.min_start_window_seconds > self.timeout_seconds_default
        ):
            raise ValueError(
                "min_start_window_seconds must not exceed timeout_seconds_default"
            )
        if type(self.max_attempts_default) is not int or self.max_attempts_default < 1:
            raise ValueError("max_attempts_default must be a positive integer")
        if self.max_total_retries is not None and (
            type(self.max_total_retries) is not int
            or self.max_total_retries < 0
        ):
            raise ValueError("max_total_retries must be a non-negative integer")

    def allows(self, tool_name: str) -> bool:
        if tool_name in self.blocked_tools:
            return False
        if str(tool_name).startswith("mcp.") and not self.allow_mcp_tools:
            return False
        if self.require_explicit_allowlist:
            return tool_name in self.allowed_tools
        return True

    def exposes(self, definition: ToolDefinition) -> bool:
        if not self.allows(definition.name):
            return False
        if _requires_network_access(definition) and not self.allow_network_access:
            return False
        if (
            (definition.is_dangerous or is_default_dangerous_tool_name(definition.name))
            and not self.allow_dangerous_tools
        ):
            return False
        return True

    def can_call(self, definition: ToolDefinition) -> tuple[bool, str | None]:
        if not self.allows(definition.name):
            return False, f"tool is not allowed: {definition.name}"
        if _requires_network_access(definition) and not self.allow_network_access:
            return False, f"network access is not allowed for tool: {definition.name}"
        if (
            (definition.is_dangerous or is_default_dangerous_tool_name(definition.name))
            and not self.allow_dangerous_tools
        ):
            return False, f"dangerous tool is not allowed: {definition.name}"
        if definition.max_result_bytes is not None and definition.max_result_bytes > self.max_result_bytes:
            return False, f"tool max_result_bytes exceeds policy for: {definition.name}"
        return True, None

    def requires_approval(self, definition: ToolDefinition) -> bool:
        # Explicit per-tool approval is a hard requirement. The policy switch
        # controls the default side-effect approval gate, including tool
        # definitions that declare approval as their default.
        if definition.name in self.require_approval_for:
            return True
        if not self.require_approval_for_side_effects:
            return False
        if (
            definition.requires_approval
            or definition.is_dangerous
            or is_default_dangerous_tool_name(definition.name)
        ):
            return True
        side_effect = definition.side_effect
        if isinstance(side_effect, ToolSideEffect):
            return side_effect.requires_approval()
        return _has_side_effects(str(side_effect))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_tools": list(self.allowed_tools),
            "blocked_tools": list(self.blocked_tools),
            "denied_tools": list(self.denied_tools),
            "require_approval_for": list(self.require_approval_for),
            "allow_network_access": self.allow_network_access,
            "allow_dangerous_tools": self.allow_dangerous_tools,
            "max_result_bytes": self.max_result_bytes,
            "default_timeout_seconds": self.default_timeout_seconds,
            "allow_mcp_tools": self.allow_mcp_tools,
            "max_tool_calls_per_iteration": self.max_tool_calls_per_iteration,
            "max_tool_calls_per_agent": self.max_tool_calls_per_agent,
            "require_explicit_allowlist": self.require_explicit_allowlist,
            "require_approval_for_side_effects": self.require_approval_for_side_effects,
            "max_result_chars_inline": self.max_result_chars_inline,
            "spill_large_results_to_artifact": self.spill_large_results_to_artifact,
            "timeout_seconds_default": self.timeout_seconds_default,
            "min_start_window_seconds": self.min_start_window_seconds,
            "max_attempts_default": self.max_attempts_default,
            "cancellation_grace_seconds": self.cancellation_grace_seconds,
            "completion_reserve_seconds": self.completion_reserve_seconds,
            "max_total_retries": self.max_total_retries,
        }

    @classmethod
    def from_any(cls, value: Any) -> "ToolPolicy":
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        data = value.to_dict() if hasattr(value, "to_dict") else value
        if isinstance(data, dict):
            if "max_total_attempts" in data:
                raise ValueError(
                    "legacy max_total_attempts requires explicit migration to "
                    "max_total_retries"
                )
            supported = cls().to_dict().keys()
            return cls(**{key: data[key] for key in supported if key in data})
        return cls(
            allowed_tools=[str(item) for item in getattr(value, "allowed_tools", [])],
            blocked_tools=[str(item) for item in getattr(value, "blocked_tools", [])],
            allow_mcp_tools=bool(getattr(value, "allow_mcp_tools", False)),
            denied_tools=[str(item) for item in getattr(value, "denied_tools", [])],
            require_approval_for=[str(item) for item in getattr(value, "require_approval_for", [])],
            allow_network_access=bool(getattr(value, "allow_network_access", True)),
            max_result_bytes=int(getattr(value, "max_result_bytes", 1_000_000)),
            default_timeout_seconds=float(getattr(value, "default_timeout_seconds", 30.0)),
            max_tool_calls_per_iteration=int(getattr(value, "max_tool_calls_per_iteration", 3)),
            max_tool_calls_per_agent=int(getattr(value, "max_tool_calls_per_agent", 20)),
            require_explicit_allowlist=bool(getattr(value, "require_explicit_allowlist", True)),
            allow_dangerous_tools=bool(getattr(value, "allow_dangerous_tools", False)),
            require_approval_for_side_effects=bool(getattr(value, "require_approval_for_side_effects", True)),
            max_result_chars_inline=int(getattr(value, "max_result_chars_inline", 8000)),
            spill_large_results_to_artifact=bool(getattr(value, "spill_large_results_to_artifact", True)),
            timeout_seconds_default=getattr(value, "timeout_seconds_default", 30.0),
            min_start_window_seconds=float(
                getattr(value, "min_start_window_seconds", 0.0)
            ),
            max_attempts_default=int(getattr(value, "max_attempts_default", 1)),
            cancellation_grace_seconds=float(
                getattr(value, "cancellation_grace_seconds", 0.1)
            ),
            completion_reserve_seconds=float(
                getattr(value, "completion_reserve_seconds", 0.0)
            ),
            max_total_retries=(
                int(getattr(value, "max_total_retries"))
                if getattr(value, "max_total_retries", None) is not None
                else None
            ),
        )


def is_default_dangerous_tool_name(tool_name: str) -> bool:
    normalized = str(tool_name).strip()
    dangerous_names = {
        "system.execute",
        "system.execute_command",
        "file.write",
        "file.delete",
        "postgres.query",
        "http.request",
        "generic_http_request",
        "publish.external",
        "notification.send",
    }
    dangerous_prefixes = ("postgres.", "notification.", "publish.")
    return normalized in dangerous_names or normalized.startswith(dangerous_prefixes)


def _requires_network_access(definition: ToolDefinition) -> bool:
    side_effect = definition.side_effect
    if side_effect == ToolSideEffect.NETWORK_ACCESS:
        return True
    return str(side_effect) == ToolSideEffect.NETWORK_ACCESS.value or definition.name.startswith(("web.", "mcp."))


def _has_side_effects(side_effect: str) -> bool:
    return side_effect not in {"", "none", "read_only", ToolSideEffect.READ_ONLY.value}
