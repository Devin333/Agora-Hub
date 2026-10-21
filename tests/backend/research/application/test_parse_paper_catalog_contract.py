from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

import pytest

from backend.research.application.catalog import (
    InMemoryResearchCatalogRepository,
    ResearchPaperCatalogService,
)
from backend.research.application.parse_paper import (
    InMemoryResearchEventSink,
    MetadataOnlySourceResolver,
    ParsePaperError,
    ParsePaperRequest,
    ParsePaperUseCase,
    PARSE_OPTION_MAX_TIMEOUT_SECONDS,
    ResolvedPaperSource,
    infer_source_type,
    validate_parse_options,
    _parse_cache_fingerprint,
    _parse_cache_identity,
    _quality_report,
)
from backend.research.document.cascade_parser import (
    CascadeDocumentParser,
    DocumentQualityProbe,
    PyMuPDFTextDocumentParser,
)
from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.domain import (
    ResearchDocument,
    ResearchEvidencePack,
    ResearchPaper,
    ResearchPaperIdentity,
    ResearchSection,
    ResearchSourceSnapshot,
    SourceLineage,
)
from backend.research.domain.document_fidelity import DocumentFidelity, DocumentPage
from backend.research.domain.paper import PaperSourceRecord


class _Resolver:
    def __init__(self, *, source_url: str, paper_id: str, content: bytes) -> None:
        self.source_url = source_url
        self.paper_id = paper_id
        self.content = content

    def resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
        paper = ResearchPaper(
            paper_id=self.paper_id,
            title="A Stable Paper Identity",
            authors=["Ada Lovelace"],
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
            source="publisher",
            source_url=self.source_url,
        )
        digest = __import__("hashlib").sha256(self.content).hexdigest()
        snapshot = ResearchSourceSnapshot(
            snapshot_id=f"snapshot-{self.paper_id}",
            paper_id=self.paper_id,
            source_type="publisher",
            canonical_url=self.source_url,
            external_id=None,
            content_type="text/html",
            source_hash=digest,
            fetched_at=datetime.now(UTC),
            lineage=SourceLineage(source_refs=[self.source_url], source_hash=digest),
        )
        return ResolvedPaperSource(
            paper=paper,
            snapshot=snapshot,
            content=self.content,
            content_type="text/html",
        )


class _Parser:
    def cache_identity(self):
        return {"version": "1", "configuration": "catalog-contract-parser"}

    def __init__(self) -> None:
        self.calls = 0

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls += 1
        digest = __import__("hashlib").sha256(source_bytes).hexdigest()
        source_ref = f"paper://{paper_id}/method"
        return ResearchDocument(
            paper_id=paper_id,
            source_hash=digest,
            sections=[
                ResearchSection(
                    section_id="method",
                    title="Method",
                    text="The method is fully traceable.",
                    source_ref=source_ref,
                )
            ],
            lineage=SourceLineage(source_refs=[source_ref], source_hash=digest),
            metadata={"parse_source": "html"},
        )


class _Artifacts:
    def __init__(self) -> None:
        self.payloads: list[tuple[str, dict]] = []

    def publish(self, *, artifact_type: str, payload: dict, metadata: dict | None = None) -> str:
        self.payloads.append((artifact_type, {"payload": payload, "metadata": metadata or {}}))
        return f"artifact://test/{artifact_type}/{len(self.payloads)}"


class _FailingParser:
    def parse(self, _paper_id: str, _source_bytes: bytes) -> ResearchDocument:
        raise RuntimeError("primary parser unavailable")


class _CompilerFallback:
    def __init__(self) -> None:
        self.source: PaperSourceRecord | None = None

    def compile(self, source: PaperSourceRecord) -> ResearchDocument:
        self.source = source
        return ResearchDocument(
            paper_id=source.paper_id,
            source_hash=source.source_hash or "fallback-hash",
            sections=[
                ResearchSection(
                    section_id="fallback-method",
                    title="Method",
                    text="Compiler fallback preserved the source lineage.",
                    source_ref="paper://fallback/method",
                )
            ],
            lineage=SourceLineage(
                source_refs=["paper://fallback/method"],
                source_hash=source.source_hash or "fallback-hash",
            ),
            metadata={"compiler": "fallback"},
        )


def test_equivalent_sources_merge_by_fingerprint_and_retain_provenance(tmp_path: Path) -> None:
    repository = InMemoryResearchCatalogRepository()
    parser = _Parser()
    events = []
    artifacts = _Artifacts()

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    use_case = ParsePaperUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/paper",
            paper_id="publisher-paper",
            content=b"<html><h1>Method</h1><p>text</p></html>",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        document_parser=parser,
        artifact_store=artifacts,
        event_sink=_Events(),
        chunker=PaperDocumentChunker(),
        chunk_manifest=ChunkManifestManager(tmp_path / "manifests"),
    )

    first = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/paper",
            run_id="run-1",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )

    use_case._resolver = _Resolver(  # noqa: SLF001 - contract test swaps source adapter
        source_url="https://publisher.example/alternate",
        paper_id="publisher-paper-v2",
        content=b"<html><h1>Method</h1><p>text</p></html>",
    )
    second = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/alternate",
            run_id="run-2",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )

    assert first.status == "parsed"
    assert second.paper_id == first.paper_id
    assert second.identity is not None
    assert len(repository.list_for_paper(first.paper_id, actor_scope={"tenant_id": "tenant-a", "user_id": "user-a"})) == 2
    assert set(second.identity.field_provenance["title"]) == {first.source_snapshots[0].snapshot_id, second.source_snapshots[0].snapshot_id}
    assert second.idempotent is True
    assert second.artifact_refs == first.artifact_refs
    assert len(artifacts.payloads) == 3
    assert parser.calls == 1
    assert any(event["from_status"] == "resolving" and event["to_status"] == "parsing" for event in events)
    manifest_files = list((tmp_path / "manifests").rglob("chunk_manifest.json"))
    assert len(manifest_files) == 1

    third = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/alternate",
            run_id="run-3",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )
    assert third.idempotent is True
    assert len(repository.list_for_paper(first.paper_id, actor_scope={"tenant_id": "tenant-a", "user_id": "user-a"})) == 2

    manifest_payload = __import__("json").loads(manifest_files[0].read_text(encoding="utf-8"))
    with pytest.raises(ParsePaperError) as blocked:
        use_case.parse(
            ParsePaperRequest(
                source="https://publisher.example/alternate",
                run_id="run-4",
                tenant_id="tenant-a",
                user_id="user-a",
                options={"quality_profile": "reading"},
            )
        )
    assert blocked.value.code == "parse_snapshot_activation_blocked"
    assert blocked.value.details["blocker"] == "index_snapshot_atomic_activation_unavailable"
    assert parser.calls == 1
    assert __import__("json").loads(manifest_files[0].read_text(encoding="utf-8")) == manifest_payload




