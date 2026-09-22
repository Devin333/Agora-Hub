from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator

from framework.llm.budget.tracker import GlobalBudgetTracker
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True, slots=True)
class LLMBudgetInvocation:
    """Invocation-local budget authority passed without mutating a shared client."""

    tracker: GlobalBudgetTracker | None
    execution_identity: GraphExecutionIdentity | None
    execution_guard: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if self.tracker is not None and not isinstance(
            self.tracker,
            GlobalBudgetTracker,
        ):
            raise TypeError("tracker must be GlobalBudgetTracker or None")
        if self.execution_identity is not None and not isinstance(
            self.execution_identity,
            GraphExecutionIdentity,
        ):
            raise TypeError("execution_identity must be GraphExecutionIdentity or None")
        if (
            self.tracker is not None
            and self.tracker.execution_identity != self.execution_identity
        ):
            raise ValueError(
                "invocation budget tracker does not match its Graph execution identity"
            )
        if self.execution_guard is not None and not callable(self.execution_guard):
            raise TypeError("execution_guard must be callable or None")


_CURRENT_LLM_BUDGET_INVOCATION: ContextVar[LLMBudgetInvocation | None] = (
    ContextVar("newsroom_llm_budget_invocation", default=None)
)


@contextmanager
def bind_llm_budget_invocation(
    tracker: GlobalBudgetTracker | None,
    *,
    execution_identity: GraphExecutionIdentity | None,
    execution_guard: Callable[[], None] | None = None,
) -> Iterator[LLMBudgetInvocation]:
    """Bind one budget capability for the dynamic extent of an LLM invocation."""

    invocation = LLMBudgetInvocation(
        tracker=tracker,
        execution_identity=execution_identity,
        execution_guard=execution_guard,
    )
    token = _CURRENT_LLM_BUDGET_INVOCATION.set(invocation)
    try:
        yield invocation
    finally:
        _CURRENT_LLM_BUDGET_INVOCATION.reset(token)


def current_llm_budget_invocation() -> LLMBudgetInvocation | None:
    return _CURRENT_LLM_BUDGET_INVOCATION.get()


__all__ = [
    "LLMBudgetInvocation",
    "bind_llm_budget_invocation",
    "current_llm_budget_invocation",
]
