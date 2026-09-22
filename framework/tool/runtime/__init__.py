from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.tool.runtime.errors import (
    ToolDefinitionError,
    ToolIndeterminateError,
    ToolPermissionError,
    ToolRuntimeError,
    ToolSecretError,
    ToolTimeoutError,
)

if TYPE_CHECKING:
    from framework.tool.runtime.batch_executor import ToolBatchExecutor
    from framework.tool.runtime.evidence import (
        ToolEvidencePersistenceError,
        ToolExecutionEvidencePort,
        ToolRunnerTurnEvidence,
    )
    from framework.tool.runtime.http_adapter import (
        HTTPClientProtocol,
        TraceAwareHTTPToolTransport,
    )
    from framework.tool.runtime.mcp_adapter import MCPServerConfig, MCPToolAdapter
    from framework.tool.runtime.retry import ToolRetryController
    from framework.tool.runtime.sandbox import ToolSandbox
    from framework.tool.runtime.timeout import ToolTimeoutRunner, run_with_timeout

_LAZY_EXPORTS = {
    "HTTPClientProtocol": "framework.tool.runtime.http_adapter",
    "MCPServerConfig": "framework.tool.runtime.mcp_adapter",
    "MCPToolAdapter": "framework.tool.runtime.mcp_adapter",
    "ToolBatchExecutor": "framework.tool.runtime.batch_executor",
    "ToolExecutor": "framework.tool.runtime.executor",
    "ToolEvidencePersistenceError": "framework.tool.runtime.evidence",
    "ToolExecutionEvidencePort": "framework.tool.runtime.evidence",
    "ToolRunnerTurnEvidence": "framework.tool.runtime.evidence",
    "ToolRetryController": "framework.tool.runtime.retry",
    "ToolSandbox": "framework.tool.runtime.sandbox",
    "ToolTimeoutRunner": "framework.tool.runtime.timeout",
    "TraceAwareHTTPToolTransport": "framework.tool.runtime.http_adapter",
    "run_with_timeout": "framework.tool.runtime.timeout",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = [
    "HTTPClientProtocol",
    "MCPServerConfig",
    "MCPToolAdapter",
    "ToolBatchExecutor",
    "ToolDefinitionError",
    "ToolExecutor",
    "ToolEvidencePersistenceError",
    "ToolExecutionEvidencePort",
    "ToolIndeterminateError",
    "ToolPermissionError",
    "ToolRetryController",
    "ToolRuntimeError",
    "ToolRunnerTurnEvidence",
    "ToolSandbox",
    "ToolSecretError",
    "ToolTimeoutError",
    "ToolTimeoutRunner",
    "TraceAwareHTTPToolTransport",
    "run_with_timeout",
]
