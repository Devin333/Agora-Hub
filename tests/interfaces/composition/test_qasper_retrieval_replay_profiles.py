from __future__ import annotations

import pytest

from interfaces.composition.qasper_retrieval_replay import default_qasper_retrieval_replay_profiles


def test_default_profiles_pair_answer_run_profiles_with_expanded_recall() -> None:
    profiles = {profile.name: profile for profile in default_qasper_retrieval_replay_profiles()}

    assert list(profiles) == [
        "baseline_chunk",
        "baseline_field_rerank",
        "expanded_chunk",
        "expanded_field_without_rerank",
        "expanded_field_rerank",
    ]
    # baseline_field_rerank is the replay twin of the answer run's field_lightweight profile.
    lightweight = profiles["baseline_field_rerank"]
    assert lightweight.policy.overfetch_multiplier == profiles["baseline_chunk"].policy.overfetch_multiplier == 3
    assert lightweight.field_index_enabled and lightweight.reranker_enabled and lightweight.field_reranker_enabled
    assert profiles["expanded_chunk"].policy.overfetch_multiplier == 10
    assert {(p.max_context_items, p.max_context_tokens) for p in profiles.values()} == {(8, 2048)}


def test_context_budget_override_applies_to_every_profile() -> None:
    profiles = default_qasper_retrieval_replay_profiles(max_context_items=12, max_context_tokens=3072)

    assert {(p.max_context_items, p.max_context_tokens) for p in profiles} == {(12, 3072)}


def test_partial_context_budget_override_keeps_the_other_default() -> None:
    profiles = default_qasper_retrieval_replay_profiles(max_context_items=12)

    assert {(p.max_context_items, p.max_context_tokens) for p in profiles} == {(12, 2048)}


def test_invalid_context_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_context_items"):
        default_qasper_retrieval_replay_profiles(max_context_items=0)
