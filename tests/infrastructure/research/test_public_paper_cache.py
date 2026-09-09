from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit
from datetime import UTC, datetime

import pytest

from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent
from infrastructure.research.public_paper_cache import (
    PublicPaperCacheGuidedSearch,
    PublicPaperCacheRepository,
    PublicPaperCacheUnavailableError,
)


def _write_cache(path) -> None:
    path.write_text(json.dumps({
        "source": "arxiv", "collectedAt": "2026-05-24T13:41:06+00:00",
        "papers": [{
            "id": "arxiv-1", "slug": "agent-evaluation-survey", "title": "Agent Evaluation Survey",
            "abstractSnippet": "A benchmark survey with code.", "authors": ["Alice"],
            "publishedAt": "2026-05-21T00:00:00Z", "venue": "arXiv", "tags": ["cs.AI", "evaluation"],
            "paperUrl": "https://arxiv.org/abs/1", "arxivUrl": "https://arxiv.org/abs/1",
            "repoUrl": "https://github.com/example/agent-eval", "isPublished": True,
        }, {
            "id": "arxiv-2", "slug": "unrelated", "title": "Vision Model",
            "abstractSnippet": "Image classification.", "authors": ["Bob"],
            "publishedAt": "2024-01-01T00:00:00Z", "venue": "arXiv", "tags": ["cs.CV"],
            "paperUrl": "https://arxiv.org/abs/2", "isPublished": True,
        }],
    }), encoding="utf-8")


def test_cache_search_uses_real_lexical_fields_and_declares_cache_date(tmp_path) -> None:
    cache = tmp_path / "papers.json"
    _write_cache(cache)
    search = PublicPaperCacheGuidedSearch(
        PublicPaperCacheRepository(path=cache),
        reader_ready=lambda paper_id, user_id: paper_id == "arxiv-1",
        now=lambda: datetime(2026, 9, 9, tzinfo=UTC),
    )
    result = search.search(
        intent=GuidedIntent(
            summary="Agent 评测综述", query="agent evaluation", sources=["papers"],
            constraints=GuidedConstraints(recentYear=True, hasCode=True, paperType="survey"), clarification=None,
        ),
        materials=[], actor_user_id=None,
    )
    assert result.total == 1
    assert result.results[0].url == "https://arxiv.org/abs/1"
    assert result.results[0].source == "arXiv · 缓存 2026-05-24"
    assert result.results[0].href == "/design-demo/papers/agent-evaluation-survey/read"
    params = parse_qs(urlsplit(result.moreHref).query)
    assert params["q"] == ["agent evaluation"]
    assert params["from"] == ["2025-09-09"]
    assert params["has"] == ["code"]
    assert params["paperType"] == ["survey"]


def test_recent_year_is_a_rolling_range_and_only_explicitly_published_records_are_public(tmp_path):
    cache = tmp_path / "papers.json"
    _write_cache(cache)
    payload = json.loads(cache.read_text())
    original = payload["papers"][0]
    payload["papers"] = [
        {**original, "id": "within", "publishedAt": "2025-09-09T00:00:00Z"},
        {**original, "id": "older", "publishedAt": "2025-09-08T23:59:59Z"},
        {**original, "id": "private", "isPublished": False},
        {key: value for key, value in original.items() if key != "isPublished"},
    ]
    cache.write_text(json.dumps(payload))
    result = PublicPaperCacheGuidedSearch(PublicPaperCacheRepository(path=cache), now=lambda: datetime(2026, 9, 9, tzinfo=UTC)).search(
        intent=GuidedIntent(summary="Agent", query="agent", sources=["papers"], constraints=GuidedConstraints(recentYear=True), clarification=None), materials=[], actor_user_id=None,
    )
    assert [item.id for item in result.results] == ["within"]


def test_cache_result_omits_reader_href_until_native_reader_is_ready(tmp_path) -> None:
    cache = tmp_path / "papers.json"
    _write_cache(cache)
    result = PublicPaperCacheGuidedSearch(
        PublicPaperCacheRepository(path=cache), reader_ready=lambda _paper, _user: False,
    ).search(
        intent=GuidedIntent(summary="Agent", query="agent", sources=["papers"], constraints=GuidedConstraints(), clarification=None),
        materials=[], actor_user_id="user-1",
    )
    assert result.results[0].href is None


def test_missing_or_invalid_cache_is_source_unavailable(tmp_path) -> None:
    with pytest.raises(PublicPaperCacheUnavailableError):
        PublicPaperCacheRepository(path=tmp_path / "missing.json").load()
