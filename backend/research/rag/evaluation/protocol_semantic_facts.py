"""Deterministic semantic fact model and per-dimension fact comparison.

A ``SemanticFact`` is a structured projection of one factual statement: what is
measured (object/dataset/split/statistic/conditions) and what is asserted
(numeric value with unit, yes/no polarity, a categorical value, or an exact
quote). ``compare_semantic_facts`` compares two facts dimension by dimension and
derives one ``FactRelation``. It never uses lexical similarity: anything that
cannot be decided from explicit structured fields is ``UNDETERMINED`` and is
left to the separate semantic judge workflow.

This module is shared by answer scoring, fact extraction verification, and judge
calibration mutations, so it depends only on protocol primitives.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel

DETERMINISTIC_SEMANTIC_RULE_VERSION = "deterministic-semantic/v2"

# Absolute floor applied on top of the reference tolerance so that exact unit
# conversions (for example 95 percent -> 0.95 fraction) are not rejected because
# of binary floating point representation error.
_FLOAT_EPSILON = 1e-9

ContentModality = Literal["text", "table", "figure", "equation"]


class NumericUnit(StrEnum):
    FRACTION = "fraction"
    PERCENT = "percent"
    PERCENTAGE_POINT = "percentage_point"
    RELATIVE_PERCENT = "relative_percent"
    SCALAR = "scalar"


class SemanticCondition(ProtocolModel):
    name: NonEmptyStr
    value: NonEmptyStr


class SemanticFact(ProtocolModel):
    """Immutable deterministic fact projection, not a replacement QA record.

    Scope fields describe *what* the statement is about; content fields describe
    *what* it asserts. At least one content field is required. Numeric content
    must name the measured statistic so that a bare number can never match an
    unrelated number.
    """

    exact_quote: NonEmptyStr | None = None
    numeric_value: float | None = None
    unit: NumericUnit | None = None
    tolerance: float = Field(default=0.0, ge=0.0)
    polarity: bool | None = None
    categorical_value: NonEmptyStr | None = None
    object_id: NonEmptyStr | None = None
    dataset_id: NonEmptyStr | None = None
    split_id: NonEmptyStr | None = None
    statistic: NonEmptyStr | None = None
    conditions: tuple[SemanticCondition, ...] = ()
    modality: ContentModality = "text"

    @field_validator("numeric_value", "tolerance", mode="before")
    @classmethod
    def _finite_number(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("semantic numeric fields must be finite numbers")
        if not math.isfinite(float(value)):
            raise ValueError("semantic numeric fields must be finite numbers")
        return float(value)

    @field_validator("unit", mode="before")
    @classmethod
    def _parse_unit(cls, value: Any) -> Any:
        return NumericUnit(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _complete_fact(self) -> "SemanticFact":
        if (
            self.exact_quote is None
            and self.numeric_value is None
            and self.polarity is None
            and self.categorical_value is None
        ):
            raise ValueError(
                "semantic fact requires an exact quote, numeric value, polarity, or categorical value"
            )
        if (self.numeric_value is None) != (self.unit is None):
            raise ValueError("numeric_value and unit must be supplied together")
        if self.numeric_value is not None and self.statistic is None:
            raise ValueError("numeric facts must name the measured statistic")
        names = [item.name.casefold() for item in self.conditions]
        if len(names) != len(set(names)):
            raise ValueError("semantic condition names must not contain duplicate values")
        return self

    @property
    def has_structured_content(self) -> bool:
        return (
            self.numeric_value is not None
            or self.polarity is not None
            or self.categorical_value is not None
        )


class FactDimension(StrEnum):
    MODALITY = "modality"
    OBJECT = "object"
    DATASET = "dataset"
    SPLIT = "split"
    STATISTIC = "statistic"
    CONDITION = "condition"
    NUMERIC_VALUE = "numeric_value"
    UNIT = "unit"
    POLARITY = "polarity"
    CATEGORICAL_VALUE = "categorical_value"
    EXACT_QUOTE = "exact_quote"


SCOPE_DIMENSIONS: frozenset[FactDimension] = frozenset({
    FactDimension.OBJECT,
    FactDimension.DATASET,
    FactDimension.SPLIT,
    FactDimension.STATISTIC,
    FactDimension.CONDITION,
})
EXPERIMENTAL_CONDITION_DIMENSIONS: frozenset[FactDimension] = frozenset({
    FactDimension.DATASET,
    FactDimension.SPLIT,
    FactDimension.CONDITION,
})
CONTENT_DIMENSIONS: frozenset[FactDimension] = frozenset({
    FactDimension.NUMERIC_VALUE,
    FactDimension.UNIT,
    FactDimension.POLARITY,
    FactDimension.CATEGORICAL_VALUE,
    FactDimension.EXACT_QUOTE,
})


class DimensionOutcome(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    MISSING = "missing"
    EXTRA = "extra"
    UNDETERMINED = "undetermined"


class FactRelation(StrEnum):
    """Relation of an observed fact to one expected fact."""

    MATCH = "match"
    CONFLICT = "conflict"
    SCOPE_MISMATCH = "scope_mismatch"
    UNDETERMINED = "undetermined"


class DimensionComparison(ProtocolModel):
    dimension: FactDimension
    outcome: DimensionOutcome
    name: NonEmptyStr | None = None
    expected: NonEmptyStr | None = None
    actual: NonEmptyStr | None = None
    reason: NonEmptyStr


class FactComparison(ProtocolModel):
    relation: FactRelation
    bound: bool
    reason: NonEmptyStr
    dimensions: tuple[DimensionComparison, ...]
    rule_version: NonEmptyStr = DETERMINISTIC_SEMANTIC_RULE_VERSION

    def outcomes(self, dimensions: frozenset[FactDimension]) -> tuple[DimensionComparison, ...]:
        return tuple(item for item in self.dimensions if item.dimension in dimensions)

    @property
    def scope_established(self) -> bool:
        """Whether every explicit scope dimension agrees, so content is comparable."""

        return all(
            item.outcome is DimensionOutcome.MATCH
            for item in self.dimensions
            if item.dimension in SCOPE_DIMENSIONS or item.dimension is FactDimension.MODALITY
        )


def compare_semantic_facts(
    actual: SemanticFact,
    expected: SemanticFact,
    *,
    bound: bool = False,
) -> FactComparison:
    """Compare ``actual`` against ``expected`` dimension by dimension.

    ``bound`` means an upstream alignment already asserted that ``actual``
    addresses ``expected`` (for example an answer claim aligned to one reference
    claim). For bound pairs an explicit scope difference is a factual conflict
    (wrong dataset, split, or experimental condition); for unbound pairs it only
    means the two facts are about different things.
    """

    dimensions = (
        _compare_modality(actual.modality, expected.modality),
        *_compare_scope(actual, expected),
        *_compare_conditions(actual, expected),
        *_compare_content(actual, expected),
    )
    relation, reason = _derive_relation(dimensions, bound=bound)
    return FactComparison(relation=relation, bound=bound, reason=reason, dimensions=dimensions)


def _compare_modality(actual: str, expected: str) -> DimensionComparison:
    if actual == expected:
        return DimensionComparison(
            dimension=FactDimension.MODALITY,
            outcome=DimensionOutcome.MATCH,
            expected=expected,
            actual=actual,
            reason="modalities are identical",
        )
    if "figure" in {actual, expected}:
        return DimensionComparison(
            dimension=FactDimension.MODALITY,
            outcome=DimensionOutcome.UNDETERMINED,
            expected=expected,
            actual=actual,
            reason="text/table evidence cannot establish full figure scope",
        )
    if actual == "text":
        # A plain-text claim does not restrict which modality reports the value.
        return DimensionComparison(
            dimension=FactDimension.MODALITY,
            outcome=DimensionOutcome.MATCH,
            expected=expected,
            actual=actual,
            reason="text claim does not restrict the reporting modality",
        )
    return DimensionComparison(
        dimension=FactDimension.MODALITY,
        outcome=DimensionOutcome.MISMATCH,
        expected=expected,
        actual=actual,
        reason="evidence modality differs from explicit claim scope",
    )


_SCOPE_FIELDS: tuple[tuple[FactDimension, str], ...] = (
    (FactDimension.OBJECT, "object_id"),
    (FactDimension.DATASET, "dataset_id"),
    (FactDimension.SPLIT, "split_id"),
    (FactDimension.STATISTIC, "statistic"),
)


def _compare_scope(actual: SemanticFact, expected: SemanticFact) -> tuple[DimensionComparison, ...]:
    result: list[DimensionComparison] = []
    for dimension, field_name in _SCOPE_FIELDS:
        comparison = _compare_key(
            dimension,
            actual=getattr(actual, field_name),
            expected=getattr(expected, field_name),
        )
        if comparison is not None:
            result.append(comparison)
    return tuple(result)


def _compare_key(
    dimension: FactDimension,
    *,
    actual: str | None,
    expected: str | None,
    name: str | None = None,
) -> DimensionComparison | None:
    label = (
        f"experimental condition {name}" if dimension is FactDimension.CONDITION and name
        else dimension.value
    )
    if actual is None and expected is None:
        return None
    if expected is None:
        outcome, reason = DimensionOutcome.EXTRA, f"extra {label} changes semantic scope"
    elif actual is None:
        outcome, reason = DimensionOutcome.MISSING, f"missing explicit {label}"
    elif actual.casefold() == expected.casefold():
        outcome, reason = DimensionOutcome.MATCH, f"explicit {label} matches"
    else:
        outcome, reason = DimensionOutcome.MISMATCH, f"explicit {label} differs"
    return DimensionComparison(
        dimension=dimension,
        outcome=outcome,
        name=name,
        expected=expected,
        actual=actual,
        reason=reason,
    )


def _compare_conditions(
    actual: SemanticFact, expected: SemanticFact,
) -> tuple[DimensionComparison, ...]:
    actual_by_name = {item.name.casefold(): item for item in actual.conditions}
    expected_by_name = {item.name.casefold(): item for item in expected.conditions}
    result: list[DimensionComparison] = []
    for key in sorted(set(actual_by_name) | set(expected_by_name)):
        actual_item = actual_by_name.get(key)
        expected_item = expected_by_name.get(key)
        name = (expected_item or actual_item).name  # type: ignore[union-attr]
        comparison = _compare_key(
            FactDimension.CONDITION,
            actual=actual_item.value if actual_item else None,
            expected=expected_item.value if expected_item else None,
            name=name,
        )
        if comparison is not None:
            result.append(comparison)
    return tuple(result)


def _compare_content(
    actual: SemanticFact, expected: SemanticFact,
) -> tuple[DimensionComparison, ...]:
    result: list[DimensionComparison] = []
    if actual.numeric_value is not None or expected.numeric_value is not None:
        result.extend(_compare_numeric(actual, expected))
    if actual.polarity is not None or expected.polarity is not None:
        result.append(_compare_polarity(actual.polarity, expected.polarity))
    if actual.categorical_value is not None or expected.categorical_value is not None:
        result.append(_compare_categorical(actual.categorical_value, expected.categorical_value))
    # An exact quote is the asserted content only when no structured content
    # exists; otherwise it is provenance for the structured value.
    if not expected.has_structured_content and expected.exact_quote is not None:
        result.append(_compare_quote(actual.exact_quote, expected.exact_quote))
    return tuple(result)


_UNIT_CANONICAL: dict[NumericUnit, tuple[str, float]] = {
    NumericUnit.FRACTION: ("absolute_fraction", 1.0),
    NumericUnit.PERCENT: ("absolute_fraction", 0.01),
    NumericUnit.PERCENTAGE_POINT: ("percentage_point", 1.0),
    NumericUnit.RELATIVE_PERCENT: ("relative_gain", 0.01),
    NumericUnit.SCALAR: ("scalar", 1.0),
}


def normalize_numeric(value: float, unit: NumericUnit) -> tuple[str, float]:
    """Map a value to (physical dimension, canonical value)."""

    dimension, scale = _UNIT_CANONICAL[unit]
    return dimension, value * scale


def normalize_tolerance(tolerance: float, unit: NumericUnit) -> float:
    """Express a tolerance in the canonical scale of its unit's dimension."""

    return tolerance * _UNIT_CANONICAL[unit][1]


