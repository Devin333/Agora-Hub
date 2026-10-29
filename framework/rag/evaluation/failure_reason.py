from __future__ import annotations

from enum import StrEnum


class RAGFailureReason(StrEnum):
    MISSING_GOLD_IN_RETRIEVAL = "missing_gold_in_retrieval"
    LOW_RANK_GOLD = "low_rank_gold"
    CONTEXT_MISSING_GOLD = "context_missing_gold"
    CITATION_MISSING_SOURCE = "citation_missing_source"
    FACT_MATCH_LOW = "fact_match_low"
    ANSWER_NOT_GROUNDED = "answer_not_grounded"
    # Legacy, direction-ambiguous abstention mismatch; scorers emit the two reasons below.
    ABSTENTION_EXPECTED = "abstention_expected"
    ABSTAINED_OVER_CONSERVATIVE = "abstained_over_conservative"
    ABSTENTION_WRONG = "abstention_wrong"
    SYSTEM_FAILURE = "system_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RERANKER_UNAVAILABLE = "reranker_unavailable"


_FAILURE_REASON_ALIASES: dict[str, RAGFailureReason] = {
    "": RAGFailureReason.ANSWER_NOT_GROUNDED,
    "missing_gold_in_retrieval": RAGFailureReason.MISSING_GOLD_IN_RETRIEVAL,
    "missing_gold_in_llm_context": RAGFailureReason.CONTEXT_MISSING_GOLD,
    "context_missing_gold": RAGFailureReason.CONTEXT_MISSING_GOLD,
    "missing_gold_citation": RAGFailureReason.CITATION_MISSING_SOURCE,
    "missing_source_citation": RAGFailureReason.CITATION_MISSING_SOURCE,
    "citation_missing_source": RAGFailureReason.CITATION_MISSING_SOURCE,
    "fact_match_low": RAGFailureReason.FACT_MATCH_LOW,
    "answer_not_grounded": RAGFailureReason.ANSWER_NOT_GROUNDED,
    "unexpected_abstention": RAGFailureReason.ABSTENTION_EXPECTED,
    "abstention_mismatch": RAGFailureReason.ABSTENTION_EXPECTED,
    "abstention_expected": RAGFailureReason.ABSTENTION_EXPECTED,
    "budget_exhausted": RAGFailureReason.BUDGET_EXHAUSTED,
    "reranker_unavailable": RAGFailureReason.RERANKER_UNAVAILABLE,
    "other": RAGFailureReason.ANSWER_NOT_GROUNDED,
}


def normalize_failure_reason(
    reason: RAGFailureReason | str | object,
    *,
    default: RAGFailureReason = RAGFailureReason.ANSWER_NOT_GROUNDED,
) -> RAGFailureReason:
    if isinstance(reason, RAGFailureReason):
        return reason
    text = str(reason or "").strip()
    if not text:
        return default
    try:
        return RAGFailureReason(text)
    except ValueError:
        pass
    return _FAILURE_REASON_ALIASES.get(text, default)


__all__ = ["RAGFailureReason", "normalize_failure_reason"]
