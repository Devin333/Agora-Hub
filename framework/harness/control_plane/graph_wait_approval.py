"""Authoritative approval grants carried by durable Graph approval Waits.

``authoritative_wait_approval_id`` names the grant an approval Wait is parked
on, which the application boundary must match before accepting a decision.

``approval_resume_for_node`` resolves the grant a re-entered node redeems.

A re-entrant approval Wait (``Wait.resume_node_id``) parks a loop iteration
after an executable node asked for human approval.  Once the approval is
granted the Wait resolves, the enclosing loop starts its next iteration and
the same executable node is activated again.  That new activation must redeem
exactly the grant recorded by the Wait in the *immediately preceding*
iteration of the same branch -- never one supplied by a caller and never a
grant from an older or sibling iteration.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from framework.events.errors import EventIncompleteHistoryError
from framework.harness.control_plane.graph_state import (
    HarnessGraphState,
    HarnessNodeInstanceIdentity,
    HarnessNodeInstanceState,
    HarnessWaitStatus,
)
from framework.harness.graph.dsl import WaitKind
from framework.harness.graph.model import HarnessGraphNodeKind

APPROVAL_RESUME_TASK_KEY = "approval_resume"


def authoritative_wait_approval_id(
    state: HarnessGraphState,
    node_instance_id: str,
    *,
    graph_inputs: Mapping[str, Any] | None = None,
) -> str | None:
    """Return the grant the approval Wait at ``node_instance_id`` waits on.

    Re-entrant Waits record the grant in their durable registration, which
    is the only authority.  Historical non-re-entrant Waits fall back to the
    ``approval_id`` their node projection carries, resolving a
    ``graph.inputs.*`` reference against the run inputs.
    """

    registrations = [
        item
        for item in state.wait_registrations
        if item.node_instance_id == node_instance_id
        and item.kind is WaitKind.APPROVAL
    ]
    if registrations:
        registration = max(
            registrations,
            key=lambda item: (item.registered_sequence, item.wait_id),
        )
        if registration.reentrant:
            return registration.approval_id
    node = next(
        (item for item in state.node_instances if item.instance_id == node_instance_id),
        None,
    )
    if node is None or node.node_kind is not HarnessGraphNodeKind.WAIT:
        return None
    return _projected_approval_id(node.metadata, graph_inputs) or _projected_approval_id(
        node.output_refs, graph_inputs
    )


def _projected_approval_id(
    value: object,
    graph_inputs: Mapping[str, Any] | None,
) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get("approval_id")
        if isinstance(candidate, str) and candidate.strip():
            candidate = candidate.strip()
            if not candidate.startswith("graph.inputs."):
                return candidate
            if graph_inputs is None:
                return None
            resolved = graph_inputs.get(candidate.removeprefix("graph.inputs."))
            return resolved.strip() if isinstance(resolved, str) and resolved.strip() else None
        for nested in value.values():
            found = _projected_approval_id(nested, graph_inputs)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for nested in value:
            found = _projected_approval_id(nested, graph_inputs)
            if found is not None:
                return found
    return None


def approval_resume_for_node(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
) -> Mapping[str, Any] | None:
    """Return the durable approval grant ``node`` re-enters with, if any."""

    instances = {item.instance_id: item for item in state.node_instances}
    matches = []
    for registration in state.wait_registrations:
        if (
            not registration.reentrant
            or registration.status is not HarnessWaitStatus.RESUMED
            or registration.resume_node_id != node.identity.node_id
        ):
            continue
        wait_node = instances.get(registration.node_instance_id)
        if wait_node is None:
            raise EventIncompleteHistoryError(
                "re-entrant Wait registration references an unknown node instance"
            )
        if _immediately_precedes(wait_node.identity, node.identity):
            matches.append((registration, wait_node))
    if not matches:
        return None
    if len(matches) != 1:
        raise EventIncompleteHistoryError(
            "node re-entry resolves more than one approval Wait"
        )
    registration, wait_node = matches[0]
    return {
        "approval_id": registration.approval_id,
        "wait_id": registration.wait_id,
        "wait_node_instance_id": wait_node.instance_id,
    }


def _immediately_precedes(
    wait: HarnessNodeInstanceIdentity,
    resumed: HarnessNodeInstanceIdentity,
) -> bool:
    """Whether ``wait`` parked the loop iteration right before ``resumed``.

    The Wait usually sits inside a branch (for example the ``waiting`` arm of
    a Choice) of the loop body that also contains the resume target, so the
    target's branch scope must enclose the Wait's scope rather than equal it.
    The compiler pins both to the same innermost loop, which keeps the shared
    prefix of the scope outside the loop identical.
    """

    resumed_scope = resumed.branch_path
    if wait.branch_path[: len(resumed_scope)] != resumed_scope:
        return False
    wait_vector = wait.iteration_vector
    resumed_vector = resumed.iteration_vector
    if not resumed_vector or len(wait_vector) != len(resumed_vector):
        return False
    if wait_vector[:-1] != resumed_vector[:-1]:
        return False
    previous, current = wait_vector[-1], resumed_vector[-1]
    return (
        previous.loop_id == current.loop_id
        and previous.iteration + 1 == current.iteration
    )


__all__ = [
    "APPROVAL_RESUME_TASK_KEY",
    "approval_resume_for_node",
    "authoritative_wait_approval_id",
]
