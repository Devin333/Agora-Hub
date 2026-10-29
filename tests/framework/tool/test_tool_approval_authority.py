from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolApprovalAuthority,
    ToolApprovalBinding,
    ToolApprovalVerdict,
    ToolCall,
    tool_arguments_digest,
    tool_call_from_approval,
)
from framework.tool.governance.approval import ToolApprovalRequest, is_tool_approval
from framework.workers.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _identity(**overrides: object) -> GraphExecutionIdentity:
    values: dict[str, object] = {
        "run_id": "run-approval",
        "graph_id": "research.graph",
        "graph_version": "1",
        "graph_ref": "research.graph@1",
        "graph_checksum": "sha256:" + "c" * 64,
        "node_id": "publish",
        "node_instance_id": "publish:1",
        "activity_id": "activity-1",
        "attempt": 1,
    }
    values.update(overrides)
    return GraphExecutionIdentity(**values)  # type: ignore[arg-type]


def _call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "tool_name": "sample.publish",
        "arguments": {"channel": "news", "title": "Daily brief"},
        "requested_by_agent_id": "agent-1",
        "call_id": "call-1",
        "graph_identity": _identity(),
    }
    values.update(overrides)
    return ToolCall(**values)  # type: ignore[arg-type]


def _binding(call: ToolCall, *, tool_version: str = "1.0.0") -> ToolApprovalBinding:
    return ToolApprovalBinding.for_call(
        call,
        tool_version=tool_version,
        arguments=call.arguments,
        run_id=None,
    )


def _setup(
    *,
    ttl_seconds: float | None = None,
) -> tuple[ToolApprovalAuthority, InMemoryApprovalStore, _Clock, ApprovalRequest, ToolCall]:
    clock = _Clock()
    store = InMemoryApprovalStore(now_fn=clock)
    authority = ToolApprovalAuthority(store, now_fn=clock)
    call = _call()
    record = authority.request(
        ToolApprovalRequest(
            tool_call=call,
            tool_name=call.tool_name,
            side_effect="external_write",
            reason="publishing requires review",
            risk_level="high",
            graph_identity=call.graph_identity,
            run_id=call.graph_identity.run_id,
            created_at=clock.now,
        ),
        ttl_seconds=ttl_seconds,
    )
    return authority, store, clock, record, call


def _approve(store: InMemoryApprovalStore, approval_id: str) -> None:
    store.record_decision(
        approval_id,
        decision=ApprovalDecision(decision_type="approve", decided_by="reviewer-1"),
    )


def test_request_persists_bound_replayable_tool_approval() -> None:
    _, store, clock, record, call = _setup(ttl_seconds=60)

    stored = store.get_approval(record.approval_id)
    assert is_tool_approval(stored)
    assert stored.status == ApprovalStatus.PENDING
    assert stored.requested_by == "agent-1"
    assert stored.graph_identity == call.graph_identity
    assert stored.expires_at == clock.now + timedelta(seconds=60)
    assert stored.payload["arguments"] == call.arguments
    assert stored.payload["binding"]["arguments_digest"] == tool_arguments_digest(call.arguments)


def test_pending_approval_does_not_grant() -> None:
    authority, _, _, record, call = _setup()

    resolution = authority.resolve(record.approval_id, _binding(call))

    assert resolution.verdict == ToolApprovalVerdict.PENDING
    assert resolution.granted is False


def test_approved_grant_is_single_use() -> None:
    authority, store, _, record, call = _setup()
    _approve(store, record.approval_id)

    resolution = authority.resolve(record.approval_id, _binding(call))
    assert resolution.verdict == ToolApprovalVerdict.GRANTED
    assert resolution.effective_arguments == call.arguments
    assert resolution.decided_by == "reviewer-1"
    assert resolution.decision_type == "approve"

    redeemed = authority.consume(resolution, consumed_by="call-1:approved")
    assert redeemed.granted is True
    consumed = store.get_approval(record.approval_id)
    assert consumed.status == ApprovalStatus.CONSUMED
    assert consumed.consumed_by == "call-1:approved"

    assert authority.resolve(record.approval_id, _binding(call)).verdict == (
        ToolApprovalVerdict.CONSUMED
    )
    # A racer that resolved before the first redemption still loses at consume.
    assert authority.consume(resolution, consumed_by="racer").verdict == (
        ToolApprovalVerdict.CONSUMED
    )


def test_rejected_approval_never_grants() -> None:
    authority, store, _, record, call = _setup()
    store.record_decision(
        record.approval_id,
        decision=ApprovalDecision(decision_type="reject", decided_by="reviewer-1"),
    )

    resolution = authority.resolve(record.approval_id, _binding(call))

    assert resolution.verdict == ToolApprovalVerdict.REJECTED
    assert resolution.verdict.reason_code == "approval_rejected"
    assert resolution.decided_by == "reviewer-1"
    with pytest.raises(ValueError):
        authority.consume(resolution, consumed_by="call-1")


def test_cancelled_approval_never_grants() -> None:
    authority, store, _, record, call = _setup()
    store.upsert_approval(replace(record, status=ApprovalStatus.CANCELLED))

    resolution = authority.resolve(record.approval_id, _binding(call))

    assert resolution.verdict == ToolApprovalVerdict.CANCELLED
    assert resolution.granted is False


