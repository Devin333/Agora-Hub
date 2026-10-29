from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_GROUNDED_SYSTEM_INSTRUCTION = (
    "You are a research assistant answering questions about an academic paper. "
    "Answer ONLY from the numbered context passages below. "
    "Cite the passages you use with bracketed numbers like [1], [2]. "
    "If the context does not contain the answer, say so explicitly. "
    "Be concise and precise."
)

_BRACKET_CITATION_RE = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class CitationMarkers:
    """Bracketed citation markers found in an answer, split by validity."""

    valid_indexes: tuple[int, ...]
    """1-based context indexes in first-seen order, without duplicates."""
    invalid_markers: tuple[str, ...]
    """Markers such as ``[9]`` that point outside the provided contexts."""


def numbered_context_block(contexts: Sequence[str]) -> str:
    return "\n\n".join(f"[{index + 1}] {text}" for index, text in enumerate(contexts))


def build_numbered_context_prompt(
    *,
    question: str,
    contexts: Sequence[str],
    system_instruction: str = DEFAULT_GROUNDED_SYSTEM_INSTRUCTION,
) -> str:
    return (
        f"{system_instruction}\n\n"
        f"Context passages:\n{numbered_context_block(contexts)}\n\n"
        f"Question: {question}\n\n"
        "Answer (with citations):"
    )


def partition_citation_markers(answer: str, *, context_count: int) -> CitationMarkers:
    valid: list[int] = []
    invalid: list[str] = []
    for match in _BRACKET_CITATION_RE.finditer(str(answer or "")):
        index = int(match.group(1))
        if 1 <= index <= context_count:
            if index not in valid:
                valid.append(index)
        elif match.group(0) not in invalid:
            invalid.append(match.group(0))
    return CitationMarkers(valid_indexes=tuple(valid), invalid_markers=tuple(invalid))


def cited_context_indexes(answer: str, *, context_count: int) -> tuple[int, ...]:
    """0-based indexes of valid citations; out-of-range markers are ignored."""
    markers = partition_citation_markers(answer, context_count=context_count)
    return tuple(index - 1 for index in markers.valid_indexes)


__all__ = [
    "CitationMarkers",
    "DEFAULT_GROUNDED_SYSTEM_INSTRUCTION",
    "build_numbered_context_prompt",
    "cited_context_indexes",
    "numbered_context_block",
    "partition_citation_markers",
]
