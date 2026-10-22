from __future__ import annotations

from typing import Any

from framework.harness.rag.gates import RAGGateResult
from framework.harness.rag.models import GroundedAnswerCandidate, RAGContextPack


class RAGAnswerGate:
    """Pure deterministic verification for grounded answer candidates."""

    def evaluate(
        self,
        candidate: GroundedAnswerCandidate,
        pack: RAGContextPack,
    ) -> tuple[RAGGateResult, ...]:
        evidence = pack.accepted_evidence or pack.evidence
        available = {item.evidence_id for item in evidence}
        evidence_by_id = {item.evidence_id: item for item in evidence}
        return (
            _citation_integrity(candidate, available),
            _span_citation_integrity(candidate, evidence_by_id),
            _claim_coverage(candidate),
            _answer_shape(candidate),
            _abstention_shape(candidate),
        )


def _citation_integrity(candidate: GroundedAnswerCandidate, available: set[str]) -> RAGGateResult:
    cited = set(candidate.cited_evidence_ids)
    claim_cited = {evidence_id for claim in candidate.claims for evidence_id in claim.evidence_ids}
    missing = sorted((cited | claim_cited) - available)
    unresolved = list(candidate.unresolved_citations)
    passed = not missing and not unresolved
    if missing:
        reason = "answer cites evidence ids outside the verified context pack"
    elif unresolved:
        reason = "answer contains citation markers that do not resolve to verified context"
    else:
        reason = None
    return RAGGateResult(
        "rag_answer_citation_integrity",
        passed,
        reason,
        {
            "available_evidence_ids": sorted(available),
            "cited_evidence_ids": sorted(cited),
            "claim_evidence_ids": sorted(claim_cited),
            "missing_evidence_ids": missing,
            "unresolved_citations": unresolved,
        },
    )


def _span_citation_integrity(
    candidate: GroundedAnswerCandidate,
    evidence_by_id: dict[str, Any],
) -> RAGGateResult:
    available_span_refs_by_evidence_id = {
        evidence_id: _span_refs_from_evidence(evidence)
        for evidence_id, evidence in evidence_by_id.items()
    }
    span_owner_ids: dict[str, set[str]] = {}
    for evidence_id, span_refs in available_span_refs_by_evidence_id.items():
        for span_ref in span_refs:
            span_owner_ids.setdefault(span_ref, set()).add(evidence_id)

    missing_claim_span_refs: list[dict[str, Any]] = []
    unknown_claim_span_refs: list[dict[str, Any]] = []
    mismatched_claim_span_refs: list[dict[str, Any]] = []
    evidence_ids_without_claim_span_refs: list[dict[str, Any]] = []

    if not candidate.abstained:
        for claim in candidate.claims:
            claim_evidence_ids = _ordered_unique(claim.evidence_ids)
            if not claim_evidence_ids:
                continue
            if any(evidence_id not in evidence_by_id for evidence_id in claim_evidence_ids):
                continue

            claim_span_refs = _ordered_unique(claim.span_refs)
            if not claim_span_refs:
                missing_claim_span_refs.append({
                    "claim_id": claim.claim_id,
                    "evidence_ids": list(claim_evidence_ids),
                })
                continue

            covered_evidence_ids: set[str] = set()
            for span_ref in claim_span_refs:
                owner_ids = span_owner_ids.get(span_ref)
                if not owner_ids:
                    unknown_claim_span_refs.append({
                        "claim_id": claim.claim_id,
                        "span_ref": span_ref,
                    })
                    continue

                matching_ids = owner_ids.intersection(claim_evidence_ids)
                if not matching_ids:
                    mismatched_claim_span_refs.append({
                        "claim_id": claim.claim_id,
                        "span_ref": span_ref,
                        "owner_evidence_ids": sorted(owner_ids),
                        "claim_evidence_ids": list(claim_evidence_ids),
                    })
                    continue

                covered_evidence_ids.update(matching_ids)

            missing_evidence_ids = [
                evidence_id for evidence_id in claim_evidence_ids if evidence_id not in covered_evidence_ids
            ]
            if missing_evidence_ids:
                evidence_ids_without_claim_span_refs.append({
                    "claim_id": claim.claim_id,
                    "evidence_ids": missing_evidence_ids,
                })

    passed = not (
        missing_claim_span_refs
        or unknown_claim_span_refs
        or mismatched_claim_span_refs
        or evidence_ids_without_claim_span_refs
    )
    return RAGGateResult(
        "rag_answer_span_citation_integrity",
        passed,
        None if passed else "answer claim spans are not grounded in their cited evidence",
        {
            "available_span_refs_by_evidence_id": {
                evidence_id: list(span_refs)
                for evidence_id, span_refs in available_span_refs_by_evidence_id.items()
            },
            "missing_claim_span_refs": missing_claim_span_refs,
            "unknown_claim_span_refs": unknown_claim_span_refs,
            "mismatched_claim_span_refs": mismatched_claim_span_refs,
            "evidence_ids_without_claim_span_refs": evidence_ids_without_claim_span_refs,
            "claim_count": len(candidate.claims),
            "abstained": candidate.abstained,
        },
    )


def _claim_coverage(candidate: GroundedAnswerCandidate) -> RAGGateResult:
    uncovered = [
        {"claim_id": claim.claim_id, "text": claim.text}
        for claim in candidate.claims
        if not claim.evidence_ids
    ]
    return RAGGateResult(
        "rag_answer_claim_coverage",
        candidate.abstained or not uncovered,
        None if candidate.abstained or not uncovered else "one or more answer claims lack evidence ids",
        {"unsupported_claims": uncovered, "claim_count": len(candidate.claims)},
    )


def _answer_shape(candidate: GroundedAnswerCandidate) -> RAGGateResult:
    nonempty = bool(candidate.answer_text.strip())
    passed = candidate.abstained or nonempty
    return RAGGateResult(
        "rag_answer_shape",
        passed,
        None if passed else "non-abstention answer text is empty",
        {"answer_chars": len(candidate.answer_text.strip()), "abstained": candidate.abstained},
    )


def _abstention_shape(candidate: GroundedAnswerCandidate) -> RAGGateResult:
    text = candidate.answer_text.strip()
    passed = not candidate.abstained or not text
    return RAGGateResult(
        "rag_answer_abstention_shape",
        passed,
        None if passed else "abstention candidates must not include answer text",
        {"abstained": candidate.abstained, "answer_chars": len(text)},
    )


def unsupported_claims_from_answer_gate(results: tuple[RAGGateResult, ...]) -> tuple[dict[str, Any], ...]:
    claims: list[dict[str, Any]] = []
    for result in results:
        if result.passed:
            continue
        claims.extend(dict(item) for item in result.details.get("unsupported_claims", ()))
    return tuple(claims)


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def _span_refs_from_evidence(evidence: Any) -> tuple[str, ...]:
    raw = getattr(evidence, "span_refs", None)
    if raw is None:
        metadata = getattr(evidence, "metadata", {}) or {}
        raw = metadata.get("span_refs") or metadata.get("section_refs") or getattr(evidence, "source_refs", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (tuple, list, set)):
        return ()
    return tuple(str(ref) for ref in raw if str(ref).strip())


__all__ = ["RAGAnswerGate", "unsupported_claims_from_answer_gate"]
