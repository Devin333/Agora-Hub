"""Read-only adapter for the verified public arXiv cache used by the paper UI."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from backend.research.ports.guided_research import (
    GuidedIntent,
    GuidedMaterial,
    GuidedResult,
    GuidedSearchResponse,
    GuidedSourceUnavailableError,
)


PAPERS_DATA_PATH_ENV = "NEWSROOM_PAPERS_DATA_PATH"
MAX_CACHE_BYTES = 8_000_000
MAX_CACHE_PAPERS = 5_000


class PublicPaperCacheUnavailableError(GuidedSourceUnavailableError):
    pass


@dataclass(frozen=True)
class PublicPaperCacheSnapshot:
    path: Path
    collected_at: str
    papers: tuple[Mapping[str, Any], ...]


class PublicPaperCacheRepository:
    def __init__(self, *, project_root: str | Path | None = None, path: str | Path | None = None) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parents[2]).resolve()
        self.path = Path(path).resolve() if path is not None else None

    def load(self) -> PublicPaperCacheSnapshot:
        for path in self._candidate_paths():
            payload = _read_cache(path)
            if payload is None:
                continue
            papers = payload.get("papers")
            collected_at = str(payload.get("collectedAt") or "").strip()
            if not isinstance(papers, list) or not _valid_timestamp(collected_at):
                continue
            verified = tuple(item for item in papers[:MAX_CACHE_PAPERS] if _is_public_paper(item))
            if verified:
                return PublicPaperCacheSnapshot(path=path, collected_at=collected_at, papers=verified)
        raise PublicPaperCacheUnavailableError(
            "No verified public paper cache is available; configure NEWSROOM_PAPERS_DATA_PATH or refresh the shared cache"
        )

    def _candidate_paths(self) -> tuple[Path, ...]:
        if self.path is not None:
            return (self.path,)
        configured = os.environ.get(PAPERS_DATA_PATH_ENV, "").strip()
        if configured:
            path = Path(configured)
            return ((self.project_root / path).resolve() if not path.is_absolute() else path.resolve(),)
        return (
            self.project_root / ".newsroom" / "papers" / "arxiv-papers.json",
            self.project_root / "frontend" / "data" / "papers" / "arxiv-papers.json",
        )


class PublicPaperCacheGuidedSearch:
    def __init__(
        self,
        repository: PublicPaperCacheRepository,
        *,
        reader_ready: Callable[[str, str | None], bool] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._reader_ready = reader_ready
        self._now = now or (lambda: datetime.now(UTC))

    def search(
        self,
        *,
        intent: GuidedIntent,
        materials: Sequence[GuidedMaterial],
        actor_user_id: str | None,
    ) -> GuidedSearchResponse:
        del materials
        snapshot = self._repository.load()
        terms = _query_terms(intent.query)
        candidates = [
            paper
            for paper in snapshot.papers
            if _matches(paper, terms=terms, intent=intent, now=self._now())
        ]
        candidates.sort(key=lambda paper: _sort_key(paper, terms), reverse=True)
        collected_date = snapshot.collected_at[:10]
        parameters = {"question": intent.summary, "q": intent.query, "sort": "relevance"}
        if intent.constraints.recentYear:
            parameters["from"] = (self._now() - timedelta(days=365)).date().isoformat()
        if intent.constraints.hasCode:
            parameters["has"] = "code"
        if intent.constraints.paperType:
            parameters["paperType"] = intent.constraints.paperType
        results = [
            _result(
                paper,
                collected_date=collected_date,
                reader_ready=self._reader_ready,
                actor_user_id=actor_user_id,
            )
            for paper in candidates[:10]
        ]
        return GuidedSearchResponse(
            source="papers",
            results=results,
            total=len(candidates),
            moreHref=f"/design-demo/papers?{urlencode(parameters)}",
        )


def _read_cache(path: Path) -> Mapping[str, Any] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_CACHE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _is_public_paper(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("isPublished") is not True:
        return False
    return bool(
        _text(value.get("id"))
        and _text(value.get("slug"))
        and _text(value.get("title"))
        and _text(value.get("abstractSnippet") or value.get("summary"))
        and isinstance(value.get("authors"), list)
        and _valid_timestamp(_text(value.get("publishedAt")))
        and _public_url(value.get("paperUrl"))
    )


def _matches(paper: Mapping[str, Any], *, terms: tuple[str, ...], intent: GuidedIntent, now: datetime) -> bool:
    if terms and not all(term in _searchable_text(paper) for term in terms):
        return False
    constraints = intent.constraints
    title = _text(paper.get("title")).casefold()
    if constraints.paperType == "survey" and not any(word in title for word in ("survey", "review", "综述")):
        return False
    if constraints.recentYear is True:
        published = _timestamp(_text(paper.get("publishedAt")))
        if published is None or published.date() < (now - timedelta(days=365)).date():
            return False
    if constraints.hasCode is True and _repo_url(paper) is None:
        return False
    return True


def _result(
    paper: Mapping[str, Any],
    *,
    collected_date: str,
    reader_ready: Callable[[str, str | None], bool] | None,
    actor_user_id: str | None,
) -> GuidedResult:
    paper_id = _text(paper.get("id"))
    slug = _text(paper.get("slug"))
    url = _public_url(paper.get("arxivUrl")) or _public_url(paper.get("paperUrl"))
    href = None
    if reader_ready is not None and re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        try:
            if reader_ready(paper_id, actor_user_id):
                href = f"/design-demo/papers/{slug}/read"
        except Exception:
            href = None
    authors = [str(item).strip() for item in paper.get("authors", []) if str(item).strip()]
    return GuidedResult(
        id=paper_id,
        kind="papers",
        title=_text(paper.get("title")),
        description=_strip_html(_text(paper.get("abstractSnippet") or paper.get("summary")))[:2000],
        source=f"{_text(paper.get('venue')) or 'arXiv'} · 缓存 {collected_date}",
        url=str(url),
        href=href,
        authors=", ".join(authors) or None,
        publishedAt=_text(paper.get("publishedAt")) or None,
    )


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.casefold() for part in re.split(r"\s+", query.strip()) if part))


def _searchable_text(paper: Mapping[str, Any]) -> str:
    refs: list[str] = []
    for key in ("taskRefs", "methodRefs"):
        for value in paper.get(key, []) if isinstance(paper.get(key), list) else []:
            if isinstance(value, Mapping):
                refs.extend(_text(value.get(field)) for field in ("slug", "name", "nameZh", "group", "area"))
    values = [
        _text(paper.get("title")),
        _text(paper.get("titleZh")),
        _text(paper.get("abstractSnippet") or paper.get("summary")),
        _text(paper.get("abstractSnippetZh")),
        " ".join(_text(item) for item in paper.get("authors", []) if _text(item)),
        " ".join(_text(item) for item in paper.get("tags", []) if _text(item)),
        *refs,
    ]
    return " ".join(values).casefold()


def _sort_key(paper: Mapping[str, Any], terms: tuple[str, ...]) -> tuple[int, float, str]:
    title = _text(paper.get("title")).casefold()
    tags = " ".join(_text(item) for item in paper.get("tags", [])).casefold()
    abstract = _text(paper.get("abstractSnippet") or paper.get("summary")).casefold()
    score = sum(16 for term in terms if term in title)
    score += sum(10 for term in terms if term in tags)
    score += sum(4 for term in terms if term in abstract)
    published = _timestamp(_text(paper.get("publishedAt")))
    return score, published.timestamp() if published else 0.0, _text(paper.get("id"))


def _repo_url(paper: Mapping[str, Any]) -> str | None:
    direct = _public_url(paper.get("repoUrl"))
    if direct and urlsplit(direct).hostname == "github.com":
        return direct
    abstract = _text(paper.get("abstractSnippet") or paper.get("summary"))
    match = re.search(r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", abstract)
    return _public_url(match.group(0)) if match else None


def _public_url(value: Any) -> str | None:
    text = _text(value)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return text if not any(char in text for char in "\\\r\n") else None


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _valid_timestamp(value: str) -> bool:
    return _timestamp(value) is not None


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value)).strip()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = [
    "PublicPaperCacheGuidedSearch",
    "PublicPaperCacheRepository",
    "PublicPaperCacheSnapshot",
    "PublicPaperCacheUnavailableError",
]
