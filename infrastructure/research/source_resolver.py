from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import os
import socket
import base64
from datetime import UTC, datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from framework.shared.windows_paths import resolve_storage_path, storage_io_path

from backend.research.ports.source_resolver import ResolvedPaperSource
from backend.research.domain.catalog import ResearchSourceSnapshot
from backend.research.domain.common import SourceLineage, stable_research_id
from backend.research.domain.paper import PaperSourceRecord, ResearchPaper
from infrastructure.external.sources.arxiv import ArxivSourceConnector
from infrastructure.external.sources.fetch_policy import (
    DomainRateLimiter,
    RobotsDisallowedError,
    SourceFetchPolicy,
    SourceRateLimitExceededError,
    UnsupportedContentTypeError,
    ensure_robots_allowed,
    ensure_supported_content_type,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from infrastructure.external.sources.html import extract_html
from infrastructure.research.errors import ResearchSourceError


_SUPPORTED_REMOTE_CONTENT_TYPES = (
    "application/pdf",
    "text/html",
    "application/xhtml+xml",
    "application/json",
    "application/octet-stream",
    "text/plain",
    "text/x-tex",
    "application/x-tex",
    "application/gzip",
    "application/x-gzip",
    "application/x-tar",
)


class ResearchSourceResolverAdapter:
    """Resolve supported paper inputs under existing source fetch policies."""

    def __init__(
        self,
        *,
        arxiv_provider: Any | None = None,
        arxiv_fetcher: ArxivSourceConnector | Any | None = None,
        github_repository: Any | None = None,
        fetch_policy: SourceFetchPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        local_root: str | Path | None = None,
        artifact_reader: Any | None = None,
        fetch_bytes: Callable[[str, SourceFetchPolicy], tuple[bytes, str | None, str]] | None = None,
    ) -> None:
        self._arxiv_provider = arxiv_provider
        self._arxiv_fetcher = arxiv_fetcher or ArxivSourceConnector()
        self._github = github_repository
        self._fetch_policy = fetch_policy or SourceFetchPolicy(
            max_bytes=25 * 1024 * 1024,
            user_agent="AgoraHub/1.0 (research-paper-resolver)",
        )
        self._rate_limiter = rate_limiter or DomainRateLimiter()
        self._local_root = resolve_storage_path(local_root) if local_root else None
        self._artifact_reader = artifact_reader
        self._fetch_bytes_impl = fetch_bytes

    def resolve(self, request: Any) -> ResolvedPaperSource:
        source = request.source.strip()
        source_type = request.source_type or _infer_source_type(source)
        if request.content_ref and source_type == "local":
            source = request.content_ref.strip()
        elif request.content_ref and source_type != "github":
            content_resolved = self._resolve_content_ref(source, source_type, request)
            if content_resolved is not None:
                return content_resolved
        if source_type == "arxiv":
            return self._resolve_arxiv(source, request)
        if source_type == "local":
            return self._resolve_local(source, request)
        if source_type == "github":
            return self._resolve_github(source, request)
        return self._resolve_remote(source, source_type, request)

    def _resolve_content_ref(
        self,
        source: str,
        source_type: str,
        request: Any,
    ) -> ResolvedPaperSource | None:
        """Use an authorized local upload as full text while retaining source metadata."""

        content_ref = str(request.content_ref or "").strip()
        if not content_ref:
            return None
        if content_ref.startswith("artifact://"):
            return self._resolve_artifact_ref(source, source_type, request, content_ref)
        if content_ref.startswith("content://") or content_ref.startswith(("https://", "http://")):
            return self._metadata_only(source, source_type, request, "content_ref_scheme_not_supported")
        try:
            path = _local_path(content_ref)
            if self._local_root is not None and not _is_descendant(path, self._local_root):
                return self._metadata_only(source, source_type, request, "content_ref_outside_allowed_root")
            if not path.is_file():
                return None
            content = path.read_bytes()
            if len(content) > self._fetch_policy.max_bytes:
                return self._metadata_only(source, source_type, request, "content_ref_size_exceeded")
            source_url = source if source.startswith(("http://", "https://")) else ""
            canonical = canonicalize_url(source_url) if source_url else ""
            external_id = _metadata_external_id(source, source_type, metadata=request.metadata)
            metadata = {
                **dict(request.metadata),
                "content_ref": path.as_uri(),
                "content_type": mimetypes.guess_type(path.name)[0] or _guess_content_type(content),
            }
            paper_id = _explicit_paper_id(metadata) or stable_research_id(
                "paper", source_type, external_id or canonical or source
            )
            paper = _paper_from_metadata(
                paper_id=paper_id,
                source_type=source_type,
                canonical_url=canonical,
                external_id=external_id,
                metadata=metadata,
            )
            source_hash = hashlib.sha256(content).hexdigest()
            source_ref = canonical or path.as_uri()
            snapshot = _snapshot(
                paper=paper,
                source_type=source_type,
                canonical_url=canonical,
                external_id=external_id,
                content_type=metadata["content_type"],
                source_hash=source_hash,
                access_status="available",
                metadata={"content_ref": path.as_uri(), "source_descriptor": source},
            )
            record = PaperSourceRecord(
                source_id=snapshot.snapshot_id,
                paper_id=paper_id,
                source_type=source_type,
                source_url=canonical or _local_source_url(path),
                fetched_at=snapshot.fetched_at,
                source_hash=source_hash,
                metadata=metadata,
            )
            return ResolvedPaperSource(
                paper=paper,
                snapshot=snapshot,
                content=content,
                content_type=snapshot.content_type,
                source_record=record,
                access_status="available",
                diagnostics=({"code": "content_ref_used", "content_ref": path.as_uri()},),
            )
        except OSError:
            return None

    def _resolve_artifact_ref(
        self,
        source: str,
        source_type: str,
        request: Any,
        content_ref: str,
    ) -> ResolvedPaperSource | None:
        reader = self._artifact_reader
        read = getattr(reader, "read", None)
        if not callable(read):
            return self._metadata_only(source, source_type, request, "artifact_reader_not_configured")
        try:
            envelope = read(
                content_ref,
                actor_scope=dict(getattr(request, "actor_scope", {}) or {}),
                include_payload=True,
            )
            payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
            if isinstance(payload, Mapping):
                raw = payload.get("content") or payload.get("text") or payload.get("bytes_base64")
                content = base64.b64decode(raw) if payload.get("bytes_base64") else str(raw or "").encode("utf-8")
            elif isinstance(payload, str):
                content = payload.encode("utf-8")
            else:
                return self._metadata_only(source, source_type, request, "artifact_payload_not_content")
            if len(content) > self._fetch_policy.max_bytes:
                return self._metadata_only(source, source_type, request, "artifact_content_size_exceeded")
            metadata = {**dict(request.metadata), "content_ref": content_ref, "artifact_ref": content_ref}
            paper_id = _explicit_paper_id(metadata) or stable_research_id("paper", source_type, source)
            paper = _paper_from_metadata(
                paper_id=paper_id,
                source_type=source_type,
                canonical_url=canonicalize_url(source) if source.startswith(("http://", "https://")) else "",
                external_id=_metadata_external_id(source, source_type, metadata=metadata),
                metadata=metadata,
            )
            digest = hashlib.sha256(content).hexdigest()
            snapshot = _snapshot(
                paper=paper,
                source_type=source_type,
                canonical_url=content_ref,
                external_id=content_ref,
                content_type=str((envelope.get("metadata") or {}).get("content_type") or _guess_content_type(content)),
                source_hash=digest,
                access_status="available",
                metadata={"content_ref": content_ref, "artifact_ref": content_ref},
            )
            return ResolvedPaperSource(
                paper=paper,
                snapshot=snapshot,
                content=content,
                content_type=snapshot.content_type,
                access_status="available",
                diagnostics=({"code": "artifact_ref_used", "content_ref": content_ref},),
            )
        except Exception as exc:
            return self._metadata_only(source, source_type, request, "artifact_ref_unavailable", exc)

    def _resolve_arxiv(self, source: str, request: Any) -> ResolvedPaperSource:
        if self._arxiv_provider is None:
            return self._metadata_only(source, "arxiv", request, "arxiv_provider_not_configured")
        try:
            paper = self._arxiv_provider.fetch_paper(source)
            source_record = self._arxiv_provider.fetch_source_record(paper.paper_id)
            preferred = str(request.options.get("format") or "latex").casefold()
            fetch_method = "fetch_pdf_package" if preferred == "pdf" else "fetch_source_package"
            package = getattr(self._arxiv_fetcher, fetch_method)(paper.paper_id)
            content = bytes(package.content)
            source_hash = package.checksum
            source_ref = str(package.url or source_record.source_url)
            snapshot = _snapshot(
                paper=paper,
                source_type="arxiv",
                canonical_url=source_ref,
                external_id=str(paper.metadata.get("arxiv_id") or paper.paper_id),
                content_type=package.content_type or _guess_content_type(content),
                source_hash=source_hash,
                access_status="available",
                metadata={"requested_format": preferred, "file_name": package.file_name},
            )
            record = source_record.model_copy(update={"source_hash": source_hash, "source_url": source_ref})
            return ResolvedPaperSource(
                paper=paper,
                snapshot=snapshot,
                content=content,
                content_type=snapshot.content_type,
                source_record=record,
                access_status="available",
            )
        except Exception as exc:
            return self._metadata_only(source, "arxiv", request, "arxiv_source_fetch_failed", exc)

    def _resolve_local(self, source: str, request: Any) -> ResolvedPaperSource:
        path = _local_path(source)
        if self._local_root is not None and not _is_descendant(path, self._local_root):
            return self._metadata_only(source, "local", request, "local_path_outside_allowed_root")
        try:
            io_path = storage_io_path(path)
            if not io_path.is_file():
                raise FileNotFoundError(str(path))
            content = io_path.read_bytes()
            if len(content) > self._fetch_policy.max_bytes:
                raise ValueError("local source exceeds configured maximum size")
            source_type = "local"
            canonical = ""
            source_locator = path.as_uri()
            source_hash = hashlib.sha256(content).hexdigest()
            metadata = dict(request.metadata)
            metadata.setdefault("title", path.stem)
            paper = _paper_from_metadata(
                paper_id=stable_research_id("paper", source_type, source_locator),
                source_type=source_type,
                canonical_url=canonical,
                external_id=path.name,
                metadata=metadata,
            )
            snapshot = _snapshot(
                paper=paper,
                source_type=source_type,
                canonical_url=canonical,
                external_id=path.name,
                content_type=mimetypes.guess_type(path.name)[0] or _guess_content_type(content),
                source_hash=source_hash,
                access_status="available",
                metadata={"path_name": path.name, "source_locator": source_locator},
            )
            record = PaperSourceRecord(
                source_id=snapshot.snapshot_id,
                paper_id=paper.paper_id,
                source_type="local",
                # The legacy Source URL value object requires a host. Keep
                # the real file locator in metadata, while using a stable,
                # non-sensitive internal URI for the source record itself.
                source_url=_local_source_url(path),
                fetched_at=snapshot.fetched_at,
                source_hash=source_hash,
                metadata={
                    "title": paper.title,
                    "content_type": snapshot.content_type,
                    "source_locator": source_locator,
                },
            )
            return ResolvedPaperSource(paper=paper, snapshot=snapshot, content=content, content_type=snapshot.content_type, source_record=record)
        except Exception as exc:
            return self._metadata_only(source, "local", request, "local_source_unavailable", exc)

    def _resolve_github(self, source: str, request: Any) -> ResolvedPaperSource:
        if not _has_paper_context(request):
            raise ResearchSourceError(
                "GitHub repository observation requires explicit paper context"
            )
        metadata = dict(request.metadata)
        profile = None
        if self._github is not None:
            try:
                profile = self._github.fetch_profile(source)
                metadata.update({"code_profile": profile.model_dump(mode="json", exclude_none=True), "code_url": profile.repo_url})
            except Exception as exc:
                metadata["github_diagnostic"] = {"error_type": type(exc).__name__}
        metadata.setdefault("title", source.rstrip("/").split("/")[-1])
        resolved = self._metadata_only(source, "github", request, "github_is_metadata_observation", metadata=metadata)
        return resolved

    def _resolve_remote(self, source: str, source_type: str, request: Any) -> ResolvedPaperSource:
        fetch_source = _remote_fetch_url(source, source_type)
        if not fetch_source:
            return self._metadata_only(source, source_type, request, "remote_source_url_required")
        try:
            content, content_type, final_url = self._fetch_bytes(fetch_source)
            canonical = canonicalize_url(final_url or source)
            source_hash = hashlib.sha256(content).hexdigest()
            metadata = dict(request.metadata)
            if (content_type or "").split(";", 1)[0].casefold() in {"text/html", "application/xhtml+xml"} or content.lstrip().startswith((b"<", b"<!doctype")):
                extraction = extract_html(content.decode("utf-8", errors="replace"))
                metadata.update({
                    "title": extraction.title or metadata.get("title") or canonical,
                    "abstract": extraction.summary or metadata.get("abstract") or "",
                    "authors": extraction.authors or metadata.get("authors") or [],
                    "published_at": extraction.published_at.isoformat() if extraction.published_at else metadata.get("published_at"),
                })
            doi = metadata.get("doi") or _doi_from_source(source)
            if doi:
                metadata["doi"] = doi
            if source_type == "openreview":
                openreview_id = metadata.get("openreview_id") or _metadata_external_id(
                    source,
                    source_type,
                    metadata=metadata,
                )
                if openreview_id:
                    metadata["openreview_id"] = openreview_id
            external_id = str(
                doi
                or metadata.get("openreview_id")
                or _metadata_external_id(source, source_type, metadata=metadata)
                or canonical
            )
            paper_id = stable_research_id("paper", source_type, doi or canonical)
            paper = _paper_from_metadata(
                paper_id=paper_id,
                source_type=source_type,
                canonical_url=canonical,
                external_id=external_id,
                metadata=metadata,
            )
            snapshot = _snapshot(
                paper=paper,
                source_type=source_type,
                canonical_url=canonical,
                external_id=external_id,
                content_type=content_type or _guess_content_type(content),
                source_hash=source_hash,
                access_status="available",
                metadata={"final_url": final_url},
            )
            record = PaperSourceRecord(
                source_id=snapshot.snapshot_id,
                paper_id=paper.paper_id,
                source_type=source_type,
                source_url=canonical,
                fetched_at=snapshot.fetched_at,
                source_hash=source_hash,
                metadata=metadata,
            )
            return ResolvedPaperSource(paper=paper, snapshot=snapshot, content=content, content_type=snapshot.content_type, source_record=record)
        except Exception as exc:
            return self._metadata_only(source, source_type, request, "remote_source_denied_or_failed", exc)

    def _fetch_bytes(self, url: str) -> tuple[bytes, str | None, str]:
        _ensure_remote_target_allowed(url)
        if self._fetch_bytes_impl is not None:
            # Test/deployment transports are still subject to the same
            # resolver contract as urllib: never accept an unbounded or
            # unsupported payload merely because it came from an injected
            # implementation.
            result = self._fetch_bytes_impl(url, self._fetch_policy)
            if not isinstance(result, tuple) or len(result) != 3:
                raise TypeError("fetch_bytes must return (body, content_type, final_url)")
            body, content_type, final_url = result
            if isinstance(body, bytearray):
                body = bytes(body)
            if not isinstance(body, bytes):
                raise TypeError("fetch_bytes body must be bytes")
            if len(body) > self._fetch_policy.max_bytes:
                raise ValueError("source response exceeds configured maximum size")
            if not body:
                raise ValueError("source response is empty")
            normalized_content_type = str(content_type).strip() if content_type else None
            ensure_supported_content_type(
                normalized_content_type,
                _SUPPORTED_REMOTE_CONTENT_TYPES,
            )
            normalized_final_url = str(final_url or url).strip()
            if not normalized_final_url:
                raise ValueError("fetch_bytes final_url is required")
            _ensure_remote_target_allowed(normalized_final_url)
            return body, normalized_content_type, normalized_final_url
        policy = self._fetch_policy
        decision = self._rate_limiter.reserve(url, limit_per_minute=policy.rate_limit_per_domain_per_minute)
        if not decision.allowed:
            raise ResearchSourceError("source rate limit exceeded", retryable=True)

        def operation() -> tuple[bytes, str | None, str]:
            ensure_robots_allowed(url, policy)
            request = Request(url, headers={"User-Agent": policy.user_agent, "Accept": "application/pdf,text/html,application/xhtml+xml,application/json,*/*"})
            with open_request_with_fetch_policy(request, policy) as response:
                final_url = str(getattr(response, "url", None) or url)
                _ensure_remote_target_allowed(final_url)
                content_type = str(response.headers.get("Content-Type") or "") if response.headers is not None else None
                ensure_supported_content_type(content_type, _SUPPORTED_REMOTE_CONTENT_TYPES)
                content_length = response.headers.get("Content-Length") if response.headers is not None else None
                if content_length and int(content_length) > policy.max_bytes:
                    raise ValueError("source response exceeds configured maximum size")
                body = response.read(policy.max_bytes + 1)
            if len(body) > policy.max_bytes:
                raise ValueError("source response exceeds configured maximum size")
            if not body:
                raise ValueError("source response is empty")
            return body, content_type, final_url

        return run_with_fetch_retries(operation, policy)

    def _metadata_only(self, source: str, source_type: str, request: Any, reason: str, exc: Exception | None = None, *, metadata: dict[str, Any] | None = None) -> ResolvedPaperSource:
        canonical = canonicalize_url(source) if source.startswith(("http://", "https://")) else ""
        combined_metadata = {**dict(request.metadata), **dict(metadata or {})}
        external_id = _metadata_external_id(source, source_type, metadata=combined_metadata)
        explicit_paper_id = _explicit_paper_id(combined_metadata)
        paper = _paper_from_metadata(
            paper_id=(explicit_paper_id or stable_research_id("paper", source_type, external_id or canonical or source)),
            source_type=source_type,
            canonical_url=canonical,
            external_id=external_id,
            metadata=combined_metadata,
        )
        access_status = _access_status_for_failure(reason, exc)
        diagnostic = {"code": reason, "source_type": source_type, "access_status": access_status}
        if exc is not None:
            diagnostic.update({"error_type": type(exc).__name__, "retryable": bool(getattr(exc, "retryable", False))})
            retry_after = getattr(exc, "retry_after_seconds", None)
            if retry_after is not None:
                diagnostic["retry_after_seconds"] = int(retry_after)
        retryable = bool(diagnostic.get("retryable", access_status == "rate_limited"))
        user_action_required = access_status in {"denied", "unsupported", "not_found"}
        retry_after_seconds = diagnostic.get("retry_after_seconds")
        snapshot = _snapshot(
            paper=paper,
            source_type=source_type,
            canonical_url=canonical,
            external_id=external_id,
            content_type=str((metadata or {}).get("content_type") or "text/html"),
            source_hash=None,
            access_status=access_status,
            metadata={
                "reason": reason,
                **diagnostic,
                "retryable": retryable,
                "user_action_required": user_action_required,
                **({"retry_after_seconds": retry_after_seconds} if retry_after_seconds is not None else {}),
                "diagnostics": [diagnostic],
                "source_policy": {
                    "timeout_seconds": self._fetch_policy.timeout_seconds,
                    "max_bytes": self._fetch_policy.max_bytes,
                    "max_redirects": self._fetch_policy.max_redirects,
                    "respect_robots": self._fetch_policy.respect_robots,
                    "rate_limit_per_domain_per_minute": self._fetch_policy.rate_limit_per_domain_per_minute,
                    "retry_times": self._fetch_policy.retry_times,
                    "https_only": True,
                },
            },
        )
        return ResolvedPaperSource(paper=paper, snapshot=snapshot, content=None, content_type=snapshot.content_type, access_status=access_status, diagnostics=(diagnostic,))


def _snapshot(*, paper: ResearchPaper, source_type: str, canonical_url: str, external_id: str | None, content_type: str | None, source_hash: str | None, access_status: str, metadata: dict[str, Any]) -> ResearchSourceSnapshot:
    source_ref = canonical_url or f"source://{source_type}/{external_id or paper.paper_id}"
    metadata = dict(metadata)
    reason_code = str(metadata.get("reason_code") or metadata.get("reason") or "").strip() or None
    raw_diagnostics = metadata.get("diagnostics")
    diagnostics = [dict(item) for item in raw_diagnostics if isinstance(item, Mapping)] if isinstance(raw_diagnostics, list) else []
    retry_after = metadata.get("retry_after_seconds")
    try:
        retry_after = max(0, int(retry_after)) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after = None
    policy = metadata.get("source_policy")
    return ResearchSourceSnapshot(
        snapshot_id=stable_research_id("source_snapshot", paper.paper_id, source_ref, source_hash or ""),
        paper_id=paper.paper_id,
        source_type=source_type,
        canonical_url=canonical_url or None,
        external_id=external_id,
        content_type=content_type,
        source_hash=source_hash,
        checksum=source_hash,
        fetched_at=datetime.now(UTC),
        access_status=access_status,
        reason_code=reason_code,
        retryable=metadata.get("retryable") if isinstance(metadata.get("retryable"), bool) else None,
        user_action_required=metadata.get("user_action_required") if isinstance(metadata.get("user_action_required"), bool) else None,
        retry_after_seconds=retry_after,
        source_policy={str(key): value for key, value in policy.items()} if isinstance(policy, Mapping) else {},
        diagnostics=diagnostics,
        version_id=str(metadata.get("version_id") or metadata.get("version") or "").strip() or None,
        resolver_version=str(metadata.get("resolver_version") or "research-source-resolver-v1").strip() or None,
        lineage=SourceLineage(source_refs=[source_ref], source_hash=source_hash),
        metadata=metadata,
    )


def _paper_from_metadata(*, paper_id: str, source_type: str, canonical_url: str, external_id: str | None, metadata: Mapping[str, Any]) -> ResearchPaper:
    authors = metadata.get("authors") or []
    if isinstance(authors, str):
        authors = [item.strip() for item in authors.split(",") if item.strip()]
    return ResearchPaper(
        paper_id=paper_id,
        title=str(metadata.get("title") or external_id or canonical_url or "Untitled paper"),
        authors=[str(item) for item in authors],
        abstract=str(metadata.get("abstract") or ""),
        published_at=_datetime(metadata.get("published_at")),
        source=source_type,
        source_url=canonical_url or None,
        pdf_url=str(metadata["pdf_url"]) if metadata.get("pdf_url") else None,
        code_url=str(metadata["code_url"]) if metadata.get("code_url") else None,
        topics=[str(item) for item in metadata.get("topics", [])] if isinstance(metadata.get("topics"), (list, tuple)) else [],
        metadata={**dict(metadata), "external_id": external_id, "source_type": source_type},
    )


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _guess_content_type(content: bytes) -> str:
    if content.startswith(b"%PDF"):
        return "application/pdf"
    if content.lstrip().startswith((b"<", b"<!doctype")):
        return "text/html"
    return "application/octet-stream"


def _doi_from_source(source: str) -> str | None:
    value = str(source or "").strip()
    if value.casefold().startswith("doi:"):
        return value[4:].strip() or None
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.casefold() in {"doi.org", "dx.doi.org"}:
        return parsed.path.strip("/") or None
    if value.startswith("10.") and "/" in value:
        return value
    return None


def _metadata_external_id(
    source: str,
    source_type: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    values = metadata or {}
    explicit = values.get("doi") or values.get("openreview_id")
    if explicit:
        return str(explicit).strip()
    if source_type in {"doi", "crossref"}:
        return _doi_from_source(source) or str(source).strip()
    if source_type == "openreview":
        parsed = urlsplit(source)
        query_id = parse_qsl(parsed.query, keep_blank_values=True)
        for key, value in query_id:
            if key.casefold() in {"id", "note"} and value.strip():
                return value.strip()
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else str(source).strip()
    if source_type == "github":
        parts = [part.removesuffix(".git") for part in urlsplit(source).path.split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[:2])
    if source_type == "local":
        try:
            return _local_source_url(_local_path(source))
        except (OSError, ValueError):
            pass
    return str(source).rstrip("/").split("/")[-1] or str(source).strip()


def _has_paper_context(request: Any) -> bool:
    """Require an explicit paper reference before resolving a GitHub repo."""

    values: dict[str, Any] = {}
    metadata = getattr(request, "metadata", None)
    options = getattr(request, "options", None)
    if isinstance(metadata, Mapping):
        values.update(metadata)
    if isinstance(options, Mapping):
        values.update(options)
    for key in (
        "paper_id",
        "paperId",
        "paper_identity_id",
        "paperIdentityId",
        "paper_url",
        "paperUrl",
        "paper_source",
        "paperSource",
    ):
        value = values.get(key)
        if value is not None and str(value).strip():
            return True
    content_ref = str(getattr(request, "content_ref", "") or "").strip()
    source = str(getattr(request, "source", "") or "").strip()
    return bool(content_ref and content_ref != source)


def _explicit_paper_id(values: Mapping[str, Any]) -> str | None:
    for key in ("paper_id", "paperId", "paper_identity_id", "paperIdentityId"):
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _remote_fetch_url(source: str, source_type: str) -> str | None:
    value = str(source or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    if source_type in {"doi", "crossref"}:
        doi = _doi_from_source(value)
        return f"https://doi.org/{quote(doi, safe='10./():;-._')}" if doi else None
    return None


def _ensure_remote_target_allowed(url: str) -> None:
    """Reject non-HTTPS and non-public targets before any transport is invoked."""

    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ResearchSourceError("remote source must use HTTPS with a hostname")
    addresses: set[str] = {parsed.hostname}
    try:
        address = ipaddress.ip_address(parsed.hostname)
        addresses = {str(address)}
    except ValueError:
        try:
            addresses.update(
                str(item[4][0])
                for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme.casefold() == "https" else 80), type=socket.SOCK_STREAM)
            )
        except OSError:
            # An unresolved hostname will be handled by the fetch policy; do
            # not turn DNS availability into an identity or parsing failure.
            return
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_reserved or address.is_multicast:
            raise ResearchSourceError("remote source target is not publicly routable")


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _local_source_url(path: Path) -> str:
    """Build a valid opaque URI without exposing an absolute local path."""

    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
    return f"source://local/{digest}"


def _local_path(source: str) -> Path:
    """Parse plain paths and RFC 8089 file URIs on the current platform."""

    value = str(source or "").strip()
    if value.casefold().startswith("file://"):
        parsed = urlsplit(value)
        decoded = unquote(parsed.path)
        if os.name == "nt" and decoded.startswith("/") and len(decoded) > 2 and decoded[2] == ":":
            decoded = decoded[1:]
        if parsed.netloc and parsed.netloc.casefold() not in {"", "localhost"}:
            decoded = f"//{parsed.netloc}{decoded}"
        value = decoded
    return resolve_storage_path(value)


def _access_status_for_failure(reason: str, exc: Exception | None) -> str:
    """Classify source failures without exposing raw transport details."""

    if isinstance(exc, HTTPError):
        if exc.code in {401, 403}:
            return "denied"
        if exc.code == 404:
            return "not_found"
        if exc.code == 429:
            return "rate_limited"
    if isinstance(exc, (RobotsDisallowedError,)):
        return "denied"
    if isinstance(exc, (SourceRateLimitExceededError,)):
        return "rate_limited"
    if isinstance(exc, UnsupportedContentTypeError):
        return "unsupported"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, ResearchSourceError):
        message = str(exc).casefold()
        if "rate" in message or "limit" in message:
            return "rate_limited"
    # Explicit resolver outcomes are metadata observations rather than fetch
    # failures, even though no full text is available.
    if reason.endswith("not_configured") or reason.endswith("metadata_observation") or reason.endswith("url_required"):
        return "metadata_only"
    return "failed"


