"""Deterministic error injection for calibrating fact judges without humans.

Starting from verified reference facts (benchmark gold), each mutation makes one
controlled, known error in the reference statement: a value outside tolerance,
percent swapped with percentage points, a different dataset/split/condition,
negation, or an unsupported generalization. The unmodified statement is the
positive control. Because the expected outcome of every variant is known by
construction, judge detection and false-alarm rates are measured exactly.

Mutations are pure text/fact edits with fixed rules (no model involved), and a
mutation that cannot be applied unambiguously is skipped and reported.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel
from backend.research.rag.evaluation.protocol_dataset import ReferenceLabel
from backend.research.rag.evaluation.protocol_fact_extraction import canonical_key, parse_value_text
from backend.research.rag.evaluation.protocol_fact_projections import ReferenceFactProjection
from backend.research.rag.evaluation.protocol_semantic_facts import (
    FactRelation,
    NumericUnit,
    SemanticCondition,
    SemanticFact,
    compare_semantic_facts,
)

FACT_MUTATION_RULE_VERSION = "fact-mutation/v1"

_NUMBER_SPAN = re.compile(r"[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")
_AUXILIARY = re.compile(r"\b(is|are|was|were|does|do|did|can|could|has|have|had|will)\b", re.IGNORECASE)
_NEGATED = re.compile(r"\b(is|are|was|were|does|do|did|can|could|has|have|had|will) not\b", re.IGNORECASE)
_SPLIT_FALLBACK = {"test": "dev", "dev": "test", "validation": "test", "val": "test", "train": "test"}
_OVERGENERALIZATION = " This result holds on every dataset and under every setting."


class MutationKind(StrEnum):
    IDENTITY = "identity"
    NUMERIC_SHIFT = "numeric_shift"
    UNIT_SWAP = "unit_swap"
    DATASET_SWAP = "dataset_swap"
    SPLIT_SWAP = "split_swap"
    CONDITION_SWAP = "condition_swap"
    NEGATION = "negation"
    OVERGENERALIZATION = "overgeneralization"


class FactVariant(ProtocolModel):
    variant_id: NonEmptyStr
    case_id: NonEmptyStr
    reference_claim_id: NonEmptyStr
    kind: MutationKind
    text: NonEmptyStr
    original_text: NonEmptyStr
    expect_entailed: bool
    mutated_fact: SemanticFact | None = None
    deterministic_relation: FactRelation | None = None

    @property
    def deterministically_detected(self) -> bool | None:
        """Whether the deterministic comparator flags this structured mutation."""

        if self.deterministic_relation is None:
            return None
        return self.deterministic_relation is not FactRelation.MATCH


class SkippedMutation(ProtocolModel):
    case_id: NonEmptyStr
    reference_claim_id: NonEmptyStr
    kind: MutationKind
    reason: NonEmptyStr


class MutationReport(ProtocolModel):
    rule_version: NonEmptyStr = FACT_MUTATION_RULE_VERSION
    variants: int = Field(ge=0, strict=True)
    by_kind: dict[NonEmptyStr, int]
    skipped: tuple[SkippedMutation, ...] = ()


def _value_spans(text: str, fact: SemanticFact) -> list[tuple[int, int, int]]:
    """(start, end, decimal places) of spans that write the fact's number."""

    assert fact.numeric_value is not None
    spans: list[tuple[int, int, int]] = []
    for match in _NUMBER_SPAN.finditer(text):
        parsed = parse_value_text(match.group(0))
        if parsed is None:
            continue
        value, places = parsed
        if abs(value - fact.numeric_value) <= 1e-9:
            spans.append((match.start(), match.end(), places))
    return spans


def _format(value: float, places: int) -> str:
    return f"{value:.{places}f}" if places > 0 else str(int(round(value)))


