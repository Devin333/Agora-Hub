"""Replay-safe, dependency-free lifecycle validators for parallel execution."""
from __future__ import annotations

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.parallel_lifecycle import (
    DispatchGroupState,
    DispatchWaveState,
    DispatchWaveTerminalOutcome,
    _GROUP_TRANSITIONS,
    _WAVE_TRANSITIONS,
)


def validate_group_transition(current: str, target: str, *, code: str = "TASK_GROUP_INVALID_TRANSITION") -> None:
    try:
        source = DispatchGroupState(current)
        destination = DispatchGroupState(target)
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError("unknown DispatchGroup state", code=code) from exc
    if destination is source:
        return
    if destination not in _GROUP_TRANSITIONS[source]:
        raise HarnessValidationError(
            "DispatchGroup transition is not allowed",
            code=code,
            details={"from_state": source.value, "to_state": destination.value},
        )


def validate_wave_transition(current: str, target: str, *, code: str = "TASK_WAVE_INVALID_TRANSITION") -> None:
    try:
        source = DispatchWaveState(current)
        destination = DispatchWaveState(target)
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError("unknown DispatchWave state", code=code) from exc
    if destination is source:
        return
    if destination not in _WAVE_TRANSITIONS[source]:
        raise HarnessValidationError(
            "DispatchWave transition is not allowed",
            code=code,
            details={"from_state": source.value, "to_state": destination.value},
        )


__all__ = [
    "DispatchGroupState",
    "DispatchWaveState",
    "DispatchWaveTerminalOutcome",
    "validate_group_transition",
    "validate_wave_transition",
]
