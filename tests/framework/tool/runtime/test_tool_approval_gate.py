from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from framework.shared.attempts import current_attempt_context
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
    ToolStatus,
)
from framework.workers.approval import (
    ApprovalDecision,
    ApprovalStatus,
    InMemoryApprovalStore,
)

_RUN_ID = "run-gate"
_SCHEMA = {
    "type": "object",
    "properties": {
        "channel": {"type": "string"},
        "title": {"type": "string"},
        "api_key": {"type": "string"},
    },
    "required": ["channel", "title"],
    "additionalProperties": False,
}


class _PublishTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.idempotency_keys: list[str] = []

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        context = current_attempt_context()
        assert context is not None
        self.idempotency_keys.append(context.idempotency_key)
        self.calls.append(dict(arguments))
        return {"published": arguments["title"]}


def _setup(*, ttl_seconds: float | None = None):
    tool = _PublishTool()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish",
            input_schema=_SCHEMA,
            side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE,
            version="1.2.0",
        ),
        tool,
    )
    store = InMemoryApprovalStore()
    executor = ToolExecutor(registry, run_id=_RUN_ID, approval_store=store)
    policy = ToolPolicy(
        allowed_tools=["sample.publish"],
        require_approval_for_side_effects=True,
        approval_ttl_seconds=ttl_seconds,
    )
    return tool, store, executor, policy


def _call(**overrides: Any) -> ToolCall:
    values: dict[str, Any] = {
        "tool_name": "sample.publish",
        "arguments": {"channel": "news", "title": "Daily brief"},
        "requested_by_agent_id": "agent-1",
        "call_id": "call-1",
    }
    values.update(overrides)
    return ToolCall(**values)


def _decide(store: InMemoryApprovalStore, approval_id: str, decision_type: str, **extra: Any) -> None:
    store.record_decision(
        approval_id,
        decision=ApprovalDecision(decision_type=decision_type, decided_by="reviewer-1", **extra),
    )


def _approval_check(observation) -> dict[str, Any]:
    checks = observation.result.policy_trace.to_dict()["checks"]
    return next(check for check in checks if check["check_id"] == "tool.approval")


def test_first_call_creates_bound_pending_approval_without_executing() -> None:
    tool, store, executor, policy = _setup(ttl_seconds=120)

    observation = executor.execute(_call(), policy)

    assert observation.status == ToolStatus.APPROVAL_REQUIRED
    approval_id = observation.result.approval_id
    assert approval_id
    record = store.get_approval(approval_id)
    assert record.status == ApprovalStatus.PENDING
    assert record.run_id == _RUN_ID
    assert record.requested_by == "agent-1"
    assert record.expires_at is not None
    assert record.payload["binding"]["tool_version"] == "1.2.0"
    assert record.payload["arguments"] == {"channel": "news", "title": "Daily brief"}
    assert observation.result.metadata["reason_code"] == "approval_required"
    assert _approval_check(observation)["metadata"]["approval_id"] == approval_id
    assert tool.calls == []


def test_approved_call_executes_once_and_cannot_be_replayed() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(store, approval_id, "approve")

    approved = executor.execute(_call(call_id="call-1:approved", approval_id=approval_id), policy)

    assert approved.status == ToolStatus.SUCCEEDED
    assert tool.calls == [{"channel": "news", "title": "Daily brief"}]
    assert tool.idempotency_keys == [f"tool:approval:{approval_id}"]
    assert approved.result.policy_trace.approval_granted is True
    check = _approval_check(approved)
    assert check["passed"] is True
    assert check["metadata"]["verdict"] == "granted"
    assert check["metadata"]["decided_by"] == "reviewer-1"
    consumed = store.get_approval(approval_id)
    assert consumed.status == ApprovalStatus.CONSUMED
    assert consumed.consumed_by == "call-1:approved"
    redeemed_events = [
        event for event in executor.list_events() if event.event_type == "tool_approval_decided"
    ]
    assert redeemed_events[-1].payload["status"] == "redeemed"

    replayed = executor.execute(_call(call_id="call-1:again", approval_id=approval_id), policy)

    assert replayed.status == ToolStatus.DENIED
    assert replayed.result.metadata["reason_code"] == "approval_consumed"
    assert len(tool.calls) == 1