def test_parse_cache_identity_binds_source_schema_profile_normalizer_and_instances() -> None:
    parser = CascadeDocumentParser(
        backends=(("text", PyMuPDFTextDocumentParser(max_pages=100, max_text_chars=100_000)),),
        probe=DocumentQualityProbe(min_sections=3, min_body_chars=3000),
    )
    identity = _parse_cache_identity(
        parser,
        None,
        PaperDocumentChunker(),
        {"quality_profile": "reading", "parser_backend": "cascade"},
        "application/pdf",
        source_hash="sha256:source-a",
    )

    assert identity is not None
    assert identity["source"] == {"hash": "sha256:source-a", "content_type": "application/pdf"}
    assert identity["profile"] == {"quality_profile": "reading", "parser_backend": "cascade"}
    assert identity["schemas"]["document_sha256"]
    assert identity["schemas"]["fidelity_sha256"]
    assert identity["normalizer"]["module_sha256"]
    assert identity["parser"]["probe"]["thresholds"]["min_sections"] == 3
    assert identity["chunker"]["configuration"] == "stateless"
    changed_source = _parse_cache_fingerprint(
        parser,
        None,
        PaperDocumentChunker(),
        {"quality_profile": "reading", "parser_backend": "cascade"},
        "application/pdf",
        source_hash="sha256:source-b",
    )
    assert changed_source != _parse_cache_fingerprint(
        parser,
        None,
        PaperDocumentChunker(),
        {"quality_profile": "reading", "parser_backend": "cascade"},
        "application/pdf",
        source_hash="sha256:source-a",
    )


def test_parse_cache_identity_fails_closed_for_opaque_production_chunker() -> None:
    class OpaqueChunker(PaperDocumentChunker):
        __module__ = "backend.research.document.opaque_chunker"

        def __init__(self) -> None:
            self.window_size = 99

    assert _parse_cache_identity(
        _Parser(),
        None,
        OpaqueChunker(),
        {},
        "text/html",
        source_hash="sha256:source",
    ) is None

def test_parse_cache_fingerprint_tracks_known_parser_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = DocumentQualityProbe(min_sections=3, min_body_chars=3000)
    first_backend = PyMuPDFTextDocumentParser(max_pages=100, max_text_chars=100_000)
    second_backend = PyMuPDFTextDocumentParser(max_pages=200, max_text_chars=200_000)
    parser = CascadeDocumentParser(
        backends=(("first", first_backend), ("second", second_backend)),
        probe=probe,
    )

    def fingerprint(candidate: CascadeDocumentParser) -> str | None:
        return _parse_cache_fingerprint(candidate, None, None, {}, "application/pdf")

    original = fingerprint(parser)
    assert original is not None
    assert fingerprint(parser) == original

    # Mutable operational state is intentionally excluded from cache identity.
    first_backend.calls = 1
    assert fingerprint(parser) == original

    changed_probe = CascadeDocumentParser(
        backends=(("first", first_backend), ("second", second_backend)),
        probe=DocumentQualityProbe(min_sections=4, min_body_chars=3000),
    )
    assert fingerprint(changed_probe) != original

    reversed_backends = CascadeDocumentParser(
        backends=(("second", second_backend), ("first", first_backend)),
        probe=probe,
    )
    assert fingerprint(reversed_backends) != original

    monkeypatch.setenv("NEWSROOM_PARSER_QUALITY_MIN_BODY_CHARS", "4100")
    assert fingerprint(parser) != original



def test_parse_cache_fingerprint_tracks_production_compiler_instance_configuration() -> None:
    from backend.research.document.latex_compiler import ArxivLatexDocumentCompiler
    from infrastructure.research.document_compiler import ResearchDocumentCompilerAdapter

    class _Fetcher:
        def cache_identity(self):
            return {"version": "1", "configuration": "catalog-contract-fetcher"}

    fetcher = _Fetcher()
    pdf_parser = CascadeDocumentParser(
        backends=(("text", PyMuPDFTextDocumentParser(max_pages=100, max_text_chars=100_000)),),
        probe=DocumentQualityProbe(),
    )
    enabled = ResearchDocumentCompilerAdapter(
        fetcher,
        latex_compiler=ArxivLatexDocumentCompiler(fetcher),
        pdf_parser=pdf_parser,
        allow_abstract_fallback=True,
    )
    disabled = ResearchDocumentCompilerAdapter(
        fetcher,
        latex_compiler=ArxivLatexDocumentCompiler(fetcher),
        pdf_parser=pdf_parser,
        allow_abstract_fallback=False,
    )

    enabled_fingerprint = _parse_cache_fingerprint(
        _Parser(), enabled, PaperDocumentChunker(), {}, "application/pdf"
    )
    disabled_fingerprint = _parse_cache_fingerprint(
        _Parser(), disabled, PaperDocumentChunker(), {}, "application/pdf"
    )

    assert enabled_fingerprint is not None
    assert disabled_fingerprint is not None
    assert enabled_fingerprint != disabled_fingerprint

