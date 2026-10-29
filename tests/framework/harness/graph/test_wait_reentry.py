"""Re-entrant approval Waits: DSL contract, compiler placement and re-entry."""

from __future__ import annotations

import pytest

from framework.events.canonical import checksum_for
from framework.harness.agent_loop import AgentLoopGraphApprovalWaitBinding
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_state import (
    HarnessLoopIteration,
    HarnessNodeInstanceIdentity,
)
from framework.harness.control_plane.graph_wait_approval import _immediately_precedes
from framework.harness.graph import (
    BoundedLoop,
    Choice,
    ChoiceBranch,
    ConditionPredicate,
    HarnessContractKind,
    HarnessContractReference,
    HarnessControlNode,
    HarnessGraphCompiler,
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessGraphNodeKind,
    HarnessGraphSpec,
    HarnessLeafActivityKind,
    HarnessStepSpec,
    HarnessWorkerType,
    Pass,
    Sequence,
    StepRef,
    Wait,
)
from framework.harness.graph.dsl import expression_from_dict
from framework.harness.side_effects.models import HarnessTerminalSideEffectPolicy

_GRAPH_ID = "test.wait-reentry"
_GRAPH_CHECKSUM = "sha256:" + "a" * 64


def _approval_wait(
    *,
    resume_node_id: str | None = "agent",
    kind: str = "approval",
    correlation: dict[str, str] | None = None,
) -> Wait:
    return Wait(
        wait_id="tool-approval",
        kind=kind,
        correlation=(
            {"approval_id": "graph.outputs.agent_output.approval_wait.approval_id"}
            if correlation is None
            else correlation
        ),
        signal_type="approval",
        signal_version="1",
        tenant_scope_path="graph.inputs.tenant_id",
        identity_scope_path="graph.inputs.actor_id",
        resume_node_id=resume_node_id,
    )


def _approval_loop(body_wait: Wait) -> BoundedLoop:
    return BoundedLoop(
        loop_id="approval-loop",
        body=Sequence(
            (
                StepRef("agent"),
                Choice(
                    "approval-choice",
                    (
                        ChoiceBranch(
                            "waiting",
                            body_wait,
                            priority=1,
                            condition=ConditionPredicate(
                                "graph.outputs.agent_output.waiting", "equals", True
                            ),
                        ),
                        ChoiceBranch(
                            "completed",
                            Pass("approval-completed"),
                            priority=2,
                            is_default=True,
                        ),
                    ),
                ),
            ),
            sequence_id="approval-loop.body",
        ),
        condition=ConditionPredicate(
            "graph.outputs.agent_output.waiting", "equals", True
        ),
        max_iterations=3,
        exit=Pass("approval-exit"),
    )


def _definition(root_expression) -> HarnessGraphDefinition:
    activities = (
        HarnessStepSpec("agent", HarnessWorkerType.FUNCTION, output_key="agent_output"),
        HarnessStepSpec("after", HarnessWorkerType.FUNCTION, output_key="after_output"),
    )
    return HarnessGraphDefinition(
        graph_id=_GRAPH_ID,
        graph_version="1",
        root=HarnessGraphSpec(
            graph_id=_GRAPH_ID,
            root=root_expression,
            input_keys=("actor_id", "tenant_id"),
        ),
        activities=activities,
        leaf_activity_bindings=tuple(
            HarnessGraphLeafBinding(
                activity_id=activity.step_id,
                leaf_activity_kind=HarnessLeafActivityKind.FUNCTION,
                worker_ref=HarnessContractReference(
                    HarnessContractKind.WORKER, f"test.{activity.step_id}.worker", "1"
                ),
                activity_ref=HarnessContractReference(
                    HarnessContractKind.ACTIVITY,
                    f"test.{activity.step_id}.activity",
                    "1",
                ),
            )
            for activity in activities
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="test.terminal",
            version="1",
            handler="test.terminal@1",
            kind="artifact",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for({"terminal": _GRAPH_ID}),
        ),
    )


def _compile(root_expression):
    return HarnessGraphCompiler().compile(_definition(root_expression)).graph


def test_pass_and_reentrant_wait_round_trip_through_the_dsl() -> None:
    expression = Sequence((Pass("noop"), _approval_wait()))

    payload = expression.to_dict()

    assert payload["children"][0] == {"kind": "pass", "pass_id": "noop"}
    assert payload["children"][1]["resume_node_id"] == "agent"
    assert expression_from_dict(payload) == expression
    # Non re-entrant Waits keep their historical wire shape.
    assert "resume_node_id" not in _approval_wait(resume_node_id=None).to_dict()


def test_only_approval_waits_may_reenter_a_node() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _approval_wait(kind="signal")

    assert raised.value.code == "wait_resume_node_requires_approval"


