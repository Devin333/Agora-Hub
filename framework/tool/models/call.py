from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.governance.redaction import redact_sensitive_values


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    requested_by_agent_id: str = ""
    call_id: str = field(default_factory=lambda: uuid4().hex)
    requested_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    graph_identity: GraphExecutionIdentity | None = None
    # Runtime-owned redemption handle for a human approval. It is set only by
    # trusted replay paths, never parsed from model-generated tool arguments.
    approval_id: str | None = None

    def __post_init__(self) -> None:
        requested_by = self.requested_by
        if requested_by is None and self.requested_by_agent_id:
            requested_by = self.requested_by_agent_id
        requested_by_agent_id = self.requested_by_agent_id or (requested_by or "")
        object.__setattr__(self, "requested_by", requested_by)
        object.__setattr__(self, "requested_by_agent_id", requested_by_agent_id)
        object.__setattr__(self, "arguments", dict(self.arguments or {}))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        identity = self.graph_identity
        if identity is not None and not isinstance(identity, GraphExecutionIdentity):
            identity = GraphExecutionIdentity.from_dict(identity)
        object.__setattr__(self, "graph_identity", identity)
        if self.approval_id is not None:
            approval_id = str(self.approval_id).strip()
            if not approval_id:
                raise ValueError("approval_id must be a non-empty string when provided")
            object.__setattr__(self, "approval_id", approval_id)

    @classmethod
    def new(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        requested_by: str | None = None,
        metadata: dict[str, Any] | None = None,
        graph_identity: GraphExecutionIdentity | dict[str, Any] | None = None,
    ) -> "ToolCall":
        return cls(
            tool_name=tool_name,
            arguments=dict(arguments),
            requested_by=requested_by,
            metadata=dict(metadata or {}),
            graph_identity=graph_identity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": redact_sensitive_values(dict(self.arguments)),
            "requested_by": self.requested_by,
            "requested_by_agent_id": self.requested_by_agent_id,
            "metadata": redact_sensitive_values(dict(self.metadata)),
            "graph_identity": (
                self.graph_identity.to_dict()
                if self.graph_identity is not None
                else None
            ),
            "approval_id": self.approval_id,
        }