def test_pending_approval_keeps_call_waiting() -> None:
    tool, _, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id

    observation = executor.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.APPROVAL_REQUIRED
    assert observation.result.approval_id == approval_id
    assert observation.result.metadata["reason_code"] == "approval_pending"
    assert tool.calls == []


def test_rejected_approval_denies_call() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(store, approval_id, "reject", reason="not newsworthy")

    observation = executor.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.error_type == "ToolApprovalDenied"
    assert observation.result.metadata["reason_code"] == "approval_rejected"
    assert _approval_check(observation)["severity"] == "error"
    assert tool.calls == []


def test_approval_cannot_release_different_arguments_or_agent() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(store, approval_id, "approve")

    other_arguments = executor.execute(
        _call(arguments={"channel": "news", "title": "Something else"}, approval_id=approval_id),
        policy,
    )
    other_agent = executor.execute(
        _call(requested_by_agent_id="agent-2", approval_id=approval_id),
        policy,
    )

    assert other_arguments.status == ToolStatus.DENIED
    assert other_arguments.result.metadata["reason_code"] == "approval_binding_mismatch"
    assert other_agent.status == ToolStatus.DENIED
    assert "agent_id" in _approval_check(other_agent)["metadata"]["mismatched_fields"]
    assert tool.calls == []
    assert store.get_approval(approval_id).status == ApprovalStatus.APPROVED


def test_approval_from_another_run_is_refused() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(store, approval_id, "approve")
    other_run = ToolExecutor(executor._registry, run_id="run-other", approval_store=store)

    observation = other_run.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_binding_mismatch"
    assert tool.calls == []


def test_modified_approval_executes_reviewer_arguments() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(
        store,
        approval_id,
        "modify",
        modifications={"arguments": {"channel": "news", "title": "Reviewed brief"}},
    )

    observation = executor.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.SUCCEEDED
    assert tool.calls == [{"channel": "news", "title": "Reviewed brief"}]
    redeemed = [event for event in executor.list_events() if event.event_type == "tool_approval_decided"]
    assert redeemed[-1].payload["arguments_modified"] is True
    assert redeemed[-1].payload["decision_type"] == "modify"


def test_modified_approval_with_invalid_arguments_is_denied() -> None:
    tool, store, executor, policy = _setup()
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(
        store,
        approval_id,
        "modify",
        modifications={"arguments": {"channel": "news", "unknown": True}},
    )

    observation = executor.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_arguments_invalid"
    assert tool.calls == []
    assert store.get_approval(approval_id).status == ApprovalStatus.MODIFIED


def test_secret_bearing_arguments_are_never_put_up_for_approval() -> None:
    tool, store, executor, policy = _setup()

    observation = executor.execute(
        _call(arguments={"channel": "news", "title": "Daily", "api_key": "sk-live-1234567890"}),
        policy,
    )

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_arguments_sensitive"
    assert store.list_approvals() == []
    assert tool.calls == []


def test_approval_id_without_store_is_unverifiable() -> None:
    tool, _, executor, policy = _setup()
    storeless = ToolExecutor(executor._registry, run_id=_RUN_ID)

    observation = storeless.execute(_call(approval_id="tool_appr_forged"), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_unverifiable"
    assert tool.calls == []


def test_unknown_approval_id_is_denied() -> None:
    tool, _, executor, policy = _setup()

    observation = executor.execute(_call(approval_id="tool_appr_forged"), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_not_found"
    assert tool.calls == []


def test_expired_approval_is_denied() -> None:
    tool, store, executor, policy = _setup(ttl_seconds=60)
    approval_id = executor.execute(_call(), policy).result.approval_id
    _decide(store, approval_id, "approve")
    record = store.get_approval(approval_id)
    # Shift the whole validity window into the past: the grant lapsed unused.
    lapsed = timedelta(days=1)
    store.upsert_approval(
        replace(
            record,
            created_at=record.created_at - lapsed,
            expires_at=record.expires_at - lapsed,
        )
    )

    observation = executor.execute(_call(approval_id=approval_id), policy)

    assert observation.status == ToolStatus.DENIED
    assert observation.result.metadata["reason_code"] == "approval_expired"
    assert tool.calls == []


def test_tool_call_approval_id_is_serialized() -> None:
    call = _call(approval_id="  tool_appr_1  ")

    assert call.approval_id == "tool_appr_1"
    assert call.to_dict()["approval_id"] == "tool_appr_1"
