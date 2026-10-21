"""Verified fact projections bound to their exact sources.

A projection attaches one ``SemanticFact`` to the native object it was verified
against: a reference claim with its evidence anchors, an answer claim, or an
exact excerpt of the context actually shown to the model. The index helpers
enforce those bindings before any comparison happens.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import EvidenceLocation, ReferenceLabel
from backend.research.rag.evaluation.protocol_semantic_facts import ContentModality, SemanticFact
from framework.harness.rag.models import AnswerClaim, EvidenceCandidate, RAGContextPack


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalized_exact_text(value: str) -> str:
    return " ".join(value.split())


def rule_key(rule_id: str, rule_version: str) -> str:
    return f"{rule_id}@{rule_version}"


class ExactSourceExcerpt(ProtocolModel):
    """Exact source text bound to actual context and reproducible identity."""

    context_pack_id: NonEmptyStr
    evidence_id: NonEmptyStr
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    source_locator: NonEmptyStr
    location: EvidenceLocation | None = None
    content_type: ContentModality
    source_text: NonEmptyStr
    source_text_sha256: Sha256Digest
    quote_start: int = Field(ge=0, strict=True)
    quote_end: int = Field(gt=0, strict=True)
    quote_text: NonEmptyStr
    quote_sha256: Sha256Digest

    @model_validator(mode="after")
    def _verify_exact_text(self) -> "ExactSourceExcerpt":
        if self.quote_end <= self.quote_start or self.quote_end > len(self.source_text):
            raise ValueError("invalid exact quote range")
        if self.source_text[self.quote_start : self.quote_end] != self.quote_text:
            raise ValueError("quote_text must equal the exact source_text slice")
        if self.source_text_sha256 != text_sha256(self.source_text):
            raise ValueError("source_text_sha256 does not match source_text")
        if self.quote_sha256 != text_sha256(self.quote_text):
            raise ValueError("quote_sha256 does not match quote_text")
        return self

    @property
    def evidence_reference(self) -> str:
        locator = self.location.model_dump(mode="json") if self.location else self.source_locator
        return (
            f"source://{self.paper_id}/{self.document_version}/{self.source_sha256}/"
            f"{content_digest(locator)}#quote={self.quote_sha256}"
        )


class ReferenceFactProjection(ProtocolModel):
    reference_claim_id: NonEmptyStr
    fact: SemanticFact
    evidence_anchor_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr

    @model_validator(mode="after")
    def _unique_anchors(self) -> "ReferenceFactProjection":
        if len(self.evidence_anchor_ids) != len(set(self.evidence_anchor_ids)):
            raise ValueError("evidence_anchor_ids must not contain duplicate values")
        return self


class CandidateFactProjection(ProtocolModel):
    answer_claim_id: NonEmptyStr
    reference_claim_id: NonEmptyStr | None = None
    fact: SemanticFact
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr


class EvidenceFactProjection(ProtocolModel):
    evidence_id: NonEmptyStr
    fact: SemanticFact
    source: ExactSourceExcerpt
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr

    @model_validator(mode="after")
    def _source_matches_evidence(self) -> "EvidenceFactProjection":
        if self.source.evidence_id != self.evidence_id:
            raise ValueError("evidence fact and exact source must use the same evidence_id")
        if self.fact.exact_quote is not None and (
            normalized_exact_text(self.fact.exact_quote)
            != normalized_exact_text(self.source.quote_text)
        ):
            raise ValueError("exact-quote fact must equal its verified source quote")
        return self


def evidence_source_identity(evidence: EvidenceCandidate) -> dict[str, str | None]:
    """Immutable source identity an accepted context item declares.

    Identity may be carried on the evidence metadata, on an embedded
    ``paper_chunk``, or on that chunk's own metadata, in that precedence order.
    """

    metadata = evidence.metadata
    raw_chunk = metadata.get("paper_chunk")
    chunk: Mapping[str, Any] = raw_chunk if isinstance(raw_chunk, Mapping) else {}
    raw_chunk_metadata = chunk.get("metadata")
    chunk_metadata: Mapping[str, Any] = (
        raw_chunk_metadata if isinstance(raw_chunk_metadata, Mapping) else {}
    )

    def pick(name: str) -> str | None:
        value = metadata.get(name)
        if value in (None, ""):
            value = chunk.get(name)
        if value in (None, ""):
            value = chunk_metadata.get(name)
        return str(value) if value not in (None, "") else None

    return {
        "paper_id": pick("paper_id"),
        "document_version": pick("document_version"),
        "source_sha256": pick("source_sha256"),
        "source_locator": pick("source_locator") or evidence.source_ref,
    }


def source_identity_mismatch(
    evidence: EvidenceCandidate,
    source: ExactSourceExcerpt,
) -> str | None:
    expected = evidence_source_identity(evidence)
    actual = {
        "paper_id": source.paper_id,
        "document_version": source.document_version,
        "source_sha256": source.source_sha256,
        "source_locator": source.source_locator,
    }
    missing = [name for name, value in expected.items() if value is None]
    if missing:
        return f"actual context lacks immutable source identity fields: {', '.join(missing)}"
    mismatch = [name for name in expected if expected[name] != actual[name]]
    if mismatch:
        return f"exact citation source identity differs for: {', '.join(mismatch)}"
    # Only the exact text actually exposed to the model may support faithfulness.
    # A complete source document or arbitrary retrieved chunk cannot repair a
    # truncated or compressed accepted context.
    if source.quote_text not in evidence.summary:
        return "verified source quote is absent from actual model context text"
    return None


def index_reference_facts(
    projections: Sequence[ReferenceFactProjection],
    reference: ReferenceLabel,
) -> dict[str, ReferenceFactProjection]:
    allowed_claim_ids = {item.claim_id for item in reference.reference_claims}
    anchors = {item.anchor_id: item for item in reference.evidence_anchors}
    result: dict[str, ReferenceFactProjection] = {}
    for projection in projections:
        if projection.reference_claim_id not in allowed_claim_ids:
            raise ValueError("reference fact projection targets an unknown reference claim")
        if projection.reference_claim_id in result:
            raise ValueError("reference claims may have only one deterministic projection")
        for anchor_id in projection.evidence_anchor_ids:
            anchor = anchors.get(anchor_id)
            if anchor is None or projection.reference_claim_id not in anchor.claim_ids:
                raise ValueError("reference fact projection must use native anchors for its claim")
        result[projection.reference_claim_id] = projection
    return result


def index_candidate_facts(
    projections: Sequence[CandidateFactProjection],
    claims: Sequence[AnswerClaim],
    reference: ReferenceLabel,
) -> dict[str, CandidateFactProjection]:
    claim_by_id = {item.claim_id: item for item in claims}
    reference_claim_ids = {item.claim_id for item in reference.reference_claims}
    result: dict[str, CandidateFactProjection] = {}
    for projection in projections:
        claim = claim_by_id.get(projection.answer_claim_id)
        if claim is None:
            raise ValueError("candidate fact projection targets an unknown answer claim")
        if projection.answer_claim_id in result:
            raise ValueError("answer claims may have only one deterministic projection")
        if (
            projection.reference_claim_id is not None
            and projection.reference_claim_id not in reference_claim_ids
        ):
            raise ValueError("candidate fact projection is aligned to an unknown reference claim")
        if projection.fact.exact_quote is not None and (
            normalized_exact_text(projection.fact.exact_quote)
            != normalized_exact_text(claim.text)
        ):
            raise ValueError("exact-quote candidate projection must equal its native AnswerClaim text")
        result[projection.answer_claim_id] = projection
    return result


def index_evidence_facts(
    projections: Sequence[EvidenceFactProjection],
    actual_context: RAGContextPack | None,
) -> dict[str, tuple[EvidenceFactProjection, ...]]:
    """Group evidence facts by passage, dropping facts whose source drifted.

    A dropped fact leaves its passage without deterministic facts, which keeps
    that passage unresolved rather than letting a stale quote decide support.
    """

    actual_by_id = (
        {item.evidence_id: item for item in actual_context.accepted_evidence}
        if actual_context is not None
        else {}
    )
    grouped: dict[str, list[EvidenceFactProjection]] = {}
    for projection in projections:
        if actual_context is None or projection.evidence_id not in actual_by_id:
            raise ValueError("evidence facts must belong to actual_context.accepted_evidence")
        if projection.source.context_pack_id != actual_context.pack_id:
            raise ValueError("evidence fact source belongs to a different actual context pack")
        if source_identity_mismatch(actual_by_id[projection.evidence_id], projection.source):
            continue
        grouped.setdefault(projection.evidence_id, []).append(projection)
    return {key: tuple(value) for key, value in grouped.items()}


__all__ = [
    "CandidateFactProjection",
    "EvidenceFactProjection",
    "ExactSourceExcerpt",
    "ReferenceFactProjection",
    "evidence_source_identity",
    "index_candidate_facts",
    "index_evidence_facts",
    "index_reference_facts",
    "normalized_exact_text",
    "rule_key",
    "source_identity_mismatch",
    "text_sha256",
]