def canonicalize_url(value: str) -> str:
    """Normalize an external URL without depending on the interface layer."""

    candidate = str(value or "").strip()
    if not candidate:
        return ""
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.hostname:
        return candidate
    try:
        port = parsed.port
    except ValueError:
        return candidate
    if (parsed.scheme.casefold(), port) in {("http", 80), ("https", 443)}:
        port = None
    host = parsed.hostname.casefold()
    netloc = host if port is None else f"{host}:{port}"
    query_pairs = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid"}
    ]
    query_pairs.sort()
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path.rstrip("/") or "/",
            urlencode(query_pairs),
            "",
        )
    )


def _infer_source_type(source: str) -> str:
    value = str(source or "").strip().casefold()
    if value.startswith("arxiv:") or "arxiv.org" in value:
        return "arxiv"
    if "openreview.net" in value:
        return "openreview"
    parsed = urlsplit(value)
    if (
        value.startswith("doi:")
        or (value.startswith("10.") and "/" in value)
        or parsed.hostname in {"doi.org", "dx.doi.org"}
    ):
        return "doi"
    if "github.com" in value:
        return "github"
    if value.startswith("file:") or value.lower().endswith((".pdf", ".tex", ".tar.gz")):
        return "local"
    if value.startswith(("http://", "https://")):
        return "publisher"
    return "manual"


__all__ = ["ResearchSourceResolverAdapter"]
