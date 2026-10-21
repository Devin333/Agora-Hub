"""Deterministic verification of model-proposed fact drafts.

Fact drafts come from an untrusted extractor (see
``backend.research.ports.answer_fact_extraction``). This module is the only path
from a draft to a scoring projection and it accepts a draft only when:

* its statement is a verbatim span of the exact source text;
* any numeric value is carried by a verbatim ``value_text`` inside that
  statement, parses to the declared number, and agrees with the declared unit;
* numeric facts name the measured statistic;
* candidate drafts align only to reference claims offered to the extractor;
* reference drafts are anchored to native evidence of their own claim.

Tolerance is never taken from the extractor; it is the rounding half-unit
implied by the verbatim value text. Every rejection is kept with its reason so
extraction loss is measured instead of silently shrinking denominators.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from pydantic import Field, ValidationError, model_validator

from backend.research.ports.answer_fact_extraction import (
    AlignmentTarget,
    FactDraft,
    FactExtractionResult,
    FactSourceRole,
    ScopeVocabulary,
)
from backend.research.rag.evaluation.protocol_fact_projections import (
    CandidateFactProjection,
    EvidenceFactProjection,
    ExactSourceExcerpt,
    ReferenceFactProjection,
    evidence_source_identity,
    text_sha256,
)
from backend.research.rag.evaluation.protocol_common import (
    NonEmptyStr,
    ProtocolModel,
)
from backend.research.rag.evaluation.protocol_dataset import ReferenceLabel
from backend.research.rag.evaluation.protocol_semantic_facts import (
    NumericUnit,
    SemanticCondition,
    SemanticFact,
)
from framework.harness.rag.models import AnswerClaim, RAGContextPack

FACT_VERIFICATION_RULE_VERSION = "fact-verification/v1"

_NUMBER = re.compile(r"[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[-+−]?\.\d+")
_PERCENT_UNITS = frozenset({
    NumericUnit.PERCENT,
    NumericUnit.PERCENTAGE_POINT,
    NumericUnit.RELATIVE_PERCENT,
})


def canonical_key(value: str) -> str:
    """Whitespace- and case-normalized id used to align scope vocabularies."""

    return " ".join(value.split()).casefold()


def parse_value_text(value_text: str) -> tuple[float, int] | None:
    """Parse the single number in ``value_text`` into (value, decimal places)."""

    matches = _NUMBER.findall(value_text)
    if len(matches) != 1:
        return None
    raw = matches[0].replace(",", "").replace("−", "-")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    exponent = number.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return float(number), places


def numbers_in(text: str) -> frozenset[float]:
    """All numbers written in ``text`` (used to stop decomposition inventing values)."""

    result: set[float] = set()
    for raw in _NUMBER.findall(text):
        parsed = parse_value_text(raw)
        if parsed is not None:
            result.add(parsed[0])
    return frozenset(result)


def stated_tolerance(decimal_places: int) -> float:
    """Rounding half-unit of a value written with ``decimal_places`` decimals."""

    return 0.5 * 10.0 ** (-decimal_places)


class FactRejection(ProtocolModel):
    role: FactSourceRole
    target_id: NonEmptyStr
    statement: NonEmptyStr | None
    reason: NonEmptyStr


class FactVerificationReport(ProtocolModel):
    """Accounting of every draft and every target that should have a fact."""

    role: FactSourceRole
    rule_version: NonEmptyStr = FACT_VERIFICATION_RULE_VERSION
    extractor_rules: tuple[NonEmptyStr, ...]
    targets: int = Field(ge=0, strict=True)
    projected_targets: int = Field(ge=0, strict=True)
    drafts: int = Field(ge=0, strict=True)
    accepted_drafts: int = Field(ge=0, strict=True)
    rejections: tuple[FactRejection, ...] = ()

    @model_validator(mode="after")
    def _counts(self) -> "FactVerificationReport":
        if self.projected_targets > self.targets or self.accepted_drafts > self.drafts:
            raise ValueError("verification counts are inconsistent")
        return self

    @property
    def rejection_rate(self) -> float | None:
        return None if self.drafts == 0 else (self.drafts - self.accepted_drafts) / self.drafts


class _Verified(ProtocolModel):
    draft: FactDraft
    fact: SemanticFact


def _canonical_or_none(value: str | None) -> str | None:
    return None if value is None else canonical_key(value)


def verify_draft(
    draft: FactDraft,
    *,
    source_texts: Sequence[str],
    role: FactSourceRole,
    alignment_targets: Sequence[AlignmentTarget] = (),
) -> tuple[_Verified | None, str | None]:
    """Verify one draft against the exact texts it may quote."""

    if not any(draft.statement in text for text in source_texts):
        return None, "statement is not a verbatim span of the source text"
    if (draft.numeric_value is None) != (draft.unit is None):
        return None, "numeric_value and unit must be supplied together"
    tolerance = 0.0
    if draft.numeric_value is not None:
        if draft.value_text is None:
            return None, "numeric fact lacks verbatim value_text"
        if draft.value_text not in draft.statement:
            return None, "value_text is not a verbatim span of the statement"
        parsed = parse_value_text(draft.value_text)
        if parsed is None:
            return None, "value_text does not contain exactly one parseable number"
        value, places = parsed
        if not math.isclose(value, draft.numeric_value, rel_tol=0.0, abs_tol=1e-9):
            return None, "numeric_value does not equal the number written in value_text"
        unit = NumericUnit(draft.unit)
        if "%" in draft.value_text and unit not in _PERCENT_UNITS:
            return None, "a percent sign contradicts a non-percent unit"
        if "%" not in draft.value_text and unit is NumericUnit.PERCENT:
            return None, "percent unit requires a percent sign in value_text"
        tolerance = stated_tolerance(places)
    elif draft.value_text is not None:
        return None, "value_text without a numeric value"
    if draft.categorical_value is not None and canonical_key(draft.categorical_value) not in canonical_key(draft.statement):
        return None, "categorical value is not stated in the statement"
    if role is FactSourceRole.CANDIDATE:
        allowed = {item.target_id for item in alignment_targets}
        if draft.aligned_target_id is not None and draft.aligned_target_id not in allowed:
            return None, "draft is aligned to a reference claim that was not offered"
    elif draft.aligned_target_id is not None:
        return None, "only candidate drafts may align to a reference claim"
    try:
        fact = SemanticFact(
            exact_quote=draft.statement,
            numeric_value=draft.numeric_value,
            unit=draft.unit,
            tolerance=tolerance,
            polarity=draft.polarity,
            categorical_value=draft.categorical_value,
            object_id=_canonical_or_none(draft.object_id),
            dataset_id=_canonical_or_none(draft.dataset_id),
            split_id=_canonical_or_none(draft.split_id),
            statistic=_canonical_or_none(draft.statistic),
            conditions=tuple(
                SemanticCondition(name=canonical_key(item.name), value=canonical_key(item.value))
                for item in draft.conditions
            ),
            modality=draft.modality,
        )
    except ValidationError as exc:
        return None, f"draft is not a valid semantic fact: {exc.errors()[0]['msg']}"
    return _Verified(draft=draft, fact=fact), None


def _single_accepted(
    result: FactExtractionResult | None,
    *,
    role: FactSourceRole,
    target_id: str,
    source_texts: Sequence[str],
    alignment_targets: Sequence[AlignmentTarget] = (),
    rejections: list[FactRejection],
) -> tuple[_Verified | None, int, int]:
    """Verify all drafts of one target; a target projects only one atomic fact."""

    if result is None:
        rejections.append(FactRejection(
            role=role, target_id=target_id, statement=None,
            reason="no extraction result for this target",
        ))
        return None, 0, 0
    accepted: list[_Verified] = []
    for draft in result.drafts:
        verified, reason = verify_draft(
            draft, source_texts=source_texts, role=role, alignment_targets=alignment_targets,
        )
        if verified is None:
            rejections.append(FactRejection(
                role=role, target_id=target_id, statement=draft.statement, reason=reason or "rejected",
            ))
        else:
            accepted.append(verified)
    if not accepted and not result.drafts:
        rejections.append(FactRejection(
            role=role, target_id=target_id, statement=None,
            reason="extractor proposed no fact for this target",
        ))
    if len(accepted) > 1:
        for item in accepted:
            rejections.append(FactRejection(
                role=role, target_id=target_id, statement=item.draft.statement,
                reason="target asserts more than one fact; it is not atomic",
            ))
        return None, len(result.drafts), 0
    return (accepted[0] if accepted else None), len(result.drafts), len(accepted)


def _rule(result: FactExtractionResult | None) -> tuple[str, str]:
    if result is None:
        return "fact-extraction/missing", FACT_VERIFICATION_RULE_VERSION
    extractor = result.extractor
    return extractor.rule_id, f"{extractor.rule_version}+{FACT_VERIFICATION_RULE_VERSION}"


def _extractor_rules(results: Mapping[str, FactExtractionResult]) -> tuple[str, ...]:
    return tuple(sorted({"@".join(_rule(item)) for item in results.values()}))


def build_reference_projections(
    reference: ReferenceLabel,
    results: Mapping[str, FactExtractionResult],
) -> tuple[tuple[ReferenceFactProjection, ...], FactVerificationReport]:
    """Project each reference claim to at most one verified, anchored fact.

    ``results`` is keyed by reference claim id. A reference fact may quote the
    claim text or one of the claim's own evidence anchor excerpts.
    """

    rejections: list[FactRejection] = []
    projections: list[ReferenceFactProjection] = []
    drafts = accepted = 0
    for claim in reference.reference_claims:
        anchors = [item for item in reference.evidence_anchors if claim.claim_id in item.claim_ids]
        result = results.get(claim.claim_id)
        verified, seen, kept = _single_accepted(
            result,
            role=FactSourceRole.REFERENCE,
            target_id=claim.claim_id,
            source_texts=(claim.claim_text, *(item.source_excerpt for item in anchors)),
            rejections=rejections,
        )
        drafts += seen
        if verified is None:
            continue
        quoting = [item.anchor_id for item in anchors if verified.draft.statement in item.source_excerpt]
        anchor_ids = quoting or [item.anchor_id for item in anchors]
        if not anchor_ids:
            rejections.append(FactRejection(
                role=FactSourceRole.REFERENCE, target_id=claim.claim_id,
                statement=verified.draft.statement,
                reason="reference claim has no native evidence anchor",
            ))
            continue
        accepted += kept
        rule_id, rule_version = _rule(result)
        projections.append(ReferenceFactProjection(
            reference_claim_id=claim.claim_id,
            fact=verified.fact,
            evidence_anchor_ids=tuple(anchor_ids),
            rule_id=rule_id,
            rule_version=rule_version,
        ))
    report = FactVerificationReport(
        role=FactSourceRole.REFERENCE,
        extractor_rules=_extractor_rules(results),
        targets=len(reference.reference_claims),
        projected_targets=len(projections),
        drafts=drafts,
        accepted_drafts=accepted,
        rejections=tuple(rejections),
    )
    return tuple(projections), report


def build_candidate_projections(
    claims: Sequence[AnswerClaim],
    results: Mapping[str, FactExtractionResult],
    *,
    alignment_targets: Sequence[AlignmentTarget],
) -> tuple[tuple[CandidateFactProjection, ...], FactVerificationReport]:
    """Project each atomic answer claim to at most one verified fact.

    ``results`` is keyed by answer claim id. The claim text is the only source a
    candidate fact may quote; decomposition already guaranteed that claim text
    introduces no number absent from the answer.
    """

    rejections: list[FactRejection] = []
    projections: list[CandidateFactProjection] = []
    drafts = accepted = 0
    for claim in claims:
        result = results.get(claim.claim_id)
        verified, seen, kept = _single_accepted(
            result,
            role=FactSourceRole.CANDIDATE,
            target_id=claim.claim_id,
            source_texts=(claim.text,),
            alignment_targets=alignment_targets,
            rejections=rejections,
        )
        drafts += seen
        if verified is None:
            continue
        accepted += kept
        fact = verified.fact
        # A candidate quote must equal its native claim; it only matters when the
        # fact has no structured content to compare.
        fact = fact.model_copy(update={
            "exact_quote": None if fact.has_structured_content else claim.text,
        })
        rule_id, rule_version = _rule(result)
        projections.append(CandidateFactProjection(
            answer_claim_id=claim.claim_id,
            reference_claim_id=verified.draft.aligned_target_id,
            fact=SemanticFact.model_validate(fact.model_dump()),
            rule_id=rule_id,
            rule_version=rule_version,
        ))
    report = FactVerificationReport(
        role=FactSourceRole.CANDIDATE,
        extractor_rules=_extractor_rules(results),
        targets=len(claims),
        projected_targets=len(projections),
        drafts=drafts,
        accepted_drafts=accepted,
        rejections=tuple(rejections),
    )
    return tuple(projections), report


def build_evidence_projections(
    context: RAGContextPack,
    results: Mapping[str, FactExtractionResult],
) -> tuple[tuple[EvidenceFactProjection, ...], FactVerificationReport]:
    """Project accepted context passages to verified, exactly quoted facts.

    ``results`` is keyed by evidence id. A passage may assert several facts; each
    accepted draft becomes one projection bound to the exact text the model saw.
    """

    rejections: list[FactRejection] = []
    projections: list[EvidenceFactProjection] = []
    drafts = accepted = 0
    projected: set[str] = set()
    for evidence in context.accepted_evidence:
        result = results.get(evidence.evidence_id)
        if result is None:
            rejections.append(FactRejection(
                role=FactSourceRole.EVIDENCE, target_id=evidence.evidence_id, statement=None,
                reason="no extraction result for this evidence",
            ))
            continue
        identity = evidence_source_identity(evidence)
        missing = [name for name, value in identity.items() if value is None]
        rule_id, rule_version = _rule(result)
        for draft in result.drafts:
            drafts += 1
            if missing:
                rejections.append(FactRejection(
                    role=FactSourceRole.EVIDENCE, target_id=evidence.evidence_id,
                    statement=draft.statement,
                    reason=f"evidence lacks immutable source identity: {', '.join(missing)}",
                ))
                continue
            verified, reason = verify_draft(
                draft, source_texts=(evidence.summary,), role=FactSourceRole.EVIDENCE,
            )
            if verified is None:
                rejections.append(FactRejection(
                    role=FactSourceRole.EVIDENCE, target_id=evidence.evidence_id,
                    statement=draft.statement, reason=reason or "rejected",
                ))
                continue
            start = evidence.summary.index(draft.statement)
            try:
                source = ExactSourceExcerpt(
                    context_pack_id=context.pack_id,
                    evidence_id=evidence.evidence_id,
                    paper_id=identity["paper_id"],
                    document_version=identity["document_version"],
                    source_sha256=identity["source_sha256"],
                    source_locator=identity["source_locator"],
                    content_type=verified.fact.modality,
                    source_text=evidence.summary,
                    source_text_sha256=text_sha256(evidence.summary),
                    quote_start=start,
                    quote_end=start + len(draft.statement),
                    quote_text=draft.statement,
                    quote_sha256=text_sha256(draft.statement),
                )
                projection = EvidenceFactProjection(
                    evidence_id=evidence.evidence_id,
                    fact=verified.fact,
                    source=source,
                    rule_id=rule_id,
                    rule_version=rule_version,
                )
            except ValidationError as exc:
                rejections.append(FactRejection(
                    role=FactSourceRole.EVIDENCE, target_id=evidence.evidence_id,
                    statement=draft.statement,
                    reason=f"exact source excerpt is invalid: {exc.errors()[0]['msg']}",
                ))
                continue
            accepted += 1
            projected.add(evidence.evidence_id)
            projections.append(projection)
    report = FactVerificationReport(
        role=FactSourceRole.EVIDENCE,
        extractor_rules=_extractor_rules(results),
        targets=len(context.accepted_evidence),
        projected_targets=len(projected),
        drafts=drafts,
        accepted_drafts=accepted,
        rejections=tuple(rejections),
    )
    return tuple(projections), report


def vocabulary_from_reference(projections: Sequence[ReferenceFactProjection]) -> ScopeVocabulary:
    """Canonical scope ids of verified reference facts, for later extractions."""

    def collect(values: Sequence[str | None]) -> tuple[str, ...]:
        return tuple(sorted({item for item in values if item is not None}))

    facts = [item.fact for item in projections]
    return ScopeVocabulary(
        object_ids=collect([item.object_id for item in facts]),
        dataset_ids=collect([item.dataset_id for item in facts]),
        split_ids=collect([item.split_id for item in facts]),
        statistics=collect([item.statistic for item in facts]),
        condition_names=collect([cond.name for item in facts for cond in item.conditions]),
    )


def alignment_targets_from_reference(reference: ReferenceLabel) -> tuple[AlignmentTarget, ...]:
    return tuple(
        AlignmentTarget(target_id=item.claim_id, text=item.claim_text)
        for item in reference.reference_claims
    )


__all__ = [
    "FACT_VERIFICATION_RULE_VERSION",
    "FactRejection",
    "FactVerificationReport",
    "alignment_targets_from_reference",
    "build_candidate_projections",
    "build_evidence_projections",
    "build_reference_projections",
    "canonical_key",
    "numbers_in",
    "parse_value_text",
    "stated_tolerance",
    "verify_draft",
    "vocabulary_from_reference",
]
