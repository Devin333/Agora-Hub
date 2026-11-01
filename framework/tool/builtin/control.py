from __future__ import annotations

import math
from typing import Any, Protocol

from framework.shared.hashing import stable_hash
from framework.shared.json import ensure_strict_json
from framework.tool.governance.redaction import reject_sensitive_mapping_keys
from framework.tool.governance.approval import ApprovalRequest
from framework.tool.models.definition import ToolDefinition
from framework.tool.registry.registry import ToolRegistry
from framework.tool.runtime.errors import ToolEffectNotAppliedError, effect_not_applied_on
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.workers.approval.model import ApprovalNotFoundError
from framework.workers.models.result import TaskEnqueueResult
from framework.workers.models.task import DEFAULT_TASK_QUEUE, Task

# Approval stores and task queues may be remote (Postgres, Redis); control
# writes declare their own bound instead of the 30s policy default.
CONTROL_STORE_TIMEOUT_SECONDS = 30.0
# A delegated task is retried by workers; an unbounded count would let one
# failing task occupy the queue forever.
MAX_DELEGATED_TASK_ATTEMPTS = 10
RISK_LEVELS = ("low", "medium", "high", "critical")
# A queue refuses a task whose dedup key is still unfinished; for delegation
# that means the same work from the same run is already queued.
DUPLICATE_TASK_REASON = "duplicate_dedup_key"


class TaskQueueWriter(Protocol):
    def enqueue(self, task: Any) -> Any: ...


