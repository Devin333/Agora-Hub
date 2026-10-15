from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.catalog import ResearchPaperCatalogService
from backend.research.application.parse_paper import ParsePaperError, ParsePaperUseCase
from backend.research.application.single_paper_runtime import ResearchAnalysisResult
from backend.research.document.cascade_parser import (
    CascadeArxivDocumentParser,
    SelectableTextPdfDocumentParser,
)
from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.format_router import MultiFormatDocumentParser
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy
from infrastructure.research.catalog_store import (
    FilesystemResearchCatalogStore,
    FilesystemResearchEventSink,
)
from infrastructure.research.document_repository import FilesystemResearchDocumentRepository
from infrastructure.research.filesystem_run_store import (
    RESEARCH_RUN_RECORD_SCHEMA_VERSION,
    RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    FilesystemResearchRunStore,
)
from infrastructure.research.source_resolver import ResearchSourceResolverAdapter, _local_path
from interfaces.composition.research_settings import ResearchLocalRuntimeSettings
from framework.harness import ContextGraphIdentity
from framework.shared.windows_paths import resolve_storage_path
from interfaces.services.research_service import (
    ResearchApplicationService,
    ResearchAskInput,
    ResearchServiceError,
)


class LocalOnlyResearchSourceResolver(ResearchSourceResolverAdapter):
    """Reuse local source identity/bytes handling without remote adapters.

    Only the local branch of the existing resolver is admitted. Its required
    state is initialized here so the general resolver's default Arxiv connector
    and rate limiter are never constructed in a null-LLM desktop process.
    """

    def __init__(self, settings: ResearchLocalRuntimeSettings) -> None:
        self._local_root = settings.local_authorized_root
        self._fetch_policy = SourceFetchPolicy(
            max_bytes=settings.parser.max_document_bytes,
            user_agent="AgoraHub/1.0 (local-research-import)",
            retry_times=0,
        )

    def resolve(self, request: Any) -> Any:
        source_type = str(getattr(request, "source_type", None) or "").strip().casefold()
        if source_type != "local":
            raise ParsePaperError(
                "source_denied",
                "Local-only Research runtime accepts imported local documents only",
                status_code=503,
                retryable=False,
            )
        scope = AskPaperUseCase().resolve_actor_scope(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            memory_namespace=request.memory_namespace,
        )
        source = str(request.content_ref or request.source).strip()
        path = _local_path(source)
        owner_root = resolve_storage_path(self._local_root / (scope.user_id or ""))
        if (
            not scope.user_id
            or not owner_root.is_relative_to(self._local_root)
            or not path.is_relative_to(owner_root)
        ):
            raise ParsePaperError(
                "source_denied",
                "Local document is outside the authenticated import authority",
                status_code=403,
                retryable=False,
            )
        return self._resolve_local(source, request)


class _LocalReadingApplicationService(ResearchApplicationService):
    """Local capability projection; generation remains explicitly unavailable."""

    def capability_projection(self) -> dict[str, bool]:
        return {**super().capability_projection(), "ask": False}

    def ask_paper(
        self,
        paper_id: str,
        request: ResearchAskInput,
        *,
        graph_identity: ContextGraphIdentity | None = None,
    ) -> dict[str, Any]:
        raise ResearchServiceError(
            "research_runtime_unavailable",
            "Research question answering is disabled in local-only mode",
            status_code=503,
            retryable=False,
            user_action_required=True,
        )


def build_local_research_service(
    settings: ResearchLocalRuntimeSettings,
) -> ResearchApplicationService:
    """Compose deterministic local import and persisted-paper reading only."""

    if not isinstance(settings, ResearchLocalRuntimeSettings):
        raise TypeError("settings must be ResearchLocalRuntimeSettings")

    catalog_root = settings.research_root / "paper-catalog"
    catalog_store = FilesystemResearchCatalogStore(catalog_root)
    documents = FilesystemResearchDocumentRepository(catalog_store)
    catalog_service = ResearchPaperCatalogService(
        catalog_repository=catalog_store,
        identity_repository=catalog_store,
        relation_repository=catalog_store,
        source_snapshot_repository=catalog_store,
        paper_repository=catalog_store,
        document_repository=documents,
        evidence_repository=catalog_store,
        code_profile_repository=catalog_store,
        sota_claim_repository=catalog_store,
    )
    pdf_parser = SelectableTextPdfDocumentParser()
    parse_use_case = ParsePaperUseCase(
        source_resolver=LocalOnlyResearchSourceResolver(settings),
        paper_repository=catalog_store,
        identity_repository=catalog_store,
        source_snapshot_repository=catalog_store,
        document_repository=documents,
        evidence_repository=catalog_store,
        document_parser=MultiFormatDocumentParser(
            arxiv_parser=CascadeArxivDocumentParser(pdf_parser=pdf_parser),
            pdf_parser=pdf_parser,
        ),
        artifact_store=catalog_store,
        event_sink=FilesystemResearchEventSink(catalog_root),
        catalog_projection=catalog_service,
        chunker=PaperDocumentChunker(),
        chunk_manifest=ChunkManifestManager(catalog_root / "chunk-manifests"),
    )
    run_store = FilesystemResearchRunStore(
        settings.run_store.root,
        result_decoder=ResearchAnalysisResult.from_dict,
        max_record_bytes=settings.run_store.max_record_bytes,
        write_schema_version=_run_store_schema_version(settings.run_store.write_schema_version),
        supported_schema_versions=tuple(
            _run_store_schema_version(version)
            for version in settings.run_store.supported_schema_versions
        ),
    )
    return _LocalReadingApplicationService(
        ask_use_case=AskPaperUseCase(),
        run_store=run_store,
        artifact_reader=catalog_store,
        parse_use_case=parse_use_case,
        catalog_service=catalog_service,
        runtime_config_identity=_local_runtime_identity(settings),
    )


def _run_store_schema_version(version: str) -> str:
    normalized = str(version).strip().casefold()
    if normalized == "v1":
        return RESEARCH_RUN_RECORD_SCHEMA_VERSION
    if normalized == "v2":
        return RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2
    raise ValueError("unsupported local Research run-store schema")


def _local_runtime_identity(settings: ResearchLocalRuntimeSettings) -> str:
    payload = {
        "mode": "local-only",
        "researchRoot": str(settings.research_root),
        "authorizedRoot": str(settings.local_authorized_root),
        "parser": "selectable-text-pdf",
        "runStoreRoot": str(settings.run_store.root),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = ["LocalOnlyResearchSourceResolver", "build_local_research_service"]
