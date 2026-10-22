"""Shared character budget for the passages sent to the paper answer model.

Selected passages share one total budget. Everything is sent verbatim when it fits.
On overflow, primary evidence is funded before interpretation context, and each tier
is split by water-filling so short passages stay whole and long ones share the rest.

A passage that must be trimmed keeps its pinned lines (title, locators) and then the
sentences that share the most content terms with the question, in original order.
It never keeps a fixed head/tail window: the answer to a question is frequently in
the middle of a passage.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

ELISION = "[…]"
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Sentence ends after terminal punctuation, optionally followed by a closing quote or bracket.
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|(?<=[.!?;][\"”’)\]])\s+")
_STOPWORDS = frozenset(
    """
    a an and are as at be been being but by can could did do does doing for from had has
    have how if in into is it its may might of on or should so such than that the their
    them then there these they this those to under was were what when where which while
    who whom whose why will with within would about above after again against all also
    any because before below between both during each few further here more most other
    our out over own same some only very via upon not no nor
    paper papers text passage passages context provided according describe described
    explain explained mention mentioned report reported show shown shows state stated
    say says said give given value values used use using
    """.split()
)
_SUFFIXES = ("ing", "ed", "es", "s")
_MIN_STEM = 3


@dataclass(frozen=True)
class ContextLine:
    label: str
    text: str
    pinned: bool = False

    def render(self) -> str:
        return f"{self.label}: {self.text}" if self.label else self.text


def render_lines(lines: Sequence[ContextLine]) -> str:
    return "\n".join(line.render() for line in lines)


def allocate_budgets(
    lengths: Sequence[int],
    *,
    tiers: Sequence[int],
    total: int,
    floor: int,
) -> list[int]:
    """Per-passage character budgets that sum to at most ``total``.

    Lower tier numbers are funded first. Every passage receives at least
    ``min(length, floor)`` so a selected citation never disappears from the prompt.
    """
    if len(lengths) != len(tiers):
        raise ValueError("lengths and tiers must have the same size")
    if total < 1 or floor < 1:
        raise ValueError("total and floor must be positive")
    if any(length < 0 for length in lengths):
        raise ValueError("lengths must not be negative")
    if sum(lengths) <= total:
        return list(lengths)
    reserved = [min(length, floor) for length in lengths]
    if sum(reserved) > total:
        raise ValueError(
            f"context budget {total} cannot hold {len(lengths)} passages at the {floor}-char floor"
        )
    budgets = list(reserved)
    remaining = total - sum(reserved)
    for tier in sorted(set(tiers)):
        if remaining <= 0:
            break
        members = [index for index, value in enumerate(tiers) if value == tier]
        remaining = _water_fill(budgets, lengths, members, remaining)
    return budgets


def _water_fill(budgets: list[int], lengths: Sequence[int], members: list[int], amount: int) -> int:
    """Raise the members toward their full length as evenly as possible; return leftover."""
    open_members = [index for index in members if budgets[index] < lengths[index]]
    while amount > 0 and open_members:
        share = max(1, amount // len(open_members))
        for index in list(open_members):
            if amount <= 0:
                break
            grant = min(share, lengths[index] - budgets[index], amount)
            budgets[index] += grant
            amount -= grant
            if budgets[index] >= lengths[index]:
                open_members.remove(index)
    return amount


def question_terms(question: str) -> frozenset[str]:
    return frozenset(
        stem
        for token in _TOKEN_RE.findall(str(question or ""))
        if (stem := _stem(token)) and stem not in _STOPWORDS
    )


def fit_lines(lines: Sequence[ContextLine], *, max_chars: int, terms: frozenset[str]) -> str:
    """Render ``lines`` within ``max_chars``, keeping the most question-relevant sentences."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    full = render_lines(lines)
    if len(full) <= max_chars:
        return full

    sentences = [_split_sentences(line.text) for line in lines]
    pinned: set[int] = set()
    chosen: dict[int, set[int]] = {}
    partial: dict[tuple[int, int], str] = {}

    def rendered() -> str:
        return _render_selection(lines, sentences, pinned, chosen, partial)

    # Pinned lines (title, locators) are kept in order while they fit.
    for index, line in enumerate(lines):
        if not line.pinned:
            continue
        pinned.add(index)
        if len(rendered()) > max_chars:
            pinned.discard(index)

    units = [
        (line_index, sentence_index)
        for line_index, line_sentences in enumerate(sentences)
        if line_index not in pinned and not lines[line_index].pinned
        for sentence_index in range(len(line_sentences))
    ]
    units.sort(key=lambda unit: (-_score(sentences[unit[0]][unit[1]], terms), unit))
    for line_index, sentence_index in units:
        chosen.setdefault(line_index, set()).add(sentence_index)
        if len(rendered()) > max_chars:
            chosen[line_index].discard(sentence_index)
            if not chosen[line_index]:
                del chosen[line_index]

    if not chosen and units:
        # Not even one whole sentence fits: keep a word-bounded prefix of the best one.
        line_index, sentence_index = units[0]
        chosen[line_index] = {sentence_index}
        overflow = len(rendered()) - max_chars
        sentence = sentences[line_index][sentence_index]
        keep = max(0, len(sentence) - overflow - len(ELISION) - 1)
        prefix = sentence[:keep].rsplit(" ", 1)[0].rstrip()
        if prefix:
            partial[(line_index, sentence_index)] = f"{prefix} {ELISION}"
        else:
            del chosen[line_index]

    text = rendered()
    if len(text) > max_chars:
        raise AssertionError("fit_lines exceeded its character budget")
    return text


def _render_selection(
    lines: Sequence[ContextLine],
    sentences: Sequence[Sequence[str]],
    pinned: set[int],
    chosen: dict[int, set[int]],
    partial: dict[tuple[int, int], str],
) -> str:
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in pinned:
            out.append(line.render())
            continue
        keep = sorted(chosen.get(index, ()))
        if not keep:
            continue
        parts: list[str] = [ELISION] if keep[0] > 0 else []
        previous = None
        for sentence_index in keep:
            if previous is not None and sentence_index != previous + 1:
                parts.append(ELISION)
            parts.append(partial.get((index, sentence_index), sentences[index][sentence_index]))
            previous = sentence_index
        if keep[-1] < len(sentences[index]) - 1 and (index, keep[-1]) not in partial:
            parts.append(ELISION)
        out.append(ContextLine(line.label, " ".join(parts)).render())
    return "\n".join(out)


def _split_sentences(text: str) -> list[str]:
    return [part for part in (piece.strip() for piece in _SENTENCE_RE.split(str(text or ""))) if part]


def _score(sentence: str, terms: frozenset[str]) -> int:
    if not terms:
        return 0
    return len(terms & {_stem(token) for token in _TOKEN_RE.findall(sentence)})


def _stem(token: str) -> str:
    word = token.casefold()
    if len(word) < _MIN_STEM:
        return ""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    return word


__all__ = [
    "ELISION",
    "ContextLine",
    "allocate_budgets",
    "fit_lines",
    "question_terms",
    "render_lines",
]
