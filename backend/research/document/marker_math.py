from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from typing import Iterable


_SUPPRESSED_TAGS = {"script", "style", "noscript", "template"}
_BLOCK_BOUNDARY_TAGS = {"br", "p", "div", "tr", "li"}


class MarkerTextSource(str, Enum):
    PLAIN = "plain"
    LATEX = "latex"
    HTML = "html"


@dataclass(frozen=True)
class MarkerMathSpan:
    start_offset: int
    end_offset: int
    raw_latex: str
    display: bool


@dataclass(frozen=True)
class MarkerMathText:
    text: str
    spans: tuple[MarkerMathSpan, ...]


@dataclass(frozen=True)
class MarkerTextSegment:
    text: str
    raw_latex: str | None = None
    display: bool = False


class _MarkerMathHTMLParser(HTMLParser):
    """Extract Marker text/math without interpreting or executing embedded content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[MarkerTextSegment] = []
        self._suppressed_depth = 0
        self._math_depth = 0
        self._math_display = False
        self._math_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _SUPPRESSED_TAGS:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if tag == "math":
            if self._math_depth == 0:
                values = {key.casefold(): value for key, value in attrs}
                self._math_display = str(values.get("display") or "").casefold() in {"block", "display"}
                self._math_parts = []
            self._math_depth += 1
            return
        if self._math_depth:
            self._math_parts.append(self.get_starttag_text() or f"<{tag}>")
            return
        if tag in _BLOCK_BOUNDARY_TAGS:
            self.segments.append(MarkerTextSegment(" "))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _SUPPRESSED_TAGS and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if tag == "math" and self._math_depth:
            self._math_depth -= 1
            if self._math_depth == 0:
                raw_latex = _strip_paired_math_formatting("".join(self._math_parts))
                self.segments.append(MarkerTextSegment(
                    text=raw_latex,
                    raw_latex=raw_latex,
                    display=self._math_display,
                ))
                self._math_parts = []
                self._math_display = False
            return
        if self._math_depth:
            self._math_parts.append(f"</{tag}>")
            return
        if tag in _BLOCK_BOUNDARY_TAGS:
            self.segments.append(MarkerTextSegment(" "))

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        if self._math_depth:
            self._math_parts.append(data)
        else:
            self.segments.append(MarkerTextSegment(data))


def marker_math_text(
    value: str,
    *,
    source: MarkerTextSource,
    display: bool = False,
) -> MarkerMathText:
    """Parse text according to its declared Marker source semantics."""
    if not isinstance(source, MarkerTextSource):
        raise TypeError("source must be a MarkerTextSource")
    if not value:
        return MarkerMathText(text="", spans=())
    if source is MarkerTextSource.HTML:
        parser = _MarkerMathHTMLParser()
        parser.feed(value)
        parser.close()
        return compose_marker_math_text(parser.segments)
    if source is MarkerTextSource.LATEX:
        return MarkerMathText(
            text=value,
            spans=(MarkerMathSpan(
                start_offset=0,
                end_offset=len(value),
                raw_latex=value,
                display=display,
            ),),
        )
    return _delimited_math_text(value)


def compose_marker_math_text(segments: Iterable[MarkerTextSegment]) -> MarkerMathText:
    parts: list[str] = []
    spans: list[MarkerMathSpan] = []
    cursor = 0
    for segment in segments:
        normalized = _normalize(segment.text)
        if not normalized:
            continue
        if parts:
            parts.append(" ")
            cursor += 1
        start = cursor
        parts.append(normalized)
        cursor += len(normalized)
        if segment.raw_latex is not None and segment.raw_latex.strip():
            spans.append(MarkerMathSpan(
                start_offset=start,
                end_offset=cursor,
                raw_latex=segment.raw_latex,
                display=segment.display,
            ))
    return MarkerMathText(text="".join(parts), spans=tuple(spans))


def _delimited_math_text(value: str) -> MarkerMathText:
    text = _normalize(value)
    spans: list[MarkerMathSpan] = []
    index = 0
    while index < len(text):
        delimiter = _math_open_delimiter(text, index)
        if delimiter is None:
            index += 1
            continue
        opening, closing, display = delimiter
        content_start = index + len(opening)
        close_at = _find_unescaped(text, closing, content_start)
        if close_at < 0:
            index = content_start
            continue
        if close_at > content_start:
            spans.append(MarkerMathSpan(
                start_offset=content_start,
                end_offset=close_at,
                raw_latex=text[content_start:close_at],
                display=display,
            ))
        index = close_at + len(closing)
    return MarkerMathText(text=text, spans=tuple(spans))


def _math_open_delimiter(text: str, index: int) -> tuple[str, str, bool] | None:
    if text.startswith(r"\[", index):
        return r"\[", r"\]", True
    if text.startswith(r"\(", index):
        return r"\(", r"\)", False
    if text[index] == "$" and not _is_escaped(text, index):
        if text.startswith("$$", index):
            return "$$", "$$", True
        return "$", "$", False
    return None


def _find_unescaped(text: str, delimiter: str, start: int) -> int:
    index = start
    while True:
        found = text.find(delimiter, index)
        if found < 0:
            return -1
        if not _is_escaped(text, found):
            return found
        index = found + len(delimiter)


def _is_escaped(text: str, index: int) -> bool:
    slash_count = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slash_count += 1
        index -= 1
    return slash_count % 2 == 1


_MATH_FORMATTING_PAIR = re.compile(
    r"<(b|strong|i|em|span)(?:\s[^>]*)?>(.*?)</\1\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)


def _strip_paired_math_formatting(value: str) -> str:
    previous = None
    while value != previous:
        previous = value
        value = _MATH_FORMATTING_PAIR.sub(lambda match: match.group(2), value)
    return value


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


__all__ = [
    "MarkerMathSpan",
    "MarkerMathText",
    "MarkerTextSegment",
    "MarkerTextSource",
    "compose_marker_math_text",
    "marker_math_text",
]