def _format_number(value: float, unit: NumericUnit) -> str:
    return f"{value!r} {unit.value}"


def _compare_numeric(
    actual: SemanticFact, expected: SemanticFact,
) -> tuple[DimensionComparison, ...]:
    if expected.numeric_value is None or expected.unit is None:
        assert actual.numeric_value is not None and actual.unit is not None
        return (DimensionComparison(
            dimension=FactDimension.NUMERIC_VALUE,
            outcome=DimensionOutcome.EXTRA,
            actual=_format_number(actual.numeric_value, actual.unit),
            reason="numeric value is not asserted by the expected fact",
        ),)
    expected_text = _format_number(expected.numeric_value, expected.unit)
    if actual.numeric_value is None or actual.unit is None:
        return (DimensionComparison(
            dimension=FactDimension.NUMERIC_VALUE,
            outcome=DimensionOutcome.MISSING,
            expected=expected_text,
            reason="numeric semantic input is incomplete",
        ),)
    actual_text = _format_number(actual.numeric_value, actual.unit)
    actual_dimension, actual_value = normalize_numeric(actual.numeric_value, actual.unit)
    expected_dimension, expected_value = normalize_numeric(expected.numeric_value, expected.unit)
    if actual_dimension != expected_dimension:
        return (
            DimensionComparison(
                dimension=FactDimension.UNIT,
                outcome=DimensionOutcome.MISMATCH,
                expected=expected.unit.value,
                actual=actual.unit.value,
                reason="percentage points, relative gain, and absolute values are distinct",
            ),
            DimensionComparison(
                dimension=FactDimension.NUMERIC_VALUE,
                outcome=DimensionOutcome.UNDETERMINED,
                expected=expected_text,
                actual=actual_text,
                reason="values in different unit dimensions are not comparable",
            ),
        )
    # Tolerance is the rounding half-unit implied by each side's stated
    # precision (derived deterministically from the verbatim value text, never
    # chosen by a model). A coarser but correctly rounded statement such as
    # "95%" for 95.3% is not a contradiction; anything outside both rounding
    # intervals is.
    allowed = max(
        normalize_tolerance(expected.tolerance, expected.unit),
        normalize_tolerance(actual.tolerance, actual.unit),
    )
    within = math.isclose(
        actual_value,
        expected_value,
        rel_tol=0.0,
        abs_tol=allowed + _FLOAT_EPSILON,
    )
    return (
        DimensionComparison(
            dimension=FactDimension.UNIT,
            outcome=DimensionOutcome.MATCH,
            expected=expected.unit.value,
            actual=actual.unit.value,
            reason="units share the same physical dimension",
        ),
        DimensionComparison(
            dimension=FactDimension.NUMERIC_VALUE,
            outcome=DimensionOutcome.MATCH if within else DimensionOutcome.MISMATCH,
            expected=expected_text,
            actual=actual_text,
            reason=(
                "normalized numeric value is within tolerance" if within
                else "normalized numeric value exceeds tolerance"
            ),
        ),
    )


