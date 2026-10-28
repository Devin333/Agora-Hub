from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ResearchArtifactStorePort(Protocol):
    def publish(self, *, artifact_type: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
        ...


@runtime_checkable
class ResearchArtifactReadPort(Protocol):
    """Read a published Research artifact through an actor-scoped boundary."""

    def read(
        self,
        ref: str,
        *,
        actor_scope: Mapping[str, str],
        include_payload: bool = False,
        max_chars: int = 200_000,
    ) -> Mapping[str, Any]:
        ...


@runtime_checkable
class ResearchRunArtifactReadPort(Protocol):
    """Read one artifact a Research run published, by its canonical run ref.

    Run artifacts (``artifact://<run_id>/<type>``) live in the run artifact
    store, not the paper catalog; only artifacts of an accepted run are
    readable. The returned envelope carries ``artifact_type``, ``metadata``
    and ``payload``. A missing artifact raises ``FileNotFoundError``.
    """

    def read_artifact(self, ref: str) -> Mapping[str, Any]:
        ...


__all__ = [
    "ResearchArtifactReadPort",
    "ResearchArtifactStorePort",
    "ResearchRunArtifactReadPort",
]
