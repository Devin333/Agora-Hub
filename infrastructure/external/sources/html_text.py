"""Plain text from an HTML fragment carried inside a feed or API payload.

JSON Feed ``content_html`` and the Hacker News ``text`` field are HTML. Their
text must come from parsing it: unescaping alone keeps the tags, and stripping
``<...>`` after unescaping also deletes text that was escaped to look like a
tag (``&lt;b&gt;`` is the literal text "<b>"). Script and style contents are
not text; block elements separate words.
"""

from __future__ import annotations

from html.parser import HTMLParser

_SKIPPED = frozenset({"script", "style", "template", "noscript"})
_BLOCKS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "td", "th", "tr", "ul",
    }
)


class _FragmentText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skipping += 1
        elif tag in _BLOCKS:
            self.parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCKS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCKS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_fragment_text(value: str | None) -> str | None:
    """The fragment's text with whitespace collapsed, or None when it has none."""

    if value is None:
        return None
    parser = _FragmentText()
    parser.feed(value)
    parser.close()
    return " ".join("".join(parser.parts).split()) or None


__all__ = ["html_fragment_text"]