def _compare_polarity(actual: bool | None, expected: bool | None) -> DimensionComparison:
    expected_text = None if expected is None else ("yes" if expected else "no")
    actual_text = None if actual is None else ("yes" if actual else "no")
    if expected is None:
        outcome, reason = DimensionOutcome.EXTRA, "polarity is not asserted by the expected fact"
    elif actual is None:
        outcome, reason = DimensionOutcome.MISSING, "polarity semantic input is absent"
    elif actual == expected:
        outcome, reason = DimensionOutcome.MATCH, "yes/no polarity matches"
    else:
        outcome, reason = DimensionOutcome.MISMATCH, "yes/no polarity is opposite"
    return DimensionComparison(
        dimension=FactDimension.POLARITY,
        outcome=outcome,
        expected=expected_text,
        actual=actual_text,
        reason=reason,
    )


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _compare_categorical(actual: str | None, expected: str | None) -> DimensionComparison:
    if expected is None:
        outcome, reason = DimensionOutcome.EXTRA, "categorical value is not asserted by the expected fact"
    elif actual is None:
        outcome, reason = DimensionOutcome.MISSING, "categorical semantic input is absent"
    elif _normalized_text(actual) == _normalized_text(expected):
        outcome, reason = DimensionOutcome.MATCH, "categorical value matches after normalization"
    else:
        # Different surface forms may be synonyms; never a deterministic conflict.
        outcome, reason = DimensionOutcome.UNDETERMINED, "different categorical wording requires semantic judgment"
    return DimensionComparison(
        dimension=FactDimension.CATEGORICAL_VALUE,
        outcome=outcome,
        expected=expected,
        actual=actual,
        reason=reason,
    )