def test_parse_cache_fingerprint_fails_closed_for_opaque_production_parser() -> None:
    from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser

    assert _parse_cache_fingerprint(
        MarkerPdfDocumentParser(), None, None, {}, "application/pdf"
    ) is None


def _configure_marker_cache_test_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    weights = tmp_path / "marker-cache" / "models" / "model.safetensors"
    weights.parent.mkdir(parents=True, exist_ok=True)
    weights.write_bytes(b"contract-test-weights")
    manifest = tmp_path / "marker-weights-manifest.json"
    manifest.write_text(json.dumps({
        "schema": "newsroom-marker-weights-manifest.v1",
        "files": [{
            "path": "models/model.safetensors",
            "sha256": sha256(b"contract-test-weights").hexdigest(),
            "size": len(b"contract-test-weights"),
        }],
    }), encoding="utf-8")
    monkeypatch.setenv(
        "NEWSROOM_MARKER_DOCKER_IMAGE",
        f"registry.example/newsroom-marker@sha256:{'a' * 64}",
    )
    monkeypatch.setenv("NEWSROOM_MARKER_CACHE_DIR", str(weights.parents[1].resolve()))
    monkeypatch.setenv("NEWSROOM_MARKER_WEIGHTS_MANIFEST", str(manifest.resolve()))
    monkeypatch.setenv("NEWSROOM_MARKER_TEST_BINDING", json.dumps({
        "schema": "newsroom-marker-weights-binding.v1",
        "host_cache_dir": str(weights.parents[1].resolve()),
        "container_cache_dir": "/root/.cache",
        "verified_manifest_sha256": sha256(json.dumps({
            "schema": "newsroom-marker-weights-manifest.v1",
            "files": [{
                "path": "models/model.safetensors",
                "sha256": sha256(b"contract-test-weights").hexdigest(),
                "size": len(b"contract-test-weights"),
            }],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }))
    monkeypatch.delenv("NEWSROOM_MARKER_DOCKER_ARGS", raising=False)


def test_marker_cache_configuration_requires_execution_adapter_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from backend.research.application.parse_paper import _known_document_parser_configuration
    from backend.research.document.marker_pdf_parser import (
        MarkerPdfDocumentParser,
        MarkerReadingOrderProfile,
    )

    _configure_marker_cache_test_identity(monkeypatch, tmp_path)

    class DeclaredRunner:
        def __call__(self, *_args, **_kwargs):
            return None

        def cache_identity(self):
            return {
                "profile": {"mode": "external_process"},
                "provider_identity": "test-provider/v1",
                "marker_weights_binding": json.loads(__import__("os").environ["NEWSROOM_MARKER_TEST_BINDING"]),
            }

    class OpaqueRunner:
        def __call__(self, *_args, **_kwargs):
            return None

    configured = _known_document_parser_configuration(
        MarkerPdfDocumentParser(command_runner=DeclaredRunner())
    )
    assert configured is not None
    assert configured["execution_adapter"]["profile"] == {"mode": "external_process"}
    assert configured["execution_adapter"]["marker_weights_binding"]["container_cache_dir"] == "/root/.cache"
    assert configured["reading_order_profile"] == "backend_preserved"
    assert configured["instance_configuration"]["reading_order_profile"] == "backend_preserved"
    admitted = MarkerPdfDocumentParser(
        command_runner=DeclaredRunner(),
        reading_order_profile=MarkerReadingOrderProfile.LAYOUT_ADMITTED_V1,
    )
    admitted_config = _known_document_parser_configuration(admitted)
    assert admitted_config is not None
    assert admitted_config["instance_configuration"]["reading_order_profile"] == "layout_admitted_v1"
    assert _parse_cache_fingerprint(
        admitted, None, PaperDocumentChunker(), {}, "application/pdf"
    ) != _parse_cache_fingerprint(
        MarkerPdfDocumentParser(command_runner=DeclaredRunner()),
        None,
        PaperDocumentChunker(),
        {},
        "application/pdf",
    )
    assert _known_document_parser_configuration(
        MarkerPdfDocumentParser(command_runner=OpaqueRunner())
    ) is None


def test_multiformat_cache_fingerprint_tracks_marker_adapter_and_ignores_dynamic_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from backend.research.document.cascade_parser import (
        CascadeArxivDocumentParser,
        CascadeDocumentParser,
        DocumentQualityProbe,
    )
    from backend.research.document.format_router import MultiFormatDocumentParser
    from backend.research.document.html_parser import HtmlDocumentParser
    from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser

    _configure_marker_cache_test_identity(monkeypatch, tmp_path)

    class DeclaredRunner:
        def __init__(self):
            self.last_outcome = None

        def __call__(self, *_args, **_kwargs):
            return None

        def cache_identity(self):
            return {
                "adapter": "declared-v1",
                "provider_identity": "test-provider/v1",
                "marker_weights_binding": json.loads(__import__("os").environ["NEWSROOM_MARKER_TEST_BINDING"]),
            }

    runner = DeclaredRunner()
    marker = MarkerPdfDocumentParser(command_runner=runner)
    pdf_cascade = CascadeDocumentParser(
        backends=(("marker", marker),),
        probe=DocumentQualityProbe(),
    )
    parser = MultiFormatDocumentParser(
        arxiv_parser=CascadeArxivDocumentParser(pdf_parser=pdf_cascade),
        html_parser=HtmlDocumentParser(),
        pdf_parser=None,
    )

    initial = _parse_cache_fingerprint(parser, None, None, {}, "application/pdf")
    assert initial is not None
    runner.last_outcome = {"execution_id": "dynamic-run-1"}
    assert _parse_cache_fingerprint(parser, None, None, {}, "application/pdf") == initial

    opaque_parser = MultiFormatDocumentParser(
        arxiv_parser=CascadeArxivDocumentParser(
            pdf_parser=CascadeDocumentParser(
                backends=(("marker", MarkerPdfDocumentParser(command_runner=lambda *_a, **_k: None)),),
                probe=DocumentQualityProbe(),
            )
        ),
        html_parser=HtmlDocumentParser(),
        pdf_parser=None,
    )
    assert _parse_cache_fingerprint(opaque_parser, None, None, {}, "application/pdf") is None


def test_metadata_only_never_claims_a_document() -> None:
    repository = InMemoryResearchCatalogRepository()
    result = ParsePaperUseCase(
        source_resolver=MetadataOnlySourceResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
    ).parse(
        ParsePaperRequest(
            source="https://publisher.example/restricted",
            source_type="publisher",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )

    assert result.status == "metadata_only"
    assert result.document is None
    assert result.source_snapshots[0].access_status == "metadata_only"
    assert result.source_snapshots[0].actor_scope == {"tenant_id": "tenant-a", "user_id": "user-a"}
    assert repository.get_document(result.paper_id, actor_scope={"tenant_id": "tenant-a", "user_id": "user-a"}) is None


def test_metadata_only_projects_catalog_candidates_without_claiming_document() -> None:
    repository = InMemoryResearchCatalogRepository()
    catalog = ResearchPaperCatalogService(catalog_repository=repository)
    result = ParsePaperUseCase(
        source_resolver=MetadataOnlySourceResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        catalog_projection=catalog,
    ).parse(
        ParsePaperRequest(
            source="https://publisher.example/restricted-with-metadata",
            source_type="publisher",
            tenant_id="tenant-a",
            user_id="user-a",
            metadata={"title": "Metadata paper", "tasks": ["task-x"]},
        )
    )

    assert result.status == "metadata_only"
    assert result.catalog_status == "catalog_partial"
    assert result.catalog_entry is not None
    assert result.catalog_entry.relations[0].status == "candidate"
    assert result.document is None


def test_github_repository_requires_explicit_paper_context() -> None:
    repository = InMemoryResearchCatalogRepository()
    events = []

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    use_case = ParsePaperUseCase(
        source_resolver=MetadataOnlySourceResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        event_sink=_Events(),
    )

    with pytest.raises(ParsePaperError) as error:
        use_case.parse(
            ParsePaperRequest(
                source="https://github.com/example/research-code",
                source_type="github",
                tenant_id="tenant-a",
                user_id="user-a",
            )
        )

    assert error.value.code == "github_paper_context_required"
    assert repository._papers == {}  # noqa: SLF001 - verify no identity was persisted
    assert events[-1]["to_status"] == "failed"


def test_explicit_github_observation_does_not_overwrite_existing_paper_title() -> None:
    repository = InMemoryResearchCatalogRepository()
    existing = ResearchPaper(
        paper_id="paper-known",
        title="Authoritative paper title",
        authors=["Paper Author"],
        source="arxiv",
        metadata={"actor_scope": {"tenant_id": "tenant-a", "user_id": "user-a"}},
    )
    repository.save(existing)
    repository.save(
        ResearchPaperIdentity(
            paper_id=existing.paper_id,
            title=existing.title,
            authors=existing.authors,
            metadata={"actor_scope": {"tenant_id": "tenant-a", "user_id": "user-a"}},
        )
    )

    resolved = MetadataOnlySourceResolver().resolve(
        ParsePaperRequest(
            source="https://github.com/example/research-code",
            source_type="github",
            tenant_id="tenant-a",
            user_id="user-a",
            metadata={"paper_id": existing.paper_id, "title": "Repository name"},
        )
    )
    resolved = resolved.__class__(
        paper=resolved.paper.model_copy(update={"paper_id": existing.paper_id}),
        snapshot=resolved.snapshot.model_copy(update={"paper_id": existing.paper_id}),
        content=resolved.content,
        content_type=resolved.content_type,
        source_record=resolved.source_record,
        access_status=resolved.access_status,
        diagnostics=resolved.diagnostics,
    )
    class _ResolvedResolver:
        def resolve(self, _request: ParsePaperRequest) -> ResolvedPaperSource:
            return resolved

    result = ParsePaperUseCase(
        source_resolver=_ResolvedResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
    )
    parsed = result.parse(
        ParsePaperRequest(
            source="https://github.com/example/research-code",
            source_type="github",
            tenant_id="tenant-a",
            user_id="user-a",
            metadata={"paper_id": existing.paper_id, "title": "Repository name"},
        )
    )

    assert parsed.paper.title == existing.title
    assert repository.get_paper(existing.paper_id, actor_scope={"tenant_id": "tenant-a", "user_id": "user-a"}).title == existing.title


def test_metadata_only_reuses_snapshot_and_reports_idempotency() -> None:
    repository = InMemoryResearchCatalogRepository()
    use_case = ParsePaperUseCase(
        source_resolver=MetadataOnlySourceResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
    )
    request = ParsePaperRequest(
        source="https://publisher.example/restricted",
        source_type="publisher",
        tenant_id="tenant-a",
        user_id="user-a",
    )

    first = use_case.parse(request.model_copy(update={"run_id": "metadata-run-1"}))
    second = use_case.parse(request.model_copy(update={"run_id": "metadata-run-2"}))

    assert first.status == second.status == "metadata_only"
    assert second.idempotent is True
    assert second.source_snapshots[0].snapshot_id == first.source_snapshots[0].snapshot_id


def test_parser_failure_uses_compiler_fallback_with_canonical_source_record() -> None:
    repository = InMemoryResearchCatalogRepository()
    compiler = _CompilerFallback()
    use_case = ParsePaperUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/fallback",
            paper_id="fallback-paper",
            content=b"fallback content",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        document_parser=_FailingParser(),
        document_compiler=compiler,
    )

    result = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/fallback",
            tenant_id="tenant-a",
            user_id="user-a",
        )
    )

    assert result.status == "parsed"
    assert result.document is not None
    assert compiler.source is not None
    assert compiler.source.paper_id == result.paper_id
    assert compiler.source.source_hash == result.source_snapshots[0].source_hash
    assert result.document.metadata["parser_attempts"][0]["backend"] == "document_parser"
    assert result.document.metadata["parser_attempts"][0]["reason_code"] == "document_parser_failed"
    assert "primary parser unavailable" not in str(result.document.model_dump(mode="json"))


def test_unsupported_binary_source_fails_without_text_fallback() -> None:
    repository = InMemoryResearchCatalogRepository()
    use_case = ParsePaperUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/archive.zip",
            paper_id="unsupported-paper",
            content=b"PK\x03\x04not-a-paper-archive",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
    )

    with pytest.raises(ParsePaperError) as error:
        use_case.parse(
            ParsePaperRequest(
                source="https://publisher.example/archive.zip",
                source_type="publisher",
            )
        )

    assert error.value.code == "unsupported_format"
    assert error.value.status_code == 415