def _shifted(value: float, places: int, tolerance: float, unit: NumericUnit) -> float:
    step = 10.0 ** (-places)
    delta = max(abs(value) * 0.05, step, tolerance * 4.0)
    delta = round(delta / step) * step or step
    ceiling = 100.0 if unit is NumericUnit.PERCENT else 1.0 if unit is NumericUnit.FRACTION else None
    if ceiling is not None and value + delta > ceiling:
        return value - delta
    return value + delta


def _mutate_numeric(text: str, fact: SemanticFact) -> tuple[str, SemanticFact] | str:
    if fact.numeric_value is None or fact.unit is None:
        return "fact has no numeric value"
    spans = _value_spans(text, fact)
    if len(spans) != 1:
        return "numeric value does not occur exactly once in the statement"
    start, end, places = spans[0]
    new_value = _shifted(fact.numeric_value, places, fact.tolerance, fact.unit)
    written = _format(new_value, places)
    suffix = "%" if text[start:end].endswith("%") else ""
    mutated = fact.model_copy(update={"numeric_value": float(written)})
    return text[:start] + written + suffix + text[end:], SemanticFact.model_validate(mutated.model_dump())


def _mutate_unit(text: str, fact: SemanticFact) -> tuple[str, SemanticFact] | str:
    if fact.unit is NumericUnit.PERCENT:
        spans = [item for item in _value_spans(text, fact) if text[item[1] - 1] == "%"]
        if len(spans) != 1:
            return "percent value does not occur exactly once"
        start, end, _ = spans[0]
        new_text = text[:end - 1] + " percentage points" + text[end:]
        new_unit = NumericUnit.PERCENTAGE_POINT
    elif fact.unit is NumericUnit.PERCENTAGE_POINT:
        matches = list(re.finditer(r"\s*(?:percentage points|points|pp)\b", text, re.IGNORECASE))
        if len(matches) != 1:
            return "percentage-point marker does not occur exactly once"
        match = matches[0]
        new_text = text[:match.start()] + "%" + text[match.end():]
        new_unit = NumericUnit.PERCENT
    else:
        return "unit swap applies only to percent and percentage points"
    mutated = fact.model_copy(update={"unit": new_unit})
    return new_text, SemanticFact.model_validate(mutated.model_dump())


def _replace_once_casefold(text: str, surface: str, replacement: str) -> str | None:
    pattern = re.compile(re.escape(surface), re.IGNORECASE)
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    return text[:match.start()] + replacement + text[match.end():]


def _mutate_scope(
    text: str,
    fact: SemanticFact,
    *,
    field_name: str,
    alternatives: Sequence[str],
) -> tuple[str, SemanticFact] | str:
    current = getattr(fact, field_name)
    if current is None:
        return f"fact has no {field_name}"
    options = [item for item in alternatives if canonical_key(item) != canonical_key(current)]
    if not options and field_name == "split_id":
        fallback = _SPLIT_FALLBACK.get(canonical_key(current))
        options = [fallback] if fallback else []
    if not options:
        return f"no alternative {field_name} is known for this case"
    replacement = sorted(options)[0]
    new_text = _replace_once_casefold(text, current, replacement)
    if new_text is None:
        return f"{field_name} is not written exactly once in the statement"
    mutated = fact.model_copy(update={field_name: canonical_key(replacement)})
    return new_text, SemanticFact.model_validate(mutated.model_dump())


def _mutate_condition(text: str, fact: SemanticFact) -> tuple[str, SemanticFact] | str:
    for index, condition in enumerate(fact.conditions):
        parsed = parse_value_text(condition.value)
        if parsed is None or parsed[1] != 0:
            continue
        # The value must be a whole number token: not part of 0.7 or 17, but a
        # sentence-final "seed 7." still counts.
        pattern = re.compile(rf"(?<!\d)(?<!\d\.){re.escape(condition.value)}(?!\d|\.\d)")
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            continue
        new_value = str(int(parsed[0]) + 1)
        match = matches[0]
        conditions = list(fact.conditions)
        conditions[index] = SemanticCondition(name=condition.name, value=new_value)
        mutated = fact.model_copy(update={"conditions": tuple(conditions)})
        return (
            text[:match.start()] + new_value + text[match.end():],
            SemanticFact.model_validate(mutated.model_dump()),
        )
    return "no integer-valued condition is written exactly once in the statement"


