from __future__ import annotations

from framework.agent.loop.events import AgentLoopEventRecorder
from framework.agent.models import AgentLoopEventType
from framework.agent.models.trace import AgentLoopTrace
from framework.events import TraceContext, is_valid_span_id, is_valid_trace_id
from framework.events.runtime.projection import RuntimeEventProjection, RuntimeEventType
from framework.shared import GraphExecutionIdentity


def test_agent_loop_event_recorder_outputs_trace_fields() -> None:
    trace = TraceContext.root(
        execution_identity=GraphExecutionIdentity(
            run_id="run-1",
            graph_id="research.graph",
            graph_version="1",
            graph_ref="research.graph@1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analyze",
            node_instance_id="analyze:1",
            activity_id="activity-1",
            attempt=1,
        ),
        trace_id="1" * 32,
        span_id="2" * 16,
    )

    recorder = AgentLoopEventRecorder(agent_id="agent-1", trace_context=trace)
    event = recorder.emit(AgentLoopEventType.AGENT_STARTED)

    assert event["run_id"] == "run-1"
    assert is_valid_trace_id(event["trace_id"])
    assert is_valid_span_id(event["span_id"])
    assert event["parent_span_id"] == trace.span_id
    assert event["agent_id"] == "agent-1"


def test_agent_loop_human_review_wait_is_projected_as_canonical_approval_request() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-approval",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        activity_id="activity-1",
        attempt=1,
    )
    projection = RuntimeEventProjection()
    recorder = AgentLoopEventRecorder(
        agent_id="agent-1",
        execution_identity=identity,
        runtime_event_sink=projection,
    )

    recorder.waiting_for_approval(
        iteration=2,
        stop_reason="tool_approval_required",
        approval_id="approval-human-1",
        approval_kind="human_review",
        tool_name="control.request_human_review",
        control_action="request_human_review",
    )

    events = projection.store.all_events(stream_id=identity.run_id)
    assert len(events) == 1
    event = events[0]
    assert event.event_type is RuntimeEventType.APPROVAL_REQUESTED
    assert event.reason_code == "approval_requested"
    assert event.refs == ("approval-human-1",)
    assert event.identity.graph_identity == identity


def test_agent_loop_tool_approval_wait_is_not_duplicated_by_agent_loop_projection() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-tool-approval",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum="sha256:" + "b" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        activity_id="activity-1",
        attempt=1,
    )
    projection = RuntimeEventProjection()
    recorder = AgentLoopEventRecorder(
        agent_id="agent-1",
        execution_identity=identity,
        runtime_event_sink=projection,
    )

    recorder.waiting_for_approval(
        iteration=2,
        stop_reason="tool_approval_required",
        approval_id="approval-tool-1",
        approval_kind="tool_approval",
    )

    assert projection.store.all_events(stream_id=identity.run_id) == ()


def test_agent_loop_trace_default_iteration_ids_are_w3c_compatible() -> None:
    trace = AgentLoopTrace(agent_id="agent-1")

    first = trace.start_iteration(
        1,
        feedback=None,
        tool_observation_count_before=0,
        tools_available=[],
    )
    second = trace.start_iteration(
        2,
        feedback="retry",
        tool_observation_count_before=1,
        tools_available=["sample.echo"],
    )

    assert is_valid_trace_id(trace.trace_id)
    assert is_valid_span_id(trace.root_span_id)
    assert first.trace_id == second.trace_id == trace.trace_id
    assert first.parent_span_id == second.parent_span_id == trace.root_span_id
    assert is_valid_span_id(first.span_id)
    assert is_valid_span_id(second.span_id)
    assert first.span_id != second.span_id


def test_agent_loop_trace_round_trip_preserves_execution_identity() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-trace-round-trip",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum="sha256:" + "b" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        activity_id="activity-1",
        attempt=1,
    )
    trace = AgentLoopTrace(
        agent_id="agent-1",
        execution_identity=identity,
    )
    trace.record_memory_candidate(
        {
            "memory_id": "candidate-1",
            "metadata": {"candidate_only": True},
        }
    )

    payload = trace.to_dict()
    restored = AgentLoopTrace.from_dict(payload)

    assert payload["execution_identity"] == identity.to_dict()
    assert restored.execution_identity == identity
    assert restored.memory_candidates == trace.memory_candidates