def test_evidence_pack_preserves_lineage_scope_when_metadata_has_other_fields() -> None:
    pack = ResearchEvidencePack(
        pack_id="pack-scope",
        paper_id="paper-scope",
        lineage=SourceLineage(
            source_refs=["paper://scope"],
            metadata={"actor_scope": {"tenant_id": "tenant-a"}},
        ),
        metadata={"builder": "test"},
    )

    assert pack.actor_scope == {"tenant_id": "tenant-a"}
    assert pack.metadata["actor_scope"] == {"tenant_id": "tenant-a"}
    assert pack.lineage.metadata["actor_scope"] == {"tenant_id": "tenant-a"}


def test_source_snapshot_and_document_merge_lineage_scope_with_unrelated_metadata() -> None:
    lineage = SourceLineage(
        source_refs=["paper://scope"],
        metadata={"actor_scope": {"tenant_id": "tenant-a"}},
    )
    snapshot = ResearchSourceSnapshot(
        snapshot_id="snapshot-scope",
        paper_id="paper-scope",
        canonical_url="https://example.test/paper",
        lineage=lineage,
        metadata={"fetcher": "test"},
    )
    document = ResearchDocument(
        paper_id="paper-scope",
        source_hash="hash-scope",
        lineage=SourceLineage(
            source_refs=["paper://scope"],
            source_hash="hash-scope",
            metadata={"actor_scope": {"tenant_id": "tenant-a"}},
        ),
        metadata={"parser_backend": "test"},
    )

    assert snapshot.actor_scope == {"tenant_id": "tenant-a"}
    assert snapshot.metadata["actor_scope"] == {"tenant_id": "tenant-a"}
    assert document.actor_scope == {"tenant_id": "tenant-a"}
    assert document.metadata["actor_scope"] == {"tenant_id": "tenant-a"}


