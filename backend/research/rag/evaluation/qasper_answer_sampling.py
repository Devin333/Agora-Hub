"""Deterministic stratified sampling of QASPER questions by first-annotator answer type.

Each stratum is ordered by ``sha256(seed:case_id)`` and truncated to the quota, so a
sample depends only on the dataset and the seed, never on any run result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.research.rag.evaluation.qasper_answer_metrics import (
    ANSWER_TYPES,
    AnswerType,
    first_annotator_type,
)


@dataclass(frozen=True)
class SampledQuestion:
    case_id: str
    paper_id: str
    question_id: str
    question: str
    answer_type: AnswerType


@dataclass(frozen=True)
class QasperAnswerSample:
    seed: str
    per_type: int
    questions: tuple[SampledQuestion, ...]
    available_by_type: Mapping[str, int]

    def selected_by_type(self) -> dict[str, int]:
        counts = {answer_type: 0 for answer_type in ANSWER_TYPES}
        for question in self.questions:
            counts[question.answer_type] += 1
        return counts

    def manifest(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "per_type": self.per_type,
            "available_by_type": dict(self.available_by_type),
            "selected_by_type": self.selected_by_type(),
            "case_ids": [question.case_id for question in self.questions],
        }


def sample_questions(raw_dataset: Mapping[str, Any], *, seed: str, per_type: int) -> QasperAnswerSample:
    if per_type < 1:
        raise ValueError("per_type must be positive")
    if not str(seed).strip():
        raise ValueError("seed is required")
    papers = raw_dataset.get("papers", raw_dataset)
    strata: dict[str, list[tuple[str, SampledQuestion]]] = {answer_type: [] for answer_type in ANSWER_TYPES}
    for paper_id in sorted(papers):
        for qa in papers[paper_id].get("qas") or ():
            question_id = str(qa["question_id"])
            case_id = f"{paper_id}:{question_id}"
            answer_type = first_annotator_type(qa)
            order = hashlib.sha256(f"{seed}:{case_id}".encode("utf-8")).hexdigest()
            strata[answer_type].append(
                (order, SampledQuestion(case_id, paper_id, question_id, str(qa["question"]), answer_type))
            )
    selected: list[SampledQuestion] = []
    for answer_type in ANSWER_TYPES:
        ranked = sorted(strata[answer_type], key=lambda item: item[0])
        selected.extend(question for _, question in ranked[:per_type])
    return QasperAnswerSample(
        seed=str(seed),
        per_type=per_type,
        questions=tuple(selected),
        available_by_type={answer_type: len(strata[answer_type]) for answer_type in ANSWER_TYPES},
    )


__all__ = ["QasperAnswerSample", "SampledQuestion", "sample_questions"]
