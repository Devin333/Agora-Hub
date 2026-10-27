from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import socket

import pytest

from backend.research.domain import (
    PaperSourceRecord,
    ResearchDocument,
    ResearchPaper,
    ResearchSection,
)
from backend.research.domain.common import SourceLineage
from infrastructure.external.sources.models import RawSourceItem, SourceType
from infrastructure.external.sources.fetch_policy import (
    RateLimitDecision,
    SourceRateLimitExceededError,
    UnsupportedContentTypeError,
)
from framework.execution_environment import ExecutionEnvironmentUnavailableError
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.research import (
    ArxivResearchSourceProvider,
    CatalogBoundResearchSourceProvider,
    ResearchDocumentCompileError,
    ResearchDocumentCompilerAdapter,
    ResearchSourceError,
    require_arxiv_id,
)
from infrastructure.research.source_resolver import ResearchSourceResolverAdapter
from backend.research.application.parse_paper import ParsePaperRequest


UTC = timezone.utc
# A fixed public address, so the resolver's routability check never depends on
# the DNS of the machine running the tests.
_PUBLIC_TEST_ADDRESS = "93.184.215.14"


@pytest.fixture(autouse=True)
def _offline_public_dns(monkeypatch) -> None:
    def resolve(host, port, *_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_TEST_ADDRESS, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def test_local_source_resolver_keeps_file_locator_without_breaking_legacy_source_url() -> None:
    # Use a tiny source fixture so the resolver exercises the real local path,
    # checksum and PaperSourceRecord construction path.
    root = Path(__file__).resolve().parent
    source = root / "_tmp_local_research.tex"
    source.write_text(r"\documentclass{article}\begin{document}text\end{document}", encoding="utf-8")
    try:
        resolver = ResearchSourceResolverAdapter(local_root=root)
        result = resolver.resolve(
            ParsePaperRequest(source=str(source), source_type="local")
        )
    finally:
        source.unlink(missing_ok=True)

    assert result.access_status == "available"
    assert result.source_record is not None
    assert result.source_record.source_url.startswith("source://local/")
    assert result.source_record.metadata["source_locator"].startswith("file://")


def test_local_source_resolver_accepts_file_uri() -> None:
    root = Path(__file__).resolve().parent
    source = root / "_tmp_local_research_file_uri.tex"
    source.write_text("\\section{Method} local", encoding="utf-8")
    try:
        result = ResearchSourceResolverAdapter(local_root=root).resolve(
            ParsePaperRequest(source=source.as_uri(), source_type="local")
        )
    finally:
        source.unlink(missing_ok=True)

    assert result.access_status == "available"
    assert result.content == b"\\section{Method} local"


def test_remote_injected_fetcher_obeys_size_and_content_type_policy() -> None:
    policy = replace(
        ResearchSourceResolverAdapter()._fetch_policy,  # noqa: SLF001 - exercise adapter policy boundary
        max_bytes=4,
    )

    oversized = ResearchSourceResolverAdapter(
        fetch_policy=policy,
        fetch_bytes=lambda _url, _policy: (b"12345", "application/pdf", "https://example.test/paper.pdf"),
    ).resolve(
        ParsePaperRequest(source="https://example.test/paper.pdf", source_type="publisher")
    )
    assert oversized.access_status == "failed"
    assert oversized.diagnostics[0]["code"] == "remote_source_denied_or_failed"
    assert oversized.diagnostics[0]["error_type"] == "ValueError"
    assert oversized.snapshot.diagnostics[0]["code"] == "remote_source_denied_or_failed"
    assert oversized.snapshot.source_policy["https_only"] is True

    unsupported = ResearchSourceResolverAdapter(
        fetch_bytes=lambda _url, _policy: (b"body", "application/zip", "https://example.test/paper.zip"),
    ).resolve(
        ParsePaperRequest(source="https://example.test/paper.zip", source_type="publisher")
    )
    assert unsupported.access_status == "unsupported"
    assert unsupported.diagnostics[0]["error_type"] == "UnsupportedContentTypeError"


def test_remote_injected_fetcher_rejects_private_target_before_transport() -> None:
    calls: list[str] = []

    def fetch(url, _policy):
        calls.append(url)
        return b"body", "text/plain", url

    result = ResearchSourceResolverAdapter(fetch_bytes=fetch).resolve(
        ParsePaperRequest(source="http://127.0.0.1/paper", source_type="publisher")
    )

    assert result.access_status == "failed"
    assert calls == []
    assert result.diagnostics[0]["error_type"] == "ResearchSourceError"
    assert result.snapshot.user_action_required is False


def test_remote_injected_fetcher_rejects_plain_http_before_transport() -> None:
    calls: list[str] = []

    def fetch(url, _policy):
        calls.append(url)
        return b"body", "text/plain", url

    result = ResearchSourceResolverAdapter(fetch_bytes=fetch).resolve(
        ParsePaperRequest(source="http://example.test/paper", source_type="publisher")
    )

    assert result.access_status == "failed"
    assert calls == []
    assert result.diagnostics[0]["error_type"] == "ResearchSourceError"
    assert result.snapshot.user_action_required is False


def test_openreview_query_id_is_used_as_external_identity() -> None:
    source = "https://openreview.net/forum?id=paper-note-123"
    resolver = ResearchSourceResolverAdapter(
        fetch_bytes=lambda url, _policy: (
            b"<html><title>OpenReview paper</title><p>Abstract</p></html>",
            "text/html",
            url,
        )
    )

    result = resolver.resolve(ParsePaperRequest(source=source, source_type="openreview"))

    assert result.snapshot.external_id == "paper-note-123"
    assert result.paper.title == "OpenReview paper"


def test_github_source_adapter_rejects_repository_without_paper_context() -> None:
    resolver = ResearchSourceResolverAdapter()

    with pytest.raises(ResearchSourceError, match="paper context"):
        resolver.resolve(
            ParsePaperRequest(
                source="https://github.com/example/research-code",
                source_type="github",
            )
        )


def test_github_source_adapter_preserves_explicit_paper_id_for_observation() -> None:
    resolver = ResearchSourceResolverAdapter()

    result = resolver.resolve(
        ParsePaperRequest(
            source="https://github.com/example/research-code",
            source_type="github",
            metadata={"paper_id": "paper-known"},
        )
    )

    assert result.paper.paper_id == "paper-known"
    assert result.snapshot.paper_id == "paper-known"
    assert result.access_status == "metadata_only"


def test_typed_scope_merges_metadata_and_explicit_scope() -> None:
    from backend.research.benchmark.models import ResearchMetric
    from backend.research.domain import ResearchPaperIdentity

    paper = ResearchPaper(
        paper_id="paper-scope-merge",
        title="Scope merge",
        actor_scope={"tenant_id": "tenant-a"},
        metadata={"actor_scope": {"user_id": "user-a"}},
    )
    identity = ResearchPaperIdentity(
        paper_id="paper-scope-merge",
        title="Scope merge",
        actor_scope={"tenant_id": "tenant-a"},
        metadata={"actor_scope": {"user_id": "user-a"}},
    )
    metric = ResearchMetric(
        metric_id="metric-scope-merge",
        name="Accuracy",
        actor_scope={"tenant_id": "tenant-a"},
        metadata={"actor_scope": {"user_id": "user-a"}},
    )

    assert paper.actor_scope == {"tenant_id": "tenant-a", "user_id": "user-a"}
    assert identity.actor_scope == {"tenant_id": "tenant-a", "user_id": "user-a"}
    assert metric.actor_scope == {"tenant_id": "tenant-a", "user_id": "user-a"}


def test_arxiv_provider_projects_exact_recorded_connector_item() -> None:
    connector = _RecordedArxivConnector([_raw_item()])
    provider = ArxivResearchSourceProvider(connector)

    paper = provider.fetch_paper("https://arxiv.org/abs/2606.00001")
    record = provider.fetch_source_record("2606.00001")

    assert connector.queries == ["id:2606.00001"]
    assert paper.paper_id == "2606.00001"
    assert paper.title == "Harness-grounded Research"
    assert paper.abstract == "Evidence-backed abstract."
    assert paper.source_url == "https://arxiv.org/abs/2606.00001"
    assert record.paper_id == paper.paper_id
    assert record.source_hash
    assert record.metadata["abstract"] == paper.abstract
    assert record.metadata["source_item_id"] == "raw-recorded"


def test_arxiv_provider_accepts_one_resolved_version_for_unversioned_request() -> None:
    connector = _RecordedArxivConnector([_raw_item(arxiv_id="2606.00001v2")])
    provider = ArxivResearchSourceProvider(connector)

    paper = provider.fetch_paper("2606.00001")
    record = provider.fetch_source_record("2606.00001")

    assert paper.paper_id == "2606.00001"
    assert paper.source_url == "https://arxiv.org/abs/2606.00001v2"
    assert record.paper_id == "2606.00001"
    assert record.metadata["arxiv_id"] == "2606.00001v2"
    assert record.metadata["requested_arxiv_id"] == "2606.00001"


@pytest.mark.parametrize(
    "value",
    [
        "2606.00001",
        "https://arxiv.org/abs/2606.00001",
        "http://www.arxiv.org/abs/2606.00001?context=cs",
        "https://arxiv.org/pdf/2606.00001.pdf",
        "https://export.arxiv.org/e-print/2606.00001",
        "https://arxiv.org/src/2606.00001#source",
    ],
)
def test_require_arxiv_id_accepts_supported_identity_aliases(value: str) -> None:
    assert require_arxiv_id(value) == "2606.00001"


def test_arxiv_provider_rejects_wrong_explicit_version() -> None:
    provider = ArxivResearchSourceProvider(
        _RecordedArxivConnector([_raw_item(arxiv_id="2606.00001v2")])
    )

    with pytest.raises(ResearchSourceError, match="requested paper"):
        provider.fetch_paper("2606.00001v1")


def test_arxiv_provider_cache_reuses_records_and_evicts_at_its_bound() -> None:
    connector = _RecordedArxivConnector(
        [
            _raw_item(arxiv_id="2606.00001"),
            _raw_item(arxiv_id="2606.00002"),
        ]
    )
    provider = ArxivResearchSourceProvider(connector, cache_size=1)

    first = provider.fetch_paper("2606.00001")
    cached = provider.fetch_paper("2606.00001")
    first_record = provider.fetch_source_record("2606.00001")

    assert cached is first
    assert first_record.source_hash == first.metadata["source_hash"]
    assert connector.queries == ["id:2606.00001"]

    provider.fetch_paper("2606.00002")

    assert connector.queries == ["id:2606.00001", "id:2606.00002"]
    with pytest.raises(ResearchSourceError, match="not available"):
        provider.fetch_source_record("2606.00001")


def test_arxiv_provider_uses_top_level_retryability() -> None:
    provider = ArxivResearchSourceProvider(
        _RecordedArxivConnector(
            [],
            errors=[
                _SourceError(
                    error_type="fetch_timeout",
                    retryable=True,
                    metadata={"retryable": False},
                )
            ],
        )
    )

    with pytest.raises(ResearchSourceError) as exc_info:
        provider.fetch_paper("2606.00001")

    assert exc_info.value.retryable is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "https://example.com/abs/2606.00001",
        "file:///tmp/paper.pdf",
        "../2606.00001",
        "not-an-arxiv-id",
    ],
)
def test_arxiv_provider_rejects_unsupported_or_invalid_source(value: str) -> None:
    with pytest.raises(ResearchSourceError):
        require_arxiv_id(value)


