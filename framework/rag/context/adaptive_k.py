"""Question-adaptive context size from a relevance score distribution.

Instead of a fixed top-k, keep the passages above the largest drop in the sorted
scores (Adaptive-k). The cut is searched only inside ``[min_k, max_k]`` so a single
dominant passage cannot starve the context and a flat distribution cannot flood it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

ADAPTIVE_K_STRATEGY = "largest_score_gap_v1"


@dataclass(frozen=True)
class AdaptiveKPolicy:
    min_k: int
    max_k: int
    # Drops smaller than this are noise: the distribution is treated as flat and
    # ``max_k`` passages are kept.
    min_gap: float = 0.0

    def __post_init__(self) -> None:
        if type(self.min_k) is not int or self.min_k < 1:
            raise ValueError("min_k must be a positive integer")
        if type(self.max_k) is not int or self.max_k < self.min_k:
            raise ValueError("max_k must be an integer no smaller than min_k")
        if not math.isfinite(self.min_gap) or self.min_gap < 0.0:
            raise ValueError("min_gap must be a finite non-negative number")

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": ADAPTIVE_K_STRATEGY,
            "min_k": self.min_k,
            "max_k": self.max_k,
            "min_gap": self.min_gap,
        }


@dataclass(frozen=True)
class AdaptiveKDecision:
    k: int
    # Largest drop found inside the search window, and whether it decided the cut.
    gap: float
    gap_applied: bool

    def to_dict(self) -> dict[str, object]:
        return {"k": self.k, "gap": self.gap, "gap_applied": self.gap_applied}


def select_adaptive_k(scores: Sequence[float], policy: AdaptiveKPolicy) -> AdaptiveKDecision:
    """Number of leading items to keep from ``scores`` sorted in descending order."""
    values = [float(score) for score in scores]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("scores must be finite")
    if any(left < right for left, right in zip(values, values[1:])):
        raise ValueError("scores must be sorted in descending order")
    count = len(values)
    if count <= policy.min_k:
        return AdaptiveKDecision(k=count, gap=0.0, gap_applied=False)
    upper = min(policy.max_k, count)
    # Cutting after position k (1-based) separates values[k - 1] from values[k];
    # k == count has no following item and therefore no gap.
    best_k, best_gap = upper, 0.0
    for k in range(policy.min_k, min(upper, count - 1) + 1):
        gap = values[k - 1] - values[k]
        if gap > best_gap:
            best_k, best_gap = k, gap
    if best_gap <= policy.min_gap:
        return AdaptiveKDecision(k=upper, gap=best_gap, gap_applied=False)
    return AdaptiveKDecision(k=best_k, gap=best_gap, gap_applied=True)


__all__ = [
    "ADAPTIVE_K_STRATEGY",
    "AdaptiveKDecision",
    "AdaptiveKPolicy",
    "select_adaptive_k",
]
