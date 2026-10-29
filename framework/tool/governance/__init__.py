from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.tool.governance.redaction import (
    REDACTED_VALUE,
    ToolRedactor,
    contains_redacted_value,
    redact_sensitive_values,
)

if TYPE_CHECKING:
    from framework.tool.governance.approval import (
        ApprovalRequest,
        ToolApprovalAuthority,
        ToolApprovalBinding,
        ToolApprovalDecision,
        ToolApprovalRequest,
        ToolApprovalResolution,
        ToolApprovalStore,
        ToolApprovalVerdict,
        tool_arguments_digest,
        tool_call_from_approval,
    )
    from framework.tool.governance.boundary import (
        AgentToolBoundaryFinding,
        AgentToolBoundaryReport,
        audit_agent_spec_tool_boundary,
        audit_agent_tool_boundary,
        harden_restricted_agent_tool_policy,
        is_external_fetch_tool,
        is_restricted_agent_id,
    )
    from framework.tool.governance.guardrails import ToolGuardrail, ToolGuardrailChain
    from framework.tool.governance.risk import ToolRiskClassifier, ToolRiskLevel
    from framework.tool.governance.secrets import (
        EnvironmentSecretProvider,
        MappingSecretProvider,
        SecretProvider,
    )

_LAZY_EXPORTS = {
    "ApprovalRequest": "framework.tool.governance.approval",
    "AgentToolBoundaryFinding": "framework.tool.governance.boundary",
    "AgentToolBoundaryReport": "framework.tool.governance.boundary",
    "EnvironmentSecretProvider": "framework.tool.governance.secrets",
    "MappingSecretProvider": "framework.tool.governance.secrets",
    "SecretProvider": "framework.tool.governance.secrets",
    "ToolApprovalAuthority": "framework.tool.governance.approval",
    "ToolApprovalBinding": "framework.tool.governance.approval",
    "ToolApprovalDecision": "framework.tool.governance.approval",
    "ToolApprovalRequest": "framework.tool.governance.approval",
    "ToolApprovalResolution": "framework.tool.governance.approval",
    "ToolApprovalStore": "framework.tool.governance.approval",
    "ToolApprovalVerdict": "framework.tool.governance.approval",
    "tool_arguments_digest": "framework.tool.governance.approval",
    "tool_call_from_approval": "framework.tool.governance.approval",
    "ToolGuardrail": "framework.tool.governance.guardrails",
    "ToolGuardrailChain": "framework.tool.governance.guardrails",
    "ToolRiskClassifier": "framework.tool.governance.risk",
    "ToolRiskLevel": "framework.tool.governance.risk",
    "audit_agent_spec_tool_boundary": "framework.tool.governance.boundary",
    "audit_agent_tool_boundary": "framework.tool.governance.boundary",
    "harden_restricted_agent_tool_policy": "framework.tool.governance.boundary",
    "is_external_fetch_tool": "framework.tool.governance.boundary",
    "is_restricted_agent_id": "framework.tool.governance.boundary",
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
    "ApprovalRequest",
    "AgentToolBoundaryFinding",
    "AgentToolBoundaryReport",
    "EnvironmentSecretProvider",
    "MappingSecretProvider",
    "REDACTED_VALUE",
    "SecretProvider",
    "ToolApprovalAuthority",
    "ToolApprovalBinding",
    "ToolApprovalDecision",
    "ToolApprovalRequest",
    "ToolApprovalResolution",
    "ToolApprovalStore",
    "ToolApprovalVerdict",
    "ToolGuardrail",
    "ToolGuardrailChain",
    "ToolRedactor",
    "ToolRiskClassifier",
    "ToolRiskLevel",
    "audit_agent_spec_tool_boundary",
    "audit_agent_tool_boundary",
    "contains_redacted_value",
    "harden_restricted_agent_tool_policy",
    "is_external_fetch_tool",
    "is_restricted_agent_id",
    "redact_sensitive_values",
    "tool_arguments_digest",
    "tool_call_from_approval",
]
