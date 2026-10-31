from __future__ import annotations

from collections.abc import Callable
from time import perf_counter
from typing import Any

from framework.tool.models.artifact_ref import ArtifactRef
from framework.tool.models.call import ToolCall
from framework.tool.models.definition import ToolDefinition
from framework.tool.models.observation import ToolObservation
from framework.tool.models.policy import ToolPolicy, is_default_dangerous_tool_name
from framework.tool.models.result import ToolPolicyTrace, ToolResult
from framework.tool.models.result_envelope import (
    ToolResultEnvelope,
    ToolSideEffectReceipt,
)
from framework.tool.models.result_persistence import ToolResultPersistenceContract
from framework.tool.models.status import ToolSideEffect, ToolStatus
from framework.tool.runtime.errors import (
    ToolDefinitionError,
    ToolEffectNotAppliedError,
    ToolIndeterminateError,
    ToolPermissionError,
    ToolRuntimeError,
    ToolSecretError,
    ToolTimeoutError,
)

ToolExecutorFn = Callable[[dict[str, Any]], Any]


def timed_tool_call(function: Callable[[], ToolResult]) -> tuple[ToolResult, float]:
    start = perf_counter()
    result = function()
    return result, (perf_counter() - start) * 1000


__all__ = [
    "ArtifactRef",
    "ToolCall",
    "ToolDefinition",
    "ToolDefinitionError",
    "ToolExecutorFn",
    "ToolEffectNotAppliedError",
    "ToolIndeterminateError",
    "ToolObservation",
    "ToolPermissionError",
    "ToolPolicy",
    "ToolPolicyTrace",
    "ToolResult",
    "ToolResultEnvelope",
    "ToolResultPersistenceContract",
    "ToolSideEffectReceipt",
    "ToolRuntimeError",
    "ToolSecretError",
    "ToolSideEffect",
    "ToolStatus",
    "ToolTimeoutError",
    "is_default_dangerous_tool_name",
    "timed_tool_call",
]
