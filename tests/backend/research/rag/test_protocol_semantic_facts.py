from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_semantic_facts import (
    DimensionOutcome,
    FactDimension,
    FactRelation,
    NumericUnit,
    SemanticCondition,
    SemanticFact,
    compare_semantic_facts,
)


def _numeric(
    value: float,
    unit: NumericUnit,
    *,
    dataset: str | None = "dataset-a",
    split: str | None = "test",
    seed: str | None = "7",
    modality: str = "text",
    quote: str | None = None,
) -> SemanticFact:
    return SemanticFact(
        exact_quote=quote,
        numeric_value=value,
        unit=unit,
        tolerance=0.0001,
        object_id="model-a",
        dataset_id=dataset,
        split_id=split,
        statistic="accuracy",
        conditions=() if seed is None else (SemanticCondition(name="seed", value=seed),),
        modality=modality,
    )


def _outcome(comparison, dimension: FactDimension) -> DimensionOutcome:
    return next(item.outcome for item in comparison.dimensions if item.dimension is dimension)


def test_percent_and_fraction_of_same_value_match() -> None:
    comparison = compare_semantic_facts(
        _numeric(95.0, NumericUnit.PERCENT),
        _numeric(0.95, NumericUnit.FRACTION),
    )
    assert comparison.relation is FactRelation.MATCH
    assert _outcome(comparison, FactDimension.UNIT) is DimensionOutcome.MATCH
    assert _outcome(comparison, FactDimension.NUMERIC_VALUE) is DimensionOutcome.MATCH


def test_value_outside_reference_tolerance_conflicts() -> None:
    comparison = compare_semantic_facts(
        _numeric(93.0, NumericUnit.PERCENT),
        _numeric(0.95, NumericUnit.FRACTION),
    )
    assert comparison.relation is FactRelation.CONFLICT
    assert _outcome(comparison, FactDimension.NUMERIC_VALUE) is DimensionOutcome.MISMATCH


def test_different_unit_dimension_is_unit_conflict_not_numeric_comparison() -> None:
    comparison = compare_semantic_facts(
        _numeric(5.0, NumericUnit.RELATIVE_PERCENT),
        _numeric(5.0, NumericUnit.PERCENTAGE_POINT),
    )
    assert comparison.relation is FactRelation.CONFLICT
    assert _outcome(comparison, FactDimension.UNIT) is DimensionOutcome.MISMATCH
    assert _outcome(comparison, FactDimension.NUMERIC_VALUE) is DimensionOutcome.UNDETERMINED


def test_condition_difference_is_scope_mismatch_unless_bound() -> None:
    actual = _numeric(95.0, NumericUnit.PERCENT, seed="8")
    expected = _numeric(0.95, NumericUnit.FRACTION, seed="7")
    unbound = compare_semantic_facts(actual, expected)
    bound = compare_semantic_facts(actual, expected, bound=True)
    assert unbound.relation is FactRelation.SCOPE_MISMATCH
    assert bound.relation is FactRelation.CONFLICT
    assert _outcome(bound, FactDimension.CONDITION) is DimensionOutcome.MISMATCH
    # Content is still compared so dimension accuracy can be attributed.
    assert _outcome(bound, FactDimension.NUMERIC_VALUE) is DimensionOutcome.MATCH


def test_missing_scope_dimension_is_undetermined_even_with_different_value() -> None:
    comparison = compare_semantic_facts(
        _numeric(80.0, NumericUnit.PERCENT, split=None),
        _numeric(0.95, NumericUnit.FRACTION),
    )
    assert comparison.relation is FactRelation.UNDETERMINED
    assert not comparison.scope_established


def test_extra_condition_changes_scope_and_is_undetermined() -> None:
    actual = _numeric(95.0, NumericUnit.PERCENT).model_copy(update={
        "conditions": (
            SemanticCondition(name="seed", value="7"),
            SemanticCondition(name="beam", value="4"),
        ),
    })
    comparison = compare_semantic_facts(actual, _numeric(0.95, NumericUnit.FRACTION))
    assert comparison.relation is FactRelation.UNDETERMINED


def test_condition_names_compare_case_insensitively() -> None:
    actual = _numeric(95.0, NumericUnit.PERCENT).model_copy(update={
        "conditions": (SemanticCondition(name="Seed", value="7"),),
    })
    comparison = compare_semantic_facts(actual, _numeric(0.95, NumericUnit.FRACTION))
    assert comparison.relation is FactRelation.MATCH


def test_polarity_is_decided_deterministically() -> None:
    def fact(value: bool) -> SemanticFact:
        return SemanticFact(polarity=value, object_id="model-a", statistic="uses_pretraining")

    assert compare_semantic_facts(fact(True), fact(True)).relation is FactRelation.MATCH
    assert compare_semantic_facts(fact(False), fact(True)).relation is FactRelation.CONFLICT


def test_different_categorical_wording_is_never_a_deterministic_conflict() -> None:
    def fact(value: str) -> SemanticFact:
        return SemanticFact(categorical_value=value, object_id="paper-1", statistic="encoder")

    assert compare_semantic_facts(fact(" bert  BASE"), fact("BERT base")).relation is FactRelation.MATCH
    assert compare_semantic_facts(fact("BERT"), fact("RoBERTa")).relation is FactRelation.UNDETERMINED


def test_quote_is_provenance_when_structured_content_exists() -> None:
    comparison = compare_semantic_facts(
        _numeric(95.0, NumericUnit.PERCENT),
        _numeric(0.95, NumericUnit.FRACTION, quote="The model obtains 0.95 accuracy."),
    )
    assert comparison.relation is FactRelation.MATCH
    assert all(item.dimension is not FactDimension.EXACT_QUOTE for item in comparison.dimensions)


def test_text_claim_matches_table_evidence_but_figure_scope_is_undetermined() -> None:
    text_vs_table = compare_semantic_facts(
        _numeric(0.95, NumericUnit.FRACTION, modality="text"),
        _numeric(0.95, NumericUnit.FRACTION, modality="table"),
    )
    table_vs_figure = compare_semantic_facts(
        _numeric(0.95, NumericUnit.FRACTION, modality="table"),
        _numeric(0.95, NumericUnit.FRACTION, modality="figure"),
    )
    assert text_vs_table.relation is FactRelation.MATCH
    assert table_vs_figure.relation is FactRelation.UNDETERMINED


def test_fact_validation_rejects_unscoped_or_empty_content() -> None:
    with pytest.raises(ValidationError):
        SemanticFact(numeric_value=0.95, unit=NumericUnit.FRACTION)
    with pytest.raises(ValidationError):
        SemanticFact(object_id="model-a", statistic="accuracy")
    with pytest.raises(ValidationError):
        SemanticFact(
            categorical_value="x",
            conditions=(
                SemanticCondition(name="seed", value="1"),
                SemanticCondition(name="SEED", value="2"),
            ),
        )