def test_reentrant_wait_must_correlate_on_approval_id() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _approval_wait(correlation={"run": "graph.inputs.run_id"})

    assert raised.value.code == "wait_resume_approval_id_missing"


def test_compiler_pins_reentrant_wait_to_its_loop_target() -> None:
    graph = _compile(Sequence((_approval_loop(_approval_wait()), StepRef("after"))))

    (wait_node,) = (
        node
        for node in graph.nodes
        if isinstance(node, HarnessControlNode) and node.wait is not None
    )
    assert wait_node.wait.resume_node_id == "agent"
    # Pass compiles to a control-only node that never dispatches a worker.
    pass_kinds = {
        node.node_id: node.node_kind
        for node in graph.nodes
        if node.node_id in {"approval-completed", "approval-exit"}
    }
    assert pass_kinds == {
        "approval-completed": HarnessGraphNodeKind.TERMINAL,
        "approval-exit": HarnessGraphNodeKind.TERMINAL,
    }


def test_compiler_rejects_resume_target_that_is_not_executable() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _compile(
            Sequence(
                (
                    _approval_loop(_approval_wait(resume_node_id="approval-completed")),
                    StepRef("after"),
                )
            )
        )

    assert raised.value.code == "wait_resume_node_invalid"


def test_compiler_rejects_unknown_resume_target() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _compile(
            Sequence(
                (
                    _approval_loop(_approval_wait(resume_node_id="missing")),
                    StepRef("after"),
                )
            )
        )

    assert raised.value.code == "wait_resume_node_invalid"


def test_compiler_rejects_resume_target_outside_the_waits_loop() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _compile(
            Sequence(
                (
                    _approval_loop(_approval_wait(resume_node_id="after")),
                    StepRef("after"),
                )
            )
        )

    assert raised.value.code == "wait_resume_node_scope_invalid"


def test_compiler_rejects_reentrant_wait_outside_any_loop() -> None:
    with pytest.raises(HarnessValidationError) as raised:
        _compile(Sequence((StepRef("agent"), _approval_wait(), StepRef("after"))))

    assert raised.value.code == "wait_resume_node_scope_invalid"


@pytest.mark.parametrize("rounds", [0, -1, True, "2"])
def test_agent_loop_approval_loop_requires_a_positive_round_budget(rounds) -> None:
    binding = AgentLoopGraphApprovalWaitBinding(
        source_node_id="agent",
        result_output_key="agent_output",
        wait_id="tool-approval",
    )

    with pytest.raises(HarnessValidationError) as raised:
        binding.approval_loop(loop_id="approval-loop", max_approval_rounds=rounds)

    assert raised.value.code == "agent_loop_graph_approval_loop_invalid"


def _identity(
    node_id: str,
    *,
    branch_path: tuple[str, ...] = (),
    iterations: tuple[tuple[str, int], ...] = (("approval-loop", 0),),
) -> HarnessNodeInstanceIdentity:
    return HarnessNodeInstanceIdentity(
        run_id="run-1",
        graph_checksum=_GRAPH_CHECKSUM,
        node_id=node_id,
        branch_path=branch_path,
        iteration_vector=tuple(
            HarnessLoopIteration(loop_id=loop_id, iteration=iteration)
            for loop_id, iteration in iterations
        ),
    )


def test_wait_in_a_choice_branch_precedes_the_next_iteration_of_its_node() -> None:
    wait = _identity("tool-approval", branch_path=("waiting",))
    resumed = _identity("agent", iterations=(("approval-loop", 1),))

    assert _immediately_precedes(wait, resumed) is True


@pytest.mark.parametrize(
    ("wait", "resumed"),
    [
        # Same iteration: the node ran before the Wait, it is not a re-entry.
        (
            _identity("tool-approval", branch_path=("waiting",)),
            _identity("agent"),
        ),
        # Two iterations later: an older grant must never be redeemed.
        (
            _identity("tool-approval", branch_path=("waiting",)),
            _identity("agent", iterations=(("approval-loop", 2),)),
        ),
        # Sibling branch scope: the Wait belongs to another branch.
        (
            _identity("tool-approval", branch_path=("left", "waiting")),
            _identity("agent", branch_path=("right",), iterations=(("approval-loop", 1),)),
        ),
        # Different loop identity.
        (
            _identity("tool-approval", branch_path=("waiting",)),
            _identity("agent", iterations=(("other-loop", 1),)),
        ),
        # Different outer iteration.
        (
            _identity(
                "tool-approval",
                branch_path=("waiting",),
                iterations=(("outer", 0), ("approval-loop", 0)),
            ),
            _identity("agent", iterations=(("outer", 1), ("approval-loop", 1))),
        ),
    ],
)
def test_wait_does_not_precede_unrelated_activations(wait, resumed) -> None:
    assert _immediately_precedes(wait, resumed) is False
