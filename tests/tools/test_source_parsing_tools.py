"""Feed / HTML / manual parsing, URL normalization and source search with hostile input.

These tools take model- or upstream-supplied text and never touch the
network; the risks are malformed input, XML expansion attacks, external
entities, and inputs large enough to stall a worker or blow the result size.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from backend.foundation.models.source import SourceDefinition
from backend.foundation.registry.source_registry import SourceRegistry
from framework.tool import ToolStatus
from tests.tools.harness.invoke import ToolRun, assert_message_is_safe, expect_succeeded, run_tool
from tests.tools.harness.network import source_tool_environment
from tests.tools.harness.registry import ToolEnvironment

FEED = {"source_id": "feed", "name": "Feed", "url": "https://example.com/feed.xml"}
PAGE = {"source_id": "page", "name": "Page", "url": "https://example.com/news", "source_type": "html"}
MANUAL = {"source_id": "manual", "name": "Manual", "url": "https://example.com/manual", "source_type": "manual"}


def _rss(items: int) -> str:
    body = "".join(f"<item><title>Item {i}</title><link>https://example.com/{i}</link></item>" for i in range(items))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>{body}</channel></rss>'


def _atom(items: int) -> str:
    body = "".join(
        f"<entry><title>Entry {i}</title><id>urn:e:{i}</id><link href='https://example.com/{i}'/>"
        "<updated>2026-01-01T00:00:00Z</updated></entry>"
        for i in range(items)
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Feed</title>{body}</feed>'


def _billion_laughs(root: str) -> str:
    entities = '<!ENTITY a "aaaaaaaaaa">' + "".join(
        f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(8)
    )
    return f'<?xml version="1.0"?><!DOCTYPE {root} [{entities}]><{root}><title>&i;</title></{root}>'


def _feed_for(tool: str, items: int) -> tuple[dict[str, Any], str]:
    if tool == "source.parse_atom":
        return dict(FEED, source_type="atom"), _atom(items)
    return FEED, _rss(items)


@pytest.fixture
def env(tool_env_factory) -> ToolEnvironment:
    return source_tool_environment(tool_env_factory)


def _controlled(run: ToolRun) -> None:
    """Either a clean result or a clean failure; never a crash or a hang."""

    assert run.status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED}, run.describe()
    if run.status is ToolStatus.FAILED:
        assert run.error_message, run.describe()
        assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.parse_rss", "A")
@pytest.mark.tool_scenario("source.parse_atom", "A")
@pytest.mark.parametrize("tool", ("source.parse_rss", "source.parse_atom"))
@pytest.mark.parametrize(
    "xml",
    ["", "<rss><channel><item>", "<html><body>not a feed</body></html>", "\x00\x01\x02", "<?xml version='1.0'?>", "plain text"],
    ids=["empty", "unclosed", "html", "binary", "prolog-only", "text"],
)
def test_malformed_feeds_fail_cleanly(env: ToolEnvironment, tool: str, xml: str) -> None:
    source = dict(FEED, source_type="atom") if tool == "source.parse_atom" else FEED
    run = run_tool(env.registry, tool, {"source": source, "xml": xml})

    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.parse_rss", "A")
@pytest.mark.tool_scenario("source.parse_atom", "A")
@pytest.mark.parametrize("tool", ("source.parse_rss", "source.parse_atom"))
def test_well_formed_feeds_parse(env: ToolEnvironment, tool: str) -> None:
    source, xml = _feed_for(tool, 3)
    output = expect_succeeded(run_tool(env.registry, tool, {"source": source, "xml": xml}))
    assert output["item_count"] == 3


@pytest.mark.tool_scenario("source.parse_rss", "G")
@pytest.mark.tool_scenario("source.parse_atom", "G")
@pytest.mark.parametrize(("tool", "root"), [("source.parse_rss", "rss"), ("source.parse_atom", "feed")])
def test_entity_expansion_bomb_is_rejected_quickly(env: ToolEnvironment, tool: str, root: str) -> None:
    run = run_tool(env.registry, tool, {"source": FEED, "xml": _billion_laughs(root)})

    assert run.status is ToolStatus.FAILED, run.describe()
    assert "amplification" in run.error_message
    assert run.elapsed_seconds < 2.0


@pytest.mark.tool_scenario("source.parse_rss", "G")
@pytest.mark.tool_scenario("source.parse_atom", "G")
@pytest.mark.parametrize(("tool", "root"), [("source.parse_rss", "rss"), ("source.parse_atom", "feed")])
def test_external_entities_never_read_local_files(env: ToolEnvironment, tmp_path: Path, tool: str, root: str) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-FILE-CONTENT", encoding="utf-8")
    xml = (
        f'<?xml version="1.0"?><!DOCTYPE {root} [<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
        f"<{root}><title>&x;</title></{root}>"
    )

    run = run_tool(env.registry, tool, {"source": FEED, "xml": xml})

    _controlled(run)
    assert "TOP-SECRET" not in json.dumps(run.output, default=str)
    assert "TOP-SECRET" not in run.error_message


@pytest.mark.tool_scenario("source.parse_rss", "D")
@pytest.mark.tool_scenario("source.parse_atom", "D")
@pytest.mark.parametrize("tool", ("source.parse_rss", "source.parse_atom"))
def test_huge_feeds_honor_limit_and_the_result_size_cap(env: ToolEnvironment, tool: str) -> None:
    # Built here, not in parametrize: pytest would put the 20k-item feed in the
    # test id, past Windows' 32k environment-variable limit.
    source, xml = _feed_for(tool, 20_000)

    limited = run_tool(env.registry, tool, {"source": source, "xml": xml, "limit": 5})
    assert expect_succeeded(limited)["item_count"] == 5

    unlimited = run_tool(env.registry, tool, {"source": source, "xml": xml})
    _controlled(unlimited)
    if unlimited.status is ToolStatus.FAILED:
        assert "max_result_bytes" in unlimited.error_message
    assert unlimited.elapsed_seconds < 10.0


@pytest.mark.tool_scenario("source.extract_html", "A")
@pytest.mark.parametrize(
    "html",
    ["", "<html><body><p>unclosed", "<<<>>>", "\x00binary\x01", "<script>while(1){}</script>"],
    ids=["empty", "unclosed", "garbage", "binary", "script"],
)
def test_broken_html_is_handled(env: ToolEnvironment, html: str) -> None:
    _controlled(run_tool(env.registry, "source.extract_html", {"source": PAGE, "html": html}))


@pytest.mark.tool_scenario("source.extract_html", "A")
def test_extract_html_rejects_a_non_html_source(env: ToolEnvironment) -> None:
    run = run_tool(env.registry, "source.extract_html", {"source": dict(FEED, source_type="rss"), "html": "<p>x</p>"})
    assert run.status is ToolStatus.FAILED
    assert "source_type" in run.error_message


@pytest.mark.tool_scenario("source.extract_html", "D")
@pytest.mark.parametrize(
    "html",
    ["<div>" * 50_000 + "deep" + "</div>" * 50_000, "<a href='/x'>link</a>" * 30_000, "x" * 900_000],
    ids=["deeply-nested", "many-links", "huge-text"],
)
def test_pathological_html_stays_bounded(env: ToolEnvironment, html: str) -> None:
    run = run_tool(env.registry, "source.extract_html", {"source": PAGE, "html": html})
    _controlled(run)
    assert run.elapsed_seconds < 10.0


@pytest.mark.tool_scenario("source.extract_items", "A")
@pytest.mark.parametrize(
    ("source", "content"),
    [(FEED, [1, None]), (FEED, ""), (PAGE, 42), (MANUAL, "not-a-list"), ({"source_id": "x"}, "<rss/>")],
    ids=["feed-array", "feed-empty", "html-number", "manual-string", "incomplete-source"],
)
def test_extract_items_rejects_mismatched_content(env: ToolEnvironment, source: dict, content: Any) -> None:
    run = run_tool(env.registry, "source.extract_items", {"source": source, "content": content})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.extract_items", "D")
def test_extract_items_dispatches_large_feeds_with_limit(env: ToolEnvironment) -> None:
    output = expect_succeeded(
        run_tool(env.registry, "source.extract_items", {"source": FEED, "content": _rss(20_000), "limit": 3})
    )
    assert output["item_count"] == 3


@pytest.mark.tool_scenario("source.extract_manual", "A")
def test_manual_records_that_are_not_objects_are_reported_not_fatal(env: ToolEnvironment) -> None:
    records = [1, "x", None, {"title": "Kept", "url": "https://example.com/kept"}]
    run = run_tool(env.registry, "source.extract_manual", {"source": MANUAL, "records": records})

    _controlled(run)
    if run.status is ToolStatus.SUCCEEDED:
        assert run.output["item_count"] <= 1


@pytest.mark.tool_scenario("source.extract_manual", "A")
@pytest.mark.parametrize("records", ["not-a-list", {"title": "x"}, 5])
def test_manual_records_must_be_a_list(env: ToolEnvironment, records: Any) -> None:
    run = run_tool(env.registry, "source.extract_manual", {"source": MANUAL, "records": records})
    assert run.status is ToolStatus.FAILED, run.describe()


@pytest.mark.tool_scenario("source.extract_manual", "D")
def test_many_manual_records_honor_limit(env: ToolEnvironment) -> None:
    records = [{"title": f"R{i}", "url": f"https://example.com/{i}"} for i in range(20_000)]
    run = run_tool(env.registry, "source.extract_manual", {"source": MANUAL, "records": records, "limit": 10})
    assert expect_succeeded(run)["item_count"] == 10
    assert run.elapsed_seconds < 10.0


@pytest.mark.tool_scenario("source.normalize_url", "A")
@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "javascript:alert(1)",
        "http://[::1",
        "data:text/html,<script>",
        "//",
        "/relative/path",
        "?q=1",
        "example.com/no-scheme",
    ],
)
def test_unusable_urls_are_refused(env: ToolEnvironment, url: str) -> None:
    run = run_tool(env.registry, "source.normalize_url", {"url": url})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.normalize_url", "A")
def test_normalization_strips_tracking_and_keeps_unicode_hosts(env: ToolEnvironment) -> None:
    output = expect_succeeded(
        run_tool(env.registry, "source.normalize_url", {"url": "HTTPS://Bücher.Example/a/?utm_source=x&b=1"})
    )
    assert "utm_source" not in json.dumps(output)


@pytest.mark.tool_scenario("source.normalize_url", "A")
def test_relative_urls_resolve_against_base_url(env: ToolEnvironment) -> None:
    output = expect_succeeded(
        run_tool(env.registry, "source.normalize_url", {"url": "/a/b?utm_medium=x", "base_url": "https://example.com/x/"})
    )
    assert output == {"canonical_url": "https://example.com/a/b"}


@pytest.mark.tool_scenario("source.normalize_url", "D")
def test_enormous_url_fails_on_the_result_limit_not_a_crash(env: ToolEnvironment) -> None:
    run = run_tool(env.registry, "source.normalize_url", {"url": "https://example.com/" + "a" * 200_000})
    _controlled(run)
    assert run.elapsed_seconds < 5.0


@pytest.mark.tool_scenario("source.search", "A")
@pytest.mark.parametrize(
    "args",
    [{"reliability": "nonsense"}, {"source_type": "nonsense"}, {"limit": "many"}],
)
def test_search_rejects_unknown_filters(env: ToolEnvironment, args: dict[str, Any]) -> None:
    run = run_tool(env.registry, "source.search", args)
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.search", "D")
def test_search_over_a_large_registry_is_fast_and_limited(tool_env_factory) -> None:
    registry = SourceRegistry(
        [
            SourceDefinition(
                source_id=f"source-{i}",
                name=f"Source {i}",
                source_type="rss",
                url=f"https://example.com/{i}/feed.xml",
                topics=["agents" if i % 2 else "robotics"],
            )
            for i in range(5_000)
        ]
    )
    environment = source_tool_environment(tool_env_factory, source_registry=registry)

    started = time.monotonic()
    output = expect_succeeded(run_tool(environment.registry, "source.search", {"query": "agents", "limit": 7}))
    assert output["source_count"] == 7
    assert time.monotonic() - started < 5.0
