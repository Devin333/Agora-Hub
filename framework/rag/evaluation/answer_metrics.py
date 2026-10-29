from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
import re

from framework.rag.evaluation.failure_reason import RAGFailureReason
from framework.rag.evaluation.report import MetricValue


class AnswerOutcome(StrEnum):
    """What the system actually did for one evaluated question."""

    ANSWERED = "answered"
    ABSTAINED = "abstained"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    HALTED = "halted"
    FAILED = "failed"

    @property
    def is_refusal(self) -> bool:
        return self in {AnswerOutcome.ABSTAINED, AnswerOutcome.INSUFFICIENT_EVIDENCE}

    @property
    def is_system_failure(self) -> bool:
        return self in {AnswerOutcome.HALTED, AnswerOutcome.FAILED}


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_STRUCTURAL_FACT_STOPWORDS = {
    "figure",
    "fig",
    "table",
    "tbl",
    "equation",
    "eq",
    "caption",
    "nearby",
    "context",
    "source",
    "section",
    "latex",
    "normalized",
    "symbols",
    "operators",
    "begin",
    "end",
    "label",
    "left",
    "mathcal",
    "mathbf",
    "mathrm",
    "mathbb",
    "operatorname",
    "sigma",
    "text",
    "theta",
    "a",
    "an",
    "and",
    "are",
    "as",
    "built",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "our",
    "the",
    "this",
    "to",
    "top",
    "use",
    "we",
    "where",
    "with",
    "modify",
    "separately",
    "further",
}
_LATEX_WRAPPED_COMMAND_RE = re.compile(
    r"\\(?:mathbf|mathrm|mathcal|mathbb|text|operatorname|boldsymbol)\{([^{}]*)\}"
)


@dataclass(frozen=True)
class AnswerMetricCase:
    case_id: str
    question: str
    answer: str
    outcome: AnswerOutcome
    expected_facts: tuple[str, ...] = ()
    cited_evidence_ids: tuple[str, ...] = ()
    context_evidence_ids: tuple[str, ...] = ()
    gold_evidence_ids: tuple[str, ...] = ()
    equivalent_gold_evidence_ids: tuple[str, ...] = ()
    expected_abstain: bool = False
    cited_source_locators: tuple[str, ...] = ()
    gold_source_locators: tuple[str, ...] = ()
    retrieved_evidence_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.case_id).strip():
            raise ValueError("case_id is required")
        object.__setattr__(self, "question", str(self.question or ""))
        object.__setattr__(self, "answer", str(self.answer or ""))
        object.__setattr__(self, "outcome", AnswerOutcome(self.outcome))
        if self.outcome is not AnswerOutcome.ANSWERED and self.answer.strip():
            raise ValueError("only answered cases may carry answer text")
        object.__setattr__(self, "expected_facts", _clean_tuple(self.expected_facts))
        object.__setattr__(self, "cited_evidence_ids", _clean_tuple(self.cited_evidence_ids))
        object.__setattr__(self, "context_evidence_ids", _clean_tuple(self.context_evidence_ids))
        object.__setattr__(self, "gold_evidence_ids", _clean_tuple(self.gold_evidence_ids))
        equivalent = _clean_tuple(self.equivalent_gold_evidence_ids) or _clean_tuple(self.gold_evidence_ids)
        object.__setattr__(self, "equivalent_gold_evidence_ids", equivalent)
        object.__setattr__(self, "cited_source_locators", _clean_tuple(self.cited_source_locators))
        object.__setattr__(self, "gold_source_locators", _clean_tuple(self.gold_source_locators))
        object.__setattr__(self, "retrieved_evidence_ids", _clean_tuple(self.retrieved_evidence_ids))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class AnswerMetricScore:
    case: AnswerMetricCase
    fact_coverage: float | None
    citation_grounding: float | None
    source_locator_grounding: float | None
    abstention_correct: float | None
    answer_success: bool
    retrieval_context_coverage: float | None = None
    citation_gold_coverage: float | None = None
    strict_retrieval_context_coverage: float | None = None
    equivalent_retrieval_context_coverage: float | None = None
    strict_citation_gold_coverage: float | None = None
    equivalent_citation_gold_coverage: float | None = None
    equivalent_gold_supported: bool = False
    failure_reason: str = ""
    # Why an expected abstention counted as correct: "refusal", "cited_negation", or "".
    abstention_basis: str = ""
    matched_facts: tuple[str, ...] = ()
    missing_facts: tuple[str, ...] = ()