def test_arxiv_provider_rejects_ambiguous_or_mismatched_feed() -> None:
    connector = _RecordedArxivConnector(
        [_raw_item(arxiv_id="2606.00002"), _raw_item(arxiv_id="2606.00003")]
    )
    provider = ArxivResearchSourceProvider(connector)

    with pytest.raises(ResearchSourceError, match="requested paper"):
        provider.fetch_paper("2606.00001")


def test_arxiv_provider_rejects_conflicting_versions_for_unversioned_request() -> None:
    provider = ArxivResearchSourceProvider(
        _RecordedArxivConnector(
            [
                _raw_item(arxiv_id="2606.00001v1"),
                _raw_item(arxiv_id="2606.00001v2"),
            ]
        )
    )

    with pytest.raises(ResearchSourceError, match="requested paper"):
        provider.fetch_paper("2606.00001")


def test_arxiv_provider_rejects_item_url_and_metadata_identity_conflict() -> None:
    item = replace(
        _raw_item(arxiv_id="2606.00001"),
        url="https://arxiv.org/abs/2606.00002",
    )
    provider = ArxivResearchSourceProvider(_RecordedArxivConnector([item]))

    with pytest.raises(ResearchSourceError, match="conflicting paper identities"):
        provider.fetch_paper("2606.00001")


