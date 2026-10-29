"""QASPER Answer-F1, aligned line for line with the official ``qasper_evaluator.py``.

The official scorer normalizes text SQuAD-style, takes token F1, and keeps the best
score over all annotators. This module reproduces those rules and adds the one step
the official script leaves to the caller: projecting a system outcome into the
prediction string it scores (abstention -> "Unanswerable", explicit yes/no answers
-> "Yes"/"No", citation markers removed).
"""

from __future__ import annotations

import re
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from framework.rag.evaluation.answer_metrics import AnswerOutcome

AnswerType = Literal["extractive", "abstractive", "boolean", "none"]
ANSWER_TYPES: tuple[AnswerType, ...] = ("extractive", "abstractive", "boolean", "none")
UNANSWERABLE = "Unanswerable"

_CITATION_RE = re.compile(r"\s*\[\d+\]")
_POLAR_QUESTION_RE = re.compile(
    r"^\s*(?:does|do|did|is|are|was|were|has|have|had|can|could|will|would|should)\b",
    re.IGNORECASE,
)
_YES_NO_RE = re.compile(r"^\W*(yes|no)\b", re.IGNORECASE)


@dataclass(frozen=True)
class QasperReference:
    answer: str
    answer_type: AnswerType


def normalize_answer(text: str) -> str:
    """Official SQuAD v1.1 normalization: lower, drop punctuation and articles, squash spaces."""
    text = str(text).lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def token_f1_score(prediction: str, ground_truth: str) -> float:
    prediction_tokens = normalize_answer(prediction).split()
    ground_truth_tokens = normalize_answer(ground_truth).split()
    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(prediction_tokens)
    recall = num_same / len(ground_truth_tokens)
    return (2 * precision * recall) / (precision + recall)


def references_for_question(qa: Mapping[str, Any]) -> tuple[QasperReference, ...]:
    """Build references exactly as the official ``get_answers_and_evidence`` does."""
    references: list[QasperReference] = []
    for annotation in qa.get("answers") or ():
        answer = annotation.get("answer") or {}
        if answer.get("unanswerable"):
            references.append(QasperReference(UNANSWERABLE, "none"))
        elif answer.get("extractive_spans"):
            references.append(QasperReference(", ".join(answer["extractive_spans"]), "extractive"))
        elif answer.get("free_form_answer"):
            references.append(QasperReference(str(answer["free_form_answer"]), "abstractive"))
        elif answer.get("yes_no"):
            references.append(QasperReference("Yes", "boolean"))
        elif answer.get("yes_no") is not None:
            references.append(QasperReference("No", "boolean"))
        else:
            raise ValueError(f"QASPER annotation {annotation.get('annotation_id')!r} has no answer")
    if not references:
        raise ValueError(f"QASPER question {qa.get('question_id')!r} has no annotations")
    return tuple(references)


def first_annotator_type(qa: Mapping[str, Any]) -> AnswerType:
    """Stratum used for sampling: the first annotator's answer type."""
    return references_for_question(qa)[0].answer_type


def project_prediction(question: str, outcome: AnswerOutcome, answer: str) -> str:
    """Map a system outcome to the string the official scorer compares."""
    if outcome.is_refusal:
        return UNANSWERABLE
    if outcome.is_system_failure:
        return ""
    text = _CITATION_RE.sub("", str(answer or "")).strip()
    if _POLAR_QUESTION_RE.match(question):
        match = _YES_NO_RE.match(text)
        if match:
            return match.group(1).capitalize()
    return text


@dataclass(frozen=True)
class AnswerF1:
    score: float
    matched_type: AnswerType


def max_answer_f1(prediction: str, references: Sequence[QasperReference]) -> AnswerF1:
    """Best F1 over annotators; ties keep the first, as the official stable sort does."""
    scored = [(token_f1_score(prediction, ref.answer), ref.answer_type) for ref in references]
    best = sorted(scored, key=lambda item: item[0], reverse=True)[0]
    return AnswerF1(score=best[0], matched_type=best[1])


__all__ = [
    "ANSWER_TYPES",
    "AnswerF1",
    "AnswerType",
    "QasperReference",
    "UNANSWERABLE",
    "first_annotator_type",
    "max_answer_f1",
    "normalize_answer",
    "project_prediction",
    "references_for_question",
    "token_f1_score",
]
