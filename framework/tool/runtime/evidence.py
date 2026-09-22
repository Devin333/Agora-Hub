"""Framework-neutral evidence capability for one controlled tool execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from framework.shared.attempts import AttemptIdentity, AttemptOutcome
from framework.tool.models import ToolCall, ToolDefinition, ToolObservation
from framework.tool.runtime.errors import ToolRuntimeError


class ToolEvidencePersistenceError(ToolRuntimeError):
    """A required evidence write failed at a safety boundary."""


@dataclass(frozen=True, slots=True)
class ToolRunnerTurnEvidence:
    """Durable identity of the candidate turn which requested a tool call."""

    ref: str
    checksum: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.ref, str)
            or not self.ref
            or self.ref != self.ref.strip()
            or len(self.ref) > 2048
        ):
            raise ValueError("runner turn ref must be a bounded canonical reference")
        if not isinstance(self.checksum, str) or re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.checksum
        ) is None:
            raise ValueError("runner turn checksum must be canonical sha256")


@runtime_checkable
class ToolExecutionEvidencePort(Protocol):
    """Persist tool lifecycle facts before observations become consumable.

    Implementations are injected for a single trusted child attempt.  The tool
    runtime deliberately knows nothing about Harness plans or child identities;
    the capability owns that immutable attribution and rejects cross-scope calls.
    """

    is_durable: bool

    def commit_runner_turn(
        self,
        turn: Mapping[str, Any],
    ) -> ToolRunnerTurnEvidence:
        """Persist and verify the model turn before registering its tool call."""

    def register_call(self, call: ToolCall) -> None:
        """Commit the logical request before resolution or execution."""

    def bind_tool(self, call: ToolCall, definition: ToolDefinition) -> None:
        """Commit the exact resolved tool implementation and policy metadata."""

    def admit_attempt(self, call: ToolCall, identity: AttemptIdentity) -> None:
        """Commit one physical intent before invoking the adapter."""

    def record_attempt_terminal(
        self,
        call: ToolCall,
        outcome: AttemptOutcome[Any],
    ) -> None:
        """Record trusted attempt termination without manufacturing a result."""

    def commit_observation(
        self,
        observation: ToolObservation,
        definition: ToolDefinition | None,
    ) -> str:
        """Commit and verify the final receipt, returning its immutable ref."""


__all__ = [
    "ToolEvidencePersistenceError",
    "ToolExecutionEvidencePort",
    "ToolRunnerTurnEvidence",
]
