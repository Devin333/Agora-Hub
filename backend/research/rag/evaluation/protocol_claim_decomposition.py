"""Deterministic verification of model-proposed atomic answer claims.

The production answer worker emits one conservative claim covering the whole
answer, which makes per-fact scoring impossible. Evaluation therefore asks an
untrusted decomposer (``ClaimDecompositionPort``) for atomic claims and accepts
each one only when:

* its grounding span is a verbatim span of the answer (the stated occurrence);
* its restated claim text writes no number that the answer itself does not;
* it is not a duplicate of an already accepted claim.

Citations are never taken from the decomposer. They are the answer's own
``[n]`` markers inside the sentence that contains the grounding span, resolved
through the citation index the generator used, so a claim cites exactly what the
answer cited where it said it.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace

from pydantic import Field

from backend.research.ports.answer_fact_extraction import (
    ClaimDecompositionResult,
    ClaimDraft,
)
from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel
from backend.research.rag.evaluation.protocol_fact_extraction import canonical_key, numbers_in
from framework.harness.rag.models import AnswerClaim, GroundedAnswerCandidate

CLAIM_DECOMPOSITION_RULE_VERSION = "claim-decomposition/v1"

_CITATION_MARKER = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_TRAILING_MARKERS = re.compile(r"(?:\s*\[\d+(?:\s*,\s*\d+)*\])+")


def _without_markers(text: str) -> str:
    return _CITATION_MARKER.sub(" ", text)


class ClaimRejection(ProtocolModel):
    claim_text: NonEmptyStr
    grounding_span: NonEmptyStr
    reason: NonEmptyStr


class ClaimDecompositionReport(ProtocolModel):
    rule_version: NonEmptyStr = CLAIM_DECOMPOSITION_RULE_VERSION
    extractor_rule: NonEmptyStr
    proposed: int = Field(ge=0, strict=True)
    accepted: int = Field(ge=0, strict=True)
    unresolved_citation_markers: tuple[int, ...] = ()
    rejections: tuple[ClaimRejection, ...] = ()


def _occurrence_start(text: str, span: str, occurrence: int) -> int | None:
    start = -1
    for _ in range(occurrence):
        start = text.find(span, start + 1)
        if start < 0:
            return None
    return start


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence(s) covering [start, end)."""

    # Markers written right after a sentence terminator ("... 95%. [2] Next")
    # belong to the sentence they follow, so each boundary moves past them.
    boundaries = [0, len(text)]
    for match in _SENTENCE_END.finditer(text):
        trailing = _TRAILING_MARKERS.match(text, match.start())
        boundary = trailing.end() if trailing is not None else match.end()
        while boundary < len(text) and text[boundary].isspace():
            boundary += 1
        boundaries.append(boundary)
    sentence_start = max(item for item in boundaries if item <= start)
    sentence_end = min(item for item in boundaries if item >= end)
    return sentence_start, sentence_end


def _markers(text: str) -> tuple[int, ...]:
    found: list[int] = []
    for match in _CITATION_MARKER.finditer(text):
        for raw in match.group(1).split(","):
            value = int(raw.strip())
            if value not in found:
                found.append(value)
    return tuple(found)


def apply_claim_decomposition(
    candidate: GroundedAnswerCandidate,
    result: ClaimDecompositionResult,
    *,
    citation_index: Mapping[int, str],
) -> tuple[GroundedAnswerCandidate, ClaimDecompositionReport]:
    """Replace the candidate's claims with verified atomic claims.

    ``citation_index`` maps the answer's 1-based ``[n]`` markers to evidence ids
    of the actual context the generator was given.
    """

    answer = candidate.answer_text
    # A decontextualized claim may restate scope from the question ("on SQuAD
    # 2.0"); it may not write any other number. Citation markers are not values.
    answer_numbers = numbers_in(_without_markers(answer)) | numbers_in(candidate.question)
    accepted: list[AnswerClaim] = []
    spans: dict[str, dict[str, object]] = {}
    rejections: list[ClaimRejection] = []
    unresolved: set[int] = set()
    seen: set[str] = set()
    for draft in result.claims:
        reason = _rejection_reason(draft, answer=answer, answer_numbers=answer_numbers, seen=seen)
        if reason is not None:
            rejections.append(ClaimRejection(
                claim_text=draft.claim_text, grounding_span=draft.grounding_span, reason=reason,
            ))
            continue
        start = _occurrence_start(answer, draft.grounding_span, draft.occurrence)
        assert start is not None
        end = start + len(draft.grounding_span)
        sentence_start, sentence_end = _sentence_bounds(answer, start, end)
        evidence_ids: list[str] = []
        for marker in _markers(answer[sentence_start:sentence_end]):
            evidence_id = citation_index.get(marker)
            if evidence_id is None:
                unresolved.add(marker)
            elif evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        claim_id = f"{candidate.answer_id}:claim-{len(accepted) + 1}"
        seen.add(canonical_key(draft.claim_text))
        accepted.append(AnswerClaim(
            claim_id=claim_id,
            text=draft.claim_text,
            evidence_ids=tuple(evidence_ids),
            span_refs=(f"answer:{start}-{end}",),
        ))
        spans[claim_id] = {"start": start, "end": end, "text": draft.grounding_span}
    cited = tuple(dict.fromkeys(
        evidence_id for claim in accepted for evidence_id in claim.evidence_ids
    ))
    decomposed = replace(
        candidate,
        cited_evidence_ids=tuple(dict.fromkeys((*candidate.cited_evidence_ids, *cited))),
        claims=tuple(accepted),
        metadata={
            **candidate.metadata,
            "claim_spans": spans,
            "claims_degraded": False,
            "claim_decomposition": {
                "rule_version": CLAIM_DECOMPOSITION_RULE_VERSION,
                "extractor_rule": f"{result.extractor.rule_id}@{result.extractor.rule_version}",
                "original_claim_ids": [item.claim_id for item in candidate.claims],
            },
        },
    )
    report = ClaimDecompositionReport(
        extractor_rule=f"{result.extractor.rule_id}@{result.extractor.rule_version}",
        proposed=len(result.claims),
        accepted=len(accepted),
        unresolved_citation_markers=tuple(sorted(unresolved)),
        rejections=tuple(rejections),
    )
    return decomposed, report


def _rejection_reason(
    draft: ClaimDraft,
    *,
    answer: str,
    answer_numbers: frozenset[float],
    seen: set[str],
) -> str | None:
    if _occurrence_start(answer, draft.grounding_span, draft.occurrence) is None:
        return "grounding span is not a verbatim span of the answer at the stated occurrence"
    if _CITATION_MARKER.search(draft.claim_text):
        return "claim text must not carry citation markers"
    invented = numbers_in(draft.claim_text) - answer_numbers
    if invented:
        return "claim text writes numbers the answer does not state"
    if canonical_key(draft.claim_text) in seen:
        return "duplicate atomic claim"
    return None


def citation_index_from_chunk_ids(context_chunk_ids: Sequence[str]) -> dict[int, str]:
    """Generator convention: marker ``[n]`` cites ``context_chunk_ids[n-1]``."""

    return {index: chunk_id for index, chunk_id in enumerate(context_chunk_ids, start=1)}


__all__ = [
    "CLAIM_DECOMPOSITION_RULE_VERSION",
    "ClaimDecompositionReport",
    "ClaimRejection",
    "apply_claim_decomposition",
    "citation_index_from_chunk_ids",
]
