from interfaces.events import AuditEmitter, InMemoryAuditSink
from interfaces.models import actor_context_from_headers


def test_actor_context_expands_role_permissions() -> None:
    actor = actor_context_from_headers(
        {"X-News-Actor": "devin", "X-News-Roles": "operator"},
        request_id="req-1",
    )

    assert actor.actor_id == "devin"
    assert actor.has_permission("runs:create") is True
    assert actor.has_permission("write:runs") is True
    assert actor.has_permission("reports:publish") is False
    assert actor.has_permission("manage:approvals") is False


def test_actor_context_supports_target_interface_roles() -> None:
    viewer = actor_context_from_headers(
        {"X-News-Actor": "reader", "X-News-Roles": "viewer"},
        request_id="req-1",
    )
    mcp_client = actor_context_from_headers(
        {"X-News-Actor": "mcp", "X-News-Roles": "mcp_client"},
        request_id="req-2",
    )

    assert viewer.has_permission("read:reports") is True
    assert viewer.has_permission("reports:read") is True
    assert viewer.has_permission("runs:create") is False
    assert viewer.has_permission("write:runs") is False
    assert mcp_client.has_permission("mcp:read") is True
    assert mcp_client.has_permission("reports:publish") is False


def test_event_read_permission_cannot_imply_operate_or_research_replay() -> None:
    reader = actor_context_from_headers(
        {"X-News-Actor": "reader", "X-News-Permissions": "events:read"},
        request_id="req-read",
    )
    research_replayer = actor_context_from_headers(
        {"X-News-Actor": "replayer", "X-News-Permissions": "research.event.replay"},
        request_id="req-replay",
    )

    assert reader.has_permission("events:read")
    assert not reader.has_permission("events:operate")
    assert not reader.has_permission("research.event.replay")
    assert research_replayer.has_permission("research.event.replay")
    assert research_replayer.has_permission("events:read")
    assert research_replayer.has_permission("events:operate")


def test_audit_emitter_redacts_sensitive_metadata() -> None:
    sink = InMemoryAuditSink()
    emitter = AuditEmitter(sink)
    actor = actor_context_from_headers({"Authorization": "Bearer hidden"}, request_id="req-1")

    record = emitter.emit(
        actor=actor,
        action="api_request_post",
        resource_type="runs",
        result="succeeded",
        metadata={"api_key": "secret-value", "safe": "value"},
    )

    assert record is sink.records[0]
    assert sink.records[0].metadata["api_key"] == "[redacted]"
    assert sink.records[0].metadata["safe"] == "value"