def test_doi_url_is_classified_as_doi_source() -> None:
    assert infer_source_type("https://doi.org/10.1234/Example") == "doi"


def test_parser_retry_is_bounded_and_recorded_as_durable_phase_event() -> None:
    repository = InMemoryResearchCatalogRepository()
    events: list[dict] = []

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    class _RetryingUseCase(ParsePaperUseCase):
        attempts = 0

        def _parse_document(self, paper, snapshot, resolved):  # type: ignore[no-untyped-def]
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("transient parser worker failure")
            return _Parser().parse(paper.paper_id, resolved.content or b"")

    use_case = _RetryingUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/retry",
            paper_id="retry-paper",
            content=b"retryable source",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        event_sink=_Events(),
        max_retries=1,
    )

    result = use_case.parse(ParsePaperRequest(source="https://publisher.example/retry", run_id="retry-run"))

    assert result.status == "parsed"
    assert use_case.attempts == 2
    retry_events = [event for event in events if event.get("phase") == "retry_scheduled"]
    assert len(retry_events) == 1
    assert retry_events[0]["attempt"] == 1
    assert retry_events[0]["max_retries"] == 1



def test_parser_retry_budget_never_exceeds_max_attempts() -> None:
    class _AlwaysRetryingUseCase(ParsePaperUseCase):
        attempts = 0

        def _parse_document(self, paper, snapshot, resolved):  # type: ignore[no-untyped-def]
            self.attempts += 1
            raise ConnectionError("transient parser worker failure")

    use_case = _AlwaysRetryingUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/retry-cap",
            paper_id="retry-cap-paper",
            content=b"retryable source",
        ),
        document_parser=_Parser(),
        max_retries=5,
    )

    with pytest.raises(ParsePaperError) as failed:
        use_case.parse(ParsePaperRequest(source="https://publisher.example/retry-cap", run_id="retry-cap"))
    assert failed.value.code == "parse_failed"
    assert use_case.attempts == 5