def test_arxiv_provider_rejects_pdf_and_metadata_identity_conflict() -> None:
    item = _raw_item(arxiv_id="2606.00001")
    item = replace(
        item,
        metadata={
            **dict(item.metadata),
            "pdf_url": "https://arxiv.org/pdf/2606.00003.pdf",
        },
    )
    provider = ArxivResearchSourceProvider(_RecordedArxivConnector([item]))

    with pytest.raises(ResearchSourceError, match="conflicting paper identities"):
        provider.fetch_paper("2606.00001")


def test_arxiv_provider_rejects_conflicting_explicit_versions_within_item() -> None:
    item = replace(
        _raw_item(arxiv_id="2606.00001v2"),
        url="https://arxiv.org/abs/2606.00001v1",
    )
    provider = ArxivResearchSourceProvider(_RecordedArxivConnector([item]))

    with pytest.raises(ResearchSourceError, match="conflicting paper identities"):
        provider.fetch_paper("2606.00001v2")


def test_document_compiler_prefers_real_latex_result() -> None:
    expected = _document(parse_source="latex")
    compiler = ResearchDocumentCompilerAdapter(
        _FailingFetcher(),
        latex_compiler=_Compiler(result=expected),
    )

    actual = compiler.compile(_source_record())

    assert actual.sections[0].text == "Derived full text."
    assert actual.source_hash == "a" * 64
    assert actual.lineage.source_hash == "a" * 64
    assert actual.metadata["compiler"] == "latex"
    assert actual.metadata["compiled_content_hash"] == "b" * 64
    assert actual.metadata["missing_information"] == []