def score_answer_case(
    case: AnswerMetricCase,
    *,
    fact_match_threshold: float = 0.75,
    success_fact_threshold: float = 0.8,
    success_citation_threshold: float = 0.5,
) -> AnswerMetricScore:
    outcome = case.outcome
    if outcome.is_system_failure:
        # The system produced no answer to judge; no quality or refusal metric applies.
        return AnswerMetricScore(
            case=case,
            fact_coverage=None,
            citation_grounding=None,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=False,
            failure_reason=RAGFailureReason.SYSTEM_FAILURE.value,
        )

    if case.expected_abstain:
        basis = abstention_basis(case)
        correct = bool(basis)
        return AnswerMetricScore(
            case=case,
            fact_coverage=None,
            citation_grounding=None,
            source_locator_grounding=None,
            abstention_correct=1.0 if correct else 0.0,
            answer_success=correct,
            failure_reason="" if correct else RAGFailureReason.ABSTENTION_WRONG.value,
            abstention_basis=basis,
        )

    strict_retrieval_context_coverage = id_coverage(case.context_evidence_ids, case.gold_evidence_ids)
    equivalent_retrieval_context_coverage = evidence_support_coverage(
        case.context_evidence_ids,
        case.gold_evidence_ids,
        case.equivalent_gold_evidence_ids,
    )
    if outcome.is_refusal:
        # Context coverage still explains why the refusal happened; the answer-level
        # metrics have no answer to measure.
        return AnswerMetricScore(
            case=case,
            fact_coverage=None,
            citation_grounding=None,
            source_locator_grounding=None,
            abstention_correct=None,
            answer_success=False,
            retrieval_context_coverage=equivalent_retrieval_context_coverage,
            strict_retrieval_context_coverage=strict_retrieval_context_coverage,
            equivalent_retrieval_context_coverage=equivalent_retrieval_context_coverage,
            failure_reason=RAGFailureReason.ABSTAINED_OVER_CONSERVATIVE.value,
        )

    fact_score, matched, missing = fact_coverage_details(
        case.answer,
        case.expected_facts,
        threshold=fact_match_threshold,
    )
    strict_citation_gold_coverage = id_coverage(case.cited_evidence_ids, case.gold_evidence_ids)
    equivalent_citation_gold_coverage = evidence_support_coverage(
        case.cited_evidence_ids,
        case.gold_evidence_ids,
        case.equivalent_gold_evidence_ids,
    )
    retrieval_context_coverage = equivalent_retrieval_context_coverage
    citation_gold_coverage = equivalent_citation_gold_coverage
    source_locator_score = locator_coverage(case.cited_source_locators, case.gold_source_locators)
    citation_required = bool(case.equivalent_gold_evidence_ids or case.gold_evidence_ids)
    citation_passed = (
        not citation_required
        or citation_gold_coverage is not None
        and citation_gold_coverage >= success_citation_threshold
    )
    fact_passed = fact_score is None or fact_score >= success_fact_threshold
    answer_success = fact_passed and citation_passed
    failure_reason = _answer_failure_reason(
        case,
        fact_coverage=fact_score,
        retrieval_context_coverage=retrieval_context_coverage,
        citation_grounding=citation_gold_coverage,
        fact_passed=fact_passed,
        citation_passed=citation_passed,
        answer_success=answer_success,
        success_fact_threshold=success_fact_threshold,
    )
    return AnswerMetricScore(
        case=case,
        fact_coverage=fact_score,
        citation_grounding=citation_gold_coverage,
        source_locator_grounding=source_locator_score,
        abstention_correct=None,
        answer_success=answer_success,
        retrieval_context_coverage=retrieval_context_coverage,
        citation_gold_coverage=citation_gold_coverage,
        strict_retrieval_context_coverage=strict_retrieval_context_coverage,
        equivalent_retrieval_context_coverage=equivalent_retrieval_context_coverage,
        strict_citation_gold_coverage=strict_citation_gold_coverage,
        equivalent_citation_gold_coverage=equivalent_citation_gold_coverage,
        equivalent_gold_supported=_equivalent_gold_supported(
            case,
            strict_context_coverage=strict_retrieval_context_coverage,
            strict_citation_coverage=strict_citation_gold_coverage,
            equivalent_context_coverage=equivalent_retrieval_context_coverage,
            equivalent_citation_coverage=equivalent_citation_gold_coverage,
        ),
        failure_reason=failure_reason,
        matched_facts=matched,
        missing_facts=missing,
    )