def test_source_retry_is_bounded_and_recorded_before_identity_resolution() -> None:
    repository = InMemoryResearchCatalogRepository()
    events: list[dict] = []

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    class _FlakyResolver:
        attempts = 0

        def resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
            self.attempts += 1
            if self.attempts == 1:
                error = ConnectionError("temporary source transport failure")
                error.retryable = True  # type: ignore[attr-defined]
                raise error
            return _Resolver(
                source_url="https://publisher.example/source-retry",
                paper_id="source-retry-paper",
                content=b"source retry content",
            ).resolve(request)

    resolver = _FlakyResolver()
    result = ParsePaperUseCase(
        source_resolver=resolver,
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        document_parser=_Parser(),
        event_sink=_Events(),
        max_retries=1,
    ).parse(
        ParsePaperRequest(
            source="https://publisher.example/source-retry",
            run_id="source-retry-run",
        )
    )

    assert result.status == "parsed"
    assert resolver.attempts == 2
    retry_events = [event for event in events if event.get("phase") == "retry_scheduled"]
    assert len(retry_events) == 1
    assert retry_events[0]["status"] == "resolving"
    assert retry_events[0]["attempt"] == 1


def test_parse_error_emits_terminal_failed_event_before_rethrowing() -> None:
    repository = InMemoryResearchCatalogRepository()
    events: list[dict] = []

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    class _FailingUseCase(ParsePaperUseCase):
        def _parse_document(self, paper, snapshot, resolved):  # type: ignore[no-untyped-def]
            raise ParsePaperError("document_parse_failed", "structured parser failed")

    use_case = _FailingUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/failed",
            paper_id="failed-paper",
            content=b"failed source",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        event_sink=_Events(),
    )

    with pytest.raises(ParsePaperError, match="structured parser failed"):
        use_case.parse(ParsePaperRequest(source="https://publisher.example/failed", run_id="failed-run"))

    assert any(event["to_status"] == "failed" for event in events)


def test_parse_options_are_allowlisted_and_bounded() -> None:
    assert validate_parse_options({"quality_profile": "catalog", "max_attempts": 2})["max_attempts"] == 2

    with pytest.raises(ParsePaperError) as unknown:
        validate_parse_options({"with_propositions": True})
    assert unknown.value.code == "invalid_request"
    assert unknown.value.status_code == 400

    with pytest.raises(ParsePaperError):
        validate_parse_options({"max_attempts": 6})
    assert validate_parse_options({"timeout_seconds": PARSE_OPTION_MAX_TIMEOUT_SECONDS})["timeout_seconds"] == PARSE_OPTION_MAX_TIMEOUT_SECONDS
    with pytest.raises(ParsePaperError):
        validate_parse_options({"timeout_seconds": PARSE_OPTION_MAX_TIMEOUT_SECONDS + 1})

    with pytest.raises(ParsePaperError) as parsed:
        ParsePaperUseCase().parse(
            ParsePaperRequest(
                source="https://publisher.example/invalid-options",
                options={"with_propositions": True},
            )
        )
    assert parsed.value.code == "invalid_request"


def test_parse_budget_exhaustion_is_explicit_and_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SlowResolver:
        def resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
            return MetadataOnlySourceResolver().resolve(request)

    events: list[dict] = []

    class _Events:
        def append(self, run_id: str, event: dict) -> None:
            events.append({"run_id": run_id, **event})

    ticks = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(
        "backend.research.application.parse_paper.monotonic",
        lambda: next(ticks),
    )

    with pytest.raises(ParsePaperError) as error:
        ParsePaperUseCase(source_resolver=_SlowResolver(), event_sink=_Events()).parse(
            ParsePaperRequest(
                source="https://publisher.example/budget",
                options={"timeout_seconds": 0.001},
            )
        )

    assert error.value.code == "budget_exhausted"
    assert any(item.get("to_status") == "failed" for item in events)


def test_quality_profile_emits_hard_failures_and_catalog_eligibility() -> None:
    digest = "a" * 64
    document = ResearchDocument(
        paper_id="quality-paper",
        source_hash=digest,
        title="Quality paper",
        abstract="A complete abstract.",
        sections=[
            ResearchSection(
                section_id=f"section-{index}",
                title=f"Section {index}",
                text=("body " * 700),
                source_ref=f"paper://quality-paper/section-{index}",
            )
            for index in range(3)
        ],
        lineage=SourceLineage(
            source_refs=["paper://quality-paper"],
            source_hash=digest,
        ),
    )

    report = _quality_report(document, profile="catalog")

    assert report["passed"] is True
    assert report["catalogEligible"] is True
    assert report["hardFailures"] == []
    assert report["thresholds"]["body_chars_min"] == 3000
    assert report["qualityPolicyVersion"] == "research-document-quality-policy/v1"
    assert report["dimensions"]["execution"]["status"] == "completed"
    assert report["dimensions"]["structure_and_source"]["status"] == "unknown"
    assert report["dimensions"]["content"]["status"] == "passed"


def test_quality_profile_treats_missing_abstract_as_diagnostic_and_metadata_allows_external_id() -> None:
    digest = "b" * 64
    document = ResearchDocument(
        paper_id="metadata-quality-paper",
        source_hash=digest,
        title="Metadata quality paper",
        sections=[
            ResearchSection(
                section_id=f"section-{index}",
                title=f"Section {index}",
                text=("body " * 700),
                source_ref=f"paper://metadata-quality-paper/section-{index}",
            )
            for index in range(3)
        ],
        lineage=SourceLineage(
            source_refs=["paper://metadata-quality-paper"],
            source_hash=digest,
        ),
        metadata={"doi": "10.1000/quality"},
    )

    metadata = _quality_report(document, profile="metadata")
    reading = _quality_report(document, profile="reading")

    assert metadata["passed"] is True
    assert reading["passed"] is True
    assert reading["diagnostics"] == [{"code": "abstract_missing", "severity": "info"}]


