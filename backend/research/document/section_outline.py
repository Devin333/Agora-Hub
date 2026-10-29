"""Section outline of a chunked paper, for structure-aware navigation.

The chunker emits one parent chunk per section (``metadata.is_parent``) and child
chunks that share its ``section_index``. The outline lists the sections in reading
order with their title, nesting level, passage ids and a short preview, so a
navigator can pick the sections worth reading without seeing the whole paper.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from backend.research.document.models import PaperChunk

# QASPER-style flattened headings nest with this separator ("Approach ::: Setup").
_HEADING_PATH_SEPARATOR = ":::"
DEFAULT_OUTLINE_PREVIEW_CHARS = 160


@dataclass(frozen=True)
class SectionOutlineEntry:
    section_index: int
    title: str
    level: int
    # Child passages of the section, in reading order.
    chunk_ids: tuple[str, ...]
    preview: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "section_index": self.section_index,
            "title": self.title,
            "level": self.level,
            "passages": len(self.chunk_ids),
        }


@dataclass(frozen=True)
class PaperSectionOutline:
    paper_id: str
    entries: tuple[SectionOutlineEntry, ...]

    @classmethod
    def from_chunks(
        cls,
        paper_id: str,
        chunks: Iterable[PaperChunk],
        *,
        preview_chars: int = DEFAULT_OUTLINE_PREVIEW_CHARS,
    ) -> "PaperSectionOutline":
        if type(preview_chars) is not int or preview_chars < 0:
            raise ValueError("preview_chars must be a non-negative integer")
        parents: dict[int, PaperChunk] = {}
        children: dict[int, list[PaperChunk]] = {}
        for chunk in chunks:
            if chunk.paper_id != paper_id:
                continue
            if chunk.metadata.get("is_parent"):
                parents.setdefault(chunk.section_index, chunk)
            else:
                children.setdefault(chunk.section_index, []).append(chunk)
        entries: list[SectionOutlineEntry] = []
        for section_index in sorted(children):
            passages = children[section_index]
            heading_source = parents.get(section_index) or passages[0]
            title = " ".join(str(heading_source.section_title or "").split())
            entries.append(SectionOutlineEntry(
                section_index=section_index,
                title=title,
                level=_section_level(heading_source, title),
                chunk_ids=tuple(chunk.chunk_id for chunk in passages),
                preview=_preview(passages, preview_chars),
            ))
        return cls(paper_id=paper_id, entries=tuple(entries))

    @property
    def navigable(self) -> bool:
        """True when the paper has real structure: at least two titled sections."""
        return sum(1 for entry in self.entries if entry.title) >= 2

    def entry(self, section_index: int) -> SectionOutlineEntry | None:
        for entry in self.entries:
            if entry.section_index == section_index:
                return entry
        return None

    def section_of(self) -> dict[str, int]:
        """Section index of every outlined passage."""
        return {
            chunk_id: entry.section_index
            for entry in self.entries
            for chunk_id in entry.chunk_ids
        }


def _section_level(chunk: PaperChunk, title: str) -> int:
    raw = chunk.metadata.get("section_level", chunk.metadata.get("level"))
    level = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 1
    # A flattened heading path carries its own depth even when the parser reports 1.
    return max(level, title.count(_HEADING_PATH_SEPARATOR) + 1)


def _preview(passages: list[PaperChunk], limit: int) -> str:
    if limit == 0:
        return ""
    text = " ".join(" ".join(chunk.content.split()) for chunk in passages[:2])
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut or text[:limit]} ..."


__all__ = [
    "DEFAULT_OUTLINE_PREVIEW_CHARS",
    "PaperSectionOutline",
    "SectionOutlineEntry",
]