def test_document_compiler_uses_real_pdf_bytes_after_latex_failure() -> None:
    fetcher = _PdfFetcher(b"%PDF-recorded")
    expected = _document(parse_source="pymupdf")
    parser = _PdfParser(expected)
    compiler = ResearchDocumentCompilerAdapter(
        fetcher,
        latex_compiler=_Compiler(error=RuntimeError("latex unavailable")),
        pdf_parser=parser,
    )

    actual = compiler.compile(_source_record())

    assert parser.calls == [("2606.00001", b"%PDF-recorded")]
    assert actual.source_hash == "a" * 64
    assert actual.lineage.source_hash == "a" * 64
    assert actual.metadata["compiler"] == "pdf_cascade"
    assert actual.metadata["compiled_content_hash"] == "b" * 64
    assert actual.metadata["source_package_checksum"] == fetcher.package.checksum
    assert actual.metadata["compiler_attempts"][0] == {
        "compiler": "latex",
        "status": "failed",
        "error_type": "RuntimeError",
    }


def test_document_compiler_propagates_execution_denial_without_abstract_fallback() -> None:
    identity = _execution_identity()
    denial = ExecutionEnvironmentUnavailableError("docker unavailable")
    parser = _ExecutionDeniedPdfParser(denial)
    compiler = ResearchDocumentCompilerAdapter(
        _PdfFetcher(b"%PDF-recorded"),
        latex_compiler=_Compiler(error=RuntimeError("latex unavailable")),
        pdf_parser=parser,
        allow_abstract_fallback=True,
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        compiler.compile(_source_record(), execution_identity=identity)

    assert raised.value is denial
    assert parser.identities == [identity]


def test_document_compiler_abstract_fallback_is_truth_preserving_and_sanitized() -> None:
    secret = "postgresql://admin:TOPSECRET@db/news"
    compiler = ResearchDocumentCompilerAdapter(
        _FailingFetcher(secret),
        latex_compiler=_Compiler(error=RuntimeError(secret)),
    )

    document = compiler.compile(_source_record())
    payload = document.model_dump_json()

    assert [section.title for section in document.sections] == ["Abstract"]
    assert document.sections[0].text == "Evidence-backed abstract."
    assert document.figures == []
    assert document.tables == []
    assert document.equations == []
    assert document.references == []
    assert document.source_hash == "a" * 64
    assert document.lineage.source_hash == "a" * 64
    assert document.metadata["missing_information"] == ["full_text_sections"]
    assert document.metadata["compiler_attempts"][-1] == {
        "compiler": "pdf_cascade",
        "status": "unavailable",
        "error_type": "not_configured",
    }
    assert secret not in payload


def test_document_compiler_preserves_typed_rate_limit_diagnostics() -> None:
    decision = RateLimitDecision(
        allowed=False,
        domain="arxiv.org",
        limit_per_minute=1,
        retry_after_seconds=42,
    )
    rate_limit_error = SourceRateLimitExceededError(
        "https://arxiv.org/pdf/2606.00001.pdf",
        decision,
    )
    compiler = ResearchDocumentCompilerAdapter(
        _ErrorFetcher(rate_limit_error),
        latex_compiler=_Compiler(error=RuntimeError("latex unavailable")),
        pdf_parser=_PdfParser(_document(parse_source="unused")),
    )

    document = compiler.compile(_source_record())
    attempt = document.metadata["compiler_attempts"][-1]

    assert attempt == {
        "compiler": "pdf_cascade",
        "status": "failed",
        "error_type": "SourceRateLimitExceededError",
        "error_code": "source_rate_limited",
        "retryable": True,
        "domain": "arxiv.org",
        "limit_per_minute": 1,
        "window_seconds": 60,
        "retry_after_seconds": 42,
    }


def test_document_compiler_uses_only_explicitly_injected_compilers() -> None:
    compiler = ResearchDocumentCompilerAdapter(_FailingFetcher())

    document = compiler.compile(_source_record())

    assert document.metadata["compiler"] == "abstract_fallback"
    assert document.metadata["compiler_attempts"] == [
        {
            "compiler": "latex",
            "status": "unavailable",
            "error_type": "not_configured",
        },
        {
            "compiler": "pdf_cascade",
            "status": "unavailable",
            "error_type": "not_configured",
        },
    ]


def test_document_compiler_fails_when_no_real_text_is_available() -> None:
    source = _source_record().model_copy(update={"metadata": {"title": "No abstract"}})
    compiler = ResearchDocumentCompilerAdapter(
        _FailingFetcher(),
        latex_compiler=_Compiler(error=RuntimeError("missing")),
    )

    with pytest.raises(ResearchDocumentCompileError, match="no abstract"):
        compiler.compile(source)


def test_document_compiler_rejects_accepted_record_without_source_hash() -> None:
    compiler = ResearchDocumentCompilerAdapter(_FailingFetcher())
    source = _source_record().model_copy(update={"source_hash": None})

    with pytest.raises(ResearchDocumentCompileError, match="missing its source hash"):
        compiler.compile(source)


@pytest.mark.parametrize(
    "error",
    [
        UnsupportedContentTypeError("text/html", ("application/pdf",)),
        ValueError("arXiv PDF exceeds configured maximum size"),
    ],
)
def test_document_compiler_records_pdf_policy_failure_without_fabrication(
    error: Exception,
) -> None:
    compiler = ResearchDocumentCompilerAdapter(
        _ErrorFetcher(error),
        latex_compiler=_Compiler(error=RuntimeError("latex unavailable")),
        pdf_parser=_PdfParser(_document(parse_source="unused")),
    )

    document = compiler.compile(_source_record())

    assert [section.title for section in document.sections] == ["Abstract"]
    assert document.metadata["missing_information"] == ["full_text_sections"]
    assert document.metadata["compiler_attempts"][-1]["error_type"] == type(error).__name__


class _RecordedArxivConnector:
    def __init__(self, items: list[RawSourceItem], *, errors=None) -> None:
        self.items = items
        self.errors = list(errors or [])
        self.queries: list[str] = []

    def fetch(self, source, *, query: str, limit: int):
        self.queries.append(query)
        return list(self.items[:limit]), list(self.errors)


def _raw_item(*, arxiv_id: str = "2606.00001") -> RawSourceItem:
    return RawSourceItem(
        source_item_id="raw-recorded",
        source_id="recorded-arxiv",
        source_name="Recorded arXiv",
        source_type=SourceType.ARXIV,
        title="Harness-grounded Research",
        url=f"https://arxiv.org/abs/{arxiv_id}",
        fetched_at=datetime(2026, 7, 14, tzinfo=UTC),
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        summary="Evidence-backed abstract.",
        raw_content="<entry>recorded</entry>",
        authors=["Ada Lovelace"],
        tags=["cs.AI"],
        metadata={
            "arxiv_id": arxiv_id,
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
            "primary_category": "cs.AI",
        },
    )


def _source_record() -> PaperSourceRecord:
    return PaperSourceRecord(
        source_id="arxiv:2606.00001",
        paper_id="2606.00001",
        source_type="arxiv",
        source_url="https://arxiv.org/abs/2606.00001",
        source_hash="a" * 64,
        metadata={
            "arxiv_id": "2606.00001",
            "title": "Harness-grounded Research",
            "abstract": "Evidence-backed abstract.",
        },
    )


def _execution_identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-document-compiler",
        graph_id="research-graph",
        graph_version="1.0.0",
        graph_ref="research-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="compile_document",
        node_instance_id="compile-document-1",
        activity_id="compile-document-activity",
        attempt=1,
    )


