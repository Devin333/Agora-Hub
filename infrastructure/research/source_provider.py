from __future__ import annotations

import re
from collections import OrderedDict
from hashlib import sha256
from threading import RLock
from typing import Any, Mapping
from urllib.parse import urlsplit

from infrastructure.external.sources.url_utils import canonicalize_url
from backend.research.domain.paper import PaperSourceRecord, ResearchPaper
from backend.research.ports.source_provider import BoundPaperSource
from framework.shared.json import stable_json_dumps
from infrastructure.external.sources.arxiv import (
    ARXIV_API_URL,
    ArxivConnector,
    normalize_arxiv_id,
)
from infrastructure.external.sources.models import (
    RawSourceItem,
    SourceDefinition,
    SourceReliability,
    SourceType,
)
from infrastructure.research.errors import ResearchSourceError, summarize_source_failures


_ARXIV_ID_PATTERN = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$",
    re.IGNORECASE,
)
_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
_ARXIV_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)


class ArxivResearchSourceProvider:
    """Project official arXiv connector records into the Research domain."""

    def __init__(
        self,
        connector: ArxivConnector | Any | None = None,
        *,
        api_url: str = ARXIV_API_URL,
        cache_size: int = 128,
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self._connector = connector or ArxivConnector()
        self._api_url = str(api_url).strip() or ARXIV_API_URL
        self._cache_size = int(cache_size)
        self._records: OrderedDict[str, tuple[ResearchPaper, PaperSourceRecord]] = OrderedDict()
        self._lock = RLock()

    def fetch_paper(self, source_url: str) -> ResearchPaper:
        arxiv_id = require_arxiv_id(source_url)
        cached = self._cached(arxiv_id)
        if cached is not None:
            return cached[0]

        source = SourceDefinition(
            source_id=f"research-arxiv-{_stable_suffix(arxiv_id)}",
            name=f"arXiv {arxiv_id}",
            source_type=SourceType.ARXIV,
            url=self._api_url,
            reliability=SourceReliability.HIGH,
            authority_score=1.0,
            metadata={"query": f"id:{arxiv_id}"},
        )
        items, errors = self._connector.fetch(source, query=f"id:{arxiv_id}", limit=3)
        if errors:
            failure = summarize_source_failures(
                errors,
                default_error_type="source_fetch_failed",
            )
            raise ResearchSourceError(
                f"arXiv source fetch failed ({','.join(failure.error_types)})",
                retryable=failure.retryable,
            )
        item = _select_exact_item(items, arxiv_id)
        if item is None:
            raise ResearchSourceError("arXiv response did not contain the requested paper")

        paper, record = _project_item(item, requested_id=arxiv_id)
        self._remember(arxiv_id, paper, record)
        return paper

    def fetch_source_record(self, paper_id: str) -> PaperSourceRecord:
        normalized = require_arxiv_id(paper_id)
        cached = self._cached(normalized)
        if cached is None:
            raise ResearchSourceError(
                "paper source record is not available; fetch_paper must succeed first"
            )
        return cached[1]

    def _cached(self, arxiv_id: str) -> tuple[ResearchPaper, PaperSourceRecord] | None:
        with self._lock:
            record = self._records.get(arxiv_id)
            if record is not None:
                self._records.move_to_end(arxiv_id)
            return record

    def _remember(
        self,
        arxiv_id: str,
        paper: ResearchPaper,
        record: PaperSourceRecord,
    ) -> None:
        with self._lock:
            self._records[arxiv_id] = (paper, record)
            self._records.move_to_end(arxiv_id)
            while len(self._records) > self._cache_size:
                self._records.popitem(last=False)



class CatalogBoundResearchSourceProvider:
    """Resolve an actor-visible parsed document before falling back to a live source."""

    def __init__(self, fallback: ArxivResearchSourceProvider, document_repository: Any) -> None:
        self._fallback = fallback
        self._documents = document_repository

    def fetch_paper(self, source_url: str) -> ResearchPaper:
        return self._fallback.fetch_paper(source_url)

    def fetch_source_record(self, paper_id: str) -> PaperSourceRecord:
        return self._fallback.fetch_source_record(paper_id)

    def fetch_bound_source(
        self,
        *,
        source_ref: str,
        paper_id: str,
        source_revision: str,
        actor_scope: Mapping[str, str],
    ) -> BoundPaperSource | None:
        document = self._documents.get(paper_id, actor_scope=actor_scope)
        if document is None:
            if urlsplit(source_ref).scheme == "file":
                raise ResearchSourceError(
                    "local Research source is not bound to an actor-visible parsed document"
                )
            return None
        document_scope = {
            str(key): str(value)
            for key, value in dict(getattr(document, "actor_scope", {}) or {}).items()
            if value not in (None, "")
        }
        requested_scope = {
            str(key): str(value)
            for key, value in dict(actor_scope or {}).items()
            if value not in (None, "")
        }
        if document_scope and any(
            requested_scope.get(key) != value for key, value in document_scope.items()
        ):
            raise ResearchSourceError(
                "parsed Research document is not visible to the requested actor scope"
            )
        if _normalized_revision(document.source_hash) != _normalized_revision(source_revision):
            raise ResearchSourceError("parsed Research document revision does not match the accepted input")
        accepted_locators = {
            _normalized_locator(value)
            for value in [*document.source_locators, *document.lineage.source_refs]
            if str(value or "").strip()
        }
        normalized_source = _normalized_locator(source_ref)
        parsed = urlsplit(source_ref)
        source_type = "local" if parsed.scheme == "file" else ("arxiv" if parsed.hostname in _ARXIV_HOSTS else "other")
        if normalized_source not in accepted_locators:
            raise ResearchSourceError("Research source locator is not bound to the parsed document")
        public_source_url = (
            f"source://local/{_stable_suffix(normalized_source)}"
            if source_type == "local"
            else source_ref
        )
        source_metadata = {
            "bound_document_id": document.document_id or "",
            "source_revision": source_revision,
        }
        if source_type == "local":
            source_metadata["source_locator"] = source_ref
        paper = ResearchPaper(
            paper_id=document.paper_id,
            title=document.title or document.paper_id,
            authors=list(document.authors),
            abstract=document.abstract or "",
            source=source_type,
            source_url=public_source_url,
            pdf_url=(
                public_source_url
                if source_ref.casefold().endswith(".pdf") or parsed.scheme == "file"
                else None
            ),
            actor_scope=dict(actor_scope),
            metadata=source_metadata,
        )
        record_metadata = {
            "bound_document_id": document.document_id or "",
            # The document's lineage is the provenance this adapter verified
            # (actor scope, revision, locator); downstream gates accept the
            # document's references against exactly these.
            "bound_source_refs": sorted(
                str(value).strip()
                for value in document.lineage.source_refs
                if str(value or "").strip()
            ),
            "content_revision": document.source_hash,
            "metadata_revision": str(
                document.metadata.get("source_revision")
                or document.source_snapshot_id
                or ""
            ),
            "transport_relocated": False,
        }
        if source_type == "local":
            record_metadata["source_locator"] = source_ref
        record = PaperSourceRecord(
            source_id=f"research-bound-{_stable_suffix(document.paper_id + source_revision)}",
            paper_id=document.paper_id,
            source_type=source_type,
            source_url=public_source_url,
            fetched_at=document.observed_at or document.created_at,
            # The verified content hash, spelled as the document records it.
            source_hash=document.source_hash,
            actor_scope=dict(actor_scope),
            metadata=record_metadata,
        )
        return BoundPaperSource(paper=paper, source_record=record, document=document)


def _normalized_revision(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return text.removeprefix("sha256:")


def _normalized_locator(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.casefold() == "file":
        return parsed._replace(scheme="file").geturl()
    return canonicalize_url(text)

def require_arxiv_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ResearchSourceError("arXiv source is required")
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in _ARXIV_HOSTS:
            raise ResearchSourceError("Research source must be an arXiv URL or identifier")
    normalized = normalize_arxiv_id(text)
    if not normalized or not _ARXIV_ID_PATTERN.fullmatch(normalized):
        raise ResearchSourceError("Research source contains an invalid arXiv identifier")
    return normalized


def _select_exact_item(items: list[RawSourceItem], requested_id: str) -> RawSourceItem | None:
    requested = requested_id.casefold()
    requested_base = _arxiv_base_id(requested)
    requested_is_versioned = requested != requested_base
    matches: list[RawSourceItem] = []
    for item in items:
        candidate_id = _consistent_item_arxiv_id(item)
        if _arxiv_id_matches(
            candidate_id,
            requested=requested,
            requested_base=requested_base,
            requested_is_versioned=requested_is_versioned,
        ):
            matches.append(item)
    if len(matches) != 1:
        return None
    return matches[0]


def _project_item(
    item: RawSourceItem,
    *,
    requested_id: str,
) -> tuple[ResearchPaper, PaperSourceRecord]:
    metadata = dict(item.metadata or {})
    item_id = _consistent_item_arxiv_id(item)
    requested = requested_id.casefold()
    requested_base = _arxiv_base_id(requested)
    if not _arxiv_id_matches(
        item_id,
        requested=requested,
        requested_base=requested_base,
        requested_is_versioned=requested != requested_base,
    ):
        raise ResearchSourceError("arXiv response identity does not match the request")
    source_url = f"https://arxiv.org/abs/{item_id}"
    pdf_url = f"https://arxiv.org/pdf/{item_id}.pdf"
    raw_projection = {
        "arxiv_id": item_id,
        "title": item.title,
        "summary": item.summary or "",
        "authors": list(item.authors),
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "url": source_url,
        "pdf_url": pdf_url,
        "raw_content": item.raw_content or "",
    }
    source_hash = sha256(stable_json_dumps(raw_projection).encode("utf-8")).hexdigest()
    domain_metadata = {
        "arxiv_id": item_id,
        "requested_arxiv_id": requested_id,
        "primary_category": metadata.get("primary_category"),
        "doi": metadata.get("doi"),
        "journal_ref": metadata.get("journal_ref"),
        "comment": metadata.get("comment"),
        "fetch_source_id": item.source_id,
        "source_item_id": item.source_item_id,
        "source_hash": source_hash,
    }
    domain_metadata = {
        key: value for key, value in domain_metadata.items() if value not in (None, "", [], {})
    }
    paper = ResearchPaper(
        paper_id=requested_id,
        title=item.title,
        authors=list(item.authors),
        abstract=item.summary or "",
        published_at=item.published_at,
        source="arxiv",
        source_url=source_url,
        pdf_url=pdf_url,
        topics=list(item.tags),
        metadata=domain_metadata,
    )
    record = PaperSourceRecord(
        source_id=f"arxiv:{item_id}",
        paper_id=requested_id,
        source_type="arxiv",
        source_url=source_url,
        fetched_at=item.fetched_at,
        source_hash=source_hash,
        metadata={
            **domain_metadata,
            "title": item.title,
            "abstract": item.summary or "",
            "authors": list(item.authors),
            "published_at": item.published_at.isoformat() if item.published_at else None,
            "pdf_url": pdf_url,
            "source_ref": source_url,
        },
    )
    return paper, record


def _stable_suffix(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


def _consistent_item_arxiv_id(item: RawSourceItem) -> str:
    metadata = dict(item.metadata or {})
    raw_identities = {
        "item_url": item.url,
        "metadata_arxiv_id": metadata.get("arxiv_id"),
        "metadata_pdf_url": metadata.get("pdf_url"),
    }
    identities: dict[str, str] = {}
    for field_name, raw_value in raw_identities.items():
        if raw_value in (None, ""):
            continue
        try:
            identities[field_name] = require_arxiv_id(str(raw_value))
        except ResearchSourceError as exc:
            raise ResearchSourceError(
                f"arXiv response contains an invalid {field_name} identity"
            ) from exc
    if not identities:
        raise ResearchSourceError("arXiv response contains no paper identity")
    normalized = {value.casefold() for value in identities.values()}
    if len(normalized) != 1:
        raise ResearchSourceError("arXiv response contains conflicting paper identities")
    return next(iter(identities.values()))


def _arxiv_base_id(value: str) -> str:
    return _ARXIV_VERSION_SUFFIX.sub("", str(value).casefold())


def _arxiv_id_matches(
    candidate: str,
    *,
    requested: str,
    requested_base: str,
    requested_is_versioned: bool,
) -> bool:
    normalized = str(candidate).casefold()
    if requested_is_versioned:
        return normalized == requested
    return _arxiv_base_id(normalized) == requested_base


__all__ = ["ArxivResearchSourceProvider", "CatalogBoundResearchSourceProvider", "require_arxiv_id"]