def test_expired_approval_is_refused_even_when_approved() -> None:
    authority, store, clock, record, call = _setup(ttl_seconds=30)
    _approve(store, record.approval_id)
    resolution = authority.resolve(record.approval_id, _binding(call))
    assert resolution.granted is True

    clock.now += timedelta(seconds=31)

    assert authority.resolve(record.approval_id, _binding(call)).verdict == (
        ToolApprovalVerdict.EXPIRED
    )
    assert authority.consume(resolution, consumed_by="late").verdict == ToolApprovalVerdict.EXPIRED
    assert store.get_approval(record.approval_id).status == ApprovalStatus.APPROVED


@pytest.mark.parametrize(
    ("changed_call", "field_name"),
    [
        (lambda call: replace(call, tool_name="sample.delete"), "tool_name"),
        (lambda call: replace(call, arguments={"channel": "other"}), "arguments_digest"),
        (lambda call: replace(call, requested_by_agent_id="agent-2"), "agent_id"),
        (
            lambda call: replace(call, graph_identity=_identity(run_id="run-other")),
            "run_id",
        ),
        (lambda call: replace(call, graph_identity=_identity(node_id="other")), "node_id"),
        (
            lambda call: replace(
                call,
                graph_identity=_identity(graph_checksum="sha256:" + "d" * 64),
            ),
            "graph_checksum",
        ),
    ],
)
def test_binding_mismatch_refuses_grant(changed_call, field_name: str) -> None:
    authority, store, _, record, call = _setup()
    _approve(store, record.approval_id)

    resolution = authority.resolve(record.approval_id, _binding(changed_call(call)))

    assert resolution.verdict == ToolApprovalVerdict.MISMATCH
    assert field_name in resolution.mismatched_fields


def test_tool_version_change_invalidates_approval() -> None:
    authority, store, _, record, call = _setup()
    _approve(store, record.approval_id)

    resolution = authority.resolve(record.approval_id, _binding(call, tool_version="2.0.0"))

    assert resolution.verdict == ToolApprovalVerdict.MISMATCH
    assert resolution.mismatched_fields == ("tool_version",)


def test_binding_survives_graph_reentry_with_new_attempt() -> None:
    authority, store, _, record, call = _setup()
    _approve(store, record.approval_id)
    resumed = replace(
        call,
        graph_identity=_identity(
            node_instance_id="publish:2",
            activity_id="activity-2",
            attempt=2,
        ),
    )

    assert authority.resolve(record.approval_id, _binding(resumed)).granted is True


def test_modify_decision_replaces_arguments() -> None:
    authority, store, _, record, call = _setup()
    store.record_decision(
        record.approval_id,
        decision=ApprovalDecision(
            decision_type="modify",
            decided_by="reviewer-1",
            modifications={"arguments": {"channel": "news", "title": "Reviewed brief"}},
        ),
    )

    resolution = authority.resolve(record.approval_id, _binding(call))

    assert resolution.verdict == ToolApprovalVerdict.GRANTED
    assert resolution.decision_type == "modify"
    assert resolution.effective_arguments == {"channel": "news", "title": "Reviewed brief"}


def test_modify_decision_without_complete_arguments_is_malformed() -> None:
    authority, store, _, record, call = _setup()
    store.record_decision(
        record.approval_id,
        decision=ApprovalDecision(
            decision_type="modify",
            decided_by="reviewer-1",
            modifications={"title": "partial patch"},
        ),
    )

    assert authority.resolve(record.approval_id, _binding(call)).verdict == (
        ToolApprovalVerdict.MALFORMED
    )


def test_unknown_and_non_tool_approvals_are_refused() -> None:
    authority, store, _, _, call = _setup()
    foreign = store.upsert_approval(
        ApprovalRequest(
            requested_action="publish report",
            risk_level="high",
            reason="workflow review",
            payload={},
        )
    )
    _approve(store, foreign.approval_id)

    assert authority.resolve("missing", _binding(call)).verdict == ToolApprovalVerdict.NOT_FOUND
    assert authority.resolve(foreign.approval_id, _binding(call)).verdict == (
        ToolApprovalVerdict.MALFORMED
    )


def test_tool_call_from_approval_rebuilds_original_call_for_current_identity() -> None:
    _, store, _, record, call = _setup()
    resumed_identity = _identity(activity_id="activity-2", attempt=2)

    replay = tool_call_from_approval(
        store.get_approval(record.approval_id),
        graph_identity=resumed_identity,
    )

    assert replay.tool_name == call.tool_name
    assert replay.arguments == call.arguments
    assert replay.requested_by_agent_id == "agent-1"
    assert replay.approval_id == record.approval_id
    assert replay.call_id == "call-1:approved"
    assert replay.metadata == {"approval_replay_of": "call-1"}
    assert replay.graph_identity == resumed_identity


def test_binding_round_trip_rejects_unknown_version() -> None:
    binding = _binding(_call())

    assert ToolApprovalBinding.from_dict(binding.to_dict()) == binding
    with pytest.raises(ValueError):
        ToolApprovalBinding.from_dict({**binding.to_dict(), "binding_version": "v0"})


def test_tool_approval_request_requires_matching_tool_name() -> None:
    with pytest.raises(ValueError, match="tool_name"):
        ToolApprovalRequest(
            tool_call=_call(),
            tool_name="sample.other",
            side_effect="external_write",
            reason="mismatch",
            risk_level="high",
        )