def fact_coverage(case: AnswerMetricCase) -> float:
    score, _, _ = fact_coverage_details(case.answer, case.expected_facts)
    return 1.0 if score is None else score


def fact_coverage_details(
    answer: str,
    expected_facts: Iterable[Any],
    *,
    threshold: float = 0.75,
) -> tuple[float | None, tuple[str, ...], tuple[str, ...]]:
    facts = _clean_tuple(expected_facts)
    if not facts:
        return None, (), ()
    matched: list[str] = []
    missing: list[str] = []
    for fact in facts:
        if _fact_matches(answer, fact, threshold=threshold):
            matched.append(fact)
        else:
            missing.append(fact)
    return len(matched) / len(facts), tuple(matched), tuple(missing)


def citation_grounding(case: AnswerMetricCase) -> float:
    if not case.cited_evidence_ids:
        return 0.0
    allowed = set(case.context_evidence_ids) | set(case.gold_evidence_ids)
    if not allowed:
        return 0.0
    grounded = sum(1 for citation in case.cited_evidence_ids if citation in allowed)
    return grounded / len(case.cited_evidence_ids)


def id_coverage(retrieved: Iterable[Any], required: Iterable[Any]) -> float | None:
    required_set = set(_clean_tuple(required))
    if not required_set:
        return None
    return len(required_set.intersection(_clean_tuple(retrieved))) / len(required_set)


def evidence_support_coverage(
    candidate_ids: Iterable[Any],
    strict_gold_ids: Iterable[Any],
    equivalent_gold_ids: Iterable[Any],
) -> float | None:
    strict_ids = _clean_tuple(strict_gold_ids)
    equivalent_ids = _clean_tuple(equivalent_gold_ids) or strict_ids
    if not strict_ids and not equivalent_ids:
        return None
    strict = id_coverage(candidate_ids, strict_ids)
    expanded_equivalent_ids = set(equivalent_ids) - set(strict_ids)
    if not expanded_equivalent_ids:
        return strict
    if strict is not None and strict >= 1.0:
        return 1.0
    candidate_set = set(_clean_tuple(candidate_ids))
    if candidate_set and candidate_set.intersection(expanded_equivalent_ids):
        return 1.0
    return 0.0


def locator_coverage(retrieved: Iterable[Any], required: Iterable[Any]) -> float | None:
    required_locators = _clean_tuple(required)
    if not required_locators:
        return None
    retrieved_locators = _clean_tuple(retrieved)
    hits = 0
    for locator in required_locators:
        if any(_locator_matches(candidate, locator) for candidate in retrieved_locators):
            hits += 1
    return hits / len(required_locators)


def answer_relevance(case: AnswerMetricCase) -> float:
    question_terms = _token_set(case.question)
    if not question_terms:
        return 0.0
    answer_terms = _token_set(case.answer)
    return len(question_terms & answer_terms) / len(question_terms)


def faithfulness_proxy(case: AnswerMetricCase) -> float:
    if case.expected_abstain:
        return 1.0 if abstention_basis(case) else 0.0
    if case.outcome is not AnswerOutcome.ANSWERED:
        return 0.0
    return fact_coverage(case) * citation_grounding(case)


def abstention_accuracy(case: AnswerMetricCase) -> float:
    if case.expected_abstain:
        return 1.0 if abstention_basis(case) else 0.0
    return 0.0 if case.outcome.is_refusal else 1.0


_POLAR_QUESTION_RE = re.compile(
    r"^\s*(?:does|do|did|is|are|was|were|has|have|had|can|could|will|would|should)\b",
    re.IGNORECASE,
)
_NEGATION_ANSWER_RE = re.compile(r"^\W*no\b", re.IGNORECASE)


