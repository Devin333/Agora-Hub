"""Port for structure-aware navigation: choose paper sections worth reading.

After flat retrieval, a navigator sees the paper's section outline, the question,
and which sections the retrieved passages already come from, and names further
sections likely to hold the rest of the answer (another list item, the setup a
result depends on, a limitation stated elsewhere). Their passages join the
candidate pool and go through the usual evidence verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.research.document.section_outline import PaperSectionOutline
from framework.llm.models.usage import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True)
class SectionNavigationRequest:
    question: str
    outline: PaperSectionOutline
    # Sections the retrieved passages already come from, best first.
    read_section_indexes: tuple[int, ...]
    max_sections: int
    execution_identity: GraphExecutionIdentity | None = None

    def __post_init__(self) -> None:
        if not str(self.question).strip():
            raise ValueError("question is required")
        if type(self.max_sections) is not int or self.max_sections < 1:
            raise ValueError("max_sections must be a positive integer")


@dataclass(frozen=True)
class SectionNavigationDecision:
    # Chosen unread sections, most promising first; never more than requested.
    section_indexes: tuple[int, ...]
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SectionNavigatorPort(Protocol):
    @property
    def measurement_identity(self) -> Mapping[str, Any]:
        """Stable non-secret identity (prompt version, model) for evaluation."""
        ...

    async def choose(self, request: SectionNavigationRequest) -> SectionNavigationDecision:
        """Return the sections to read next.

        Transport failures raise ``PaperAnswerModelError``; unusable model output is
        reported as an empty decision with diagnostics, never as a guess.
        """
        ...


__all__ = [
    "SectionNavigationDecision",
    "SectionNavigationRequest",
    "SectionNavigatorPort",
]
