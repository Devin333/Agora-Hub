"""Canonical-history constraints shared by admission commits and replay."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import thaw_mapping
from framework.harness.task_plan.parallel_lifecycle import DispatchWaveTerminalOutcome
from framework.harness.task_plan.models import ValidatedTaskPlan


class _AdmissionEvent(Protocol):
    event_type: str
    payload: Mapping[str, Any]


def validate_group_plan_binding(group: Mapping[str, Any], plan: ValidatedTaskPlan) -> None:
    from framework.harness.task_plan.parallel import DispatchGroup

    DispatchGroup.from_dict(group)
    expected = {
        "run_id": plan.run_id, "stage_id": plan.stage_id,
        "plan_id": plan.plan_id, "plan_version": plan.version,
        "plan_checksum": plan.plan_checksum,
        "policy_ref": plan.policy_ref, "policy_checksum": plan.policy_checksum,
        "budget_envelope": plan.limits.aggregate_task_budget.to_dict(),
    }
    parent = group.get("parent_graph_identity")
    if (
        any(group.get(name) != value for name, value in expected.items())
        or tuple(group.get("task_ids", ())) != tuple(item.task_id for item in plan.tasks)
        or tuple(group.get("required_output_roles", ())) != plan.required_output_roles
        or not isinstance(parent, Mapping)
        or any(parent.get(name) != getattr(plan, name) for name in (
            "run_id", "graph_id", "graph_version", "graph_ref", "graph_checksum",
        ))
    ):
        raise HarnessValidationError("dispatch group differs from complete accepted plan binding", code="TASK_GROUP_SCOPE_MISMATCH")


def validate_wave_admission_slot(
    group: Mapping[str, Any],
    wave: Mapping[str, Any],
    waves: Iterable[Mapping[str, Any]],
    *,
    code: str = "TASK_GROUP_ACTIVE_WAVE_CONFLICT",
) -> None:
    previous = tuple(item for item in waves if item["group_id"] == group["group_id"])
    if not set(wave["task_ids"]).issubset(group["task_ids"]) or wave["effective_parallelism"] > group["max_parallelism"]:
        raise HarnessValidationError("wave exceeds immutable group scope or capacity", code=code)
    if any(item["state"] != "TERMINAL" for item in previous):
        raise HarnessValidationError("dispatch group already has an active wave", code=code)
    ordinal = wave.get("ordinal")
    if (
        isinstance(ordinal, bool)
        or not isinstance(ordinal, int)
        or ordinal != len(previous) + 1
        or ordinal > group["max_waves"]
        or any(item["wave_id"] == wave.get("wave_id") for item in previous)
    ):
        raise HarnessValidationError("wave ordinal must advance the bounded group sequence", code=code)


def validate_parallel_admission_append(
    history: Iterable[_AdmissionEvent],
    events: Iterable[_AdmissionEvent],
) -> None:
    """Check admission against the exact prefix protected by the store CAS.

    This index is derived, never persisted independently. The event sequence is
    its revision; task outcomes and full lifecycle validation remain in replay.
    """
    batch = tuple(events)
    if not any(
        event.event_type in {"TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED", "TASK_WAVE_COMPLETED"}
        or (event.event_type.startswith("TASK_GROUP_") and "group" in event.payload)
        for event in batch
    ):
        return
    groups: dict[str, dict[str, Any]] = {}
    owners: set[tuple[Any, ...]] = set()
    waves: dict[str, dict[str, Any]] = {}
    for event in (*tuple(history), *batch):
        payload = event.payload
        if event.event_type == "TASK_GROUP_ADMITTED":
            group = payload.get("group")
            if not isinstance(group, Mapping) or not isinstance(group.get("group_id"), str):
                raise HarnessValidationError("group admission snapshot is missing", code="TASK_GROUP_ADMISSION_CONFLICT")
            if group.get("state") != "ADMITTED":
                raise HarnessValidationError("group admission requires admitted state", code="TASK_GROUP_ADMISSION_CONFLICT")
            owner = tuple(group.get(name) for name in ("run_id", "stage_id", "plan_id", "plan_version"))
            if group["group_id"] in groups or owner in owners:
                raise HarnessValidationError("accepted plan already owns a dispatch group", code="TASK_GROUP_ADMISSION_CONFLICT")
            groups[group["group_id"]] = dict(group)
            owners.add(owner)
        elif event.event_type == "TASK_WAVE_ADMITTED":
            from framework.harness.task_plan.parallel import DispatchWave

            wave = payload.get("wave")
            snapshot = payload.get("group")
            if not isinstance(wave, Mapping) or not isinstance(snapshot, Mapping):
                raise HarnessValidationError("wave admission snapshot is missing", code="TASK_GROUP_ADMISSION_CONFLICT")
            wave = DispatchWave.from_dict(thaw_mapping(wave)).to_dict()
            if wave["state"] != "ADMITTED":
                raise HarnessValidationError("wave admission requires admitted state", code="TASK_GROUP_ADMISSION_CONFLICT")
            group = groups.get(wave.get("group_id"))
            if group is None or any(snapshot.get(name) != value for name, value in group.items() if name != "state"):
                raise HarnessValidationError("wave admission differs from durable group", code="TASK_GROUP_ADMISSION_CONFLICT")
            if group["state"] not in {"ADMITTED", "DISPATCHING", "RUNNING"}:
                raise HarnessValidationError("durable group is closed to admission", code="TASK_GROUP_ADMISSION_CONFLICT")
            validate_wave_admission_slot(group, wave, waves.values())
            waves[wave["wave_id"]] = dict(wave)
        elif event.event_type == "TASK_WAVE_COMPLETED":
            wave = waves.get(payload.get("wave_id"))
            if (
                wave is None
                or wave["group_id"] != payload.get("group_id")
                or wave["state"] == "TERMINAL"
                or tuple(payload.get("task_ids", ())) != tuple(wave["task_ids"])
                or payload.get("terminal_outcome") not in {item.value for item in DispatchWaveTerminalOutcome}
            ):
                raise HarnessValidationError("wave terminal evidence conflicts with admission", code="TASK_GROUP_ACTIVE_WAVE_CONFLICT")
            wave["state"] = "TERMINAL"
        elif event.event_type.startswith("TASK_GROUP_") and isinstance(payload.get("group"), Mapping):
            snapshot = payload["group"]
            group = groups.get(snapshot.get("group_id"))
            if group is None or any(snapshot.get(name) != value for name, value in group.items() if name != "state"):
                raise HarnessValidationError("group transition differs from admission", code="TASK_GROUP_ADMISSION_CONFLICT")
            group["state"] = snapshot["state"]


__all__ = ["validate_group_plan_binding", "validate_parallel_admission_append", "validate_wave_admission_slot"]
