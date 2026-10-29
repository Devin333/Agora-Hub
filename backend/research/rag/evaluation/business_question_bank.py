"""Load the business question bank into scorable evidence QA pairs.

Only reviewed single-paper cases are scored today. Draft cases and multi-paper cases
are kept as explicit skips with a reason, so a report always states how much of the
bank it actually covers instead of silently shrinking the denominator.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair

SCORABLE_ANNOTATION_STATUSES = frozenset({"reviewed", "reviewed_reused"})
DRAFT_ANNOTATION_STATUSES = frozenset({"draft_needs_annotation"})
_KNOWN_BEHAVIORS = frozenset({"answer", "abstain"})


class BusinessQuestionBankError(ValueError):
    """The question bank file violates its schema."""


@dataclass(frozen=True)
class SkippedCase:
    case_id: str
    category: str
    reason: str


@dataclass(frozen=True)
class BusinessQuestionBank:
    pairs: tuple[EvidenceQAPair, ...]
    skipped: tuple[SkippedCase, ...]
    total_rows: int

    def coverage(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "scored": len(self.pairs),
            "skipped": len(self.skipped),
            "scored_by_category": dict(
                sorted(Counter(pair.metadata["business_category"] for pair in self.pairs).items())
            ),
            "skipped_by_reason": dict(sorted(Counter(item.reason for item in self.skipped).items())),
        }

    def paper_ids(self) -> set[str]:
        return {pair.paper_id for pair in self.pairs}


def load_business_question_bank(
    path: str | Path,
    *,
    chunks: Iterable[PaperChunk] | None = None,
) -> BusinessQuestionBank:
    source = Path(path)
    equivalence = _GoldChunkEquivalence.from_chunks(chunks)
    pairs: list[EvidenceQAPair] = []
    skipped: list[SkippedCase] = []
    seen: set[str] = set()
    total = 0
    for line_number, raw in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        total += 1
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BusinessQuestionBankError(f"{source}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise BusinessQuestionBankError(f"{source}:{line_number}: each line must be a JSON object")
        case_id = _required_text(row, "case_id", source, line_number)
        if case_id in seen:
            raise BusinessQuestionBankError(f"{source}:{line_number}: duplicate case_id {case_id!r}")
        seen.add(case_id)
        category = _required_text(row, "category", source, line_number)
        status = _required_text(row, "annotation_status", source, line_number)
        behavior = _required_text(row, "expected_behavior", source, line_number)
        if behavior not in _KNOWN_BEHAVIORS:
            raise BusinessQuestionBankError(
                f"{source}:{line_number}: expected_behavior must be one of {sorted(_KNOWN_BEHAVIORS)}"
            )

        if status in DRAFT_ANNOTATION_STATUSES:
            skipped.append(SkippedCase(case_id, category, "needs_annotation"))
            continue
        if status not in SCORABLE_ANNOTATION_STATUSES:
            raise BusinessQuestionBankError(
                f"{source}:{line_number}: unknown annotation_status {status!r}"
            )
        papers = [str(item).strip() for item in row.get("source_papers") or [] if str(item).strip()]
        if len(papers) != 1:
            # Cross-paper cases need a cross-source runner; they are not scored as single-paper.
            skipped.append(SkippedCase(case_id, category, "multi_paper_unsupported"))
            continue
        pairs.append(
            _pair_from_row(
                row,
                case_id=case_id,
                category=category,
                paper_id=papers[0],
                behavior=behavior,
                source=source,
                line_number=line_number,
                equivalence=equivalence,
            )
        )
    return BusinessQuestionBank(pairs=tuple(pairs), skipped=tuple(skipped), total_rows=total)


def _pair_from_row(
    row: dict[str, Any],
    *,
    case_id: str,
    category: str,
    paper_id: str,
    behavior: str,
    source: Path,
    line_number: int,
    equivalence: "_GoldChunkEquivalence",
) -> EvidenceQAPair:
    question = _required_text(row, "question", source, line_number)
    metadata = {
        "business_case_id": case_id,
        "business_category": category,
        "annotation_status": str(row.get("annotation_status")),
        "source_refs": list(row.get("source_refs") or []),
    }
    qa_type = f"business_{category}"
    if behavior == "abstain":
        return EvidenceQAPair.negative(question=question, paper_id=paper_id, qa_type=qa_type, metadata=metadata)
    gold = row.get("gold_evidence") if isinstance(row.get("gold_evidence"), dict) else {}
    gold_ids = [str(item) for item in gold.get("gold_chunk_ids") or [] if str(item).strip()]
    if not gold_ids:
        raise BusinessQuestionBankError(
            f"{source}:{line_number}: reviewed answer case {case_id!r} has no gold_chunk_ids"
        )
    return EvidenceQAPair(
        question=question,
        paper_id=paper_id,
        qa_type=qa_type,
        gold_chunk_ids=gold_ids,
        equivalent_gold_chunk_ids=equivalence.expand(gold_ids, paper_id=paper_id),
        gold_source_locators=list(gold.get("gold_source_locators") or []),
        required_evidence_types=list(gold.get("required_evidence_types") or []),
        metadata=metadata,
    )


@dataclass(frozen=True)
class _GoldChunkEquivalence:
    paper_by_chunk_id: dict[str, str]
    component_by_chunk_id: dict[str, tuple[str, ...]]

    @classmethod
    def from_chunks(cls, chunks: Iterable[PaperChunk] | None) -> "_GoldChunkEquivalence":
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks or ()}
        adjacency: dict[str, set[str]] = {}
        for child in chunk_by_id.values():
            parent = chunk_by_id.get(str(child.parent_chunk_id or "").strip())
            if parent is None or child.paper_id != parent.paper_id:
                continue
            child_text = _normalize_chunk_content(child.content)
            parent_text = _normalize_chunk_content(parent.content)
            if not child_text or child_text != parent_text:
                continue
            adjacency.setdefault(child.chunk_id, set()).add(parent.chunk_id)
            adjacency.setdefault(parent.chunk_id, set()).add(child.chunk_id)

        component_by_chunk_id: dict[str, tuple[str, ...]] = {}
        visited: set[str] = set()
        for seed in sorted(adjacency):
            if seed in visited:
                continue
            component: set[str] = set()
            pending = [seed]
            while pending:
                chunk_id = pending.pop()
                if chunk_id in component:
                    continue
                component.add(chunk_id)
                pending.extend(sorted(adjacency.get(chunk_id, ()), reverse=True))
            visited.update(component)
            ordered = tuple(sorted(component))
            for chunk_id in component:
                component_by_chunk_id[chunk_id] = ordered
        return cls(
            paper_by_chunk_id={chunk_id: chunk.paper_id for chunk_id, chunk in chunk_by_id.items()},
            component_by_chunk_id=component_by_chunk_id,
        )

    def expand(self, gold_ids: list[str], *, paper_id: str) -> list[str]:
        expanded: list[str] = []
        seen: set[str] = set()
        for gold_id in gold_ids:
            candidates = (gold_id,)
            if self.paper_by_chunk_id.get(gold_id) == paper_id:
                candidates = (gold_id, *self.component_by_chunk_id.get(gold_id, ()))
            for chunk_id in candidates:
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                expanded.append(chunk_id)
        return expanded


def _normalize_chunk_content(content: str) -> str:
    return " ".join(str(content or "").split())


def _required_text(row: dict[str, Any], key: str, source: Path, line_number: int) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise BusinessQuestionBankError(f"{source}:{line_number}: {key} is required")
    return value


__all__ = [
    "BusinessQuestionBank",
    "BusinessQuestionBankError",
    "SkippedCase",
    "load_business_question_bank",
]
