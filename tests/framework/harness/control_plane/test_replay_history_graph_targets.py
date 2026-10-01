from __future__ import annotations

import pytest

from framework.events.errors import EventStoreCorruptionError
from framework.events.runtime.activities import ReplayActivityKind
from framework.harness.control_plane import replay_history
from framework.harness.graph.activity import HarnessWorkerType


def test_task_plan_replay_activity_uses_composite_tool_family() -> None:
    assert (
        replay_history.harness_activity_kind(HarnessWorkerType.TASK_PLAN)
        is ReplayActivityKind.TOOL
    )


def test_replay_activity_kind_rejects_unknown_worker_type() -> None:
    with pytest.raises(
        EventStoreCorruptionError,
        match="unsupported Harness replay activity type: unknown_worker",
    ):
        replay_history.harness_activity_kind("unknown_worker")


def test_replay_command_target_never_falls_back_to_step_id() -> None:
    command = replay_history._decision_command(
        command_ordinal=1,
        decision_input={"budget": {}, "gate_results": ()},
        decision_projection={
            "decision_type": "execute_step",
            "node_id": "node:compose",
            "node_instance_id": "node:compose:1",
            "step_id": "legacy-step-target",
            "target_step_id": "legacy-repair-target",
            "target_node_id": None,
            "payload": {},
        },
        graph_version="1",
        causation_id="cause-1",
    )

    assert command.target == "node:compose"


def test_replay_kernel_rejects_legacy_repair_step_authority() -> None:
    decision_input = _decision_input(
        current_node_policy={
            "repair_step_id": "legacy-repair",
            "repair_node_ids": {
                "verification_failure": "node:repair",
            },
        },
    )

    with pytest.raises(ValueError, match="repair_step_id"):
        replay_history.harness_decision_kernel(decision_input, None)


def test_replay_kernel_routes_to_checksum_bound_repair_node() -> None:
    decision = replay_history.harness_decision_kernel(
        _decision_input(
            current_node_policy={
                "repair_node_ids": {
                    "verification_failure": "node:repair",
                },
            },
        ),
        None,
    )

    assert decision["decision_type"] == "route_to_repair"
    assert decision["target_node_id"] == "node:repair"


def _decision_input(*, current_node_policy: dict[str, object]) -> dict[str, object]:
    return {
        "schema": replay_history.HARNESS_DECISION_INPUT_SCHEMA,
        "command_ordinal": 1,
        "causation_id": "cause-1",
        "run_id": "run-1",
        "graph_id": "graph-1",
        "graph_version": "1",
        "graph_checksum": "sha256:" + "0" * 64,
        "entry_node_ids": ("node:entry",),
        "graph_nodes": (),
        "graph_edges": (),
        "current_node_policy": current_node_policy,
        "before_state_checksum": "sha256:" + "1" * 64,
        "run_status": "verifying",
        "current_node_instance_id": "node:entry:1",
        "current_node_id": "node:entry",
        "turn_count": 0,
        "replan_count": 0,
        "worker_call_count": 0,
        "node_instances": (
            {
                "node_instance_id": "node:entry:1",
                "node_id": "node:entry",
                "status": "verifying",
                "step_status": "verifying",
                "attempts": 1,
                "replans": 0,
                "approval_granted": False,
            },
        ),
        "budget": {
            "max_turns": 1,
            "max_worker_calls": 1,
            "max_replans": 1,
            "max_retries_per_step": 1,
        },
        "gate_results": ({"passed": False},),
        "quality_verdict": None,
        "approval_outcome": None,
        "routing_values": {},
        "expected_activity": None,
    }