def abstention_basis(case: AnswerMetricCase) -> str:
    """Why a case satisfies an expected abstention, or "" when it does not.

    ``refusal``: the system declined (model abstention or insufficient evidence).
    ``cited_negation``: a yes/no question answered with an explicit "No" that cites
    retrieved context. For "Does the paper report X?" that is the correct answer, and
    the citation shows it is grounded rather than asserted from outside knowledge.
    """
    if case.outcome.is_refusal:
        return "refusal"
    if (
        case.outcome is AnswerOutcome.ANSWERED
        and case.cited_evidence_ids
        and _POLAR_QUESTION_RE.match(case.question)
        and _NEGATION_ANSWER_RE.match(case.answer)
    ):
        return "cited_negation"
    return ""


def evaluate_answer_case(case: AnswerMetricCase) -> tuple[MetricValue, ...]:
    return (
        MetricValue("fact_coverage", fact_coverage(case), {"case_id": case.case_id}),
        MetricValue("citation_grounding", citation_grounding(case), {"case_id": case.case_id}),
        MetricValue("answer_relevance", answer_relevance(case), {"case_id": case.case_id}),
        MetricValue("faithfulness_proxy", faithfulness_proxy(case), {"case_id": case.case_id}),
        MetricValue("abstention_accuracy", abstention_accuracy(case), {"case_id": case.case_id}),
    )


def _answer_failure_reason(
    case: AnswerMetricCase,
    *,
    fact_coverage: float | None,
    retrieval_context_coverage: float | None,
    citation_grounding: float | None,
    fact_passed: bool,
    citation_passed: bool,
    answer_success: bool,
    success_fact_threshold: float,
) -> str:
    if answer_success:
        return ""
    if case.gold_evidence_ids:
        if (
            case.retrieved_evidence_ids
            and evidence_support_coverage(
                case.retrieved_evidence_ids,
                case.gold_evidence_ids,
                case.equivalent_gold_evidence_ids,
            ) != 1.0
        ):
            return RAGFailureReason.MISSING_GOLD_IN_RETRIEVAL.value
        if retrieval_context_coverage is not None and retrieval_context_coverage < 1.0:
            return RAGFailureReason.CONTEXT_MISSING_GOLD.value
        if citation_grounding is not None and citation_grounding < 1.0:
            return RAGFailureReason.CITATION_MISSING_SOURCE.value
    if not fact_passed and fact_coverage is not None and fact_coverage < success_fact_threshold:
        return RAGFailureReason.FACT_MATCH_LOW.value
    if not citation_passed:
        return RAGFailureReason.CITATION_MISSING_SOURCE.value
    return RAGFailureReason.ANSWER_NOT_GROUNDED.value


def _equivalent_gold_supported(
    case: AnswerMetricCase,
    *,
    strict_context_coverage: float | None,
    strict_citation_coverage: float | None,
    equivalent_context_coverage: float | None,
    equivalent_citation_coverage: float | None,
) -> bool:
    if not case.equivalent_gold_evidence_ids:
        return False
    strict_supported = (strict_context_coverage or 0.0) >= 1.0 or (strict_citation_coverage or 0.0) >= 1.0
    equivalent_supported = (
        (equivalent_context_coverage or 0.0) >= 1.0
        or (equivalent_citation_coverage or 0.0) >= 1.0
    )
    return equivalent_supported and not strict_supported


def _fact_matches(answer: str, fact: str, *, threshold: float) -> bool:
    normalized_answer = _normalize(answer)
    normalized_fact = _normalize(fact)
    if not normalized_answer or not normalized_fact:
        return False
    if normalized_fact in normalized_answer:
        return True
    fact_tokens = _content_tokens(normalized_fact)
    if _looks_like_formula_fact(fact):
        formula_score = _formula_symbol_overlap(answer, fact)
        if formula_score is not None and formula_score >= _formula_symbol_threshold(fact):
            return True
    if not fact_tokens:
        return False
    answer_tokens = set(_content_tokens(normalized_answer))
    overlap = sum(1 for token in fact_tokens if _token_matches(token, answer_tokens))
    return overlap / len(fact_tokens) >= _effective_fact_threshold(fact, fact_tokens, threshold)


def _effective_fact_threshold(fact: str, fact_tokens: tuple[str, ...], threshold: float) -> float:
    lowered = fact.casefold()
    effective = threshold
    if len(fact_tokens) >= 12:
        effective = min(effective, 0.55)
    if len(fact_tokens) >= 24:
        effective = min(effective, 0.45)
    if "nearby context" in lowered or "caption:" in lowered:
        effective = min(effective, 0.30)
    if "latex:" in lowered or "\\begin" in lowered or "[equation" in lowered:
        effective = min(effective, 0.35)
    return effective