def _compare_quote(actual: str | None, expected: str) -> DimensionComparison:
    if actual is None:
        outcome, reason = DimensionOutcome.MISSING, "exact-quote semantic input is absent"
    elif " ".join(actual.split()) == " ".join(expected.split()):
        outcome, reason = DimensionOutcome.MATCH, "exact quote is identical after whitespace normalization"
    else:
        outcome, reason = (
            DimensionOutcome.UNDETERMINED,
            "non-identical wording is not a deterministic semantic contradiction",
        )
    return DimensionComparison(
        dimension=FactDimension.EXACT_QUOTE,
        outcome=outcome,
        expected=expected,
        actual=actual,
        reason=reason,
    )


_UNKNOWN_OUTCOMES = frozenset({
    DimensionOutcome.MISSING,
    DimensionOutcome.EXTRA,
    DimensionOutcome.UNDETERMINED,
})


def _derive_relation(
    dimensions: Sequence[DimensionComparison],
    *,
    bound: bool,
) -> tuple[FactRelation, str]:
    scope = [item for item in dimensions if item.dimension in SCOPE_DIMENSIONS]
    modality = [item for item in dimensions if item.dimension is FactDimension.MODALITY]
    content = [item for item in dimensions if item.dimension in CONTENT_DIMENSIONS]

    scope_mismatch = [item for item in scope if item.outcome is DimensionOutcome.MISMATCH]
    if scope_mismatch:
        relation = FactRelation.CONFLICT if bound else FactRelation.SCOPE_MISMATCH
        return relation, scope_mismatch[0].reason
    modality_mismatch = [item for item in modality if item.outcome is DimensionOutcome.MISMATCH]
    if modality_mismatch:
        # A different reporting modality is not proof of a wrong value.
        relation = FactRelation.UNDETERMINED if bound else FactRelation.SCOPE_MISMATCH
        return relation, modality_mismatch[0].reason
    scope_unknown = [item for item in (*modality, *scope) if item.outcome in _UNKNOWN_OUTCOMES]
    if scope_unknown:
        # Without an agreed scope a differing value may describe another setting.
        return FactRelation.UNDETERMINED, scope_unknown[0].reason
    content_mismatch = [item for item in content if item.outcome is DimensionOutcome.MISMATCH]
    if content_mismatch:
        return FactRelation.CONFLICT, content_mismatch[0].reason
    content_unknown = [item for item in content if item.outcome in _UNKNOWN_OUTCOMES]
    if content_unknown:
        return FactRelation.UNDETERMINED, content_unknown[0].reason
    if not content:
        return FactRelation.UNDETERMINED, "no comparable asserted content"
    return FactRelation.MATCH, "all deterministic semantic dimensions match"


__all__ = [
    "CONTENT_DIMENSIONS",
    "DETERMINISTIC_SEMANTIC_RULE_VERSION",
    "EXPERIMENTAL_CONDITION_DIMENSIONS",
    "SCOPE_DIMENSIONS",
    "ContentModality",
    "DimensionComparison",
    "DimensionOutcome",
    "FactComparison",
    "FactDimension",
    "FactRelation",
    "NumericUnit",
    "SemanticCondition",
    "SemanticFact",
    "compare_semantic_facts",
    "normalize_numeric",
    "normalize_tolerance",
]