def _negate(text: str) -> str | None:
    negated = list(_NEGATED.finditer(text))
    if len(negated) == 1:
        match = negated[0]
        return text[:match.start()] + match.group(1) + text[match.end():]
    if negated:
        return None
    auxiliary = list(_AUXILIARY.finditer(text))
    if not auxiliary:
        return None
    match = auxiliary[0]
    return text[:match.end()] + " not" + text[match.end():]


def generate_variants(
    case_id: str,
    reference: ReferenceLabel,
    projections: Sequence[ReferenceFactProjection],
) -> tuple[tuple[FactVariant, ...], MutationReport]:
    """All applicable variants of every verified reference fact of one case."""

    claims = {item.claim_id: item for item in reference.reference_claims}
    datasets = sorted({item.fact.dataset_id for item in projections if item.fact.dataset_id})
    splits = sorted({item.fact.split_id for item in projections if item.fact.split_id})
    variants: list[FactVariant] = []
    skipped: list[SkippedMutation] = []
    for projection in projections:
        fact = projection.fact
        claim = claims[projection.reference_claim_id]
        original = fact.exact_quote or claim.claim_text

        def add(kind: MutationKind, outcome: tuple[str, SemanticFact | None] | str | None) -> None:
            if outcome is None or isinstance(outcome, str):
                skipped.append(SkippedMutation(
                    case_id=case_id,
                    reference_claim_id=projection.reference_claim_id,
                    kind=kind,
                    reason=outcome or "mutation is not applicable to this statement",
                ))
                return
            text, mutated = outcome
            relation = (
                compare_semantic_facts(mutated, fact, bound=True).relation if mutated is not None else None
            )
            if kind is not MutationKind.IDENTITY and relation is FactRelation.MATCH:
                skipped.append(SkippedMutation(
                    case_id=case_id,
                    reference_claim_id=projection.reference_claim_id,
                    kind=kind,
                    reason="mutated fact is still within tolerance of the original",
                ))
                return
            variants.append(FactVariant(
                variant_id=f"{case_id}/{projection.reference_claim_id}/{kind.value}",
                case_id=case_id,
                reference_claim_id=projection.reference_claim_id,
                kind=kind,
                text=text,
                original_text=original,
                expect_entailed=kind is MutationKind.IDENTITY,
                mutated_fact=mutated,
                deterministic_relation=relation,
            ))

        add(MutationKind.IDENTITY, (original, fact))
        if fact.numeric_value is not None:
            add(MutationKind.NUMERIC_SHIFT, _mutate_numeric(original, fact))
            add(MutationKind.UNIT_SWAP, _mutate_unit(original, fact))
        add(MutationKind.DATASET_SWAP, _mutate_scope(original, fact, field_name="dataset_id", alternatives=datasets))
        add(MutationKind.SPLIT_SWAP, _mutate_scope(original, fact, field_name="split_id", alternatives=splits))
        add(MutationKind.CONDITION_SWAP, _mutate_condition(original, fact))
        negated = _negate(original)
        add(MutationKind.NEGATION, (negated, None) if negated else "statement has no negatable auxiliary verb")
        add(MutationKind.OVERGENERALIZATION, (original.rstrip(" .") + "." + _OVERGENERALIZATION, None))
    by_kind: dict[str, int] = {}
    for item in variants:
        by_kind[item.kind.value] = by_kind.get(item.kind.value, 0) + 1
    report = MutationReport(variants=len(variants), by_kind=by_kind, skipped=tuple(skipped))
    return tuple(variants), report


__all__ = [
    "FACT_MUTATION_RULE_VERSION",
    "FactVariant",
    "MutationKind",
    "MutationReport",
    "SkippedMutation",
    "generate_variants",
]