def test_parse_replays_completed_run_without_resolving_or_parsing_again() -> None:
    repository = InMemoryResearchCatalogRepository()
    parser = _Parser()
    sink = InMemoryResearchEventSink()
    use_case = ParsePaperUseCase(
        source_resolver=_Resolver(
            source_url="https://publisher.example/replay",
            paper_id="replay-paper",
            content=b"replay source",
        ),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        document_parser=parser,
        event_sink=sink,
    )
    request = ParsePaperRequest(source="https://publisher.example/replay", run_id="replay-run")

    first = use_case.parse(request)
    replay = use_case.parse(request)

    assert first.idempotent is False
    assert replay.idempotent is True
    assert replay.paper_id == first.paper_id
    assert parser.calls == 1
    assert replay.diagnostics == [{"code": "run_replayed", "run_id": "replay-run"}]


def test_parse_does_not_hide_intent_or_final_marker_failures() -> None:
    class _IntentFailure:
        def create_run_intent(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            raise OSError("intent store unavailable")

        def append(self, run_id: str, event: dict) -> None:
            raise AssertionError("append must not occur after intent failure")

    with pytest.raises(ParsePaperError) as intent_error:
        ParsePaperUseCase(event_sink=_IntentFailure()).parse(
            ParsePaperRequest(source="https://publisher.example/intent")
        )
    assert intent_error.value.code == "run_intent_persist_failed"

    class _FinalFailure:
        def append(self, run_id: str, event: dict) -> None:
            return None

        def finalize(self, run_id: str, payload: dict) -> None:
            raise OSError("final store unavailable")

    with pytest.raises(ParsePaperError) as final_error:
        ParsePaperUseCase(event_sink=_FinalFailure()).parse(
            ParsePaperRequest(source="https://publisher.example/final")
        )
    assert final_error.value.code == "run_final_persist_failed"


def test_refresh_appends_an_immutable_snapshot_for_unchanged_source() -> None:
    repository = InMemoryResearchCatalogRepository()
    use_case = ParsePaperUseCase(
        source_resolver=MetadataOnlySourceResolver(),
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
    )
    first = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/refresh",
            source_type="publisher",
            run_id="refresh-run-1",
            tenant_id="tenant-a",
        )
    )
    refreshed = use_case.parse(
        ParsePaperRequest(
            source="https://publisher.example/refresh",
            source_type="publisher",
            run_id="refresh-run-2",
            tenant_id="tenant-a",
            options={"refresh": True},
        )
    )

    assert refreshed.source_snapshots[0].snapshot_id != first.source_snapshots[0].snapshot_id
    assert refreshed.idempotent is False
    assert len(repository.list_for_paper(first.paper_id, actor_scope={"tenant_id": "tenant-a"})) == 2

class _LocalResolver:
    def __init__(self, sources: dict[str, tuple[str, bytes]]) -> None:
        self.sources = sources

    def resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
        title, content = self.sources[request.source]
        digest = __import__("hashlib").sha256(content).hexdigest()
        provisional_id = f"path-paper-{__import__('hashlib').sha256(request.source.encode()).hexdigest()[:12]}"
        snapshot_id = f"snapshot-{__import__('hashlib').sha256((request.source + digest).encode()).hexdigest()[:16]}"
        paper = ResearchPaper(
            paper_id=provisional_id,
            title=title,
            source="local",
            metadata=dict(request.metadata),
        )
        snapshot = ResearchSourceSnapshot(
            snapshot_id=snapshot_id,
            paper_id=provisional_id,
            source_type="local",
            external_id=Path(request.source).name,
            content_type="application/pdf",
            source_hash=digest,
            checksum=digest,
            fetched_at=datetime.now(UTC),
            lineage=SourceLineage(source_refs=[request.source], source_hash=digest),
            metadata={"source_locator": request.source},
        )
        source = PaperSourceRecord(
            source_id=snapshot_id,
            paper_id=provisional_id,
            source_type="local",
            source_url=f"source://local/{snapshot_id}",
            fetched_at=snapshot.fetched_at,
            source_hash=digest,
            metadata=dict(request.metadata),
        )
        return ResolvedPaperSource(
            paper=paper,
            snapshot=snapshot,
            content=content,
            content_type="application/pdf",
            source_record=source,
        )


def _local_use_case(
    repository: InMemoryResearchCatalogRepository,
    resolver: _LocalResolver,
    parser: _Parser,
) -> ParsePaperUseCase:
    return ParsePaperUseCase(
        source_resolver=resolver,
        paper_repository=repository,
        identity_repository=repository,
        source_snapshot_repository=repository,
        document_repository=repository,
        evidence_repository=repository,
        document_parser=parser,
    )


