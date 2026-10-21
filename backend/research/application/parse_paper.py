from __future__ import annotations

import inspect
import logging
import mimetypes
import os
from types import ModuleType
from time import monotonic
from threading import RLock
from hashlib import sha256
from pathlib import Path
import json
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from backend.foundation import PrimitiveModel, canonicalize_url
from backend.research.domain.catalog import (
    ResearchPaperCatalogEntry,
    ResearchPaperIdentity,
    ResearchSourceSnapshot,
    ResearchSourceType,
    build_local_content_paper_id,
    build_paper_identity_fingerprint,
)
from backend.research.domain.common import SourceLineage, require_text, stable_research_id, unique_texts
from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.domain.document_fidelity import DocumentFidelity
from backend.research.domain.evidence import ResearchEvidencePack
from backend.research.domain.paper import PaperSourceRecord, ResearchPaper
from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.source_format import SourceFormat, detect_source_format
from backend.research.ports.artifact_store import ResearchArtifactStorePort
from backend.research.ports.catalog import (
    ResearchPaperIdentityRepository,
    ResearchSourceSnapshotRepository,
)
from backend.research.ports.document_parser import DocumentParserPort
from backend.research.ports.paper_ingest import (
    ResearchDocumentRepository,
    ResearchEvidencePackRepository,
    ResearchEventSink,
    ResearchPaperReadRepository,
)
from backend.research.ports.source_resolver import (
    ResolvedPaperSource,
    ResearchSourceResolver,
)
from backend.research.services.evidence_builder import ResearchEvidenceBuilder


LOGGER = logging.getLogger(__name__)

ParseStatus = str
PARSE_OPTION_KEYS = frozenset(
    {
        "parser_backend",
        "quality_profile",
        "refresh",
        "include_code",
        "include_catalog",
        "include_chunks",
        "include_evidence",
        "max_attempts",
        "timeout_seconds",
    }
)
PARSE_OPTION_MAX_ATTEMPTS = 5
PARSE_OPTION_MAX_TIMEOUT_SECONDS = 600
_LOCAL_IMPORT_PROVENANCE_KEYS = (
    "import_id",
    "import_filename",
    "title_source",
    "source_revision",
    "source_fingerprint",
)
PARSE_QUALITY_PROFILES = frozenset({"metadata", "reading", "catalog"})
PARSE_STATUSES = frozenset(
    {
        "received",
        "resolving",
        "metadata_only",
        "parsing",
        "parsed",
        "degraded",
        "catalog_partial",
        "catalog_ready",
        "failed",
    }
)


class ParsePaperError(RuntimeError):
    """Stable application error for a paper parse operation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = dict(details or {})


class ParsePaperRequest(PrimitiveModel):
    """Input DTO shared by HTTP, CLI and direct application callers."""

    source: str
    source_url: str | None = None
    source_type: ResearchSourceType | None = None
    content_ref: str | None = None
    run_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    memory_namespace: str | None = None
    actor_scope: dict[str, str] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "source_url", "content_ref", "run_id", "tenant_id", "user_id", "memory_namespace")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @model_validator(mode="after")
    def _validate_source(self) -> "ParsePaperRequest":
        if not self.source:
            raise ValueError("source is required")
        if self.source_url and not _source_descriptors_equal(self.source, self.source_url):
            raise ValueError("source and source_url must identify the same source")
        return self


@runtime_checkable
class ResearchCatalogProjection(Protocol):
    def refresh_from_parse(
        self,
        *,
        paper: ResearchPaper,
        identity: ResearchPaperIdentity,
        snapshot: ResearchSourceSnapshot,
        document: ResearchDocument | None,
        evidence_pack: ResearchEvidencePack | None,
        actor_scope: Mapping[str, str],
        run_id: str | None = None,
        include_code: bool = True,
        catalog_eligible: bool = True,
    ) -> Any: ...


class MetadataOnlySourceResolver:
    """Safe fallback resolver used when no infrastructure adapter is wired.

    It creates a real identity and snapshot, but never invents full-text bytes.
    """

    def resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
        source = request.source.strip()
        source_type = request.source_type or infer_source_type(source)
        canonical = _canonical_source(source)
        external_id = _external_id(source, source_type)
        metadata = {**dict(request.metadata), **dict(request.options)}
        explicit_paper_id = _explicit_paper_id(metadata)
        paper_id = stable_research_id(
            "paper",
            source_type,
            external_id or canonical or source,
        )
        paper_id = explicit_paper_id or paper_id
        title = str(metadata.get("title") or external_id or source.rsplit("/", 1)[-1] or "Untitled paper")
        authors = _as_text_list(metadata.get("authors"))
        paper = ResearchPaper(
            paper_id=paper_id,
            title=title,
            authors=authors,
            abstract=str(metadata.get("abstract") or ""),
            published_at=_parse_datetime(metadata.get("published_at")),
            source=source_type,
            source_url=(canonical or (source if not source.startswith("file://") else None)),
            pdf_url=(str(metadata["pdf_url"]) if metadata.get("pdf_url") else None),
            code_url=(str(metadata["code_url"]) if metadata.get("code_url") else None),
            topics=_as_text_list(metadata.get("topics")),
            metadata={**metadata, "source_resolution": "metadata_only_fallback"},
        )
        source_ref = canonical or f"source://{source_type}/{external_id or paper_id}"
        lineage = SourceLineage(source_refs=[source_ref], metadata=dict(request.actor_scope))
        snapshot = ResearchSourceSnapshot(
            snapshot_id=stable_research_id("source_snapshot", paper_id, source_ref),
            paper_id=paper_id,
            source_type=source_type,
            canonical_url=canonical or None,
            external_id=external_id,
            content_type=str(metadata.get("content_type") or mimetypes.guess_type(source)[0] or "text/html"),
            fetched_at=datetime.now(UTC),
            access_status="metadata_only",
            lineage=lineage,
            metadata={"reason": "source_resolver_not_configured"},
        )
        return ResolvedPaperSource(
            paper=paper,
            snapshot=snapshot,
            content=None,
            content_type=snapshot.content_type,
            access_status="metadata_only",
            diagnostics=(
                {
                    "code": "source_resolver_not_configured",
                    "message": "full text source adapter is not configured",
                },
            ),
        )


class InMemoryResearchEventSink:
    """Deterministic event sink for tests and local diagnostics."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._intents: dict[tuple[str, str], dict[str, Any]] = {}
        self._finals: dict[tuple[str, str], dict[str, Any]] = {}

    def append(self, run_id: str, event: dict[str, Any]) -> None:
        self.events.append({"run_id": run_id, **dict(event)})

    def create_run_intent(self, run_id: str, *, request_fingerprint: str, actor_scope: Mapping[str, str], budget: Mapping[str, Any] | None = None) -> None:
        self.acquire_run_lease(
            run_id,
            request_fingerprint=request_fingerprint,
            actor_scope=actor_scope,
            budget=budget,
        )

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        request_fingerprint: str,
        actor_scope: Mapping[str, str],
        budget: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scope = _scope_key(actor_scope)
        key = (run_id, scope)
        existing = self._intents.get(key)
        if existing is not None:
            if existing["request_fingerprint"] != request_fingerprint:
                raise RuntimeError("research run intent conflicts with existing run")
            final = self._finals.get(key)
            if final is not None:
                return {"state": "completed", "final": dict(final)}
            return {"state": "in_progress", "last_committed_phase": "received", "retry_after_seconds": 5}
        intent = {
            "run_id": run_id,
            "request_fingerprint": request_fingerprint,
            "actor_scope": dict(actor_scope),
            "budget": dict(budget or {}),
        }
        self._intents[key] = intent
        self.events.append(
            {
                "run_id": run_id,
                "event_id": f"{run_id}:intent",
                "event_type": "research_parse_run_intent",
                "status": "received",
                "request_fingerprint": request_fingerprint,
                "actor_scope": dict(actor_scope),
                "budget": dict(budget or {}),
            }
        )
        return {"state": "owner", "run_id": run_id}

    def load_final(
        self, run_id: str, *, actor_scope: Mapping[str, str],
    ) -> dict[str, Any] | None:
        value = self._finals.get((run_id, _scope_key(actor_scope)))
        return dict(value) if value is not None else None

    def finalize(self, run_id: str, payload: Mapping[str, Any]) -> None:
        key = (run_id, _scope_key(payload.get("actor_scope") if isinstance(payload.get("actor_scope"), Mapping) else {}))
        existing = self._finals.get(key)
        record = {"run_id": run_id, **dict(payload)}
        if existing is not None:
            if existing != record:
                raise RuntimeError("research final result conflicts with existing run")
            return
        self._finals[key] = record
        self.events.append(
            {
                "run_id": run_id,
                "event_id": f"{run_id}:final",
                "event_type": "research_parse_final_result",
                **dict(payload),
            }
        )


def _parse_cache_fingerprint(
    parser: Any,
    compiler: Any,
    chunker: Any,
    options: Mapping[str, Any],
    content_type: str,
) -> str | None:
    """Fingerprint the implementation and inputs that shape stored parse/chunk output.

    Missing source identity fails closed: legacy documents or opaque runtime components
    are reparsed rather than silently reused across a potentially changed pipeline.
    """
    components = [parser, compiler, chunker]
    parser_configuration = _known_document_parser_configuration(parser)
    if parser_configuration is None:
        return None
    modules: dict[str, str] = {}
    pending = [component for component in components if component is not None]
    visited: set[int] = set()
    while pending:
        component = pending.pop()
        if id(component) in visited:
            continue
        visited.add(id(component))
        cls = component if inspect.isclass(component) else type(component)
        module_name = str(getattr(cls, "__module__", ""))
        try:
            source_path = Path(inspect.getfile(cls))
            source_digest = sha256(source_path.read_bytes()).hexdigest()
        except (OSError, TypeError):
            return None
        modules[module_name] = source_digest
        # Include directly imported local implementation helpers as well as the
        # owning class module: adapters such as Marker delegate geometry/table
        # conversion to sibling modules whose edits also change stored output.
        implementation_module = inspect.getmodule(cls)
        for value in getattr(implementation_module, "__dict__", {}).values():
            dependency = value if isinstance(value, ModuleType) else inspect.getmodule(value)
            dependency_name = str(getattr(dependency, "__name__", ""))
            if not dependency_name.startswith(("backend.", "framework.")):
                continue
            try:
                dependency_path = Path(inspect.getfile(dependency))
                dependency_digest = sha256(dependency_path.read_bytes()).hexdigest()
            except (OSError, TypeError):
                continue
            modules[dependency_name] = dependency_digest
        # Parser cascades and format routers own nested parser instances. Include
        # those implementation modules so changing a child invalidates the cache.
        attributes = getattr(component, "__dict__", {})
        for value in attributes.values():
            pending.extend(_nested_parser_components(value))
    try:
        for schema_type in (ResearchDocument, DocumentFidelity):
            schema_path = Path(inspect.getfile(schema_type))
            modules[schema_type.__module__] = sha256(schema_path.read_bytes()).hexdigest()
        payload = {
            "version": "research-parse-cache.v1",
            "components": dict(sorted(modules.items())),
            "parser_configuration": parser_configuration,
            "options": dict(options),
            "content_type": str(content_type),
            "document_schema": "research-document.v1",
            "fidelity_schema": "research-document-fidelity.v1",
            "runtime_config": {
                key: value
                for key, value in sorted(os.environ.items())
                if key.startswith((
                    "NEWSROOM_MARKER_",
                    "NEWSROOM_PDF_PARSER_",
                    "NEWSROOM_PARSER_QUALITY_",
                ))
            },
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return sha256(encoded.encode("utf-8")).hexdigest()



def _known_document_parser_configuration(parser: Any) -> dict[str, Any] | None:
    """Serialize declared configuration for known production document parsers.

    Runtime counters and arbitrary instance state are deliberately excluded. An
    unrecognized production document parser is opaque and cannot reuse cached
    output. Test/custom parsers remain source-fingerprinted as before.
    """
    if parser is None:
        return {"type": None}
    cls = parser if inspect.isclass(parser) else type(parser)
    module_name = str(getattr(cls, "__module__", ""))
    class_name = str(getattr(cls, "__qualname__", getattr(cls, "__name__", "")))
    if not module_name.startswith("backend.research.document."):
        return {
            "type": f"{module_name}.{class_name}",
            "configuration": "not_declared",
        }

    identity = f"{module_name}.{class_name}"
    attributes = getattr(parser, "__dict__", {})
    if (
        module_name == "backend.research.document.format_router"
        and class_name == "MultiFormatDocumentParser"
    ):
        from backend.research.document.format_router import MultiFormatDocumentParser

        if cls is not MultiFormatDocumentParser:
            return None
        arxiv_parser = _known_document_parser_configuration(attributes.get("_arxiv"))
        html_parser = _known_document_parser_configuration(attributes.get("_html"))
        pdf_parser = _known_document_parser_configuration(attributes.get("_pdf"))
        if arxiv_parser is None or html_parser is None or pdf_parser is None:
            return None
        return {
            "type": identity,
            "arxiv": arxiv_parser,
            "html": html_parser,
            "pdf": pdf_parser,
        }
    if module_name == "backend.research.document.html_parser" and class_name == "HtmlDocumentParser":
        from backend.research.document.html_parser import HtmlDocumentParser

        if cls is not HtmlDocumentParser or attributes:
            return None
        return {"type": identity}
    if module_name == "backend.research.document.cascade_parser":
        if class_name == "DocumentQualityProbe":
            names = (
                "min_sections",
                "min_body_chars",
                "min_non_empty_section_ratio",
                "max_replacement_char_ratio",
                "min_table_row_coverage_if_tables",
            )
            return {
                "type": identity,
                "thresholds": {name: getattr(parser, name) for name in names},
            }
        if class_name == "PyMuPDFTextDocumentParser":
            if "_max_pages" not in attributes or "_max_text_chars" not in attributes:
                return None
            return {
                "type": identity,
                "max_pages": attributes["_max_pages"],
                "max_text_chars": attributes["_max_text_chars"],
            }
        if class_name == "SelectableTextPdfDocumentParser":
            probe = _known_document_parser_configuration(attributes.get("_probe"))
            extractor = _known_document_parser_configuration(attributes.get("_extractor"))
            if probe is None or extractor is None:
                return None
            return {"type": identity, "probe": probe, "extractor": extractor}
        if class_name == "CascadeDocumentParser":
            probe = _known_document_parser_configuration(attributes.get("_probe"))
            fallback = _known_document_parser_configuration(attributes.get("_fallback"))
            backends = attributes.get("_backends")
            if probe is None or fallback is None or not isinstance(backends, tuple):
                return None
            ordered_backends: list[dict[str, Any]] = []
            for item in backends:
                if not isinstance(item, tuple) or len(item) != 2:
                    return None
                name, backend_parser = item
                backend_identity = _known_document_parser_configuration(backend_parser)
                if not isinstance(name, str) or backend_identity is None:
                    return None
                ordered_backends.append({"name": name, "parser": backend_identity})
            return {
                "type": identity,
                "backends": ordered_backends,
                "probe": probe,
                "fallback": fallback,
            }
        if class_name == "CascadeArxivDocumentParser":
            from backend.research.document.cascade_parser import CascadeArxivDocumentParser

            if cls is not CascadeArxivDocumentParser:
                return None
            latex_parser = attributes.get("_latex")
            latex_cls = type(latex_parser)
            if (
                getattr(latex_cls, "__module__", "")
                != "backend.research.document.latex_compiler"
                or getattr(latex_cls, "__name__", "") != "LatexSourceParser"
                or getattr(latex_parser, "__dict__", {})
            ):
                return None
            pdf_parser = _known_document_parser_configuration(attributes.get("_pdf"))
            if pdf_parser is None:
                return None
            return {
                "type": identity,
                "latex_parser": "backend.research.document.latex_compiler.LatexSourceParser",
                "pdf_parser": pdf_parser,
            }

    if (
        module_name == "backend.research.document.marker_pdf_parser"
        and class_name == "MarkerPdfDocumentParser"
    ):
        from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser

        if cls is not MarkerPdfDocumentParser:
            return None
        cache_identity = getattr(parser, "cache_identity", None)
        if not callable(cache_identity):
            return None
        try:
            declared_identity = cache_identity()
        except Exception:
            return None
        if not isinstance(declared_identity, dict):
            return None
        execution_adapter = declared_identity.get("execution_adapter")
        if not isinstance(execution_adapter, dict):
            return None
        return {"type": identity, **declared_identity}

    return None


def _nested_parser_components(value: Any) -> list[Any]:
    found: list[Any] = []
    pending = [value]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, (tuple, list, set, frozenset)):
            pending.extend(current)
        elif callable(getattr(current, "parse", None)) or callable(getattr(current, "compile", None)):
            found.append(current)
    return found