def register_control_tools(
    registry: ToolRegistry,
    *,
    approval_store: Any | None = None,
    task_queue: TaskQueueWriter | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    run_id: str | None = None,
) -> None:
    if execution_identity is not None and not isinstance(
        execution_identity, GraphExecutionIdentity
    ):
        execution_identity = GraphExecutionIdentity.from_dict(execution_identity)
    if execution_identity is not None and run_id is not None:
        if execution_identity.run_id != str(run_id).strip():
            raise ValueError("run_id must match execution_identity.run_id")
    if (approval_store is not None or task_queue is not None) and execution_identity is None:
        raise ValueError(
            "Graph control tools require an exact GraphExecutionIdentity; "
            "a standalone run_id is not an execution authority"
        )
    registry.register(
        ToolDefinition(
            name="control.set_output",
            description="Submit a final output candidate for the current agent loop.",
            input_schema={
                "required": ["output"],
                "properties": {"output": {"type": "object"}, "reason": {"type": "string"}},
                "additionalProperties": False,
            },
            side_effect="none",
            concurrency_safe=False,
        ),
        _set_output,
    )
    registry.register(
        ToolDefinition(
            name="control.report_progress",
            description="Report structured progress for the current agent loop.",
            input_schema={
                "required": ["message"],
                "properties": {
                    "message": {"type": "string"},
                    "percent": {"type": "number"},
                    "metadata": {"type": "object"},
                },
                "additionalProperties": False,
            },
            side_effect="none",
            concurrency_safe=True,
        ),
        _report_progress,
    )
    if approval_store is not None:
        registry.register(
            ToolDefinition(
                name="control.request_human_review",
                description="Create a human review approval request.",
                input_schema={
                    "required": ["requested_action", "reason"],
                    "properties": {
                        "requested_action": {"type": "string"},
                        "reason": {"type": "string"},
                        "risk_level": {"type": "string", "enum": list(RISK_LEVELS)},
                        "payload": {"type": "object"},
                        "task_id": {"type": "string"},
                        "requested_by": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                side_effect="none",
                concurrency_safe=False,
                metadata={"writes_approval_request": True},
                timeout_seconds=CONTROL_STORE_TIMEOUT_SECONDS,
            ),
            lambda args: _request_human_review(
                args,
                approval_store=approval_store,
                execution_identity=execution_identity,
            ),
        )
        registry.register(
            ToolDefinition(
                name="control.escalate",
                description="Create an escalation request for human handling.",
                input_schema={
                    "required": ["escalation_type", "reason"],
                    "properties": {
                        "escalation_type": {"type": "string"},
                        "reason": {"type": "string"},
                        "severity": {"type": "string", "enum": list(RISK_LEVELS)},
                        "payload": {"type": "object"},
                        "task_id": {"type": "string"},
                        "requested_by": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                side_effect="none",
                concurrency_safe=False,
                metadata={"writes_escalation_request": True},
                timeout_seconds=CONTROL_STORE_TIMEOUT_SECONDS,
            ),
            lambda args: _escalate(
                args,
                approval_store=approval_store,
                execution_identity=execution_identity,
            ),
        )
    if task_queue is not None:
        registry.register(
            ToolDefinition(
                name="control.delegate_to_subagent",
                description="Delegate work to a worker queue for subagent-style execution.",
                input_schema={
                    "required": ["task_type"],
                    "properties": {
                        "task_type": {"type": "string"},
                        "payload": {"type": "object"},
                        "queue_name": {"type": "string"},
                        "task_id": {"type": "string"},
                        "subagent_id": {"type": "string"},
                        "max_attempts": {"type": "integer"},
                        "metadata": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
                side_effect="writes_external_state",
                requires_approval=True,
                concurrency_safe=False,
                metadata={"writes_worker_task": True},
                timeout_seconds=CONTROL_STORE_TIMEOUT_SECONDS,
            ),
            lambda args: _delegate_to_subagent(
                args,
                task_queue=task_queue,
                execution_identity=execution_identity,
            ),
        )


def _set_output(args: dict[str, Any]) -> dict[str, Any]:
    output = args["output"]
    if not isinstance(output, dict):
        raise ValueError("output must be an object")
    # The final output is persisted as the run's result and read by other services.
    ensure_strict_json(output, "output")
    reason = args.get("reason")
    return {
        "control_action": "set_output",
        "output": dict(output),
        "reason": str(reason) if reason is not None else None,
    }


def _report_progress(args: dict[str, Any]) -> dict[str, Any]:
    percent = args.get("percent")
    if percent is not None:
        percent = float(percent)
        if not math.isfinite(percent) or not 0.0 <= percent <= 100.0:
            raise ValueError("percent must be between 0 and 100")
    metadata = args.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    ensure_strict_json(metadata, "metadata")
    return {
        "control_action": "report_progress",
        "message": str(args["message"]),
        "percent": percent,
        "metadata": dict(metadata),
    }


def _request_human_review(
    args: dict[str, Any],
    *,
    approval_store: Any,
    execution_identity: GraphExecutionIdentity | None,
) -> dict[str, Any]:
    if execution_identity is None:
        raise ValueError("human review requires an exact GraphExecutionIdentity")
    requested_action = _required_text(args["requested_action"], "requested_action")
    request = _approval_request(
        args,
        tool_name="control.request_human_review",
        requested_action=requested_action,
        risk_level=str(args.get("risk_level") or "medium"),
        execution_identity=execution_identity,
    )
    stored, existing = _submit_approval(approval_store, request)
    return {
        "control_action": "request_human_review",
        "approval_id": stored.approval_id,
        "existing": existing,
        "approval": stored.to_dict(),
    }


def _escalate(
    args: dict[str, Any],
    *,
    approval_store: Any,
    execution_identity: GraphExecutionIdentity | None,
) -> dict[str, Any]:
    if execution_identity is None:
        raise ValueError("escalation requires an exact GraphExecutionIdentity")
    escalation_type = _required_text(args["escalation_type"], "escalation_type")
    request = _approval_request(
        args,
        tool_name="control.escalate",
        requested_action=f"escalate:{escalation_type}",
        risk_level=str(args.get("severity") or "high"),
        execution_identity=execution_identity,
        extra_metadata={"escalation_type": escalation_type},
    )
    stored, existing = _submit_approval(approval_store, request)
    return {
        "control_action": "escalate",
        "escalation_type": escalation_type,
        "approval_id": stored.approval_id,
        "existing": existing,
        "approval": stored.to_dict(),
    }


def _approval_request(
    args: dict[str, Any],
    *,
    tool_name: str,
    requested_action: str,
    risk_level: str,
    execution_identity: GraphExecutionIdentity,
    extra_metadata: dict[str, Any] | None = None,
) -> ApprovalRequest:
    reason = _required_text(args["reason"], "reason")
    if risk_level not in RISK_LEVELS:
        raise ValueError(f"risk level must be one of: {', '.join(RISK_LEVELS)}")
    payload = _json_object(args.get("payload"), "payload")
    metadata = _json_object(args.get("metadata"), "metadata")
    task_id = _optional_text(args.get("task_id"))
    requested_by = _optional_text(args.get("requested_by"))
    # The id is derived from the request itself: a retry after a lost
    # acknowledgement finds the request it already created instead of adding
    # a second pending approval for a human to answer.
    fingerprint = stable_hash(
        {
            "graph_identity": execution_identity.to_dict(),
            "tool": tool_name,
            "requested_action": requested_action,
            "risk_level": risk_level,
            "reason": reason,
            "payload": payload,
            "metadata": metadata,
            "task_id": task_id,
            "requested_by": requested_by,
        }
    )
    return ApprovalRequest(
        requested_action=requested_action,
        risk_level=risk_level,
        reason=reason,
        payload=payload,
        approval_id=f"appr_{fingerprint[:32]}",
        task_id=task_id,
        run_id=execution_identity.run_id,
        graph_identity=execution_identity,
        requested_by=requested_by,
        metadata={**metadata, **(extra_metadata or {}), "control_tool": tool_name},
    )


def _submit_approval(approval_store: Any, request: ApprovalRequest) -> tuple[Any, bool]:
    """Store ``request`` unless the same request exists; never reset a decision."""

    try:
        return approval_store.get_approval(request.approval_id), True
    except ApprovalNotFoundError:
        pass
    return approval_store.upsert_approval(request), False


def _delegate_to_subagent(
    args: dict[str, Any],
    *,
    task_queue: TaskQueueWriter,
    execution_identity: GraphExecutionIdentity | None,
) -> dict[str, Any]:
    if execution_identity is None:
        raise ValueError("subagent delegation requires an exact GraphExecutionIdentity")
    with effect_not_applied_on():
        task_type = _required_text(args["task_type"], "task_type")
        # Task payloads are persisted in the queue and read by workers; secrets
        # belong in the worker's secret provider, as for review and escalation.
        payload = _json_object(args.get("payload"), "payload")
        metadata = _json_object(args.get("metadata"), "metadata")
        queue_name = str(args.get("queue_name") or DEFAULT_TASK_QUEUE).strip()
        if not queue_name:
            raise ValueError("queue_name is required")
        raw_attempts = args.get("max_attempts")
        max_attempts = 3 if raw_attempts is None else int(raw_attempts)
        if not 1 <= max_attempts <= MAX_DELEGATED_TASK_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {MAX_DELEGATED_TASK_ATTEMPTS}")
        task_id = _optional_text(args.get("task_id"))
        subagent_id = _optional_text(args.get("subagent_id"))
        task_metadata = {**metadata, "control_tool": "control.delegate_to_subagent"}
        if subagent_id is not None:
            task_metadata["subagent_id"] = subagent_id
        # The same work from the same run is queued at most once while
        # unfinished, so a retry after a lost acknowledgement cannot double it.
        dedup_key = "delegate:" + stable_hash(
            {
                "graph_identity": execution_identity.to_dict(),
                "task_type": task_type,
                "payload": payload,
                "queue_name": queue_name,
                "task_id": task_id,
                "subagent_id": subagent_id,
            }
        )
        task_kwargs: dict[str, Any] = {
            "task_type": task_type,
            "payload": payload,
            "queue_name": queue_name,
            "max_attempts": max_attempts,
            "metadata": task_metadata,
            "graph_identity": execution_identity,
            "dedup_key": dedup_key,
        }
        if task_id is not None:
            task_kwargs["task_id"] = task_id
        task = Task(**task_kwargs)
    enqueued = task_queue.enqueue(task)
    if isinstance(enqueued, TaskEnqueueResult) and not enqueued.accepted:
        if enqueued.reason == DUPLICATE_TASK_REASON:
            return {
                "control_action": "delegate_to_subagent",
                "deduplicated": True,
                "dedup_key": dedup_key,
                "task_id": task_id,
                "task_type": task_type,
                "queue_name": queue_name,
                "message_id": None,
            }
        raise ToolEffectNotAppliedError(
            f"worker queue {queue_name} refused the task: {enqueued.reason or 'rejected'}"
        )
    return {
        "control_action": "delegate_to_subagent",
        "deduplicated": False,
        "dedup_key": dedup_key,
        "task_id": task.task_id,
        "task_type": task.task_type,
        "queue_name": task.queue_name,
        "message_id": _normalize_message_id(enqueued),
        "task": task.to_dict(),
    }


def _required_text(value: Any, field_name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    """An object a store or worker can persist: JSON-only and secret-free."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    ensure_strict_json(value, field_name)
    reject_sensitive_mapping_keys(value, label=field_name)
    return dict(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_message_id(message_id: Any) -> str | None:
    if message_id is None:
        return None
    nested_message_id = getattr(message_id, "message_id", None)
    if nested_message_id is not None:
        return _normalize_message_id(nested_message_id)
    if isinstance(message_id, bytes):
        return message_id.decode("utf-8")
    return str(message_id)