def _document(*, parse_source: str) -> ResearchDocument:
    return ResearchDocument(
        paper_id="2606.00001",
        source_hash="b" * 64,
        sections=[
            ResearchSection(
                section_id="section-1",
                title="Method",
                text="Derived full text.",
                source_ref="arxiv://2606.00001/section/1",
            )
        ],
        lineage=SourceLineage(
            source_refs=["arxiv://2606.00001/section/1"],
            source_hash="b" * 64,
        ),
        metadata={"parse_source": parse_source},
    )


class _Compiler:
    def __init__(
        self,
        *,
        result: ResearchDocument | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error

    def compile(self, source: PaperSourceRecord) -> ResearchDocument:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class _Package:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.checksum = "c" * 64


class _PdfFetcher:
    def __init__(self, content: bytes) -> None:
        self.package = _Package(content)

    def fetch_pdf_package(self, arxiv_id: str) -> _Package:
        return self.package


class _FailingFetcher:
    def __init__(self, message: str = "offline") -> None:
        self.message = message

    def fetch_source_package(self, arxiv_id: str):
        raise RuntimeError(self.message)

    def fetch_pdf_package(self, arxiv_id: str):
        raise RuntimeError(self.message)


class _ErrorFetcher:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def fetch_pdf_package(self, arxiv_id: str):
        raise self.error


class _SourceError:
    def __init__(
        self,
        *,
        error_type: str,
        retryable: bool,
        metadata: dict,
    ) -> None:
        self.error_type = error_type
        self.retryable = retryable
        self.metadata = metadata


class _PdfParser:
    def __init__(self, result: ResearchDocument) -> None:
        self.result = result
        self.calls: list[tuple[str, bytes]] = []

    def parse(self, paper_id: str, content: bytes) -> ResearchDocument:
        self.calls.append((paper_id, content))
        return self.result


class _ExecutionDeniedPdfParser:
    def __init__(self, error: ExecutionEnvironmentUnavailableError) -> None:
        self.error = error
        self.identities: list[GraphExecutionIdentity | None] = []

    def parse(
        self,
        paper_id: str,
        content: bytes,
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> ResearchDocument:
        del paper_id, content
        self.identities.append(execution_identity)
        raise self.error


class _DocumentRepository:
    def __init__(self, document: ResearchDocument | None) -> None:
        self.document = document
        self.scopes = []

    def get(self, paper_id, *, actor_scope=None):
        self.scopes.append((paper_id, dict(actor_scope or {})))
        return self.document if self.document is not None and self.document.paper_id == paper_id else None


def test_catalog_bound_source_uses_exact_actor_visible_parsed_document() -> None:
    locator = "file:///owned/import.pdf"
    document = ResearchDocument(
        paper_id="paper-private",
        source_hash="a" * 64,
        title="Private paper",
        sections=[],
        lineage=SourceLineage(source_refs=[locator], source_hash="a" * 64),
        source_locators=[locator],
        actor_scope={"user_id": "alice"},
    )
    repository = _DocumentRepository(document)
    provider = CatalogBoundResearchSourceProvider(
        ArxivResearchSourceProvider(_RecordedArxivConnector([])),
        repository,
    )

    bound = provider.fetch_bound_source(
        source_ref=locator,
        paper_id="paper-private",
        source_revision="sha256:" + "a" * 64,
        actor_scope={"user_id": "alice"},
    )

    assert bound is not None
    assert bound.document is document
    assert bound.paper.source_url is not None
    assert bound.paper.source_url.startswith("source://local/")
    assert bound.paper.metadata["source_locator"] == locator
    assert bound.source_record.source_hash == "sha256:" + "a" * 64
    assert bound.source_record.source_type == "local"
    assert bound.source_record.source_url.startswith("source://local/")
    assert bound.source_record.metadata["source_locator"] == locator
    assert repository.scopes == [("paper-private", {"user_id": "alice"})]


def test_catalog_bound_source_rejects_wrong_revision_or_locator() -> None:
    locator = "https://example.test/paper.pdf"
    document = ResearchDocument(
        paper_id="paper-external",
        source_hash="b" * 64,
        title="External paper",
        sections=[],
        lineage=SourceLineage(source_refs=[locator], source_hash="b" * 64),
        source_locators=[locator],
    )
    provider = CatalogBoundResearchSourceProvider(
        ArxivResearchSourceProvider(_RecordedArxivConnector([])),
        _DocumentRepository(document),
    )

    with pytest.raises(ResearchSourceError, match="revision"):
        provider.fetch_bound_source(
            source_ref=locator,
            paper_id="paper-external",
            source_revision="c" * 64,
            actor_scope={"user_id": "alice"},
        )
    with pytest.raises(ResearchSourceError, match="locator"):
        provider.fetch_bound_source(
            source_ref="https://example.test/other.pdf",
            paper_id="paper-external",
            source_revision="b" * 64,
            actor_scope={"user_id": "alice"},
        )


def test_catalog_bound_source_rejects_document_from_another_actor_scope() -> None:
    locator = "file:///restored/immutable-source.pdf"
    document = ResearchDocument(
        paper_id="paper-private",
        source_hash="d" * 64,
        title="Private paper",
        sections=[],
        lineage=SourceLineage(source_refs=[locator], source_hash="d" * 64),
        source_locators=[locator],
        actor_scope={"user_id": "alice"},
    )
    provider = CatalogBoundResearchSourceProvider(
        ArxivResearchSourceProvider(_RecordedArxivConnector([])),
        _DocumentRepository(document),
    )

    with pytest.raises(ResearchSourceError, match="actor scope"):
        provider.fetch_bound_source(
            source_ref=locator,
            paper_id="paper-private",
            source_revision="sha256:" + "d" * 64,
            actor_scope={"user_id": "bob"},
        )
