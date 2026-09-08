"""Metadata replay does not require an unused SubAgent payload capability."""

from framework.harness.task_plan import TaskPlanRecoveryService, TaskPlanReplayReducer
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.framework.harness.task_plan.test_task_plan_recovery import (
    _EmptyQueueReader, _history_fixture,
)


def test_recorded_graph_identity_without_subagent_payload_preserves_replay():
    plan, events, worker = _history_fixture()
    execution = GraphExecutionIdentity(
        run_id=plan.run_id, graph_id=plan.graph_id, graph_version=plan.graph_version,
        graph_ref=plan.graph_ref, graph_checksum=plan.graph_checksum,
        node_id=plan.stage_id, node_instance_id="metadata-node",
        activity_id="metadata-activity", attempt=1,
    )
    baseline = TaskPlanReplayReducer().replay((plan,), events)
    bound = TaskPlanReplayReducer(execution_identity=execution).replay((plan,), events)
    assert bound == baseline
    recovered = TaskPlanRecoveryService(
        queue_reader=_EmptyQueueReader(), execution_identity=execution,
    ).recover((plan,), events)
    unbound = TaskPlanRecoveryService(queue_reader=_EmptyQueueReader()).recover((plan,), events)
    assert recovered == unbound
    assert worker.calls == 0
