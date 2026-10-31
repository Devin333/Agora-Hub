from __future__ import annotations

import pytest

from infrastructure.external.sources.html_text import html_fragment_text


@pytest.mark.parametrize(
    ("fragment", "text"),
    [
        ("<p>Policy summary.</p>", "Policy summary."),
        ("<b>bold</b>   and\n<i>italic</i>", "bold and italic"),
        ("Tom &amp; Jerry", "Tom & Jerry"),
        # Escaped markup is text, not a tag to strip.
        ("use &lt;b&gt; for bold", "use <b> for bold"),
        ("<p>one</p><p>two</p>", "one two"),
        ("line<br>break", "line break"),
        ("<script>alert(1)</script>shown<style>p{}</style>", "shown"),
        ("unclosed <b>tag", "unclosed tag"),
        ("plain text", "plain text"),
    ],
)
def test_fragment_text_is_what_a_reader_would_see(fragment: str, text: str) -> None:
    assert html_fragment_text(fragment) == text


@pytest.mark.parametrize("fragment", [None, "", "   ", "<p> </p>", "<script>x()</script>"])
def test_fragment_without_text_is_none(fragment: str | None) -> None:
    assert html_fragment_text(fragment) is None