def _locator_matches(candidate: str, required: str) -> bool:
    return bool(candidate and required) and (
        candidate == required
        or candidate.startswith(required)
        or required.startswith(candidate)
    )


def _normalize(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def _token_set(text: str) -> set[str]:
    return {match.group(0).casefold() for match in re.finditer(r"[A-Za-z0-9_]+", str(text or ""))}


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.casefold()))


def _content_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in _tokens(_strip_structural_fact_noise(text))
        if token not in _STRUCTURAL_FACT_STOPWORDS
        and not token.startswith("chunk_")
        and not token.endswith("_")
        and len(token) > 1
    )


def _strip_structural_fact_noise(text: str) -> str:
    text = re.sub(r"\\label\{[^{}]*\}", " ", text)
    text = re.sub(r"\\(?:begin|end)\{[^{}]*\}", " ", text)
    previous = None
    while previous != text:
        previous = text
        text = _LATEX_WRAPPED_COMMAND_RE.sub(r"\1", text)
    text = re.sub(r"_\{([^}]+)\}", r"_\1", text)
    text = re.sub(r"\^\{([^}]+)\}", r"_\1", text)
    text = re.sub(r"\\([A-Za-z]+)_([A-Za-z0-9]+)", r" \1_\2 ", text)
    text = re.sub(r"\\([A-Za-z]+)(?=[\s_^{},)\\]|$)", r" \1 ", text)
    text = text.replace("\\", " ")
    text = text.replace("{", " ").replace("}", " ")
    text = text.replace("`", " ")
    text = re.sub(r"(?<!\\)\[(?:figure|fig|table|tbl|equation|eq)\s+[^\]]+\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\\)\[(?:\d+|[A-Za-z]{1,16}[-_][A-Za-z0-9_-]{1,32})\]", " ", text)
    text = re.split(r"\bSource\s*:", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return text


def _token_matches(token: str, answer_tokens: set[str]) -> bool:
    if token in answer_tokens:
        return True
    if token == "qkv" and {"query", "key", "value"}.issubset(answer_tokens):
        return True
    if len(token) > 3 and token.endswith("s") and token[:-1] in answer_tokens:
        return True
    if len(token) > 3 and f"{token}s" in answer_tokens:
        return True
    return False


def _looks_like_formula_fact(fact: str) -> bool:
    lowered = str(fact or "").casefold()
    return (
        "\\begin" in lowered
        or "\\end" in lowered
        or "\\[" in lowered
        or "$" in lowered
        or bool(re.search(r"[A-Za-z0-9_')]\s*[=+\-*/]\s*[A-Za-z0-9_('\\]", str(fact or "")))
    )


def _formula_symbol_overlap(answer: str, fact: str) -> float | None:
    fact_symbols = _formula_symbol_tokens(fact)
    if not fact_symbols:
        return None
    answer_symbols = _formula_symbol_tokens(answer)
    if not answer_symbols:
        return 0.0
    return len(fact_symbols & answer_symbols) / len(fact_symbols)


def _formula_symbol_threshold(fact: str) -> float:
    symbols = _formula_symbol_tokens(fact)
    if len(symbols) <= 4:
        return 0.75
    if len(symbols) <= 8:
        return 0.55
    return 0.45


def _formula_symbol_tokens(text: str) -> set[str]:
    cleaned = _strip_structural_fact_noise(text)
    tokens = {
        token.strip("_").casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_']*", cleaned)
    }
    return {
        token.rstrip("'")
        for token in tokens
        if token
        and token not in _STRUCTURAL_FACT_STOPWORDS
        and token not in {"frac", "sqrt", "sum", "exp", "log", "sin", "cos", "min", "max"}
    }


def _clean_tuple(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return tuple(out)


__all__ = [
    "AnswerMetricCase",
    "AnswerMetricScore",
    "AnswerOutcome",
    "abstention_basis",
    "abstention_accuracy",
    "answer_relevance",
    "citation_grounding",
    "evidence_support_coverage",
    "evaluate_answer_case",
    "fact_coverage",
    "fact_coverage_details",
    "faithfulness_proxy",
    "id_coverage",
    "locator_coverage",
    "score_answer_case",
]