def test_local_content_identity_ignores_path_and_title_but_preserves_provenance() -> None:
    content = b"same verified local bytes"
    repository = InMemoryResearchCatalogRepository()
    parser = _Parser()
    use_case = _local_use_case(
        repository,
        _LocalResolver(
            {
                "file:///F/first.pdf": ("first.pdf", content),
                "file:///G/renamed.pdf": ("renamed.pdf", content),
            }
        ),
        parser,
    )
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}

    first = use_case.parse(
        ParsePaperRequest(
            source="file:///F/first.pdf",
            source_type="local",
            run_id="local-same-1",
            actor_scope=scope,
            metadata={
                "import_id": "imp_first",
                "import_filename": "first.pdf",
                "title_source": "import_filename",
                "source_revision": "revision-1",
                "source_fingerprint": f"sha256:{__import__('hashlib').sha256(content).hexdigest()}",
            },
        )
    )
    second = use_case.parse(
        ParsePaperRequest(
            source="file:///G/renamed.pdf",
            source_type="local",
            run_id="local-same-2",
            actor_scope=scope,
            metadata={
                "import_id": "imp_second",
                "import_filename": "renamed.pdf",
                "title_source": "import_filename",
                "source_revision": "revision-2",
                "source_fingerprint": f"sha256:{__import__('hashlib').sha256(content).hexdigest()}",
            },
        )
    )

    assert first.paper_id == second.paper_id
    assert parser.calls == 1
    assert first.source_snapshots[0].snapshot_id != second.source_snapshots[0].snapshot_id
    assert second.document is not None
    assert second.document.metadata["source_revision"] == "revision-1"
    identity = repository.get_identity(first.paper_id, actor_scope=scope)
    assert identity is not None
    assert identity.metadata["identity_basis"] == "owner_scoped_local_content_sha256"
    assert set(identity.source_snapshot_refs) == {
        first.source_snapshots[0].snapshot_id,
        second.source_snapshots[0].snapshot_id,
    }
    snapshots = repository.list_for_paper(first.paper_id, actor_scope=scope)
    assert {snapshot.metadata["source_locator"] for snapshot in snapshots} == {
        "file:///F/first.pdf",
        "file:///G/renamed.pdf",
    }


def test_local_content_identity_separates_different_bytes_and_owners() -> None:
    repository = InMemoryResearchCatalogRepository()
    parser = _Parser()
    first_bytes = b"first local content"
    second_bytes = b"second local content"
    use_case = _local_use_case(
        repository,
        _LocalResolver(
            {
                "file:///F/paper.pdf": ("paper.pdf", first_bytes),
                "file:///G/paper.pdf": ("paper.pdf", second_bytes),
                "file:///H/copy.pdf": ("copy.pdf", first_bytes),
            }
        ),
        parser,
    )
    alice_scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    bob_scope = {"user_id": "bob", "memory_namespace": "research:user:bob"}

    alice_first = use_case.parse(ParsePaperRequest(
        source="file:///F/paper.pdf", source_type="local", run_id="alice-first", actor_scope=alice_scope
    ))
    alice_second = use_case.parse(ParsePaperRequest(
        source="file:///G/paper.pdf", source_type="local", run_id="alice-second", actor_scope=alice_scope
    ))
    bob_copy = use_case.parse(ParsePaperRequest(
        source="file:///H/copy.pdf", source_type="local", run_id="bob-copy", actor_scope=bob_scope
    ))

    assert alice_first.paper_id != alice_second.paper_id
    assert alice_first.paper_id != bob_copy.paper_id
    assert repository.get_document(alice_first.paper_id, actor_scope=bob_scope) is None
    assert repository.get_identity(alice_first.paper_id, actor_scope=bob_scope) is None
    assert parser.calls == 3


def test_quality_policy_separates_recovered_execution_from_document_quality() -> None:
    digest = "c" * 64
    document = ResearchDocument(
        paper_id="recovered-quality-paper",
        source_hash=digest,
        title="Recovered paper",
        authors=["Author"],
        lineage=SourceLineage(source_refs=["paper://recovered"], source_hash=digest),
        metadata={"parser_attempts": [
            {"backend": "primary", "status": "parse_error"},
            {"backend": "fallback", "status": "success"},
        ]},
    )

    report = _quality_report(document, profile="metadata")

    assert report["documentStatus"] == "parsed"
    assert report["dimensions"]["execution"]["status"] == "recovered"
    assert report["dimensions"]["execution"]["failed_attempt_count"] == 1
    assert report["dimensions"]["structure_and_source"]["status"] == "unknown"


def test_quality_policy_quarantines_fidelity_source_identity_mismatch() -> None:
    digest = "d" * 64
    fidelity = DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v1",
        source_hash="different-source",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:content",
        pages=(DocumentPage(
            pdf_page_number=1,
            width=612,
            height=792,
            dimension_unit="pt",
        ),),
    )
    document = ResearchDocument(
        paper_id="mismatched-quality-paper",
        source_hash=digest,
        title="Mismatched paper",
        authors=["Author"],
        lineage=SourceLineage(source_refs=["paper://mismatch"], source_hash=digest),
        fidelity=fidelity,
    )

    report = _quality_report(document, profile="metadata")

    assert report["documentStatus"] == "quarantined"
    assert report["parseStatus"] == "quarantined"
    assert report["passed"] is False
    assert {item["code"] for item in report["hardFailures"]} >= {"fidelity_source_hash_mismatch"}
    assert report["dimensions"]["structure_and_source"]["status"] == "quarantined"


def test_quality_policy_degrades_when_marker_source_fidelity_or_formula_is_missing() -> None:
    digest = "e" * 64
    base = ResearchDocument(
        paper_id="incomplete-marker-paper",
        source_hash=digest,
        title="Incomplete Marker paper",
        authors=["Author"],
        lineage=SourceLineage(source_refs=["paper://incomplete"], source_hash=digest),
        metadata={"fidelity_status": "unavailable", "fidelity_unavailable_reason": "unmapped block",
                  "parser_warning_codes": [{"code": "formula_source_missing", "block_index": 4}]},
    )

    report = _quality_report(base, profile="metadata")

    codes = {item["code"] for item in report["hardFailures"]}
    assert report["passed"] is False
    assert report["documentStatus"] == "degraded"
    assert report["dimensions"]["structure_and_source"]["status"] == "degraded"
    assert codes >= {"source_fidelity_unavailable", "formula_source_missing"}
    assert report["dimensions"]["content"]["missing_formula_source_count"] == 1
