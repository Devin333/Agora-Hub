"""Recover a paper's section structure from native PDF text styling.

Embedded PDF text carries no section markup, but papers set their headings in
a larger or bolder face than the body. Reading that styling restores real
section titles ("Introduction", "Results", "Conclusion"), which retrieval maps
to evidence roles; a page-per-section split would leave every section titled
by its page number.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

_BOLD_FLAG = 16
# A heading is short and bold, at body size or larger. Size alone is not
# enough: margin stamps and license notes are often set large but regular.
_BODY_SIZE_TOLERANCE = 0.98
_MAX_HEADING_CHARS = 90
_MAX_HEADING_WORDS = 12
# Section numbers ("3", "3.2", "A", "IV.") that a paper may set on their own line.
_SECTION_NUMBER = re.compile(r"^(?:\d+(?:\.\d+)*|[A-Z]|[IVXLC]+)\.?$")
# Headings numbered "3", "3.2" or "IV." open the paper body. A lone capital
# letter is left out: it cannot be told apart from a title starting "A ...".
_NUMBERED_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVXLC]+\.)\s+\S")
_MIN_SECTIONS = 3


@dataclass(frozen=True)
class PdfLine:
    page: int
    text: str
    size: float
    bold: bool


@dataclass
class HeadingSection:
    title: str
    level: int
    page_start: int
    page_end: int
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def read_pdf_lines(document: Any, *, max_pages: int) -> list[PdfLine]:
    """Read every non-empty text line with the style of its first span."""

    lines: list[PdfLine] = []
    for page_index, page in enumerate(document, start=1):
        if page_index > max_pages:
            break
        for block in page.get_text("dict", sort=True).get("blocks", []):
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", []) if str(span.get("text") or "").strip()]
                if not spans:
                    continue
                text = " ".join(str(span["text"]).strip() for span in spans).strip()
                lines.append(
                    PdfLine(
                        page=page_index,
                        text=text,
                        size=float(spans[0].get("size") or 0.0),
                        bold=all(int(span.get("flags") or 0) & _BOLD_FLAG for span in spans),
                    )
                )
    return lines


def heading_sections(lines: list[PdfLine]) -> list[HeadingSection] | None:
    """Split lines into sections at styled headings.

    Returns ``None`` when the styling does not show at least three headings,
    so the caller can fall back to a structure it can still trust.
    """

    body_size = _body_size(lines)
    if body_size is None:
        return None
    heading_sizes = sorted(
        {round(line.size, 1) for line in lines if _is_heading(line, body_size)},
        reverse=True,
    )
    sections: list[HeadingSection] = []
    preamble = HeadingSection(title="", level=1, page_start=1, page_end=1)
    current = preamble
    pending_number: str | None = None
    for line in lines:
        if _is_heading(line, body_size):
            if _SECTION_NUMBER.match(line.text):
                # A number set on its own line belongs to the next heading.
                pending_number = line.text.rstrip(".")
                continue
            title = f"{pending_number} {line.text}" if pending_number else line.text
            pending_number = None
            current = HeadingSection(
                title=title,
                level=_level(line, title, heading_sizes),
                page_start=line.page,
                page_end=line.page,
            )
            sections.append(current)
            continue
        if pending_number is not None:
            current.lines.append(pending_number)
            pending_number = None
        current.lines.append(line.text)
        current.page_end = line.page
    sections = _fold_front_matter(preamble, sections)
    # A heading with no body of its own ("6 Results") still names the
    # subsections under it, so it is kept when one follows.
    sections = [
        section
        for index, section in enumerate(sections)
        if section.text or _has_subsection(sections, index)
    ]
    if sum(1 for section in sections if section.title != _FRONT_MATTER) < _MIN_SECTIONS:
        return None
    return sections


_FRONT_MATTER = "Front matter"
# Unnumbered headings that open a paper's body.
_BODY_OPENINGS = ("abstract", "introduction", "摘要", "引言")


def _fold_front_matter(preamble: HeadingSection, sections: list[HeadingSection]) -> list[HeadingSection]:
    """Merge the title block (title, authors, notices) into one front matter section."""

    first_body = next(
        (
            index
            for index, section in enumerate(sections)
            if _NUMBERED_HEADING.match(section.title)
            or section.title.casefold().startswith(_BODY_OPENINGS)
        ),
        0,
    )
    front = sections[:first_body]
    if not preamble.text and not front:
        return sections
    lines = list(preamble.lines)
    for section in front:
        lines.extend([section.title, *section.lines])
    merged = HeadingSection(
        title=_FRONT_MATTER,
        level=1,
        page_start=1,
        page_end=front[-1].page_end if front else preamble.page_end,
        lines=lines,
    )
    return [merged, *sections[first_body:]]


def _has_subsection(sections: list[HeadingSection], index: int) -> bool:
    following = sections[index + 1] if index + 1 < len(sections) else None
    return following is not None and following.level > sections[index].level


def _body_size(lines: list[PdfLine]) -> float | None:
    weights: Counter[float] = Counter()
    for line in lines:
        weights[round(line.size, 1)] += len(line.text)
    if not weights:
        return None
    return weights.most_common(1)[0][0]


def _is_heading(line: PdfLine, body_size: float) -> bool:
    text = line.text
    if len(text) > _MAX_HEADING_CHARS or len(text.split()) > _MAX_HEADING_WORDS:
        return False
    if text.endswith((".", ",", ";", ":")) and not _SECTION_NUMBER.match(text):
        return False
    if not any(character.isalpha() for character in text) and not _SECTION_NUMBER.match(text):
        return False
    return line.bold and line.size >= body_size * _BODY_SIZE_TOLERANCE


def _level(line: PdfLine, title: str, heading_sizes: list[float]) -> int:
    number = title.split(" ", 1)[0] if _NUMBERED_HEADING.match(title) else ""
    if number and number[0].isdigit():
        return min(3, number.count(".") + 1)
    size = round(line.size, 1)
    return min(3, heading_sizes.index(size) + 1) if size in heading_sizes else 2


__all__ = ["HeadingSection", "PdfLine", "heading_sections", "read_pdf_lines"]