class ParsePaperUseCase:
    """Resolve, parse and persist one paper through bounded application steps."""

    def __init__(
        self,
        *,
        source_resolver: ResearchSourceResolver | None = None,
        paper_repository: ResearchPaperReadRepository | None = None,
        identity_repository: ResearchPaperIdentityRepository | None = None,
        source_snapshot_repository: ResearchSourceSnapshotRepository | None = None,
        document_repository: ResearchDocumentRepository | None = None,
        evidence_repository: ResearchEvidencePackRepository | None = None,
        document_parser: DocumentParserPort | None = None,
        document_compiler: Any | None = None,
        evidence_builder: ResearchEvidenceBuilder | None = None,
        artifact_store: ResearchArtifactStorePort | None = None,
        event_sink: ResearchEventSink | None = None,
        catalog_projection: ResearchCatalogProjection | None = None,
        chunker: PaperDocumentChunker | None = None,
        chunk_manifest: ChunkManifestManager | None = None,
        max_retries: int = 1,
    ) -> None:
        self._resolver = source_resolver or MetadataOnlySourceResolver()
        self._paper_repository = paper_repository
        self._identity_repository = identity_repository
        self._snapshot_repository = source_snapshot_repository
        self._document_repository = document_repository
        self._evidence_repository = evidence_repository
        self._parser = document_parser
        self._compiler = document_compiler
        self._evidence_builder = evidence_builder or ResearchEvidenceBuilder()
        self._artifact_store = artifact_store
        self._event_sink = event_sink
        self._catalog_projection = catalog_projection
        self._chunker = chunker
        self._chunk_manifest = chunk_manifest
        self._identity_lock = RLock()
        if isinstance(max_retries, bool) or int(max_retries) < 0 or int(max_retries) > 5:
            raise ValueError("max_retries must be between 0 and 5")
        self._max_retries = int(max_retries)

    def parse(self, request: ParsePaperRequest) -> "ParsePaperResult":
        if not isinstance(request, ParsePaperRequest):
            raise TypeError("request must be ParsePaperRequest")
        request = request.model_copy(
            update={"options": validate_parse_options(request.options)}
        )
        run_id = request.run_id or f"research-parse-{uuid4().hex}"
        actor_scope = _actor_scope(request)
        budget = _parse_budget(request.options)
        request_fingerprint = _request_fingerprint(request, actor_scope)
        budget_started = monotonic()
        diagnostics: list[dict[str, Any]] = []
        artifact_refs: list[str] = []
        chunk_manifest_ref: str | None = None
        evidence_pack_ref: str | None = None
        snapshots: list[ResearchSourceSnapshot] = []
        snapshot_reused = False
        current_status: str | None = None
        catalog_entry: ResearchPaperCatalogEntry | None = None
        catalog_status: str | None = None
        event_context: dict[str, Any] = {"actor_scope": actor_scope}
        lease = _create_run_intent(
            self._event_sink,
            run_id,
            request_fingerprint=request_fingerprint,
            actor_scope=actor_scope,
            budget=budget,
        )
        if lease is not None:
            state = str(lease.get("state") or "owner")
            if state == "completed":
                return self._replay_completed_run(
                    run_id=run_id,
                    final=lease.get("final"),
                    actor_scope=actor_scope,
                )
            if state == "recovery_required":
                raise ParsePaperError(
                    "run_recovery_required",
                    "research parse run requires operator recovery before it can continue",
                    status_code=409,
                    retryable=False,
                    details={
                        "run_id": run_id,
                        "recovery": dict(lease.get("recovery") or {}),
                    },
                )
            if state == "in_progress":
                raise ParsePaperError(
                    "parse_in_progress",
                    "an identical paper parse is already in progress",
                    status_code=409,
                    retryable=True,
                    details={
                        "run_id": run_id,
                        "last_committed_phase": lease.get("last_committed_phase"),
                        "retry_after_seconds": lease.get("retry_after_seconds", 5),
                    },
                )

        def emit(status: str, payload: Mapping[str, Any]) -> None:
            nonlocal current_status
            event_payload = {
                "from_status": current_status,
                "to_status": status,
                **event_context,
                **dict(payload),
            }
            self._emit(run_id, status, event_payload, diagnostics=diagnostics)
            current_status = status

        emit("received", {"source": request.source, "actor_scope": actor_scope})
        try:
            emit("resolving", {})
            _ensure_budget(budget_started, budget, diagnostics, phase="resolving")
            source_type = request.source_type or infer_source_type(request.source)
            if source_type == "github" and not _has_paper_context(request):
                raise ParsePaperError(
                    "github_paper_context_required",
                    "GitHub repository observation requires explicit paper context",
                    status_code=422,
                    details={"source_type": "github"},
                )
            resolved = self._resolve_with_retries(
                request,
                run_id=run_id,
                diagnostics=diagnostics,
                event_context=event_context,
                emit=emit,
                max_retries=effective_max_retries(request.options, self._max_retries),
            )
            _ensure_budget(budget_started, budget, diagnostics, phase="resolving")
            diagnostics.extend(dict(item) for item in resolved.diagnostics)
            resolved = _bind_local_content_identity(resolved, actor_scope)
            snapshot = _snapshot_for_scope(resolved.snapshot, actor_scope, resolved.content)
            snapshot = snapshot.model_copy(
                update={
                    "request_fingerprint": request_fingerprint,
                    "metadata": {
                        **dict(snapshot.metadata),
                        "request_fingerprint": request_fingerprint,
                        "resolver_version": snapshot.resolver_version or "research-source-resolver-v1",
                    },
                }
            )
            if request.options:
                snapshot = snapshot.model_copy(
                    update={
                        "metadata": {
                            **dict(snapshot.metadata),
                            "parse_options": dict(request.options),
                        },
                    }
                )
            if bool(request.options.get("refresh", False)):
                # Refresh is a new immutable observation even when the bytes
                # are unchanged. Binding the id to run_id keeps replay stable.
                parent_snapshot_id = snapshot.snapshot_id
                snapshot = snapshot.model_copy(
                    update={
                        "snapshot_id": stable_research_id(
                            "source_snapshot_refresh",
                            snapshot.paper_id,
                            snapshot.snapshot_id,
                            run_id,
                        ),
                        "metadata": {
                            **dict(snapshot.metadata),
                            "refresh": True,
                            "run_id": run_id,
                            "parent_snapshot_id": parent_snapshot_id,
                        },
                        "parent_snapshot_id": parent_snapshot_id,
                    }
                )
            existing_snapshot = _repository_get(
                self._snapshot_repository,
                snapshot.snapshot_id,
                "snapshot",
                actor_scope=actor_scope,
            ) if self._snapshot_repository is not None else None
            if isinstance(existing_snapshot, ResearchSourceSnapshot) and _same_snapshot_content(existing_snapshot, snapshot):
                # Snapshot records are immutable observations. Reusing the
                # existing row prevents a repeated fetch (whose timestamps
                # naturally differ) from mutating or conflicting with it.
                snapshot = existing_snapshot
                snapshot_reused = True
                diagnostics.append({"code": "source_snapshot_reused", "snapshot_id": snapshot.snapshot_id})
            scoped_paper = resolved.paper.model_copy(
                update={
                    "actor_scope": dict(actor_scope),
                    "metadata": {
                        **dict(resolved.paper.metadata),
                        "actor_scope": dict(actor_scope),
                        **(
                            {"source_hash": snapshot.source_hash}
                            if (snapshot.source_hash or snapshot.checksum)
                            else {}
                        ),
                    }
                }
            )
            snapshots.append(snapshot)
            event_context.update(
                {
                    "paper_id": snapshot.paper_id,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "actor_scope": actor_scope,
                }
            )
            # Identity lookup and its first persistence form one in-process
            # critical section. Filesystem repositories serialize individual
            # writes; this lock prevents concurrent imports through this runtime
            # from both observing an absent identity and dropping provenance.
            with self._identity_lock:
                paper, identity, merged = self._merge_identity(scoped_paper, snapshot, actor_scope=actor_scope)
                if snapshot.paper_id != paper.paper_id:
                    snapshot = snapshot.model_copy(
                        update={
                            "paper_id": paper.paper_id,
                            "metadata": {
                                **dict(snapshot.metadata),
                                **dict(actor_scope),
                            },
                            "lineage": snapshot.lineage.model_copy(
                                update={"metadata": {**dict(snapshot.lineage.metadata), **dict(actor_scope)}}
                            ),
                        }
                    )
                    snapshots[0] = snapshot
                # Persist only the canonical paper snapshot. Saving the resolver's
                # provisional paper id before identity merge would leave an orphan
                # snapshot whenever a DOI/arXiv/publisher source converges.
                self._save_snapshot(snapshot)
                self._save_paper(paper)
                self._save_identity(identity)
            if merged:
                diagnostics.append({"code": "identity_merged", "paper_id": paper.paper_id})
                conflicts = identity.metadata.get("conflict_diagnostics")
                if isinstance(conflicts, list):
                    diagnostics.extend(dict(item) for item in conflicts if isinstance(item, Mapping))
            emit("resolving", {"phase": "identity_resolved", "paper_id": paper.paper_id, "snapshot_id": snapshot.snapshot_id})

            if resolved.access_status in {"metadata_only", "denied", "failed"} or resolved.content is None:
                status = "metadata_only" if resolved.access_status != "failed" else "failed"
                diagnostics.append({
                    "code": "full_text_unavailable" if status == "metadata_only" else "source_fetch_failed",
                    "access_status": resolved.access_status,
                })
                if self._catalog_projection is not None and status == "metadata_only" and request.options.get("include_catalog", True):
                    try:
                        catalog_entry = _refresh_catalog_projection(
                            self._catalog_projection,
                            paper=paper,
                            identity=identity,
                            snapshot=snapshot,
                        document=None,
                        evidence_pack=None,
                        actor_scope=actor_scope,
                        run_id=run_id,
                        include_code=bool(request.options.get("include_code", True)),
                    )
                        catalog_status = _catalog_status(catalog_entry)
                    except Exception as exc:  # catalog failure must be visible, not hide source status
                        diagnostics.append({
                            "code": "catalog_projection_failed",
                            "error_type": type(exc).__name__,
                        })
                        catalog_status = "catalog_partial"
                emit(
                    status,
                    {
                        "paper_id": paper.paper_id,
                        "catalog_status": catalog_status,
                        "diagnostics": diagnostics,
                        "terminal": True,
                    },
                )
                _finalize_run(
                    self._event_sink,
                    run_id,
                    {
                        "status": status,
                        "paper_id": paper.paper_id,
                        "catalog_status": catalog_status,
                        "artifact_refs": artifact_refs,
                        "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                        "actor_scope": actor_scope,
                        "request_fingerprint": request_fingerprint,
                    },
                )
                return ParsePaperResult(
                    run_id=run_id,
                    paper_id=paper.paper_id,
                    status=status,
                    paper=paper,
                    identity=identity,
                    source_snapshots=snapshots,
                    diagnostics=diagnostics,
                    provenance=_provenance(paper, snapshots, actor_scope),
                    artifact_refs=artifact_refs,
                    idempotent=snapshot_reused or (merged and _same_checksum(snapshot, paper)),
                    catalog_entry=catalog_entry,
                    catalog_status=catalog_status,
                )

            parse_cache_fingerprint = _parse_cache_fingerprint(
                self._parser,
                self._compiler,
                self._chunker,
                request.options,
                resolved.content_type,
            )
            existing = self._existing_document(
                paper.paper_id,
                snapshot,
                parse_cache_fingerprint=parse_cache_fingerprint,
                actor_scope=actor_scope,
            )
            published_artifacts: dict[str, str] = {}
            # Documents are keyed by paper/source hash and remain immutable;
            # refresh adds a source observation but may reuse the same parsed
            # document when the bytes did not change.
            reuse_existing_document = existing is not None
            if reuse_existing_document:
                document = existing
                idempotent = not bool(request.options.get("refresh", False))
                diagnostics.append({"code": "source_checksum_reused", "source_hash": document.source_hash})
                published_artifacts = _published_artifacts(document)
            else:
                emit("parsing", {"paper_id": paper.paper_id, "content_type": resolved.content_type})
                _ensure_budget(budget_started, budget, diagnostics, phase="parsing")
                document = self._parse_document_with_retries(
                    paper,
                    snapshot,
                    resolved,
                    run_id=run_id,
                    diagnostics=diagnostics,
                    event_context=event_context,
                    emit=emit,
                    max_retries=effective_max_retries(request.options, self._max_retries),
                )
                _ensure_budget(budget_started, budget, diagnostics, phase="parsing")
                idempotent = False
            parser_attempts = _parser_attempts(document)
            quality_report = _quality_report(
                document,
                profile=str(request.options.get("quality_profile") or "metadata"),
                paper=paper,
            )
            parse_status = str(quality_report.get("parseStatus") or "parsed")
            document_status = str(quality_report.get("documentStatus") or "parsed")
            if not reuse_existing_document:
                document = document.model_copy(
                    update={
                        "status": document_status,
                        "quality_report": quality_report,
                        "metadata": {
                            **dict(document.metadata),
                            "quality_report": quality_report,
                            "degraded": document_status == "degraded",
                            "parse_cache_fingerprint": parse_cache_fingerprint,
                        },
                    }
                )
                if self._document_repository is not None:
                    self._document_repository.save(document)
            if document_status == "degraded":
                status = parse_status
                diagnostics.append({"code": "parser_degraded", "quality": quality_report})
            else:
                status = parse_status
            evidence_pack = None
            include_evidence = bool(request.options.get("include_evidence", True))
            include_chunks = bool(request.options.get("include_chunks", True))
            if include_evidence and idempotent and self._evidence_repository is not None:
                existing_evidence = _repository_get(
                    self._evidence_repository,
                    paper.paper_id,
                    "evidence",
                    actor_scope=actor_scope,
                )
                if isinstance(existing_evidence, ResearchEvidencePack):
                    evidence_pack = existing_evidence
            evidence_pack = (evidence_pack or self._build_evidence(document)) if include_evidence else None
            if evidence_pack is not None:
                evidence_pack = evidence_pack.model_copy(
                    update={
                        "actor_scope": dict(actor_scope),
                        "metadata": {**dict(evidence_pack.metadata), "actor_scope": dict(actor_scope)},
                        "lineage": evidence_pack.lineage.model_copy(
                            update={"metadata": {**dict(evidence_pack.lineage.metadata), **dict(actor_scope)}}
                        ),
                    }
                )
                if self._evidence_repository is not None and not idempotent:
                    self._evidence_repository.save(evidence_pack)
                evidence_ref = published_artifacts.get("research-evidence-pack")
                if evidence_ref is None:
                    evidence_ref = self._publish(
                        "research-evidence-pack",
                        evidence_pack.model_dump(mode="json", exclude_none=True),
                        {
                            "paper_id": paper.paper_id,
                            "source_hash": document.source_hash,
                            "actor_scope": actor_scope,
                        },
                    )
                if evidence_ref:
                    evidence_pack_ref = evidence_ref
                    artifact_refs.append(evidence_ref)
                    published_artifacts["research-evidence-pack"] = evidence_ref
            document_ref = published_artifacts.get("research-document")
            if document_ref is None:
                document_ref = self._publish(
                    "research-document",
                    document.model_dump(mode="json", exclude_none=True),
                    {
                        "paper_id": paper.paper_id,
                        "source_hash": document.source_hash,
                        "actor_scope": actor_scope,
                        "budget": budget,
                    },
                )
            if document_ref:
                artifact_refs.append(document_ref)
                published_artifacts["research-document"] = document_ref
            chunk_manifest_ref = published_artifacts.get("research-chunk-manifest") if include_chunks else None
            if include_chunks and chunk_manifest_ref is None:
                chunk_manifest_ref = self._publish_chunk_manifest(
                    paper=paper,
                    document=document,
                    actor_scope=actor_scope,
                    run_id=run_id,
                    reuse_existing=idempotent,
                )
            if chunk_manifest_ref:
                artifact_refs.append(chunk_manifest_ref)
                published_artifacts["research-chunk-manifest"] = chunk_manifest_ref
            if published_artifacts and self._document_repository is not None:
                document = document.model_copy(update={
                    "metadata": {
                        **dict(document.metadata),
                        "published_artifacts": dict(published_artifacts),
                    },
                })
                self._document_repository.save(document)
            artifact_refs = unique_texts(artifact_refs)
            event_context.update(
                {
                    "paper_id": paper.paper_id,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "artifact_refs": list(artifact_refs),
                    "actor_scope": actor_scope,
                    "budget": budget,
                }
            )
            emit(
                status,
                {
                    "paper_id": paper.paper_id,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                    "artifact_refs": artifact_refs,
                    "parser_attempts": parser_attempts,
                },
            )
            if self._catalog_projection is not None and request.options.get("include_catalog", True):
                try:
                    catalog_entry = _refresh_catalog_projection(
                        self._catalog_projection,
                        paper=paper,
                        identity=identity,
                        snapshot=snapshot,
                        document=document,
                        evidence_pack=evidence_pack,
                        actor_scope=actor_scope,
                        run_id=run_id,
                        include_code=bool(request.options.get("include_code", True)),
                        catalog_eligible=bool(quality_report.get("catalogEligible", False)),
                    )
                    catalog_status = _catalog_status(catalog_entry)
                    if catalog_status:
                        if catalog_status != status:
                            emit(catalog_status, {"paper_id": paper.paper_id, "catalog_status": catalog_status})
                        status = _aggregate_parse_status(status, catalog_status)
                except Exception as exc:  # catalog failure must be visible, not hide parse
                    diagnostics.append({"code": "catalog_projection_failed", "error_type": type(exc).__name__})
                    status = "catalog_partial"
                    emit("catalog_partial", {"paper_id": paper.paper_id, "error_type": type(exc).__name__})
            emit(
                status,
                {
                    "paper_id": paper.paper_id,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                    "artifact_refs": artifact_refs,
                    "parser_attempts": parser_attempts,
                    "catalog_status": catalog_status,
                    "terminal": True,
                },
            )
            _finalize_run(
                self._event_sink,
                run_id,
                {
                    "status": status,
                    "paper_id": paper.paper_id,
                    "catalog_status": catalog_status,
                    "artifact_refs": artifact_refs,
                    "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                    "document_id": document.document_id,
                    "document_source_hash": document.source_hash,
                    "quality_report": quality_report,
                    "actor_scope": actor_scope,
                    "budget": budget,
                    "request_fingerprint": request_fingerprint,
                },
            )
            return ParsePaperResult(
                run_id=run_id,
                paper_id=paper.paper_id,
                status=status,
                paper=paper,
                identity=identity,
                source_snapshots=snapshots,
                document=document,
                parser_attempts=parser_attempts,
                quality_report=quality_report,
                evidence_pack=evidence_pack,
                chunk_manifest_ref=chunk_manifest_ref,
                evidence_pack_ref=evidence_pack_ref,
                catalog_entry=catalog_entry,
                catalog_status=catalog_status,
                diagnostics=diagnostics,
                provenance=_provenance(paper, snapshots, actor_scope),
                artifact_refs=artifact_refs,
                idempotent=idempotent,
            )
        except ParsePaperError as exc:
            # ParsePaperError is already a sanitized application error, but
            # every terminal path still needs a durable failed transition.
            # Event persistence itself must not recurse through this branch.
            if current_status != "failed" and exc.code != "event_persist_failed":
                diagnostics.append({
                    "code": exc.code,
                    "error_type": type(exc).__name__,
                })
                try:
                    emit("failed", {"diagnostics": diagnostics, "terminal": True})
                except ParsePaperError:
                    LOGGER.warning("research parse failed event could not be appended", exc_info=True)
            _finalize_run(
                self._event_sink,
                run_id,
                {
                    "status": "failed",
                    "paper_id": event_context.get("paper_id"),
                    "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                    "diagnostics": diagnostics,
                    "actor_scope": actor_scope,
                    "request_fingerprint": request_fingerprint,
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001 - application boundary normalizes diagnostics
            diagnostics.append({"code": "parse_failed", "error_type": type(exc).__name__})
            try:
                emit("failed", {"diagnostics": diagnostics, "terminal": True})
            finally:
                _finalize_run(
                    self._event_sink,
                    run_id,
                    {
                        "status": "failed",
                        "paper_id": event_context.get("paper_id"),
                        "source_snapshot_refs": [item.snapshot_id for item in snapshots],
                        "diagnostics": diagnostics,
                        "actor_scope": actor_scope,
                        "budget": budget,
                        "request_fingerprint": request_fingerprint,
                    },
                )
            raise ParsePaperError(
                "parse_failed",
                "paper parsing failed",
                status_code=422,
                retryable=_is_retryable(exc),
                details={"run_id": run_id, "diagnostics": diagnostics},
            ) from exc

    execute = parse

    def recover_completed(self, request: ParsePaperRequest) -> "ParsePaperResult | None":
        """Read one scoped durable parse final without acquiring a lease or executing."""

        request = request.model_copy(update={"options": validate_parse_options(request.options)})
        if not request.run_id:
            raise ParsePaperError("invalid_request", "parse recovery requires run_id", status_code=400)
        actor_scope = _actor_scope(request)
        loader = getattr(self._event_sink, "load_final", None)
        if not callable(loader):
            return None
        final = loader(request.run_id, actor_scope=actor_scope)
        if final is None:
            return None
        if not isinstance(final, Mapping) or final.get("request_fingerprint") != _request_fingerprint(request, actor_scope):
            raise ParsePaperError(
                "run_recovery_invalid",
                "durable parse final does not match the original request",
                status_code=409,
            )
        result = self._replay_completed_run(
            run_id=request.run_id, final=final, actor_scope=actor_scope,
        )
        actual_hash = getattr(result.document, "source_hash", None)
        if (
            not actual_hash
            or final.get("document_source_hash") != actual_hash
            or getattr(result.document, "document_id", None) != final.get("document_id")
            or not isinstance(final.get("quality_report"), Mapping)
            or dict(getattr(result.document, "quality_report", {}) or {}) != dict(final["quality_report"])
        ):
            raise ParsePaperError(
                "run_recovery_invalid",
                "durable parse result no longer matches its exact document and quality receipt",
                status_code=409,
            )
        return result

    def _replay_completed_run(
        self,
        *,
        run_id: str,
        final: Any,
        actor_scope: Mapping[str, str],
    ) -> "ParsePaperResult":
        """Rebuild a completed parse response without fetching or parsing again."""

        if not isinstance(final, Mapping):
            raise ParsePaperError(
                "run_recovery_invalid",
                "completed research parse record is invalid",
                status_code=503,
                details={"run_id": run_id},
            )
        status = str(final.get("status") or "").strip()
        paper_id = str(final.get("paper_id") or "").strip()
        if status not in PARSE_STATUSES or status == "failed" or not paper_id:
            raise ParsePaperError(
                "run_already_finalized",
                "research parse run is already finalized",
                status_code=409,
                retryable=False,
                details={"run_id": run_id, "status": status or "failed"},
            )
        paper = _repository_get(self._paper_repository, paper_id, "paper", actor_scope=actor_scope)
        identity = _repository_get(self._identity_repository, paper_id, "identity", actor_scope=actor_scope)
        document = _repository_get(self._document_repository, paper_id, "document", actor_scope=actor_scope)
        evidence_pack = _repository_get(self._evidence_repository, paper_id, "evidence", actor_scope=actor_scope)
        snapshots: list[ResearchSourceSnapshot] = []
        for snapshot_id in final.get("source_snapshot_refs") or []:
            snapshot = _repository_get(self._snapshot_repository, str(snapshot_id), "snapshot", actor_scope=actor_scope)
            if isinstance(snapshot, ResearchSourceSnapshot):
                snapshots.append(snapshot)
        catalog_entry = None
        getter = getattr(self._catalog_projection, "get_catalog", None)
        if callable(getter):
            try:
                catalog_entry = getter(paper_id, actor_scope=dict(actor_scope))
            except Exception:
                catalog_entry = None
        artifacts = unique_texts([str(item) for item in (final.get("artifact_refs") or [])])
        return ParsePaperResult(
            run_id=run_id,
            paper_id=paper_id,
            status=status,
            paper=paper if isinstance(paper, ResearchPaper) else None,
            identity=identity if isinstance(identity, ResearchPaperIdentity) else None,
            source_snapshots=snapshots,
            document=document if isinstance(document, ResearchDocument) else None,
            parser_attempts=_parser_attempts(document) if isinstance(document, ResearchDocument) else [],
            quality_report=dict(document.quality_report) if isinstance(document, ResearchDocument) else {},
            evidence_pack=evidence_pack if isinstance(evidence_pack, ResearchEvidencePack) else None,
            chunk_manifest_ref=_artifact_ref_by_kind(artifacts, "research-chunk-manifest"),
            evidence_pack_ref=_artifact_ref_by_kind(artifacts, "research-evidence-pack"),
            catalog_entry=catalog_entry if isinstance(catalog_entry, ResearchPaperCatalogEntry) else None,
            catalog_status=str(final.get("catalog_status") or "").strip() or None,
            artifact_refs=artifacts,
            diagnostics=[{"code": "run_replayed", "run_id": run_id}],
            provenance=_provenance(
                paper if isinstance(paper, ResearchPaper) else ResearchPaper(paper_id=paper_id, title=paper_id),
                snapshots,
                actor_scope,
            ),
            idempotent=True,
        )

    def _resolve(self, request: ParsePaperRequest) -> ResolvedPaperSource:
        resolver = self._resolver
        method = getattr(resolver, "resolve", None)
        if not callable(method):
            raise ParsePaperError("source_resolver_invalid", "source resolver is not configured", status_code=503)
        value = method(request)
        if inspect.isawaitable(value):
            raise ParsePaperError("source_resolver_async", "ParsePaper requires a bounded synchronous resolver", status_code=503)
        if not isinstance(value, ResolvedPaperSource):
            raise TypeError("source resolver must return ResolvedPaperSource")
        return value

    def _resolve_with_retries(
        self,
        request: ParsePaperRequest,
        *,
        run_id: str,
        diagnostics: list[dict[str, Any]],
        event_context: Mapping[str, Any],
        emit: Any,
        max_retries: int | None = None,
    ) -> ResolvedPaperSource:
        """Retry only resolver failures that explicitly opt into retry.

        Source adapters normally own network retry policy. This outer bounded
        loop covers injected transports and adapter lifecycle failures while
        keeping the application run replayable through durable phase events.
        Metadata-only responses are successful resolutions and are never
        retried here.
        """

        retry_budget = self._max_retries if max_retries is None else max(0, int(max_retries))
        for attempt in range(retry_budget + 1):
            try:
                return self._resolve(request)
            except Exception as exc:
                retryable = isinstance(exc, ParsePaperError) and exc.retryable
                retryable = retryable or _is_retryable(exc)
                if not retryable or attempt >= retry_budget:
                    raise
                retry_number = attempt + 1
                diagnostic = {
                    "code": "source_retry_scheduled",
                    "attempt": retry_number,
                    "max_retries": retry_budget,
                    "error_type": type(exc).__name__,
                }
                diagnostics.append(diagnostic)
                emit(
                    "resolving",
                    {
                        "phase": "retry_scheduled",
                        "attempt": retry_number,
                        "max_retries": retry_budget,
                        "diagnostics": [diagnostic],
                        "run_id": run_id,
                        **dict(event_context),
                    },
                )

    def _merge_identity(
        self,
        paper: ResearchPaper,
        snapshot: ResearchSourceSnapshot,
        *,
        actor_scope: Mapping[str, str] | None = None,
    ) -> tuple[ResearchPaper, ResearchPaperIdentity, bool]:
        identity = identity_from_paper(paper, snapshot, actor_scope=actor_scope)
        existing = None
        if self._identity_repository is not None:
            # Refresh callers may carry an explicit canonical paper id (for
            # example, when observing a GitHub repository). Resolve that id
            # before external/fingerprint matching so a repository snapshot
            # cannot create a parallel paper identity.
            existing = _repository_get(
                self._identity_repository,
                paper.paper_id,
                "identity",
                actor_scope=actor_scope,
            )
            for external_id in _identity_external_ids(identity):
                if existing is not None:
                    break
                existing = _repository_find_external(
                    self._identity_repository,
                    external_id,
                    actor_scope=actor_scope,
                )
                if existing is not None:
                    break
            if existing is None and identity.canonical_url:
                existing = _repository_find_external(
                    self._identity_repository,
                    identity.canonical_url,
                    actor_scope=actor_scope,
                )
            if existing is None and identity.fingerprint and not _is_local_content_identity(snapshot):
                existing = _repository_find_fingerprint(
                    self._identity_repository,
                    identity.fingerprint,
                    actor_scope=actor_scope,
                )
        if existing is None:
            return paper, identity, False
        if existing.paper_id == paper.paper_id:
            existing_paper = _repository_get(
                self._paper_repository,
                existing.paper_id,
                "paper",
                actor_scope=actor_scope,
            )
            if isinstance(existing_paper, ResearchPaper):
                # An observation refresh (notably GitHub code metadata) can
                # use the canonical paper id while carrying a repository
                # title. Preserve the paper's authoritative bibliographic
                # fields and merge only additive source metadata.
                incoming_metadata = dict(paper.metadata)
                existing_metadata = dict(existing_paper.metadata)
                merged_metadata = {
                    **existing_metadata,
                    **incoming_metadata,
                    "actor_scope": dict(actor_scope or {}),
                }
                for key in ("code_urls", "topics"):
                    values = unique_texts([
                        *(_as_text_list(existing_metadata.get(key))),
                        *(_as_text_list(incoming_metadata.get(key))),
                    ])
                    if values:
                        merged_metadata[key] = values
                paper = existing_paper.model_copy(update={
                    "metadata": merged_metadata,
                    "code_url": existing_paper.code_url or paper.code_url,
                    "pdf_url": existing_paper.pdf_url or paper.pdf_url,
                    "topics": unique_texts([*existing_paper.topics, *paper.topics]),
                    "authors": unique_texts([*existing_paper.authors, *paper.authors]),
                    "abstract": existing_paper.abstract or paper.abstract,
                    "source_url": existing_paper.source_url or paper.source_url,
                    "actor_scope": dict(actor_scope or {}),
                })
            return paper, _merge_identities(existing, identity), True
        merged_identity = _merge_identities(existing, identity)
        merged_paper = paper.model_copy(
            update={
                "paper_id": existing.paper_id,
                "metadata": {
                    **dict(paper.metadata),
                    "actor_scope": dict(actor_scope or {}),
                    "identity_merge": {
                        "matched_paper_id": existing.paper_id,
                        "match_reason": _identity_match_reason(existing, identity),
                    },
                },
            }
        )
        return merged_paper, merged_identity, True

    def _parse_document(
        self,
        paper: ResearchPaper,
        snapshot: ResearchSourceSnapshot,
        resolved: ResolvedPaperSource,
    ) -> ResearchDocument:
        if resolved.content is None:
            raise ParsePaperError("content_missing", "source content is missing")
        source_format, _ = detect_source_format(resolved.content)
        if source_format in {SourceFormat.ZIP, SourceFormat.UNKNOWN}:
            raise ParsePaperError(
                "unsupported_format",
                "research source format is unsupported",
                status_code=415,
                details={"source_format": source_format.value},
            )
        source_record = resolved.source_record or PaperSourceRecord(
            source_id=snapshot.snapshot_id,
            paper_id=paper.paper_id,
            source_type=snapshot.source_type,
            source_url=snapshot.canonical_url or paper.source_url or snapshot.snapshot_id,
            fetched_at=snapshot.fetched_at,
            source_hash=snapshot.source_hash or snapshot.checksum,
            actor_scope=dict(_scope_from_snapshot(snapshot)),
            metadata={**dict(paper.metadata), "source_snapshot_id": snapshot.snapshot_id},
        )
        if source_record.paper_id != paper.paper_id:
            source_record = source_record.model_copy(
                update={
                    "paper_id": paper.paper_id,
                    "actor_scope": dict(_scope_from_snapshot(snapshot)),
                }
            )
        parser_errors: list[dict[str, Any]] = []
        document = None
        if self._parser is not None:
            try:
                document = self._parser.parse(paper.paper_id, resolved.content)
            except Exception as exc:  # noqa: BLE001 - fallback is part of the contract
                parser_errors.append({
                    "backend": "document_parser",
                    "status": "parse_error",
                    "reason_code": "document_parser_failed",
                    "error_type": type(exc).__name__,
                })
        if document is None and self._compiler is not None and hasattr(self._compiler, "compile"):
            try:
                document = _call_compiler(self._compiler, source_record, resolved.content)
            except Exception as exc:  # noqa: BLE001 - normalized at application boundary
                parser_errors.append({
                    "backend": "document_compiler",
                    "status": "parse_error",
                    "reason_code": "document_compiler_failed",
                    "error_type": type(exc).__name__,
                })
        if document is None:
            if parser_errors:
                fallback = _terminal_text_document(
                    paper=paper,
                    snapshot=snapshot,
                    content=resolved.content,
                    parser_attempts=parser_errors,
                )
                if fallback is not None:
                    return fallback
                raise ParsePaperError(
                    "document_parse_failed",
                    "all configured document parsers failed and text fallback was empty",
                    details={"parser_attempts": parser_errors},
                )
            raise ParsePaperError("document_parser_unconfigured", "document parser is not configured", status_code=503)
        if not isinstance(document, ResearchDocument):
            raise TypeError("document parser must return ResearchDocument")
        lineage = document.lineage.model_copy(
            update={
                "source_refs": unique_texts([*document.lineage.source_refs, *snapshot.lineage.source_refs]),
                "source_hash": document.source_hash,
                "metadata": {
                    **dict(document.lineage.metadata),
                    "source_snapshot_id": snapshot.snapshot_id,
                    **dict(snapshot.lineage.metadata),
                },
            }
        )
        metadata = {
            **dict(document.metadata),
            "source_snapshot_id": snapshot.snapshot_id,
            "source_type": snapshot.source_type,
            "content_type": resolved.content_type or snapshot.content_type,
            "title": paper.title,
            "abstract": paper.abstract,
            "authors": list(paper.authors),
            "actor_scope": dict(_scope_from_snapshot(snapshot)),
            **_local_import_provenance(snapshot.metadata),
        }
        if parser_errors:
            existing_attempts = metadata.get("parser_attempts")
            metadata["parser_attempts"] = [
                *([dict(item) for item in existing_attempts if isinstance(item, Mapping)] if isinstance(existing_attempts, list) else []),
                *parser_errors,
            ]
        parser_attempts = metadata.get("parser_attempts") or document.parser_attempts
        typed_attempts = (
            [_sanitize_parser_attempt(item) for item in parser_attempts if isinstance(item, Mapping)]
            if isinstance(parser_attempts, list)
            else []
        )
        metadata["parser_attempts"] = typed_attempts
        for key in ("compiler_attempts", "parser_cascade"):
            raw_attempts = metadata.get(key)
            if isinstance(raw_attempts, Mapping):
                nested = dict(raw_attempts)
                if isinstance(nested.get("attempts"), list):
                    nested["attempts"] = [
                        _sanitize_parser_attempt(item)
                        for item in nested["attempts"]
                        if isinstance(item, Mapping)
                    ]
                metadata[key] = nested
            elif isinstance(raw_attempts, list):
                metadata[key] = [
                    _sanitize_parser_attempt(item)
                    for item in raw_attempts
                    if isinstance(item, Mapping)
                ]
        parser_backend = document.parser_backend or str(
            metadata.get("parser_backend") or metadata.get("parse_source") or ""
        ).strip() or None
        normalized_payload = {
            **document.model_dump(mode="python", exclude_none=True),
            "paper_id": paper.paper_id,
            "source_snapshot_id": snapshot.snapshot_id,
            "title": document.title or paper.title,
            "abstract": document.abstract or paper.abstract or None,
            "authors": document.authors or list(paper.authors),
            "parser_backend": parser_backend,
            "parser_version": document.parser_version or metadata.get("parser_version"),
            "normalization_version": document.normalization_version or metadata.get("normalization_version"),
            "language": document.language or metadata.get("language") or metadata.get("lang"),
            "lineage": lineage,
            "artifact_refs": unique_texts([*document.artifact_refs, *snapshot.artifact_refs]),
            "parser_attempts": typed_attempts,
            "source_locators": unique_texts([
                *document.source_locators,
                *document.lineage.source_refs,
                *snapshot.lineage.source_refs,
                *([str(snapshot.metadata.get("source_locator"))] if snapshot.metadata.get("source_locator") else []),
            ]),
            "created_at": document.created_at or snapshot.fetched_at or datetime.now(UTC),
            "observed_at": document.observed_at or snapshot.observed_at or snapshot.fetched_at or datetime.now(UTC),
            "actor_scope": dict(_scope_from_snapshot(snapshot)),
            "metadata": metadata,
        }
        return ResearchDocument.model_validate(normalized_payload)

    def _parse_document_with_retries(
        self,
        paper: ResearchPaper,
        snapshot: ResearchSourceSnapshot,
        resolved: ResolvedPaperSource,
        *,
        run_id: str,
        diagnostics: list[dict[str, Any]],
        event_context: Mapping[str, Any],
        emit: Any,
        max_retries: int | None = None,
    ) -> ResearchDocument:
        """Retry a failed parse a bounded number of times.

        Source fetching owns network retry policy. This loop is deliberately
        limited to parser/compiler failures so a transient worker/container
        failure is observable without creating an unbounded workflow loop.
        """

        retry_budget = self._max_retries if max_retries is None else max(0, int(max_retries))
        for attempt in range(retry_budget + 1):
            try:
                return self._parse_document(paper, snapshot, resolved)
            except Exception as exc:
                retryable = isinstance(exc, ParsePaperError) and exc.retryable
                retryable = retryable or _is_retryable(exc)
                if not retryable or attempt >= retry_budget:
                    raise
                retry_number = attempt + 1
                diagnostic = {
                    "code": "parser_retry_scheduled",
                    "attempt": retry_number,
                    "max_retries": retry_budget,
                    "error_type": type(exc).__name__,
                }
                diagnostics.append(diagnostic)
                emit(
                    "parsing",
                    {
                        "phase": "retry_scheduled",
                        "attempt": retry_number,
                        "max_retries": retry_budget,
                        "diagnostics": [diagnostic],
                        "run_id": run_id,
                        **dict(event_context),
                    },
                )

    def _existing_document(
        self,
        paper_id: str,
        snapshot: ResearchSourceSnapshot,
        *,
        parse_cache_fingerprint: str | None,
        actor_scope: Mapping[str, str] | None = None,
    ) -> ResearchDocument | None:
        if self._document_repository is None:
            return None
        existing = _repository_get(
            self._document_repository,
            paper_id,
            "document",
            actor_scope=actor_scope,
        )
        if existing is None or not isinstance(existing, ResearchDocument):
            return None
        expected = snapshot.source_hash or snapshot.checksum
        cached_fingerprint = existing.metadata.get("parse_cache_fingerprint")
        return (
            existing
            if expected
            and existing.source_hash == expected
            and parse_cache_fingerprint is not None
            and cached_fingerprint == parse_cache_fingerprint
            else None
        )

    def _build_evidence(self, document: ResearchDocument) -> ResearchEvidencePack | None:
        if not document.sections:
            return None
        return self._evidence_builder.build_from_document(document=document)

    def _publish(self, artifact_type: str, payload: dict[str, Any], metadata: dict[str, Any]) -> str | None:
        if self._artifact_store is None:
            return None
        return str(self._artifact_store.publish(artifact_type=artifact_type, payload=payload, metadata=metadata))

    def _publish_chunk_manifest(
        self,
        *,
        paper: ResearchPaper,
        document: ResearchDocument,
        actor_scope: Mapping[str, str],
        run_id: str | None = None,
        reuse_existing: bool = False,
    ) -> str | None:
        if self._chunker is None:
            return None
        try:
            parse_source = str(document.metadata.get("parse_source") or "latex")
            chunks = self._chunker.chunk(document, parse_source)  # type: ignore[arg-type]
            if self._chunk_manifest is not None:
                chunks = self._chunk_manifest.resolve_chunk_ids(
                    paper.paper_id,
                    chunks,
                    actor_scope=actor_scope,
                )
                manifest_path = self._chunk_manifest.path_for(
                    paper.paper_id,
                    actor_scope=actor_scope,
                )
                pipeline_fingerprint = str(
                    document.metadata.get("parse_cache_fingerprint") or ""
                ) or None
                manifest_is_current = self._chunk_manifest.is_current(
                    paper.paper_id,
                    document_id=document.document_id,
                    source_hash=document.source_hash,
                    pipeline_fingerprint=pipeline_fingerprint,
                    actor_scope=actor_scope,
                )
                # Reuse is safe only when source, document and the complete
                # parser/chunker pipeline identity match the committed projection.
                if not reuse_existing or not manifest_is_current:
                    manifest_path = self._chunk_manifest.write(
                        paper.paper_id,
                        chunks,
                        document_metadata={
                            **dict(document.metadata),
                            "document_id": document.document_id,
                            "source_hash": document.source_hash,
                            "actor_scope": dict(actor_scope),
                        },
                        actor_scope=actor_scope,
                        document_id=document.document_id,
                        source_hash=document.source_hash,
                        run_id=run_id,
                        observed_at=document.observed_at,
                    )
            else:
                manifest_path = None
            payload = {
                "paper_id": paper.paper_id,
                "document_id": document.document_id,
                "source_hash": document.source_hash,
                "actor_scope": dict(actor_scope),
                "chunks": [chunk.model_dump(mode="json", exclude_none=True) for chunk in chunks],
            }
            if manifest_path is not None:
                payload["manifest_path"] = str(manifest_path)
            artifact_ref = self._publish(
                "research-chunk-manifest",
                payload,
                {
                    "paper_id": paper.paper_id,
                    "source_hash": document.source_hash,
                    "actor_scope": dict(actor_scope),
                },
            )
            if artifact_ref:
                return artifact_ref
            return manifest_path.as_uri() if manifest_path is not None else None
        except Exception as exc:  # chunking is a projection; parsing remains usable
            LOGGER.warning("research chunk manifest projection failed: %s", exc)
            return None

    def _save_snapshot(self, snapshot: ResearchSourceSnapshot) -> None:
        if self._snapshot_repository is not None:
            self._snapshot_repository.save(snapshot)

    def _save_paper(self, paper: ResearchPaper) -> None:
        if self._paper_repository is not None:
            self._paper_repository.save(paper)

    def _save_identity(self, identity: ResearchPaperIdentity) -> None:
        if self._identity_repository is not None:
            self._identity_repository.save(identity)

    def _emit(
        self,
        run_id: str,
        status: str,
        payload: Mapping[str, Any],
        *,
        diagnostics: list[dict[str, Any]] | None = None,
    ) -> str | None:
        if status not in PARSE_STATUSES:
            raise ValueError(f"unsupported parse status: {status}")
        if self._event_sink is None:
            return None
        event_id = str(payload.get("event_id") or f"{run_id}:{uuid4().hex}")
        occurred_at = str(payload.get("occurred_at") or datetime.now(UTC).isoformat())
        actor_scope = payload.get("actor_scope")
        if not isinstance(actor_scope, Mapping):
            actor_scope = {}
        event = {
            "event_id": event_id,
            "run_id": run_id,
            "event_type": "research_parse_terminal" if payload.get("terminal") else "research_parse_phase",
            "status": status,
            "from_status": payload.get("from_status"),
            "to_status": payload.get("to_status", status),
            "occurred_at": occurred_at,
            "paper_id": payload.get("paper_id"),
            "source_snapshot_id": payload.get("source_snapshot_id"),
            "actor_scope": dict(actor_scope),
            "attempt_id": payload.get("attempt_id") or f"{run_id}:{status}",
            "diagnostics": [dict(item) for item in (payload.get("diagnostics") or diagnostics or []) if isinstance(item, Mapping)],
            "artifact_refs": unique_texts([str(item) for item in (payload.get("artifact_refs") or [])]),
            "causation_id": payload.get("causation_id") or run_id,
            "correlation_id": payload.get("correlation_id") or run_id,
        }
        # Keep any non-contract payload (for example parser attempts) for
        # diagnostics/replay without weakening the stable event envelope.
        event.update({key: value for key, value in dict(payload).items() if key not in event})
        try:
            self._event_sink.append(run_id, event)
        except Exception as exc:
            LOGGER.warning("research parse event append failed: %s", exc)
            if diagnostics is not None:
                diagnostics.append({
                    "code": "event_persist_failed",
                    "status": status,
                    "error_type": type(exc).__name__,
                })
            raise ParsePaperError(
                "event_persist_failed",
                "research parse event could not be persisted",
                status_code=503,
                retryable=True,
                details={
                    "run_id": run_id,
                    "event_id": event_id,
                    "status": status,
                    "error_type": type(exc).__name__,
                },
            ) from exc
        return event_id


class ParsePaperResult(PrimitiveModel):
    run_id: str
    paper_id: str
    status: str
    paper: ResearchPaper | None = None
    identity: ResearchPaperIdentity | None = None
    source_snapshots: list[ResearchSourceSnapshot] = Field(default_factory=list)
    document: ResearchDocument | None = None
    parser_attempts: list[dict[str, Any]] = Field(default_factory=list)
    quality_report: dict[str, Any] = Field(default_factory=dict)
    evidence_pack: ResearchEvidencePack | None = None
    chunk_manifest_ref: str | None = None
    evidence_pack_ref: str | None = None
    catalog_entry: ResearchPaperCatalogEntry | None = None
    catalog_status: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool = False

    @field_validator("run_id", "paper_id")
    @classmethod
    def _required_ids(cls, value: str) -> str:
        return require_text(value, "parse result identity")

    @field_validator("status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        normalized = require_text(value, "parse status")
        if normalized not in PARSE_STATUSES:
            raise ValueError(f"unsupported parse status: {normalized}")
        return normalized

    @model_validator(mode="after")
    def _normalize_refs(self) -> "ParsePaperResult":
        object.__setattr__(self, "artifact_refs", unique_texts(self.artifact_refs))
        return self

    def to_contract(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True, by_alias=False)
        payload["runId"] = payload.pop("run_id")
        payload["paperId"] = payload.pop("paper_id")
        payload["sourceSnapshots"] = payload.pop("source_snapshots", [])
        payload["parserAttempts"] = payload.pop("parser_attempts", [])
        payload["qualityReport"] = payload.pop("quality_report", {})
        payload["chunkManifestRef"] = payload.pop("chunk_manifest_ref", None)
        payload["evidencePackRef"] = payload.pop("evidence_pack_ref", None)
        payload["catalogEntry"] = payload.pop("catalog_entry", None)
        payload["catalogStatus"] = payload.pop("catalog_status", None)
        payload["artifactRefs"] = payload.pop("artifact_refs", [])
        return payload


def identity_from_paper(
    paper: ResearchPaper,
    snapshot: ResearchSourceSnapshot | None = None,
    *,
    actor_scope: Mapping[str, str] | None = None,
) -> ResearchPaperIdentity:
    metadata = dict(paper.metadata)
    source_type = snapshot.source_type if snapshot is not None else infer_source_type(paper.source_url or paper.source)
    external_id = snapshot.external_id if snapshot is not None else _external_id(paper.source_url or paper.paper_id, source_type)
    arxiv_id = (
        str(metadata.get("arxiv_id") or (external_id if source_type == "arxiv" else "")).strip()
        or None
    )
    doi = (
        str(metadata.get("doi") or (external_id if source_type in {"doi", "crossref"} else "")).strip()
        or None
    )
    openreview_id = (
        str(metadata.get("openreview_id") or (external_id if source_type == "openreview" else "")).strip()
        or None
    )
    published_year = paper.published_at.year if paper.published_at else _as_int(metadata.get("published_year"))
    canonical = snapshot.canonical_url if snapshot is not None else paper.source_url
    title_fingerprint = build_paper_identity_fingerprint(paper.title, paper.authors, published_year)
    identity_basis = str(metadata.get("identity_basis") or "").strip()
    local_content_fingerprint = str(metadata.get("local_content_fingerprint") or "").strip().casefold()
    identity_fingerprint = (
        f"local-content:{local_content_fingerprint}"
        if identity_basis == "owner_scoped_local_content_sha256" and local_content_fingerprint
        else title_fingerprint
    )
    return ResearchPaperIdentity(
        paper_id=paper.paper_id,
        canonical_paper_id=paper.paper_id,
        title=paper.title,
        canonical_title=paper.title,
        authors=list(paper.authors),
        published_year=published_year,
        publication_year=published_year,
        canonical_url=canonical,
        arxiv_id=arxiv_id,
        doi=doi,
        openreview_id=openreview_id,
        versions=_as_text_list(metadata.get("versions")) or ([str(metadata["version"])] if metadata.get("version") else []),
        source_snapshot_ids=[snapshot.snapshot_id] if snapshot is not None else [],
        source_snapshot_refs=[snapshot.snapshot_id] if snapshot is not None else [],
        fingerprint=identity_fingerprint,
        title_author_year_fingerprint=title_fingerprint,
        external_links={
            key: value
            for key, value in {
                "canonical_url": canonical,
                "arxiv": arxiv_id,
                "doi": doi,
                "openreview": openreview_id,
                "code": paper.code_url,
            }.items()
            if value
        },
        field_provenance=_identity_field_provenance(
            paper,
            snapshot.snapshot_id if snapshot is not None else None,
        ),
        metadata={
            "source_type": source_type,
            "actor_scope": dict(actor_scope or {}),
            **({"identity_basis": identity_basis} if identity_basis else {}),
            **(
                {"local_content_fingerprint": local_content_fingerprint}
                if local_content_fingerprint
                else {}
            ),
        },
    )


def infer_source_type(source: str) -> ResearchSourceType:
    value = str(source or "").strip().casefold()
    if value.startswith("arxiv:") or "arxiv.org" in value:
        return "arxiv"
    if "openreview.net" in value:
        return "openreview"
    parsed = urlsplit(value)
    if (
        value.startswith("doi:")
        or value.startswith("10.")
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


def validate_parse_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the public parse option contract at the application boundary."""

    values = dict(options or {})
    unknown = sorted(str(key) for key in values if str(key) not in PARSE_OPTION_KEYS)
    if unknown:
        raise ParsePaperError(
            "invalid_request",
            "parse options contain unsupported keys",
            status_code=400,
            details={"unknown_options": unknown},
        )
    for key in ("refresh", "include_code", "include_catalog", "include_chunks", "include_evidence"):
        if key in values and not isinstance(values[key], bool):
            raise ParsePaperError(
                "invalid_request",
                f"parse option {key} must be boolean",
                status_code=400,
                details={"option": key},
            )
    if "parser_backend" in values:
        backend = str(values["parser_backend"]).strip().casefold()
        if not backend:
            raise ParsePaperError("invalid_request", "parser_backend must not be empty", status_code=400)
        values["parser_backend"] = backend
    if "quality_profile" in values:
        profile = str(values["quality_profile"]).strip().casefold()
        if profile not in PARSE_QUALITY_PROFILES:
            raise ParsePaperError(
                "invalid_request",
                "quality_profile is unsupported",
                status_code=400,
                details={"allowed": sorted(PARSE_QUALITY_PROFILES)},
            )
        values["quality_profile"] = profile
    if "max_attempts" in values:
        raw = values["max_attempts"]
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= PARSE_OPTION_MAX_ATTEMPTS:
            raise ParsePaperError(
                "invalid_request",
                f"max_attempts must be between 1 and {PARSE_OPTION_MAX_ATTEMPTS}",
                status_code=400,
            )
    if "timeout_seconds" in values:
        raw = values["timeout_seconds"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 < float(raw) <= PARSE_OPTION_MAX_TIMEOUT_SECONDS:
            raise ParsePaperError(
                "invalid_request",
                f"timeout_seconds must be between 1 and {PARSE_OPTION_MAX_TIMEOUT_SECONDS}",
                status_code=400,
            )
        values["timeout_seconds"] = float(raw)
    return values


def effective_max_retries(options: Mapping[str, Any] | None, configured: int) -> int:
    values = dict(options or {})
    max_attempts = values.get("max_attempts")
    if max_attempts is None:
        return configured
    return min(configured, max(0, int(max_attempts) - 1))


def _source_descriptors_equal(left: str, right: str) -> bool:
    def normalize(value: str) -> str:
        text = str(value or "").strip()
        if text.startswith(("http://", "https://")):
            return canonicalize_url(text).casefold()
        return text.rstrip("/").casefold()

    return bool(normalize(left)) and normalize(left) == normalize(right)


def _canonical_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        return canonicalize_url(source)
    if source.startswith("file://"):
        return ""
    return source


def _external_id(source: str, source_type: ResearchSourceType) -> str | None:
    value = str(source or "").strip()
    if source_type == "arxiv":
        text = value.rstrip("/").split("/")[-1]
        return text.removesuffix(".pdf")
    if source_type in {"doi", "crossref"}:
        parsed = urlsplit(value)
        if parsed.hostname and parsed.hostname.casefold() in {"doi.org", "dx.doi.org"}:
            return parsed.path.strip("/") or None
        return value.removeprefix("doi:").strip()
    if source_type == "openreview":
        parsed = urlsplit(value)
        query_id = parse_qs(parsed.query).get("id", [None])[0]
        if query_id:
            return str(query_id).strip() or None
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else value
    if source_type == "github":
        parts = [part.removesuffix(".git") for part in urlsplit(value).path.split("/") if part]
        return "/".join(parts[:2]) if len(parts) >= 2 else value
    return value or None


def _actor_scope(request: ParsePaperRequest) -> dict[str, str]:
    allowed_keys = {"tenant_id", "user_id", "memory_namespace"}
    scope = {
        str(k): str(v).strip()
        for k, v in request.actor_scope.items()
        if str(k) in allowed_keys and str(v).strip()
    }
    for key, value in (
        ("tenant_id", request.tenant_id),
        ("user_id", request.user_id),
        ("memory_namespace", request.memory_namespace),
    ):
        if value:
            scope[key] = value
    return scope


def _has_paper_context(request: ParsePaperRequest) -> bool:
    """Return whether a repository observation is explicitly attached to a paper.

    A GitHub URL is not a paper identity. Callers must provide a paper id or
    paper source reference before the repository can participate in a paper
    parse workflow. The accepted aliases keep HTTP, CLI and direct callers
    interoperable without allowing an arbitrary repository title to stand in
    for paper context.
    """

    values: dict[str, Any] = {}
    values.update(request.metadata)
    values.update(request.options)
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
    content_ref = str(request.content_ref or "").strip()
    return bool(content_ref and content_ref != request.source.strip())


def _explicit_paper_id(values: Mapping[str, Any]) -> str | None:
    for key in ("paper_id", "paperId", "paper_identity_id", "paperIdentityId"):
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _bind_local_content_identity(
    resolved: ResolvedPaperSource,
    actor_scope: Mapping[str, str],
) -> ResolvedPaperSource:
    """Replace path-derived local ids with verified owner/content identity."""

    if resolved.snapshot.source_type != "local" or resolved.content is None:
        return resolved
    actual_hash = sha256(resolved.content).hexdigest()
    declared_hash = str(resolved.snapshot.source_hash or resolved.snapshot.checksum or "").strip().casefold()
    normalized_declared = declared_hash.removeprefix("sha256:")
    if normalized_declared and normalized_declared != actual_hash:
        raise ParsePaperError(
            "source_integrity_mismatch",
            "local source bytes do not match the resolver fingerprint",
            status_code=409,
            retryable=False,
        )
    paper_id = build_local_content_paper_id(actual_hash, actor_scope)
    fingerprint = f"sha256:{actual_hash}"
    provenance = _local_import_provenance(resolved.paper.metadata)
    identity_metadata = {
        "identity_basis": "owner_scoped_local_content_sha256",
        "local_content_fingerprint": fingerprint,
    }
    paper = resolved.paper.model_copy(
        update={
            "paper_id": paper_id,
            "metadata": {
                **dict(resolved.paper.metadata),
                **identity_metadata,
                **provenance,
            },
        }
    )
    snapshot = resolved.snapshot.model_copy(
        update={
            "paper_id": paper_id,
            "source_hash": actual_hash,
            "checksum": actual_hash,
            "metadata": {
                **dict(resolved.snapshot.metadata),
                **identity_metadata,
                **provenance,
            },
            "lineage": resolved.snapshot.lineage.model_copy(
                update={
                    "source_hash": actual_hash,
                    "metadata": {
                        **dict(resolved.snapshot.lineage.metadata),
                        **identity_metadata,
                        **provenance,
                    },
                }
            ),
        }
    )
    source_record = resolved.source_record
    if source_record is not None:
        source_record = source_record.model_copy(
            update={
                "paper_id": paper_id,
                "source_hash": actual_hash,
                "metadata": {
                    **dict(source_record.metadata),
                    **identity_metadata,
                    **provenance,
                },
            }
        )
    return ResolvedPaperSource(
        paper=paper,
        snapshot=snapshot,
        content=resolved.content,
        content_type=resolved.content_type,
        source_record=source_record,
        access_status=resolved.access_status,
        diagnostics=resolved.diagnostics,
    )


def _local_import_provenance(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: values[key]
        for key in _LOCAL_IMPORT_PROVENANCE_KEYS
        if key in values and values[key] is not None and str(values[key]).strip()
    }


def _is_local_content_identity(snapshot: ResearchSourceSnapshot) -> bool:
    return (
        snapshot.source_type == "local"
        and str(snapshot.metadata.get("identity_basis") or "")
        == "owner_scoped_local_content_sha256"
    )


def _snapshot_for_scope(snapshot: ResearchSourceSnapshot, actor_scope: Mapping[str, str], content: bytes | None = None) -> ResearchSourceSnapshot:
    if content is not None:
        actual_hash = sha256(content).hexdigest()
        declared_hash = str(snapshot.source_hash or snapshot.checksum or "").strip().casefold()
        declared_hash = declared_hash.removeprefix("sha256:")
        if declared_hash and declared_hash != actual_hash:
            raise ParsePaperError(
                "source_integrity_mismatch",
                "resolved source bytes do not match the source snapshot fingerprint",
                status_code=409,
                retryable=False,
            )
        snapshot = snapshot.model_copy(
            update={
                "source_hash": actual_hash,
                "checksum": actual_hash,
                "lineage": snapshot.lineage.model_copy(update={"source_hash": actual_hash}),
                "metadata": {**dict(snapshot.metadata), "source_content_sha256": actual_hash},
            }
        )
    updates: dict[str, Any] = {
        "actor_scope": dict(actor_scope),
        "lineage": snapshot.lineage.model_copy(update={"metadata": {**dict(snapshot.lineage.metadata), **dict(actor_scope)}}),
        "metadata": {**dict(snapshot.metadata), **dict(actor_scope)},
    }
    if content is not None and not (snapshot.source_hash or snapshot.checksum):
        digest = sha256(content).hexdigest()
        updates["source_hash"] = digest
        updates["checksum"] = digest
        updates["lineage"] = updates["lineage"].model_copy(update={"source_hash": digest})
    return snapshot.model_copy(
        update=updates,
    )


def _scope_from_snapshot(snapshot: ResearchSourceSnapshot) -> dict[str, str]:
    values: dict[str, str] = {}
    explicit = getattr(snapshot, "actor_scope", None)
    if isinstance(explicit, Mapping):
        values.update({str(key): str(value) for key, value in explicit.items() if str(value).strip()})
    for source in (snapshot.metadata, snapshot.lineage.metadata):
        raw = source.get("actor_scope") if isinstance(source, Mapping) else None
        if isinstance(raw, Mapping):
            values.update({str(key): str(value) for key, value in raw.items() if str(value).strip()})
    # Older adapters put scope dimensions directly on metadata/lineage.
    for source in (snapshot.metadata, snapshot.lineage.metadata):
        if isinstance(source, Mapping):
            for key in ("tenant_id", "user_id", "memory_namespace"):
                value = source.get(key)
                if value is not None and str(value).strip():
                    values[key] = str(value).strip()
    return {key: values[key] for key in ("tenant_id", "user_id", "memory_namespace") if key in values}


def _provenance(paper: ResearchPaper, snapshots: list[ResearchSourceSnapshot], actor_scope: Mapping[str, str]) -> dict[str, Any]:
    return {
        "paperId": paper.paper_id,
        "sourceSnapshotRefs": [snapshot.snapshot_id for snapshot in snapshots],
        "sourceRefs": sorted({ref for snapshot in snapshots for ref in snapshot.lineage.source_refs}),
        "actorScope": dict(actor_scope),
    }


def _same_checksum(snapshot: ResearchSourceSnapshot, paper: ResearchPaper) -> bool:
    expected = snapshot.source_hash or snapshot.checksum
    actual = str(paper.metadata.get("source_hash") or "")
    return bool(expected and actual and expected == actual)


def _same_snapshot_content(
    left: ResearchSourceSnapshot,
    right: ResearchSourceSnapshot,
) -> bool:
    left_hash = left.source_hash or left.checksum
    right_hash = right.source_hash or right.checksum
    if left_hash or right_hash:
        return bool(left_hash and right_hash and left_hash == right_hash)
    return (
        left.access_status == right.access_status
        and left.canonical_url == right.canonical_url
        and left.external_id == right.external_id
    )


def _identity_external_ids(identity: ResearchPaperIdentity) -> list[str]:
    return [value for value in (identity.arxiv_id, identity.doi, identity.openreview_id, identity.canonical_url) if value]


def _identity_field_provenance(
    paper: ResearchPaper,
    snapshot_id: str | None,
) -> dict[str, list[str]]:
    if not snapshot_id:
        return {}
    values = {
        "title": paper.title,
        "authors": paper.authors,
        "published_year": paper.published_at.year if paper.published_at else None,
        "canonical_url": paper.source_url,
        "code_url": paper.code_url,
    }
    return {
        field_name: [snapshot_id]
        for field_name, value in values.items()
        if value
    }


def _merge_identities(left: ResearchPaperIdentity, right: ResearchPaperIdentity) -> ResearchPaperIdentity:
    conflicts = _identity_conflicts(left, right)
    field_provenance: dict[str, list[str]] = {}
    for field_name in set(left.field_provenance) | set(right.field_provenance):
        refs = unique_texts([
            *left.field_provenance.get(field_name, []),
            *right.field_provenance.get(field_name, []),
        ])
        if refs:
            field_provenance[field_name] = refs
    return left.model_copy(
        update={
            "title": left.title or right.title,
            "canonical_title": left.canonical_title or right.canonical_title or left.title or right.title,
            "authors": unique_texts([*left.authors, *right.authors]),
            "published_year": left.published_year or right.published_year,
            "publication_year": left.publication_year or right.publication_year or left.published_year or right.published_year,
            "canonical_url": left.canonical_url or right.canonical_url,
            "arxiv_id": left.arxiv_id or right.arxiv_id,
            "doi": left.doi or right.doi,
            "openreview_id": left.openreview_id or right.openreview_id,
            "versions": unique_texts([*left.versions, *right.versions]),
            "source_snapshot_ids": unique_texts([*left.source_snapshot_ids, *right.source_snapshot_ids]),
            "source_snapshot_refs": unique_texts([*left.source_snapshot_refs, *right.source_snapshot_refs]),
            "external_links": {**dict(right.external_links), **dict(left.external_links)},
            "metadata_conflicts": [*left.metadata_conflicts, *right.metadata_conflicts, *conflicts],
            "field_provenance": field_provenance,
            "metadata": {
                **dict(right.metadata),
                **dict(left.metadata),
                **({"conflict_diagnostics": conflicts} if conflicts else {}),
            },
        }
    )


def _identity_conflicts(
    left: ResearchPaperIdentity,
    right: ResearchPaperIdentity,
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for field_name in ("title", "published_year", "canonical_url", "arxiv_id", "doi", "openreview_id"):
        left_value = getattr(left, field_name)
        right_value = getattr(right, field_name)
        if left_value and right_value and str(left_value).casefold() != str(right_value).casefold():
            conflicts.append({
                "code": "identity_field_conflict",
                "field": field_name,
                "existing": left_value,
                "incoming": right_value,
            })
    return conflicts


def _identity_match_reason(left: ResearchPaperIdentity, right: ResearchPaperIdentity) -> str:
    for label, left_value, right_value in (
        ("arxiv_id", left.arxiv_id, right.arxiv_id),
        ("doi", left.doi, right.doi),
        ("openreview_id", left.openreview_id, right.openreview_id),
        ("canonical_url", left.canonical_url, right.canonical_url),
        ("fingerprint", left.fingerprint, right.fingerprint),
    ):
        if left_value and right_value and str(left_value).casefold() == str(right_value).casefold():
            return label
    return "unknown"


def _call_compiler(compiler: Any, source_record: PaperSourceRecord, content: bytes) -> ResearchDocument:
    try:
        signature = inspect.signature(compiler.compile)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "content" in signature.parameters:
        return compiler.compile(source_record, content=content)
    return compiler.compile(source_record)


def _parser_attempts(document: ResearchDocument) -> list[dict[str, Any]]:
    cascade = document.metadata.get("parser_cascade")
    if isinstance(cascade, Mapping):
        attempts = cascade.get("attempts")
        if isinstance(attempts, list):
            return [_sanitize_parser_attempt(item) for item in attempts if isinstance(item, Mapping)]
    attempts = document.metadata.get("compiler_attempts")
    if isinstance(attempts, list):
        return [_sanitize_parser_attempt(item) for item in attempts if isinstance(item, Mapping)]
    attempts = document.metadata.get("parser_attempts")
    if isinstance(attempts, list):
        return [_sanitize_parser_attempt(item) for item in attempts if isinstance(item, Mapping)]
    return []


def _sanitize_parser_attempt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep parser diagnostics useful without exposing exception text."""

    result: dict[str, Any] = {}
    for key in ("backend", "status", "reason_code", "error_type"):
        raw = value.get(key)
        if raw is not None and str(raw).strip():
            result[key] = str(raw).strip()
    reason = value.get("reason")
    if reason and "reason_code" not in result:
        result["reason_code"] = "parser_attempt_failed"
    if "elapsed_ms" in value:
        try:
            result["elapsed_ms"] = round(float(value["elapsed_ms"]), 3)
        except (TypeError, ValueError):
            pass
    quality = value.get("quality")
    if isinstance(quality, Mapping):
        result["quality"] = dict(quality)
    return result


def _quality_report(
    document: ResearchDocument,
    *,
    profile: str = "reading",
    paper: ResearchPaper | None = None,
) -> dict[str, Any]:
    profile = profile if profile in {"metadata", "reading", "catalog"} else "reading"
    body_chars = sum(len(section.text or "") for section in document.sections)
    non_empty_sections = sum(1 for section in document.sections if (section.text or "").strip())
    non_empty_ratio = non_empty_sections / len(document.sections) if document.sections else 0.0
    text = "\n".join(section.text or "" for section in document.sections)
    replacement_ratio = text.count("\ufffd") / len(text) if text else 0.0
    element_count = sum(
        len(items)
        for items in (
            document.sections,
            document.figures,
            document.tables,
            document.equations,
            document.references,
        )
    )
    locator_count = len(set(document.source_locators))
    locator_score = min(1.0, locator_count / element_count) if element_count else 0.0
    structure_score = min(1.0, len(document.sections) / 3.0) * non_empty_ratio
    text_score = min(1.0, body_chars / 3000.0) * max(0.0, 1.0 - replacement_ratio)
    # A document with no optional structured elements is still structurally
    # valid; when elements exist, require their locators to be represented.
    structured_count = len(document.figures) + len(document.tables) + len(document.equations) + len(document.references)
    element_score = 1.0 if structured_count == 0 else min(1.0, locator_count / (len(document.sections) + structured_count))
    lineage_hash_matches = bool(
        document.source_hash and document.lineage.source_hash == document.source_hash
    )
    fidelity_hash_matches = (
        document.fidelity is None
        or document.fidelity.source_hash == document.source_hash
    )
    integrity_score = 1.0 if lineage_hash_matches and fidelity_hash_matches else 0.0
    title_present = bool((document.title or (paper.title if paper is not None else "") or "").strip())
    abstract_present = bool((document.abstract or "").strip())
    author_present = bool(document.authors or (paper.authors if paper is not None else []))
    external_id_present = bool(
        document.metadata.get("external_id")
        or document.metadata.get("arxiv_id")
        or document.metadata.get("doi")
        or document.metadata.get("openreview_id")
        or (
            paper is not None
            and (
                paper.metadata.get("external_id")
                or paper.metadata.get("arxiv_id")
                or paper.metadata.get("doi")
                or paper.metadata.get("openreview_id")
            )
        )
    )
    benchmark_tables = [
        table
        for table in document.tables
        if any(
            token in (table.caption or "").casefold()
            for token in ("benchmark", "result", "accuracy", "performance", "evaluation", "sota")
        )
    ]
    thresholds = {
        "metadata": {
            "title_present": True,
            "author_or_external_id_present": True,
        },
        "reading": {
            "title_present": True,
            "abstract_present": "diagnostic_only",
            "body_chars_min": 3000,
            "sections_min": 3,
            "non_empty_section_ratio_min": 0.8,
            "replacement_char_ratio_max": 0.02,
            "locator_coverage_min": 0.8,
        },
        "catalog": {
            "title_present": True,
            "abstract_present": "diagnostic_only",
            "body_chars_min": 3000,
            "sections_min": 3,
            "non_empty_section_ratio_min": 0.8,
            "replacement_char_ratio_max": 0.02,
            "locator_coverage_min": 0.9,
            "benchmark_evidence_required_when_table_present": True,
            "reference_locator_required_when_present": True,
        },
    }[profile]
    hard_failures: list[dict[str, Any]] = []

    def require(code: str, observed: Any, expected: Any, passed: bool) -> None:
        if not passed:
            hard_failures.append({"code": code, "observed": observed, "expected": expected})

    if profile == "metadata":
        require("title_missing", title_present, True, title_present)
        require(
            "author_or_external_id_missing",
            {"authors": author_present, "external_id": external_id_present},
            "at least one author or external id",
            author_present or external_id_present,
        )
    else:
        require("title_missing", title_present, True, title_present)
        # Abstract is useful for reading and catalog projections, but its
        # absence is a diagnostic rather than a hard parse failure.
        require("body_too_short", body_chars, 3000, body_chars >= 3000)
        require("sections_insufficient", len(document.sections), 3, len(document.sections) >= 3)
        require("empty_sections_excessive", non_empty_ratio, 0.8, non_empty_ratio >= 0.8)
        require("replacement_chars_excessive", replacement_ratio, 0.02, replacement_ratio <= 0.02)
        locator_threshold = 0.9 if profile == "catalog" else 0.8
        require(
            "locator_coverage_insufficient",
            locator_score,
            locator_threshold,
            locator_score >= locator_threshold,
        )
        if profile == "catalog" and benchmark_tables:
            table_evidence = sum(1 for table in benchmark_tables if table.rows and table.source_ref)
            require(
                "benchmark_evidence_missing",
                table_evidence,
                len(benchmark_tables),
                table_evidence == len(benchmark_tables),
            )
        if profile == "catalog" and document.references:
            located_references = sum(
                1 for reference in document.references if reference.source_ref.strip()
            )
            require(
                "reference_locator_missing",
                located_references,
                len(document.references),
                located_references == len(document.references),
            )
    attempts = _parser_attempts(document)
    failed_attempts = [
        item for item in attempts
        if str(item.get("status") or "").casefold() in {"parse_error", "quality_rejected", "failed", "timeout", "cancelled"}
    ]
    cascade = document.metadata.get("parser_cascade")
    cascade_degraded = isinstance(cascade, Mapping) and cascade.get("degraded") is True
    execution_degraded = (
        bool(document.metadata.get("degraded"))
        or cascade_degraded
        or document.status == "degraded"
    )
    quarantined_reasons = []
    if not lineage_hash_matches:
        quarantined_reasons.append("source_lineage_hash_mismatch")
    if not fidelity_hash_matches:
        quarantined_reasons.append("fidelity_source_hash_mismatch")
    for code in quarantined_reasons:
        hard_failures.append({"code": code, "observed": False, "expected": True})
    fidelity_status = str(document.metadata.get("fidelity_status") or "").casefold()
    fidelity_unavailable = fidelity_status == "unavailable"
    if fidelity_unavailable:
        hard_failures.append({
            "code": "source_fidelity_unavailable",
            "observed": document.metadata.get("fidelity_unavailable_reason") or "unknown",
            "expected": "all source blocks map to validated pages and Research entities",
        })
    parser_warning_codes = document.metadata.get("parser_warning_codes")
    parser_warning_codes = parser_warning_codes if isinstance(parser_warning_codes, list) else []
    missing_formula_source_count = sum(
        isinstance(item, Mapping) and item.get("code") == "formula_source_missing"
        for item in parser_warning_codes
    )
    if not parser_warning_codes:
        parser_warnings = document.metadata.get("parser_warnings")
        parser_warnings = parser_warnings if isinstance(parser_warnings, list) else []
        missing_formula_source_count = sum(
            "equation block has no usable source content" in str(warning)
            for warning in parser_warnings
        )
    if missing_formula_source_count:
        hard_failures.append({"code": "formula_source_missing", "observed": missing_formula_source_count, "expected": 0})
    if execution_degraded:
        hard_failures.append({
            "code": "parser_execution_degraded",
            "observed": [str(item.get("status") or "unknown") for item in failed_attempts],
            "expected": "the configured parse path completes without degraded recovery",
        })
    passed = not hard_failures
    document_status = (
        "quarantined" if quarantined_reasons
        else "degraded" if not passed or document.status == "degraded"
        else "parsed"
    )
    parse_status = (
        "quarantined" if document_status == "quarantined"
        else "parsed" if passed and document_status == "parsed"
        else "catalog_partial" if profile == "catalog"
        else "degraded"
    )
    scores = {
        "structure_score": round(structure_score, 6),
        "text_score": round(text_score, 6),
        "locator_score": round(locator_score, 6),
        "element_score": round(element_score, 6),
        "integrity_score": round(integrity_score, 6),
    }
    quality_score = (
        0.25 * structure_score
        + 0.25 * text_score
        + 0.20 * locator_score
        + 0.15 * element_score
        + 0.15 * integrity_score
    )
    return {
        "sections": len(document.sections),
        "figures": len(document.figures),
        "tables": len(document.tables),
        "equations": len(document.equations),
        "references": len(document.references),
        "bodyChars": body_chars,
        "nonEmptySectionRatio": round(non_empty_ratio, 6),
        "replacementCharRatio": round(replacement_ratio, 6),
        "locatorCoverage": round(locator_score, 6),
        "scores": scores,
        "quality_score": round(quality_score, 6),
        "qualityScore": round(quality_score, 6),
        "formula_version": "research-quality-v1",
        "qualityPolicyVersion": "research-document-quality-policy/v1",
        "dimensions": {
            "execution": {
                "status": (
                    "degraded" if execution_degraded
                    else "recovered" if failed_attempts
                    else "completed"
                ),
                "used_backend": document.parser_backend,
                "attempt_count": len(attempts),
                "failed_attempt_count": len(failed_attempts),
                "cascade_degraded": cascade_degraded,
            },
            "structure_and_source": {
                "status": (
                    "quarantined" if quarantined_reasons
                    else "degraded" if fidelity_unavailable
                    else "validated" if document.fidelity is not None
                    else "unknown"
                ),
                "reason_codes": quarantined_reasons + (["source_fidelity_unavailable"] if fidelity_unavailable else []),
                "source_hash_matches_lineage": lineage_hash_matches,
                "fidelity_available": document.fidelity is not None,
                "fidelity_schema_version": (
                    document.fidelity.schema_version if document.fidelity is not None else None
                ),
                "page_count": len(document.fidelity.pages) if document.fidelity is not None else None,
                "block_count": len(document.fidelity.blocks) if document.fidelity is not None else None,
                "table_grid_count": len(document.fidelity.tables) if document.fidelity is not None else None,
            },
            "content": {
                "status": "passed" if passed else "degraded",
                "hard_failure_count": len(hard_failures),
                "missing_formula_source_count": missing_formula_source_count,
                "section_count": len(document.sections),
                "body_chars": body_chars,
                "element_count": element_count,
                "locator_coverage": round(locator_score, 6),
            },
        },
        "profile": profile,
        "thresholds": thresholds,
        "hardFailures": hard_failures,
        "diagnostics": (
            ([{"code": "abstract_missing", "severity": "info"}] if not abstract_present and profile != "metadata" else [])
            + ([{"code": "source_fidelity_unavailable", "severity": "error"}] if fidelity_unavailable else [])
            + ([{"code": "formula_source_missing", "severity": "error", "count": missing_formula_source_count}] if missing_formula_source_count else [])
        ),
        "passed": passed,
        "documentStatus": document_status,
        "parseStatus": parse_status,
        "catalogEligible": passed and profile == "catalog",
        "metadataCompleteness": {
            "title": title_present,
            "authors": author_present,
            "externalId": external_id_present,
            "abstract": abstract_present,
        },
        "weights": {
            "structure_score": 0.25,
            "text_score": 0.25,
            "locator_score": 0.20,
            "element_score": 0.15,
            "integrity_score": 0.15,
        },
        "degraded": not passed or bool(document.metadata.get("degraded")),
    }


def _aggregate_parse_status(current: str, catalog_status: str) -> str:
    """Keep a parser degradation visible when catalog projection is partial."""

    order = {
        "failed": 60,
        "metadata_only": 50,
        "degraded": 40,
        "catalog_partial": 30,
        "catalog_ready": 20,
        "parsed": 10,
    }
    return current if order.get(current, 0) >= order.get(catalog_status, 0) else catalog_status


def _request_fingerprint(request: ParsePaperRequest, actor_scope: Mapping[str, str]) -> str:
    payload = {
        "source": request.source,
        "source_type": request.source_type,
        "content_ref": request.content_ref,
        "options": dict(request.options),
        "actor_scope": dict(actor_scope),
    }
    encoded = repr(sorted(payload.items())).encode("utf-8")
    return sha256(encoded).hexdigest()


def _scope_key(actor_scope: Mapping[str, Any] | None) -> str:
    return "|".join(
        f"{key}={str((actor_scope or {}).get(key) or '').strip()}"
        for key in ("tenant_id", "user_id", "memory_namespace")
        if str((actor_scope or {}).get(key) or "").strip()
    ) or "public"


def _parse_budget(options: Mapping[str, Any]) -> dict[str, Any]:
    timeout_seconds = float(options.get("timeout_seconds", 300.0))
    max_attempts = int(options.get("max_attempts", 2))
    return {
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "retry_budget": max(0, max_attempts - 1),
    }


def _ensure_budget(
    started: float,
    budget: Mapping[str, Any],
    diagnostics: list[dict[str, Any]],
    *,
    phase: str,
) -> None:
    elapsed = monotonic() - started
    timeout_seconds = float(budget.get("timeout_seconds", 300.0))
    if elapsed <= timeout_seconds:
        return
    diagnostic = {
        "code": "budget_exhausted",
        "phase": phase,
        "elapsed_seconds": round(elapsed, 6),
        "timeout_seconds": timeout_seconds,
    }
    diagnostics.append(diagnostic)
    raise ParsePaperError(
        "budget_exhausted",
        "research parse execution budget was exhausted",
        status_code=408,
        retryable=False,
        details=diagnostic,
    )


def _create_run_intent(
    sink: ResearchEventSink | None,
    run_id: str,
    *,
    request_fingerprint: str,
    actor_scope: Mapping[str, str],
    budget: Mapping[str, Any] | None = None,
) -> Mapping[str, Any] | None:
    method = getattr(sink, "acquire_run_lease", None)
    if not callable(method):
        method = getattr(sink, "create_run_intent", None)
    if not callable(method):
        return None
    try:
        try:
            result = method(
                run_id,
                request_fingerprint=request_fingerprint,
                actor_scope=dict(actor_scope),
                budget=dict(budget or {}),
            )
        except TypeError as exc:
            if "budget" not in str(exc):
                raise
            result = method(
                run_id,
                request_fingerprint=request_fingerprint,
                actor_scope=dict(actor_scope),
            )
    except Exception as exc:
        raise ParsePaperError(
            "run_intent_persist_failed",
            "research parse run intent could not be persisted",
            status_code=503,
            retryable=True,
            details={"run_id": run_id, "error_type": type(exc).__name__},
        ) from exc
    return dict(result) if isinstance(result, Mapping) else None


def _finalize_run(
    sink: ResearchEventSink | None,
    run_id: str,
    payload: Mapping[str, Any],
) -> None:
    method = getattr(sink, "finalize", None)
    if not callable(method):
        return
    try:
        method(run_id, dict(payload))
    except Exception as exc:
        raise ParsePaperError(
            "run_final_persist_failed",
            "research parse final result could not be persisted",
            status_code=503,
            retryable=True,
            details={"run_id": run_id, "error_type": type(exc).__name__},
        ) from exc


def _published_artifacts(document: ResearchDocument) -> dict[str, str]:
    raw = document.metadata.get("published_artifacts")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(kind): str(ref).strip()
        for kind, ref in raw.items()
        if str(kind).strip() and str(ref).strip()
    }


def _artifact_ref_by_kind(artifact_refs: list[str], artifact_type: str) -> str | None:
    marker = f"/{artifact_type}/"
    return next((ref for ref in artifact_refs if marker in ref), None)


def _catalog_status(value: Any) -> str | None:
    if isinstance(value, Mapping):
        status = value.get("status")
    else:
        status = getattr(value, "status", None)
    return str(status) if status in {"catalog_partial", "catalog_ready"} else None


def _refresh_catalog_projection(projection: ResearchCatalogProjection, **kwargs: Any) -> Any:
    """Call older injected projections without dropping the run linkage."""

    method = projection.refresh_from_parse
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if "run_id" not in parameters and not accepts_kwargs:
        kwargs.pop("run_id", None)
    if "include_code" not in parameters and not accepts_kwargs:
        kwargs.pop("include_code", None)
    return method(**kwargs)


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError)) or bool(getattr(exc, "retryable", False))


def _terminal_text_document(
    *,
    paper: ResearchPaper,
    snapshot: ResearchSourceSnapshot,
    content: bytes,
    parser_attempts: list[dict[str, Any]],
) -> ResearchDocument | None:
    """Preserve readable source text when every structured parser fails."""

    text = content.decode("utf-8", errors="replace")
    # Avoid treating binary PDF control bytes as meaningful text. A configured
    # PDF cascade normally handles these; this fallback is for residual text
    # sources and still records that structure was unavailable.
    if text.startswith("%PDF"):
        text = "\n".join(
            line for line in text.splitlines()
            if sum(char.isprintable() for char in line) >= max(8, len(line) // 2)
        )
    text = " ".join(text.split())
    if not text:
        return None
    source_ref = snapshot.lineage.source_refs[0] if snapshot.lineage.source_refs else snapshot.snapshot_id
    source_hash = snapshot.source_hash or snapshot.checksum or sha256(content).hexdigest()
    attempts = [dict(item) for item in parser_attempts]
    attempts.append({
        "backend": "text_fallback",
        "status": "degraded",
        "reason": "all_structured_parsers_failed",
    })
    return ResearchDocument(
        paper_id=paper.paper_id,
        source_hash=source_hash,
        source_snapshot_id=snapshot.snapshot_id,
        title=paper.title,
        abstract=paper.abstract or None,
        authors=list(paper.authors),
        parser_backend="text_fallback",
        parser_version="1",
        normalization_version="text-v1",
        language="en",
        sections=[ResearchSection(
            section_id=stable_research_id("research_section", paper.paper_id, "text_fallback"),
            title="Extracted text",
            level=1,
            text=text,
            source_ref=source_ref,
        )],
        lineage=SourceLineage(
            source_refs=[*snapshot.lineage.source_refs, source_ref],
            source_hash=source_hash,
            metadata={"source_snapshot_id": snapshot.snapshot_id, **dict(snapshot.lineage.metadata)},
        ),
        parser_attempts=attempts,
        quality_report={
            "degraded": True,
            "reason": "all_structured_parsers_failed",
            "body_chars": len(text),
        },
        source_locators=unique_texts([
            source_ref,
            *([str(snapshot.metadata.get("source_locator"))] if snapshot.metadata.get("source_locator") else []),
        ]),
        created_at=snapshot.fetched_at or datetime.now(UTC),
        observed_at=snapshot.observed_at or snapshot.fetched_at or datetime.now(UTC),
        actor_scope=dict(_scope_from_snapshot(snapshot)),
        metadata={
            "degraded": True,
            "parse_source": "text_fallback",
            "fallback_reason": "all_structured_parsers_failed",
            "parser_attempts": attempts,
            "actor_scope": dict(_scope_from_snapshot(snapshot)),
        },
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _as_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return unique_texts([part.strip() for part in value.split(",")])
    if isinstance(value, (list, tuple, set)):
        return unique_texts([str(item) for item in value])
    return []


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _repository_get(
    repository: Any,
    key: str,
    kind: str,
    *,
    actor_scope: Mapping[str, str] | None = None,
) -> Any:
    specialized = getattr(repository, f"get_{kind}", None)
    if callable(specialized):
        return _call_scoped_repository(specialized, key, actor_scope=actor_scope)
    getter = getattr(repository, "get", None)
    return _call_scoped_repository(getter, key, actor_scope=actor_scope) if callable(getter) else None


def _repository_find_external(
    repository: Any,
    external_id: str,
    *,
    actor_scope: Mapping[str, str] | None = None,
) -> Any:
    finder = getattr(repository, "find_by_external_id", None)
    if not callable(finder):
        return None
    return _call_scoped_repository(finder, external_id, actor_scope=actor_scope)


def _repository_find_fingerprint(
    repository: Any,
    fingerprint: str,
    *,
    actor_scope: Mapping[str, str] | None = None,
) -> Any:
    finder = getattr(repository, "find_by_fingerprint", None)
    if not callable(finder):
        return None
    return _call_scoped_repository(finder, fingerprint, actor_scope=actor_scope)


def _call_scoped_repository(method: Any, *args: Any, actor_scope: Mapping[str, str] | None = None) -> Any:
    try:
        return method(*args, actor_scope=actor_scope)
    except TypeError as exc:
        if "actor_scope" not in str(exc):
            raise
        if actor_scope:
            raise TypeError("scope-aware repository is required for actor-scoped research data") from exc
        return method(*args)


__all__ = [
    "InMemoryResearchEventSink",
    "MetadataOnlySourceResolver",
    "PARSE_STATUSES",
    "ParsePaperError",
    "ParsePaperRequest",
    "ParsePaperResult",
    "ParsePaperUseCase",
    "ResolvedPaperSource",
    "ResearchCatalogProjection",
    "ResearchSourceResolver",
    "identity_from_paper",
    "infer_source_type",
]
