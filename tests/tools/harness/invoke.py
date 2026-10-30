"""Run tools through the production executor and assert on the outcome.

Tests never call a tool's handler directly: going through ``ToolExecutor`` and
``ToolBatchExecutor`` exercises the same schema validation, timeout
supervision, retry policy and observation building the Agent loop uses.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.tool import (
    ToolBatchExecutor,
    ToolCall,
    ToolExecutor,
    ToolObservation,
    ToolPolicy,
    ToolRegistry,
    ToolStatus,
)

_REPO_ROOT = str(Path(__file__).resolve().parents[3])
_TRACEBACK_MARKER = "Traceback (most recent call last)"


def stability_policy(registry: ToolRegistry, **overrides: Any) -> ToolPolicy:
    """Allow every registered tool and skip the approval gate.

    Approval behaviour has its own tests; here the question is how the tool
    itself behaves once it is allowed to run.
    """

    options: dict[str, Any] = {
        "allowed_tools": [definition.name for definition in registry.list_tools()],
        "require_explicit_allowlist": True,
        "allow_dangerous_tools": True,
        "require_approval_for_side_effects": False,
    }
    options.update(overrides)
    return ToolPolicy(**options)


@dataclass(frozen=True)
class ToolRun:
    observation: ToolObservation
    elapsed_seconds: float

    @property
    def status(self) -> ToolStatus:
        return self.observation.status

    @property
    def output(self) -> Any:
        return self.observation.result.output

    @property
    def error_type(self) -> str | None:
        return self.observation.result.error_type

    @property
    def error_message(self) -> str:
        return str(self.observation.result.error_message or "")

    def describe(self) -> str:
        result = self.observation.result
        return (
            f"{self.observation.tool_name} -> {self.status.value} "
            f"error_type={result.error_type} message={result.error_message!r} "
            f"indeterminate={result.indeterminate} elapsed={self.elapsed_seconds:.2f}s"
        )


def run_tool(
    registry: ToolRegistry,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    policy: ToolPolicy | None = None,
    executor: ToolExecutor | None = None,
) -> ToolRun:
    tool_executor = executor or ToolExecutor(registry)
    started = time.monotonic()
    observation = tool_executor.execute(
        ToolCall(tool_name=tool_name, arguments=dict(arguments or {})),
        policy or stability_policy(registry),
    )
    return ToolRun(observation=observation, elapsed_seconds=time.monotonic() - started)


def run_batch(
    registry: ToolRegistry,
    calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    policy: ToolPolicy | None = None,
    max_workers: int | None = None,
    mode: str = "best_effort",
) -> tuple[list[ToolObservation], float]:
    batch = ToolBatchExecutor(ToolExecutor(registry), max_workers=max_workers or len(calls) or 1)
    started = time.monotonic()
    observations = batch.execute_batch(
        [ToolCall(tool_name=name, arguments=dict(arguments)) for name, arguments in calls],
        policy or stability_policy(registry, max_tool_calls_per_iteration=max(len(calls), 1)),
        mode=mode,
    )
    return observations, time.monotonic() - started


def expect_succeeded(run: ToolRun) -> Any:
    assert run.status is ToolStatus.SUCCEEDED, run.describe()
    return run.output


def expect_failed(
    run: ToolRun,
    *,
    status: ToolStatus = ToolStatus.FAILED,
    error_type: str | None = None,
    message_contains: str | None = None,
    within_seconds: float | None = None,
    secrets: Iterable[str] = (),
) -> ToolRun:
    """Assert a controlled, diagnosable failure.

    A controlled failure has the expected terminal status, names its cause,
    finishes within its time budget, and does not leak internals.
    """

    assert run.status is status, run.describe()
    if error_type is not None:
        assert run.error_type == error_type, run.describe()
    assert run.error_message, f"failure carries no message: {run.describe()}"
    if message_contains is not None:
        assert message_contains in run.error_message, run.describe()
    if within_seconds is not None:
        assert run.elapsed_seconds <= within_seconds, run.describe()
    assert_error_is_safe(run, secrets=secrets)
    return run


def assert_error_is_safe(run: ToolRun, *, secrets: Iterable[str] = ()) -> None:
    """The model sees error text verbatim, so it must not expose internals."""

    message = run.error_message
    assert _TRACEBACK_MARKER not in message, run.describe()
    assert _REPO_ROOT.casefold() not in message.casefold(), run.describe()
    for secret in secrets:
        if secret:
            assert secret not in message, f"secret leaked into error: {run.describe()}"
