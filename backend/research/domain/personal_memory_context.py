from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from backend.research.domain.personal_memory import (
    MAX_SNAPSHOT_RECORDS,
    PersonalMemorySelection,
    PersonalMemorySourceRef,
    PersonalMemoryTaskSnapshot,
)


@dataclass(frozen=True, slots=True)
class PersonalMemoryContextItem:
    """One confirmed preference exposed to a task as non-authoritative user context."""

    memory_id: str
    revision: int
    content: str
    category: str
    source_refs: tuple[PersonalMemorySourceRef, ...]

    def __post_init__(self) -> None:
        PersonalMemorySelection(memory_id=self.memory_id, revision=self.revision)
        if not isinstance(self.content, str) or not self.content:
            raise ValueError("personal memory context content must be nonblank")
        if not isinstance(self.category, str) or not self.category:
            raise ValueError("personal memory context category must be nonblank")
        if not isinstance(self.source_refs, tuple) or not self.source_refs:
            raise ValueError("personal memory context source_refs must be a non-empty tuple")
        if not all(isinstance(item, PersonalMemorySourceRef) for item in self.source_refs):
            raise TypeError("personal memory context source_refs must be immutable source refs")

    def to_worker_dict(self) -> dict[str, Any]:
        """Project only task-relevant fields for a remote worker.

        ``source_refs`` remain available in the frozen local snapshot for audit, but
        may contain local paths, private URLs, or conversation locators and therefore
        are deliberately excluded from the remote projection.
        """

        return {
            "memoryId": self.memory_id,
            "revision": self.revision,
            "content": self.content,
            "category": self.category,
        }


@dataclass(frozen=True, slots=True)
class PersonalMemoryContext:
    """Frozen, owner-validated personal preferences for one external task.

    This context is intentionally separate from paper evidence and execution memory.
    It may guide presentation and reading emphasis, but cannot authorize tools, change
    workflow routing, satisfy evidence gates, or override the current request.
    """

    policy_revision: int
    items: tuple[PersonalMemoryContextItem, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.policy_revision, bool)
            or not isinstance(self.policy_revision, int)
            or self.policy_revision < 1
        ):
            raise ValueError("policy_revision must be a positive integer")
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("personal memory context requires a non-empty item tuple")
        if len(self.items) > MAX_SNAPSHOT_RECORDS:
            raise ValueError("personal memory context exceeds the snapshot record limit")
        if not all(isinstance(item, PersonalMemoryContextItem) for item in self.items):
            raise TypeError("personal memory context items must be context items")
        memory_ids = {item.memory_id for item in self.items}
        if len(memory_ids) != len(self.items):
            raise ValueError("personal memory context memory ids must be unique")

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PersonalMemoryTaskSnapshot,
        *,
        policy_revision: int,
    ) -> "PersonalMemoryContext":
        if not isinstance(snapshot, PersonalMemoryTaskSnapshot):
            raise TypeError("snapshot must be PersonalMemoryTaskSnapshot")
        return cls(
            policy_revision=policy_revision,
            items=tuple(
                PersonalMemoryContextItem(
                    memory_id=record.memory_id,
                    revision=record.revision,
                    content=record.content,
                    category=record.category.value,
                    source_refs=tuple(record.source_refs),
                )
                for record in snapshot.records
            ),
        )

    @property
    def used_refs(self) -> tuple[dict[str, str | int], ...]:
        return tuple(
            {"memoryId": item.memory_id, "revision": item.revision}
            for item in self.items
        )

    def to_worker_payload(self) -> dict[str, Any]:
        return {
            "role": "user_research_preferences",
            "authority": "non_authoritative",
            "currentRequestPriority": "current_request_overrides_personal_memory",
            "permissions": {
                "mayAuthorizeTools": False,
                "mayChangeWorkflow": False,
                "maySatisfyEvidenceRequirements": False,
                "mayWriteMemory": False,
            },
            "remoteProcessing": {
                "model": True,
                "tools": False,
                "embeddings": False,
            },
            "policyRevision": self.policy_revision,
            "items": [item.to_worker_dict() for item in self.items],
        }


@runtime_checkable
class PersonalMemoryContextProvider(Protocol):
    """Revalidates the exact policy and record revisions before remote use."""

    def current(self) -> PersonalMemoryContext: ...


__all__ = [
    "PersonalMemoryContext",
    "PersonalMemoryContextItem",
    "PersonalMemoryContextProvider",
]
