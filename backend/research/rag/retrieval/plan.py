from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from backend.research.rag.retrieval.paper_policy import RetrievalRoute

FusionAlgorithm = Literal["rrf", "weighted"]


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    enabled: bool = True
    limit: int | None = None
    filters: tuple[dict[str, Any], ...] = ()
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "limit": self.limit,
            "filters": [dict(item) for item in self.filters],
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class FusionSpec:
    algorithm: FusionAlgorithm = "rrf"
    rrf_k: int = 60
    weights: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "rrf_k": self.rrf_k,
            "weights": dict(self.weights),
        }


@dataclass(frozen=True)
class RerankSpec:
    lightweight_enabled: bool = False
    field_enabled: bool = False
    score_threshold: float = 0.0
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lightweight_enabled": self.lightweight_enabled,
            "field_enabled": self.field_enabled,
            "score_threshold": self.score_threshold,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class ExpanderSpec:
    name: str
    enabled: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class RetrievalPlan:
    route: RetrievalRoute
    filters: dict[str, Any]
    candidate_filters: tuple[dict[str, Any], ...]
    candidate_limit: int
    element_query_labels: tuple[str, ...] = ()
    channels: tuple[ChannelSpec, ...] = ()
    fusion: FusionSpec = field(default_factory=FusionSpec)
    rerank: RerankSpec = field(default_factory=RerankSpec)
    expanders: tuple[ExpanderSpec, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def intent(self) -> str:
        return self.route.intent

    @property
    def recall_routes(self) -> tuple[str, ...]:
        return self.route.recall_routes

    def route_dict(self) -> dict[str, Any]:
        return {
            "primary_intent": self.route.intent,
            "recall_routes": list(self.route.recall_routes),
            "candidate_filters": [dict(item) for item in self.candidate_filters],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.route.intent,
            "recall_routes": list(self.route.recall_routes),
            "filters": dict(self.filters),
            "candidate_filters": [dict(item) for item in self.candidate_filters],
            "candidate_limit": self.candidate_limit,
            "element_query_labels": list(self.element_query_labels),
            "channels": [item.to_dict() for item in self.channels],
            "fusion": self.fusion.to_dict(),
            "rerank": self.rerank.to_dict(),
            "expanders": [item.to_dict() for item in self.expanders],
            "metadata": dict(self.metadata),
        }


__all__ = [
    "ChannelSpec",
    "ExpanderSpec",
    "FusionAlgorithm",
    "FusionSpec",
    "RerankSpec",
    "RetrievalPlan",
]
