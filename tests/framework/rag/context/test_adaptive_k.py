from __future__ import annotations

import pytest

from framework.rag.context import AdaptiveKPolicy, select_adaptive_k


def test_cuts_after_the_largest_drop_inside_the_window() -> None:
    decision = select_adaptive_k([9, 8.5, 8, 3, 2.5, 2], AdaptiveKPolicy(min_k=2, max_k=5))

    assert decision.k == 3
    assert decision.gap == pytest.approx(5.0)
    assert decision.gap_applied is True


def test_ignores_a_drop_before_min_k_so_one_dominant_passage_cannot_starve_context() -> None:
    decision = select_adaptive_k([10, 2, 1.9, 1.8, 0.5], AdaptiveKPolicy(min_k=2, max_k=5))

    # The 10 -> 2 drop would leave one passage; the next largest in the window wins.
    assert decision.k == 4
    assert decision.gap == pytest.approx(1.3)


def test_flat_distribution_keeps_max_k() -> None:
    decision = select_adaptive_k([5, 5, 5, 5, 5, 5], AdaptiveKPolicy(min_k=2, max_k=4))

    assert decision.k == 4
    assert decision.gap_applied is False


def test_gap_below_threshold_is_treated_as_flat() -> None:
    decision = select_adaptive_k([5, 4.9, 4.8, 4.7], AdaptiveKPolicy(min_k=1, max_k=3, min_gap=0.5))

    assert decision.k == 3
    assert decision.gap_applied is False


def test_never_exceeds_available_items() -> None:
    assert select_adaptive_k([3, 2, 1], AdaptiveKPolicy(min_k=1, max_k=10)).k == 1
    assert select_adaptive_k([3, 3, 3], AdaptiveKPolicy(min_k=1, max_k=10)).k == 3
    assert select_adaptive_k([], AdaptiveKPolicy(min_k=1, max_k=10)).k == 0
    assert select_adaptive_k([4, 1], AdaptiveKPolicy(min_k=3, max_k=10)).k == 2


def test_rejects_unsorted_or_non_finite_scores() -> None:
    policy = AdaptiveKPolicy(min_k=1, max_k=3)
    with pytest.raises(ValueError, match="descending"):
        select_adaptive_k([1, 2], policy)
    with pytest.raises(ValueError, match="finite"):
        select_adaptive_k([float("nan")], policy)


@pytest.mark.parametrize(
    "kwargs",
    [{"min_k": 0, "max_k": 3}, {"min_k": 3, "max_k": 2}, {"min_k": 1, "max_k": 2, "min_gap": -1.0}],
)
def test_policy_rejects_invalid_bounds(kwargs) -> None:
    with pytest.raises(ValueError):
        AdaptiveKPolicy(**kwargs)
