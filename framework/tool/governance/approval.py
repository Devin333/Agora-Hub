"""Tool-execution approvals: request records, call bindings and grant redemption.

An approval-gated tool call is executed only when it presents an
``approval_id`` whose stored record (a) was requested for exactly the same
tool, arguments and execution scope, (b) was approved or modified by a human
reviewer, (c) has not expired and (d) has not already been redeemed. The
grant is consumed *before* the side effect runs, giving at-most-once
execution per approval.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz
from enum import Enum
from hashlib import sha256
from typing import Any, Protocol
from uuid import uuid4

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps
from framework.shared.time import ensure_utc, format_datetime
from framework.tool.models.call import ToolCall
from framework.workers.approval.model import (
    ApprovalDecisionType,
    ApprovalExpiredError,
    ApprovalNotConsumableError,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalStatus,
)

UTC = _tz.utc

TOOL_APPROVAL_TYPE = "tool_execution"
TOOL_APPROVAL_BINDING_VERSION = "tool_approval_binding.v1"
_VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}


class ToolApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    MODIFY = "modify"


class ToolApprovalStore(Protocol):
    def get_approval(self, approval_id: str) -> ApprovalRequest: ...

    def upsert_approval(self, request: ApprovalRequest) -> ApprovalRequest: ...

    def consume_approval(
        self,
        approval_id: str,
        *,
        consumed_by: str,
        consumed_at: datetime | None = None,
    ) -> ApprovalRequest: ...


def tool_arguments_digest(arguments: Mapping[str, Any]) -> str:
    """Canonical digest of normalized tool arguments used to bind approvals."""

    return "sha256:" + sha256(stable_json_dumps(dict(arguments)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolApprovalBinding:
    """Exact scope an approval authorises.

    Graph bindings deliberately exclude ``node_instance_id``, ``activity_id``
    and ``attempt``: resuming after a human wait re-enters the same node in a
    new activity attempt, and the approval must survive that re-entry while
    remaining pinned to the same run, graph revision, node and agent.
    """

    tool_name: str
    tool_version: str
    arguments_digest: str
    run_id: str | None = None
    agent_id: str | None = None
    graph_id: str | None = None
    graph_version: str | None = None
    graph_checksum: str | None = None
    node_id: str | None = None
    binding_version: str = TOOL_APPROVAL_BINDING_VERSION

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool approval binding requires tool_name")
        if not self.arguments_digest.startswith("sha256:"):
            raise ValueError("tool approval binding requires a sha256 arguments digest")

    @classmethod
    def for_call(
        cls,
        call: ToolCall,
        *,
        tool_version: str,
        arguments: Mapping[str, Any],
        run_id: str | None,
    ) -> "ToolApprovalBinding":
        identity = call.graph_identity
        if identity is not None and run_id is not None and identity.run_id != run_id:
            raise ValueError("tool call graph_identity.run_id must match executor run_id")
        return cls(
            tool_name=call.tool_name,
            tool_version=str(tool_version),
            arguments_digest=tool_arguments_digest(arguments),
            run_id=identity.run_id if identity is not None else run_id,
            agent_id=call.requested_by_agent_id or None,
            graph_id=identity.graph_id if identity is not None else None,
            graph_version=identity.graph_version if identity is not None else None,
            graph_checksum=identity.graph_checksum if identity is not None else None,
            node_id=identity.node_id if identity is not None else None,
        )

    def mismatched_fields(self, other: "ToolApprovalBinding") -> tuple[str, ...]:
        mine = self.to_dict()
        theirs = other.to_dict()
        return tuple(sorted(key for key in mine if mine[key] != theirs.get(key)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_version": self.binding_version,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "arguments_digest": self.arguments_digest,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "graph_checksum": self.graph_checksum,
            "node_id": self.node_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolApprovalBinding":
        if not isinstance(value, Mapping):
            raise TypeError("tool approval binding must be an object")
        version = value.get("binding_version")
        if version != TOOL_APPROVAL_BINDING_VERSION:
            raise ValueError(f"unsupported tool approval binding version: {version}")
        return cls(
            tool_name=str(value["tool_name"]),
            tool_version=str(value["tool_version"]),
            arguments_digest=str(value["arguments_digest"]),
            run_id=_optional_text(value.get("run_id")),
            agent_id=_optional_text(value.get("agent_id")),
            graph_id=_optional_text(value.get("graph_id")),
            graph_version=_optional_text(value.get("graph_version")),
            graph_checksum=_optional_text(value.get("graph_checksum")),
            node_id=_optional_text(value.get("node_id")),
        )


@dataclass(frozen=True)
class ToolApprovalRequest:
    tool_call: ToolCall
    tool_name: str
    side_effect: str
    reason: str
    risk_level: str
    approval_id: str = field(default_factory=lambda: f"tool_appr_{uuid4().hex}")
    run_id: str | None = None
    step_id: str | None = None
    agent_id: str | None = None
    graph_identity: GraphExecutionIdentity | None = None
    tool_version: str = "1.0.0"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.risk_level not in _VALID_RISK_LEVELS:
            raise ValueError(f"invalid tool approval risk level: {self.risk_level}")
        if self.step_id is not None:
            raise ValueError("active tool approval cannot carry retired step_id authority")
        if self.tool_call.tool_name != self.tool_name:
            raise ValueError("tool approval tool_name must match tool_call.tool_name")
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        if self.expires_at is not None:
            expires_at = ensure_utc(self.expires_at)
            if expires_at <= self.created_at:
                raise ValueError("tool approval expires_at must be after created_at")
            object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "metadata", dict(self.metadata))
        identity = self.graph_identity
        if identity is not None and not isinstance(identity, GraphExecutionIdentity):
            identity = GraphExecutionIdentity.from_dict(identity)
        if identity is not None and self.run_id is not None and identity.run_id != self.run_id:
            raise ValueError("approval run_id must match graph_identity.run_id")
        object.__setattr__(self, "graph_identity", identity)
        if self.agent_id is None and self.tool_call.requested_by_agent_id:
            object.__setattr__(self, "agent_id", self.tool_call.requested_by_agent_id)

    @property
    def binding(self) -> ToolApprovalBinding:
        return ToolApprovalBinding.for_call(
            self.tool_call,
            tool_version=self.tool_version,
            arguments=self.tool_call.arguments,
            run_id=self.run_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool_name": self.tool_name,
            "tool_version": self.tool_version,
            "tool_call": self.tool_call.to_dict(),
            "side_effect": self.side_effect,
            "reason": self.reason,
            "risk_level": self.risk_level,
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "graph_identity": (
                self.graph_identity.to_dict()
                if self.graph_identity is not None
                else None
            ),
            "created_at": format_datetime(self.created_at),
            "expires_at": format_datetime(self.expires_at),
            "metadata": dict(self.metadata),
        }

    def to_worker_approval_request(self) -> ApprovalRequest:
        # ``arguments`` is stored verbatim so the reviewer sees exactly what will
        # run and the approved call can be replayed. Callers must only build
        # tool approvals from redaction-invariant (secret-free) arguments.
        return ApprovalRequest(
            approval_id=self.approval_id,
            requested_action=f"tool:{self.tool_name}",
            risk_level=self.risk_level,
            reason=self.reason,
            payload={
                "tool_approval": self.to_dict(),
                "binding": self.binding.to_dict(),
                "arguments": dict(self.tool_call.arguments),
            },
            task_id=self.tool_call.call_id,
            run_id=self.run_id,
            graph_identity=self.graph_identity,
            requested_by=self.agent_id,
            created_at=self.created_at,
            expires_at=self.expires_at,
            metadata={
                **self.metadata,
                "approval_type": TOOL_APPROVAL_TYPE,
                "tool_name": self.tool_name,
                "side_effect": self.side_effect,
            },
        )


class ToolApprovalVerdict(str, Enum):
    GRANTED = "granted"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"
    MISMATCH = "binding_mismatch"
    NOT_FOUND = "not_found"
    MALFORMED = "malformed"

    @property
    def reason_code(self) -> str:
        return f"approval_{self.value}"


@dataclass(frozen=True)
class ToolApprovalResolution:
    verdict: ToolApprovalVerdict
    approval_id: str
    message: str
    effective_arguments: dict[str, Any] | None = None
    decided_by: str | None = None
    decision_type: str | None = None
    mismatched_fields: tuple[str, ...] = ()

    @property
    def granted(self) -> bool:
        return self.verdict == ToolApprovalVerdict.GRANTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason_code": self.verdict.reason_code,
            "approval_id": self.approval_id,
            "message": self.message,
            "decided_by": self.decided_by,
            "decision_type": self.decision_type,
            "mismatched_fields": list(self.mismatched_fields),
        }


_STATUS_VERDICTS = {
    ApprovalStatus.PENDING: ToolApprovalVerdict.PENDING,
    ApprovalStatus.REJECTED: ToolApprovalVerdict.REJECTED,
    ApprovalStatus.EXPIRED: ToolApprovalVerdict.EXPIRED,
    ApprovalStatus.CANCELLED: ToolApprovalVerdict.CANCELLED,
    ApprovalStatus.CONSUMED: ToolApprovalVerdict.CONSUMED,
}


class ToolApprovalAuthority:
    """Creates tool approval records and redeems them as single-use grants."""

    def __init__(
        self,
        store: ToolApprovalStore,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now_fn = now_fn or (lambda: datetime.now(UTC))

    def request(
        self,
        request: ToolApprovalRequest,
        *,
        ttl_seconds: float | None = None,
    ) -> ApprovalRequest:
        if ttl_seconds is not None and request.expires_at is None:
            request = ToolApprovalRequest(
                tool_call=request.tool_call,
                tool_name=request.tool_name,
                side_effect=request.side_effect,
                reason=request.reason,
                risk_level=request.risk_level,
                approval_id=request.approval_id,
                run_id=request.run_id,
                agent_id=request.agent_id,
                graph_identity=request.graph_identity,
                tool_version=request.tool_version,
                created_at=request.created_at,
                expires_at=request.created_at + timedelta(seconds=ttl_seconds),
                metadata=request.metadata,
            )
        return self._store.upsert_approval(request.to_worker_approval_request())

    def resolve(self, approval_id: str, binding: ToolApprovalBinding) -> ToolApprovalResolution:
        try:
            record = self._store.get_approval(approval_id)
        except ApprovalNotFoundError:
            return _resolution(ToolApprovalVerdict.NOT_FOUND, approval_id, "approval record not found")

        stored_binding = _stored_binding(record)
        if stored_binding is None:
            return _resolution(
                ToolApprovalVerdict.MALFORMED,
                approval_id,
                "approval record is not a bound tool execution approval",
            )
        mismatched = stored_binding.mismatched_fields(binding)
        if mismatched:
            return _resolution(
                ToolApprovalVerdict.MISMATCH,
                approval_id,
                "approval was granted for a different tool call: " + ", ".join(mismatched),
                mismatched_fields=mismatched,
            )

        status = ApprovalStatus(record.status)
        decision = record.decision
        decided_by = decision.decided_by if decision is not None else None
        decision_type = (
            ApprovalDecisionType(decision.decision_type).value if decision is not None else None
        )
        if record.is_expired(self._now_fn()) and status in {
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
            ApprovalStatus.MODIFIED,
        }:
            return _resolution(
                ToolApprovalVerdict.EXPIRED,
                approval_id,
                "approval expired before execution",
                decided_by=decided_by,
                decision_type=decision_type,
            )
        verdict = _STATUS_VERDICTS.get(status)
        if verdict is not None:
            return _resolution(
                verdict,
                approval_id,
                f"approval is {status.value}",
                decided_by=decided_by,
                decision_type=decision_type,
            )

        effective_arguments = _effective_arguments(record)
        if effective_arguments is None:
            return _resolution(
                ToolApprovalVerdict.MALFORMED,
                approval_id,
                "modify decision must provide a complete 'arguments' object",
                decided_by=decided_by,
                decision_type=decision_type,
            )
        return ToolApprovalResolution(
            verdict=ToolApprovalVerdict.GRANTED,
            approval_id=approval_id,
            message=f"approval {status.value} by {decided_by}",
            effective_arguments=effective_arguments,
            decided_by=decided_by,
            decision_type=decision_type,
        )

    def consume(self, resolution: ToolApprovalResolution, *, consumed_by: str) -> ToolApprovalResolution:
        """Atomically redeem a granted resolution; losers of a race see CONSUMED."""

        if not resolution.granted:
            raise ValueError("only granted approvals can be consumed")
        try:
            self._store.consume_approval(
                resolution.approval_id,
                consumed_by=consumed_by,
                consumed_at=self._now_fn(),
            )
        except ApprovalExpiredError:
            return _resolution(
                ToolApprovalVerdict.EXPIRED,
                resolution.approval_id,
                "approval expired before execution",
                decided_by=resolution.decided_by,
                decision_type=resolution.decision_type,
            )
        except ApprovalNotConsumableError:
            return _resolution(
                ToolApprovalVerdict.CONSUMED,
                resolution.approval_id,
                "approval was already redeemed by another execution",
                decided_by=resolution.decided_by,
                decision_type=resolution.decision_type,
            )
        return resolution


def tool_call_from_approval(
    record: ApprovalRequest,
    *,
    call_id: str | None = None,
    graph_identity: GraphExecutionIdentity | None = None,
) -> ToolCall:
    """Rebuild the exact originally-requested ToolCall for approval replay."""

    payload = record.payload
    tool_approval = payload.get("tool_approval")
    arguments = payload.get("arguments")
    if not isinstance(tool_approval, Mapping) or not isinstance(arguments, Mapping):
        raise ValueError(f"approval is not a replayable tool approval: {record.approval_id}")
    original_call = tool_approval.get("tool_call") or {}
    return ToolCall(
        tool_name=str(tool_approval["tool_name"]),
        arguments=dict(arguments),
        requested_by_agent_id=str(original_call.get("requested_by_agent_id") or ""),
        requested_by=original_call.get("requested_by"),
        call_id=call_id or f"{original_call.get('call_id') or uuid4().hex}:approved",
        metadata={"approval_replay_of": original_call.get("call_id")},
        graph_identity=graph_identity if graph_identity is not None else record.graph_identity,
        approval_id=record.approval_id,
    )


def is_tool_approval(record: ApprovalRequest) -> bool:
    return record.metadata.get("approval_type") == TOOL_APPROVAL_TYPE


def _stored_binding(record: ApprovalRequest) -> ToolApprovalBinding | None:
    if not is_tool_approval(record):
        return None
    raw = record.payload.get("binding")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ToolApprovalBinding.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _effective_arguments(record: ApprovalRequest) -> dict[str, Any] | None:
    original = record.payload.get("arguments")
    if not isinstance(original, Mapping):
        return None
    if ApprovalStatus(record.status) != ApprovalStatus.MODIFIED:
        return dict(original)
    modifications = record.decision.modifications if record.decision is not None else {}
    replacement = modifications.get("arguments")
    if not isinstance(replacement, Mapping):
        return None
    return dict(replacement)


def _resolution(
    verdict: ToolApprovalVerdict,
    approval_id: str,
    message: str,
    *,
    decided_by: str | None = None,
    decision_type: str | None = None,
    mismatched_fields: tuple[str, ...] = (),
) -> ToolApprovalResolution:
    return ToolApprovalResolution(
        verdict=verdict,
        approval_id=approval_id,
        message=message,
        decided_by=decided_by,
        decision_type=decision_type,
        mismatched_fields=mismatched_fields,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
