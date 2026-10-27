from __future__ import annotations

import logging
import re
import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, is_dataclass, replace
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4
from urllib.parse import parse_qsl, urlsplit

from backend.research.application import (
    AnalyzePaperRequest,
    AskPaperUseCase,
    ResearchActorScope,
    ResearchDynamicTaskPlanUnavailableError,
    ResearchRunDispositionReconciler,
    classify_research_run_record,
    research_identity_scope_ref,
)
from backend.research.application.catalog import (
    CatalogError,
    ResearchPaperCatalogService,
)
from backend.research.application.parse_paper import (
    ParsePaperError,
    ParsePaperRequest,
    ParsePaperUseCase,
)
from backend.research.domain import stable_research_id
from backend.research.domain.personal_memory_context import (
    PersonalMemoryContext,
    PersonalMemoryContextProvider,
)
from backend.research.graphs.paper_analysis import (
    build_paper_analysis_context_graph_identity,
)
from backend.research.ports.artifact_publication import (
    ResearchArtifactDiagnosticReader,
)
from backend.research.ports.artifact_store import ResearchArtifactReadPort
from backend.research.ports.run_store import (
    ResearchRunDiagnosticStore,
    ResearchRunDisposition,
    ResearchRunRecord,
    ResearchRunStore,
    ResearchRunStoreConflictError,
    ResearchRunStoreError,
    ResearchRunStoreReason,
)
from backend.research.rag import ResearchRetrievalGoal
from framework.harness.context.models import ContextGraphIdentity
from framework.llm.clients import LLMCredentialUnavailableError, credential_unavailable_cause
from interfaces.models import ActorContext


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResearchAnalyzeInput:
    paper_id: str
    source_url: str | None = None
    pdf_url: str | None = None
    run_id: str | None = None
    user_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    memory_namespace: str | None = None
    personal_memory_context: PersonalMemoryContextProvider | None = None


@dataclass(frozen=True)
class ResearchAskInput:
    question: str
    locale: str | None = None
    selection: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    user_id: str | None = None
    memory_namespace: str | None = None
    mode: str = "summary"
    run_id: str | None = None
    request_id: str | None = None
    section_index: int = 0
    limit: int = 5
    generate: bool = False
    gated: bool = True
    personal_memory_context: PersonalMemoryContextProvider | None = None


@dataclass(frozen=True)
class ResearchParseInput:
    source: str
    source_url: str | None = None
    source_type: str | None = None
    content_ref: str | None = None
    run_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    user_id: str | None = None
    memory_namespace: str | None = None


@dataclass(frozen=True)
class ResearchActorInput:
    tenant_id: str | None = None
    user_id: str | None = None
    memory_namespace: str | None = None


class ResearchServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        user_action_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
        self.user_action_required = user_action_required

    @property
    def public_message(self) -> str:
        """Return a stable client-safe message without upstream diagnostics."""

        messages = {
            "invalid_request": "Research request is invalid",
            "forbidden": "Research actor is not authorized",
            "research_parse_unavailable": "Research parse runtime is not configured",
            "source_denied": "Paper source is unavailable",
            "source_fetch_failed": "Paper source is unavailable",
            "document_not_found": "Research document was not found",
            "catalog_not_found": "Research catalog entry was not found",
            "artifact_not_found": "Research artifact was not found",
            "artifact_forbidden": "Research artifact is not authorized",
            "artifact_corrupt": "Research artifact integrity could not be verified",
        }
        return messages.get(self.code, "Research paper operation failed")


class ResearchActorAuthorizationError(ResearchServiceError):
    pass


class ResearchAnalyzeUseCase(Protocol):
    def analyze(self, request: AnalyzePaperRequest) -> Any:
        ...


class ResearchRagAskUseCase(Protocol):
    def rag_ask(
        self,
        paper_id: str,
        question: str,
        *,
        section_index: int,
        limit: int,
        generate: bool,
        gated: bool,
        tenant_id: str | None,
        user_id: str | None,
        memory_namespace: str,
        run_id: str | None = None,
        allowed_source_refs: list[str] | tuple[str, ...] | None = None,
        target_sections: list[str] | tuple[str, ...] | None = None,
        graph_identity: ContextGraphIdentity | None = None,
        personal_memory_context: PersonalMemoryContext | None = None,
        personal_memory_context_provider: PersonalMemoryContextProvider | None = None,
    ) -> dict[str, Any]:
        ...


class InMemoryResearchRunStore:
    def __init__(self) -> None:
        self._records_by_run_id: dict[str, ResearchRunRecord] = {}
        self._run_ids_by_paper_id: dict[str, list[str]] = {}
        self._lock = RLock()

    def save(self, record: ResearchRunRecord) -> None:
        if not isinstance(record, ResearchRunRecord):
            raise TypeError("record must be ResearchRunRecord")
        classified = classify_research_run_record(
            record,
            require_publication_authority=None,
            schema_version=record.schema_version,
        )
        with self._lock:
            existing = self._records_by_run_id.get(record.run_id)
            if existing is not None:
                if existing != classified:
                    raise ResearchRunStoreConflictError(
                        reason=ResearchRunStoreReason.IDENTITY_CONFLICT,
                    )
                return
            self._records_by_run_id[record.run_id] = classified
            run_ids = self._run_ids_by_paper_id.setdefault(record.paper_id, [])
            if record.run_id not in run_ids:
                run_ids.append(record.run_id)

    def get_by_run_id(self, run_id: str) -> ResearchRunRecord | None:
        with self._lock:
            return self._records_by_run_id.get(run_id)

    def get_latest_by_paper_id(self, paper_id: str) -> ResearchRunRecord | None:
        with self._lock:
            run_ids = self._run_ids_by_paper_id.get(paper_id) or []
            for run_id in reversed(run_ids):
                record = self._records_by_run_id.get(run_id)
                if record is not None and record.accepted:
                    return record
            return None

    def list_by_paper_id(self, paper_id: str) -> list[ResearchRunRecord]:
        with self._lock:
            run_ids = reversed(self._run_ids_by_paper_id.get(paper_id) or [])
            return [
                self._records_by_run_id[run_id]
                for run_id in run_ids
                if (
                    run_id in self._records_by_run_id
                    and self._records_by_run_id[run_id].accepted
                )
            ]

    def get_diagnostic_by_run_id(
        self,
        run_id: str,
        *,
        identity_scope_ref: str,
    ) -> ResearchRunRecord | None:
        with self._lock:
            record = self._records_by_run_id.get(run_id)
            if record is None or record.identity_scope_ref != identity_scope_ref:
                return None
            return record

    def list_quarantined_by_paper_id(
        self,
        paper_id: str,
        *,
        identity_scope_ref: str,
    ) -> list[ResearchRunRecord]:
        with self._lock:
            return [
                self._records_by_run_id[run_id]
                for run_id in reversed(
                    self._run_ids_by_paper_id.get(paper_id) or []
                )
                if (
                    run_id in self._records_by_run_id
                    and self._records_by_run_id[run_id].quarantined
                    and self._records_by_run_id[run_id].identity_scope_ref
                    == identity_scope_ref
                )
            ]


class ResearchApplicationService:
    def __init__(
        self,
        *,
        analyze_use_case: ResearchAnalyzeUseCase | None = None,
        ask_use_case: AskPaperUseCase | None = None,
        rag_ask_use_case: ResearchRagAskUseCase | None = None,
        run_store: ResearchRunStore | None = None,
        run_reconciler: ResearchRunDispositionReconciler | None = None,
        diagnostic_artifact_reader: ResearchArtifactDiagnosticReader | None = None,
        artifact_reader: ResearchArtifactReadPort | None = None,
        parse_use_case: ParsePaperUseCase | None = None,
        catalog_service: ResearchPaperCatalogService | None = None,
        runtime_config_identity: str | None = None,
    ) -> None:
        self._analyze_use_case = analyze_use_case or _UnconfiguredAnalyzeUseCase()
        self._ask_use_case = ask_use_case or AskPaperUseCase()
        self._rag_ask_use_case = rag_ask_use_case or _UnconfiguredRagAskUseCase()
        self._run_store = run_store or _DEFAULT_RESEARCH_RUN_STORE
        self._run_reconciler = run_reconciler
        if diagnostic_artifact_reader is not None and not isinstance(
            diagnostic_artifact_reader,
            ResearchArtifactDiagnosticReader,
        ):
            raise TypeError(
                "diagnostic_artifact_reader must implement "
                "ResearchArtifactDiagnosticReader"
            )
        self._diagnostic_artifact_reader = diagnostic_artifact_reader
        if artifact_reader is not None and not isinstance(artifact_reader, ResearchArtifactReadPort):
            raise TypeError("artifact_reader must implement ResearchArtifactReadPort")
        self._artifact_reader = artifact_reader
        self._parse_use_case = parse_use_case
        self._catalog_service = catalog_service
        self._runtime_config_identity = str(runtime_config_identity or "unconfigured").strip()
        if self._run_reconciler is not None:
            self._run_reconciler.reconcile_pending()

    def capability_projection(self) -> dict[str, bool]:
        return {
            "parse": self._parse_use_case is not None,
            "analyze": not isinstance(self._analyze_use_case, _UnconfiguredAnalyzeUseCase),
            "ask": self._ask_use_case is not None,
            "ragAsk": not isinstance(self._rag_ask_use_case, _UnconfiguredRagAskUseCase),
            "artifactRead": self._artifact_reader is not None,
            "catalog": self._catalog_service is not None,
        }

    def capability_runtime_identity(self) -> str:
        components = {
            "analyze": _qualified_type(self._analyze_use_case),
            "ask": _qualified_type(self._ask_use_case),
            "ragAsk": _qualified_type(self._rag_ask_use_case),
            "parse": _qualified_type(self._parse_use_case),
            "catalog": _qualified_type(self._catalog_service),
            "artifactRead": _qualified_type(self._artifact_reader),
            "runtimeConfigIdentity": self._runtime_config_identity,
        }
        encoded = json.dumps(components, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def parse_paper(self, command: ResearchParseInput) -> dict[str, Any]:
        if not isinstance(command, ResearchParseInput):
            raise ResearchServiceError(
                "invalid_request",
                "parse command must be ResearchParseInput",
                status_code=400,
                user_action_required=True,
            )
        use_case = self._parse_use_case
        if use_case is None:
            raise ResearchServiceError(
                "research_parse_unavailable",
                "Research paper parse runtime is not configured",
                status_code=503,
                retryable=False,
                user_action_required=True,
            )
        actor_scope = _resolve_actor_scope(
            self._ask_use_case,
            tenant_id=command.tenant_id,
            user_id=command.user_id,
            memory_namespace=command.memory_namespace,
        )
        try:
            result = use_case.parse(
                ParsePaperRequest(
                    source=command.source,
                    source_url=command.source_url,
                    source_type=command.source_type,
                    content_ref=command.content_ref,
                    run_id=command.run_id,
                    tenant_id=actor_scope.tenant_id,
                    user_id=actor_scope.user_id,
                    memory_namespace=actor_scope.memory_namespace,
                    actor_scope=actor_scope.to_metadata(),
                    options=dict(command.options),
                    metadata=dict(command.metadata),
                )
            )
        except ParsePaperError as exc:
            raise ResearchServiceError(
                exc.code,
                exc.message,
                status_code=exc.status_code,
                details=exc.details,
                retryable=exc.retryable,
                user_action_required=exc.status_code < 500,
            ) from exc
        except ValueError as exc:
            raise ResearchServiceError("invalid_request", str(exc), status_code=400, user_action_required=True) from exc
        except Exception as exc:
            credential_failure = credential_unavailable_cause(exc)
            if credential_failure is not None:
                raise model_credential_service_error(credential_failure) from exc
            raise ResearchServiceError(
                "research_parse_failed",
                "Research paper parse request failed",
                status_code=500,
                details={"error_type": type(exc).__name__},
                retryable=True,
            ) from exc
        if hasattr(result, "to_contract"):
            payload = result.to_contract()
        elif isinstance(result, Mapping):
            payload = dict(result)
        else:
            payload = _to_dict(result)
        payload.setdefault("metadata", {})
        payload["metadata"] = {
            **_mapping(payload.get("metadata")),
            "actorScope": actor_scope.to_metadata(),
        }
        return payload

    def get_sources(self, paper_id: str, *, actor: ResearchActorInput | None = None) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        snapshots = service.list_sources(paper_id, actor_scope=actor_scope.to_metadata())
        return {
            "paperId": paper_id,
            "sources": [_to_dict(snapshot) for snapshot in snapshots],
            "provenance": {"actorScope": actor_scope.to_metadata()},
        }

    def get_document(self, paper_id: str, *, actor: ResearchActorInput | None = None) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        document = service.get_document(paper_id, actor_scope=actor_scope.to_metadata())
        if document is None:
            raise ResearchServiceError("document_not_found", f"document not found for paper {paper_id}", status_code=404, user_action_required=True)
        return {"paperId": paper_id, "document": _to_dict(document), "provenance": {"actorScope": actor_scope.to_metadata()}}

    def get_catalog(self, paper_id: str, *, actor: ResearchActorInput | None = None) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            entry = service.get_catalog(paper_id, actor_scope=actor_scope.to_metadata())
        except CatalogError as exc:
            raise ResearchServiceError(exc.code, exc.message, status_code=exc.status_code, details=exc.details, user_action_required=exc.status_code < 500) from exc
        return {"paperId": paper_id, "catalog": _to_dict(entry), "provenance": {"actorScope": actor_scope.to_metadata()}}

    def recover_prepared_source(
        self, command: ResearchParseInput,
    ) -> dict[str, Any] | None:
        use_case = self._parse_use_case
        if use_case is None:
            return None
        actor_scope = _resolve_actor_scope(
            self._ask_use_case, tenant_id=command.tenant_id,
            user_id=command.user_id, memory_namespace=command.memory_namespace,
        )
        try:
            result = use_case.recover_completed(ParsePaperRequest(
                source=command.source, source_url=command.source_url,
                source_type=command.source_type, content_ref=command.content_ref,
                run_id=command.run_id, tenant_id=actor_scope.tenant_id,
                user_id=actor_scope.user_id, memory_namespace=actor_scope.memory_namespace,
                actor_scope=actor_scope.to_metadata(), options=dict(command.options),
                metadata=dict(command.metadata),
            ))
        except ParsePaperError as exc:
            raise ResearchServiceError(
                exc.code, exc.message, status_code=exc.status_code,
                retryable=False, user_action_required=True,
            ) from exc
        if result is None:
            return None
        actual = _normalized_sha256_identity(getattr(result.document, "source_hash", None))
        if actual is None:
            raise ResearchServiceError(
                "source_identity_unavailable", "Prepared source has no verified content identity",
                status_code=422, user_action_required=True,
            )
        return {
            "status": "ready", "paperId": result.paper_id,
            "sourceRevision": f"sha256:{actual}",
            "sourceFingerprint": f"sha256:{actual}",
        }

    def get_document_identity(
        self,
        paper_id: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, str]:
        payload = self.get_document(paper_id, actor=actor)
        document = payload.get("document")
        actual = _normalized_sha256_identity(
            document.get("source_hash") if isinstance(document, Mapping) else None
        )
        if actual is None:
            raise ResearchServiceError(
                "source_identity_unavailable",
                "Parsed document does not expose a SHA-256 content identity",
                status_code=422,
                user_action_required=True,
            )
        fingerprint = f"sha256:{actual}"
        return {
            "paperId": paper_id,
            "sourceRevision": fingerprint,
            "sourceFingerprint": fingerprint,
        }

    def resolve_analyze_source(
        self,
        paper_id: str,
        *,
        source_revision: str,
        source_fingerprint: str,
        actor: ResearchActorInput | None = None,
    ) -> str:
        """Resolve an immutable actor-visible source locator for an existing parsed document."""

        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        document = service.get_document(paper_id, actor_scope=actor_scope.to_metadata())
        if document is None or document.paper_id != paper_id:
            raise ResearchServiceError(
                "document_not_found",
                "The exact actor-visible parsed document was not found",
                status_code=404,
                user_action_required=True,
            )
        actual_revision = _normalized_sha256_identity(document.source_hash)
        requested_revision = _normalized_sha256_identity(source_revision)
        requested_fingerprint = _normalized_sha256_identity(source_fingerprint)
        if (
            actual_revision is None
            or requested_revision is None
            or requested_fingerprint is None
            or actual_revision != requested_revision
            or actual_revision != requested_fingerprint
        ):
            raise ResearchServiceError(
                "source_identity_mismatch",
                "The parsed document does not match the requested source identity",
                status_code=409,
                user_action_required=True,
            )
        candidates = [
            str(value).strip()
            for value in [*document.source_locators, *document.lineage.source_refs]
            if str(value or "").strip()
        ]
        if not candidates:
            raise ResearchServiceError(
                "source_identity_unavailable",
                "The parsed document has no immutable source locator",
                status_code=422,
                user_action_required=True,
            )
        return candidates[0]


    def get_code(self, paper_id: str, *, actor: ResearchActorInput | None = None) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            profiles = service.get_code(paper_id, actor_scope=actor_scope.to_metadata())
        except CatalogError as exc:
            raise ResearchServiceError(exc.code, exc.message, status_code=exc.status_code, details=exc.details, user_action_required=exc.status_code < 500) from exc
        return {"paperId": paper_id, "repositories": profiles, "provenance": {"actorScope": actor_scope.to_metadata()}}

    def get_benchmarks(
        self,
        paper_id: str,
        *,
        benchmark_id: str | None = None,
        metric_id: str | None = None,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        split: str | None = None,
        evaluation_protocol: str | None = None,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            payload = service.get_benchmarks(
                paper_id,
                actor_scope=actor_scope.to_metadata(),
                benchmark_id=benchmark_id,
                metric_id=metric_id,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                split=split,
                evaluation_protocol=evaluation_protocol,
            )
        except CatalogError as exc:
            raise ResearchServiceError(exc.code, exc.message, status_code=exc.status_code, details=exc.details, user_action_required=exc.status_code < 500) from exc
        payload["provenance"] = {"actorScope": actor_scope.to_metadata()}
        return payload

    def get_artifact(
        self,
        artifact_ref: str,
        *,
        include_payload: bool = False,
        max_chars: int = 200_000,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        """Read a Research Catalog artifact through the scoped read port."""

        ref = _require_text(artifact_ref, "artifactRef")
        if not isinstance(include_payload, bool):
            raise ResearchServiceError(
                "invalid_request",
                "includePayload must be boolean",
                status_code=400,
                user_action_required=True,
            )
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= 1_000_000:
            raise ResearchServiceError(
                "invalid_request",
                "maxChars must be between 1 and 1000000",
                status_code=400,
                user_action_required=True,
            )
        reader = self._artifact_reader
        if reader is None:
            raise ResearchServiceError(
                "research_artifact_unavailable",
                "Research artifact reader is not configured",
                status_code=503,
                retryable=False,
                user_action_required=True,
            )
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            payload = reader.read(
                ref,
                actor_scope=actor_scope.to_metadata(),
                include_payload=include_payload,
                max_chars=max_chars,
            )
        except FileNotFoundError as exc:
            raise ResearchServiceError(
                "artifact_not_found",
                "research artifact was not found",
                status_code=404,
                user_action_required=True,
            ) from exc
        except PermissionError as exc:
            raise ResearchServiceError(
                "artifact_forbidden",
                "research artifact is not authorized",
                status_code=403,
                user_action_required=True,
            ) from exc
        except Exception as exc:
            raise ResearchServiceError(
                "artifact_corrupt",
                "research artifact integrity could not be verified",
                status_code=409,
                details={"error_type": type(exc).__name__},
                user_action_required=True,
            ) from exc
        return {
            **dict(payload),
            "provenance": {"actorScope": actor_scope.to_metadata()},
        }

    def list_catalog_papers(
        self,
        *,
        query: str = "",
        limit: int = 50,
        cursor: str | None = None,
        sort: str = "observed_at desc, stable_id asc",
        include_diagnostics: bool = False,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        if sort not in {"observed_at desc, stable_id asc", "title asc, stable_id asc"}:
            raise ResearchServiceError(
                "invalid_request",
                "unsupported catalog sort",
                status_code=400,
                details={"allowed_sort": ["observed_at desc, stable_id asc", "title asc, stable_id asc"]},
                user_action_required=True,
            )
        try:
            if isinstance(limit, bool) or not 1 <= int(limit) <= 200:
                raise CatalogError("invalid_limit", "limit must be between 1 and 200", status_code=400)
            all_entries = service.search_papers(query, limit=200, actor_scope=actor_scope.to_metadata())
            if sort == "title asc, stable_id asc":
                all_entries.sort(key=lambda entry: (entry.identity.title.casefold(), entry.paper_id))
            else:
                all_entries.sort(
                    key=lambda entry: (
                        entry.observed_at.timestamp() if entry.observed_at is not None else 0.0,
                        entry.paper_id,
                    ),
                    reverse=True,
                )
            offset = _decode_catalog_cursor(
                cursor,
                query=query,
                sort=sort,
                scope=actor_scope.to_metadata(),
            )
            entries = all_entries[offset : offset + int(limit)]
        except CatalogError as exc:
            raise ResearchServiceError(exc.code, exc.message, status_code=exc.status_code, details=exc.details, user_action_required=exc.status_code < 500) from exc
        payload_entries = [_to_dict(entry) for entry in entries]
        if not include_diagnostics:
            for entry in payload_entries:
                metadata = entry.get("metadata")
                if isinstance(metadata, dict):
                    metadata.pop("diagnostics", None)
        next_cursor = None
        if offset + len(entries) < len(all_entries):
            next_cursor = _encode_catalog_cursor(
                offset=offset + len(entries),
                query=query,
                sort=sort,
                scope=actor_scope.to_metadata(),
            )
        return {
            "papers": payload_entries,
            "query": query,
            "pagination": {"limit": int(limit), "nextCursor": next_cursor, "sort": sort},
            "provenance": {"actorScope": actor_scope.to_metadata()},
        }

    def get_leaderboards(
        self,
        *,
        benchmark_id: str | None = None,
        metric_id: str | None = None,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        split: str | None = None,
        evaluation_protocol: str | None = None,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        scores = service.all_scores(actor_scope=actor_scope.to_metadata())
        return {
            "leaderboards": service.list_leaderboards(
                scores,
                benchmark_id=benchmark_id,
                metric_id=metric_id,
                dataset_id=dataset_id,
                dataset_version=dataset_version,
                split=split,
                evaluation_protocol=evaluation_protocol,
            ),
            "provenance": {"actorScope": actor_scope.to_metadata()},
        }

    def refresh_catalog(self, paper_id: str | None = None, *, actor: ResearchActorInput | None = None) -> dict[str, Any]:
        service = self._require_catalog_service()
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        if self._parse_use_case is None:
            raise ResearchServiceError(
                "research_parse_unavailable",
                "Research paper parse runtime is not configured",
                status_code=503,
                retryable=False,
                user_action_required=True,
            )
        try:
            entries = (
                [service.get_catalog(paper_id, actor_scope=actor_scope.to_metadata())]
                if paper_id
                else service.search_papers("", limit=200, actor_scope=actor_scope.to_metadata())
            )
        except CatalogError as exc:
            raise ResearchServiceError(
                exc.code,
                exc.message,
                status_code=exc.status_code,
                details=exc.details,
                user_action_required=exc.status_code < 500,
            ) from exc
        outcomes: list[dict[str, Any]] = []
        for entry in entries:
            snapshots = service.list_sources(entry.paper_id, actor_scope=actor_scope.to_metadata())
            snapshot = next(
                (item for item in reversed(snapshots) if item.access_status in {"available", "metadata_only"}),
                None,
            )
            if snapshot is None:
                outcomes.append({"paperId": entry.paper_id, "status": "failed", "code": "source_snapshot_missing"})
                continue
            # URL sources must be refreshed through their canonical URL. An
            # external id such as a DOI is an identifier, not a fetch target;
            # arXiv/OpenReview adapters are the explicit exceptions.
            source = (
                snapshot.external_id
                if snapshot.source_type in {"arxiv", "openreview"} and snapshot.external_id
                else snapshot.canonical_url or snapshot.external_id
            )
            if not source:
                outcomes.append({"paperId": entry.paper_id, "status": "failed", "code": "source_locator_missing"})
                continue
            try:
                refresh_metadata = {}
                if snapshot.source_type == "github":
                    # A repository observation must be explicitly attached to
                    # the already-known paper; the resolver rejects
                    # stand-alone GitHub URLs by design.
                    refresh_metadata = {
                        "paper_id": entry.paper_id,
                        "title": entry.identity.title,
                    }
                outcome = self.parse_paper(
                    ResearchParseInput(
                        source=source,
                        source_type=snapshot.source_type,
                        tenant_id=actor_scope.tenant_id,
                        user_id=actor_scope.user_id,
                        memory_namespace=actor_scope.memory_namespace,
                        options={"refresh": True},
                        metadata=refresh_metadata,
                    )
                )
            except ResearchServiceError as exc:
                outcomes.append({
                    "paperId": entry.paper_id,
                    "status": "failed",
                    "code": exc.code,
                    "message": exc.public_message,
                    "retryable": exc.retryable,
                })
            else:
                outcomes.append(outcome)
        if paper_id:
            outcome = outcomes[0] if outcomes else {"paperId": paper_id, "status": "failed"}
            return {
                **outcome,
                "refreshed": outcome.get("status") not in {"failed", "metadata_only"},
                "provenance": {"actorScope": actor_scope.to_metadata()},
            }
        failed = sum(item.get("status") == "failed" for item in outcomes)
        return {
            "status": "catalog_partial" if failed else "catalog_ready",
            "refreshed": True,
            "outcomes": outcomes,
            "provenance": {"actorScope": actor_scope.to_metadata()},
        }

    def _require_catalog_service(self) -> ResearchPaperCatalogService:
        if self._catalog_service is None:
            raise ResearchServiceError(
                "research_catalog_unavailable",
                "Research paper catalog runtime is not configured",
                status_code=503,
                retryable=False,
                user_action_required=True,
            )
        return self._catalog_service

    def analyze_paper(self, command: ResearchAnalyzeInput) -> dict[str, Any]:
        paper_id = _require_text(command.paper_id, "paperId")
        source_ref = _source_ref(command)
        run_id = _optional_text(command.run_id) or f"research-run-{uuid4().hex}"
        options = _runtime_options(command)
        actor_scope = _resolve_actor_scope(
            self._ask_use_case,
            tenant_id=command.tenant_id,
            user_id=command.user_id,
            memory_namespace=command.memory_namespace,
        )
        request = AnalyzePaperRequest(
            run_id=run_id,
            paper_id=paper_id,
            source_ref=source_ref,
            user_id=actor_scope.user_id,
            options=options,
            tenant_id=actor_scope.tenant_id,
            memory_namespace=actor_scope.memory_namespace,
            personal_memory_context=command.personal_memory_context,
        )
        try:
            result = self._analyze_use_case.analyze(request)
        except ResearchServiceError as exc:
            try:
                self._reconcile_failed_run(request, actor_scope)
            except Exception as recovery_error:
                _record_recovery_failure(exc, recovery_error)
                raise exc from recovery_error
            raise
        except ValueError as exc:
            try:
                recovered = self._reconcile_failed_run(request, actor_scope)
            except Exception as recovery_error:
                service_error = _research_runtime_service_error(exc)
                _record_recovery_failure(service_error, recovery_error)
                raise service_error from recovery_error
            if recovered is not None:
                raise _research_runtime_service_error(exc) from exc
            raise ResearchServiceError(
                "invalid_request",
                str(exc),
                status_code=400,
                user_action_required=True,
            ) from exc
        except Exception as exc:
            service_error = _research_runtime_service_error(exc)
            try:
                self._reconcile_failed_run(request, actor_scope)
            except Exception as recovery_error:
                _record_recovery_failure(service_error, recovery_error)
                raise service_error from recovery_error
            raise service_error from exc

        result = _bind_result_source_identity(result, command.metadata)
        record = ResearchRunRecord(run_id=run_id, paper_id=paper_id, result=result)
        execution_error_type = _result_execution_error_type(result)
        if execution_error_type is not None:
            service_error = ResearchServiceError(
                "research_run_failed",
                "research run failed",
                status_code=500,
                details={"error_type": execution_error_type},
                retryable=True,
            )
            try:
                recovered = self._reconcile_failed_run(request, actor_scope)
            except Exception as recovery_error:
                _record_recovery_failure(service_error, recovery_error)
                raise service_error from recovery_error
            if recovered is None:
                try:
                    self._run_store.save(record)
                except ResearchRunStoreError as exc:
                    raise _run_store_service_error(exc, operation="save") from None
            raise service_error
        try:
            self._run_store.save(record)
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(exc, operation="save") from None
        stored = self._run_store.get_by_run_id(run_id) or record
        self._ensure_run_succeeded(stored)
        self._ensure_quality_passed(stored)
        self._ensure_run_accepted(stored)
        return self._analyze_response(stored)

    def get_analysis(
        self,
        paper_id: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        record = self._record_for_paper(paper_id, actor_scope=actor_scope)
        self._ensure_run_succeeded(record)
        self._ensure_quality_passed(record)
        analysis = getattr(record.result, "analysis", None)
        if analysis is None:
            raise ResearchServiceError(
                "analysis_not_found",
                f"analysis not found for paper {record.paper_id}",
                status_code=404,
                user_action_required=True,
            )
        return {
            "runId": record.run_id,
            "paperId": record.paper_id,
            "status": _result_status(record.result),
            "analysis": _to_dict(analysis),
            "quality": _to_dict(getattr(record.result, "quality", None)),
            "analysisRef": _artifact_ref(record.result, "research-analysis"),
            "qualityRef": _artifact_ref(record.result, "research-quality-result"),
            "traceRef": _trace_ref(record.result, record.run_id),
            "metadata": {"artifactRefs": _artifact_refs(record.result)},
        }

    def get_run_projection(
        self,
        run_id: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        """Project one exact Research run without substituting the latest paper result."""

        normalized_run_id = _require_text(run_id, "runId")
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            record = self._run_store.get_by_run_id(normalized_run_id)
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(exc, operation="get_by_run_id") from None
        if record is None and isinstance(self._run_store, ResearchRunDiagnosticStore):
            try:
                record = self._run_store.get_diagnostic_by_run_id(
                    normalized_run_id,
                    identity_scope_ref=research_identity_scope_ref(actor_scope.to_metadata()),
                )
            except ResearchRunStoreError as exc:
                raise _run_store_service_error(exc, operation="get_diagnostic_by_run_id") from None
        if record is None:
            raise ResearchServiceError(
                "research_run_not_found",
                "Research run was not found",
                status_code=404,
                user_action_required=True,
            )
        _ensure_record_visible_to_actor(record, actor_scope)
        result = record.result
        quality = getattr(result, "quality", None)
        quality_passed = getattr(quality, "passed", None) is True
        succeeded = _result_status(result) == "succeeded" and getattr(result, "analysis", None) is not None
        accepted = record.disposition is ResearchRunDisposition.ACCEPTED
        artifact_status, artifact_reason = self._validate_report_artifacts(
            record,
            actor_scope=actor_scope,
        )
        return {
            "runId": record.run_id,
            "paperId": record.paper_id,
            "status": _result_status(result),
            "disposition": record.disposition.value if record.disposition is not None else "quarantine",
            "accepted": accepted,
            "qualityPassed": quality_passed,
            "eligibleForAcceptedExport": bool(
                succeeded
                and quality_passed
                and accepted
                and artifact_status == "valid"
            ),
            "artifactStatus": artifact_status,
            "artifactReason": artifact_reason,
            "sourceRevision": _run_source_revision(result),
            "sourceFingerprint": _run_source_fingerprint(result),
            "analysis": _to_dict(getattr(result, "analysis", None)),
            "quality": _to_dict(quality),
            "paper": _to_dict(getattr(result, "paper_card", None)),
            "reader": _to_dict(getattr(result, "reader_payload", None)),
            "artifactRefs": _artifact_refs(result),
            "traceRef": _trace_ref(result, record.run_id),
            "diagnostics": _safe_run_diagnostics(result),
        }
    def _validate_report_artifacts(
        self,
        record: ResearchRunRecord,
        *,
        actor_scope: ResearchActorScope,
    ) -> tuple[str, str | None]:
        result = record.result
        required = (
            ("research-analysis", _artifact_ref(result, "research-analysis"), _to_dict(getattr(result, "analysis", None))),
            ("research-quality-result", _artifact_ref(result, "research-quality-result"), _to_dict(getattr(result, "quality", None))),
        )
        if any(ref is None for _, ref, _ in required):
            return "missing", "required_report_artifact_missing"
        reader = self._artifact_reader
        if reader is None:
            return "missing", "research_artifact_reader_unavailable"
        for expected_type, ref, expected_payload in required:
            try:
                envelope = reader.read(
                    str(ref),
                    actor_scope=actor_scope.to_metadata(),
                    include_payload=True,
                    max_chars=1_000_000,
                )
            except FileNotFoundError:
                return "missing", "artifact_not_found"
            except PermissionError:
                return "unauthorized", "artifact_forbidden"
            except Exception:
                return "corrupt", "artifact_corrupt"
            artifact_type = str(envelope.get("artifactType") or "") if isinstance(envelope, Mapping) else ""
            metadata = envelope.get("metadata") if isinstance(envelope, Mapping) else None
            payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
            if (
                artifact_type != expected_type
                or not isinstance(metadata, Mapping)
                or metadata.get("run_id") != record.run_id
                or metadata.get("paper_id") != record.paper_id
                or metadata.get("artifact_type") != expected_type
                or payload != expected_payload
            ):
                return "corrupt", "artifact_identity_mismatch"
            if expected_type == "research-analysis" and (
                not isinstance(payload, Mapping) or payload.get("paper_id") != record.paper_id
            ):
                return "corrupt", "artifact_identity_mismatch"
            if expected_type == "research-quality-result" and (
                not isinstance(payload, Mapping) or payload.get("target_id") != record.paper_id
            ):
                return "corrupt", "artifact_identity_mismatch"
        return "valid", None

    def get_reader(
        self,
        paper_id: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        record = self._record_for_paper(paper_id, actor_scope=actor_scope)
        self._ensure_run_succeeded(record)
        self._ensure_quality_passed(record)
        reader_payload = getattr(record.result, "reader_payload", None)
        if reader_payload is None:
            raise ResearchServiceError(
                "analysis_not_found",
                f"reader payload not found for paper {record.paper_id}",
                status_code=404,
                user_action_required=True,
            )
        return {
            "paper": _to_dict(getattr(reader_payload, "paper", None)),
            "document": _to_dict(getattr(reader_payload, "document", None)),
            "analysis": _to_dict(getattr(reader_payload, "analysis", None)),
            "evidence": _to_dict(getattr(reader_payload, "evidence", None)),
            "navigation": [_to_dict(item) for item in getattr(reader_payload, "navigation", [])],
            "quality": [_to_dict(item) for item in getattr(reader_payload, "quality", [])],
            "metadata": {
                **_mapping(getattr(reader_payload, "metadata", {})),
                "runId": record.run_id,
                "paperId": record.paper_id,
                "status": _result_status(record.result),
                "readerPayloadRef": _artifact_ref(record.result, "research-reader-payload"),
                "traceRef": _trace_ref(record.result, record.run_id),
            },
        }

    def ask_paper(
        self,
        paper_id: str,
        request: ResearchAskInput,
        *,
        graph_identity: ContextGraphIdentity | None = None,
    ) -> dict[str, Any]:
        question = _require_text(request.question, "question")
        mode = _ask_mode(request.mode)
        actor_scope = _resolve_actor_scope(
            self._ask_use_case,
            tenant_id=request.tenant_id,
            user_id=(
                request.user_id
                or _optional_text(request.options.get("userId"))
                or _optional_text(request.options.get("user_id"))
            ),
            memory_namespace=request.memory_namespace,
        )
        if mode == "chunk_rag":
            return self._ask_paper_chunks(
                paper_id,
                question,
                request=request,
                actor_scope=actor_scope,
                graph_identity=graph_identity,
            )

        record = (
            self._record_by_run_id(request.run_id, actor_scope=actor_scope, paper_id=paper_id)
            if request.run_id
            else self._record_for_paper(paper_id, actor_scope=actor_scope)
        )
        _ensure_record_visible_to_actor(record, actor_scope)
        self._ensure_run_succeeded(record)
        self._ensure_quality_passed(record)
        reader_payload = getattr(record.result, "reader_payload", None)
        analysis = getattr(record.result, "analysis", None)
        if reader_payload is None or analysis is None:
            raise ResearchServiceError(
                "analysis_not_found",
                f"reader analysis not found for paper {record.paper_id}",
                status_code=404,
                user_action_required=True,
            )
        source_refs = _reader_source_refs(reader_payload)
        question_identity = stable_research_id(
            "research_reader_question",
            record.run_id,
            question,
            json.dumps(request.selection, ensure_ascii=False, sort_keys=True),
            request.request_id or "",
        )
        actual_graph_identity = (
            graph_identity
            or build_paper_analysis_context_graph_identity(
                run_id=record.run_id,
                stage_id="run_research_rag",
            ).with_physical_activity(
                node_id="run_research_rag",
                node_instance_id=stable_research_id(
                    "research_reader_question_node",
                    record.run_id,
                    question_identity,
                ),
                activity_id=question_identity,
                activity_attempt=1,
            )
        )
        generated = self._ask_paper_chunks(
            record.paper_id,
            question,
            request=replace(request, generate=True, gated=True),
            actor_scope=actor_scope,
            graph_identity=actual_graph_identity,
            run_id=record.run_id,
            allowed_source_refs=source_refs,
            target_sections=_selection_targets(request.selection, reader_payload=reader_payload),
        )
        return _reader_answer_projection(
            generated,
            run_id=record.run_id,
            paper_id=record.paper_id,
            question=question,
            source_refs=source_refs,
            reader_payload=reader_payload,
        )

    def recover_paper_answer(
        self,
        paper_id: str,
        request: ResearchAskInput,
        *,
        include_interrupted: bool = False,
    ) -> dict[str, Any] | None:
        question = _require_text(request.question, "question")
        actor_scope = _resolve_actor_scope(
            self._ask_use_case,
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            memory_namespace=request.memory_namespace,
        )
        record = self._record_by_run_id(request.run_id, actor_scope=actor_scope, paper_id=paper_id) if request.run_id else None
        if record is None:
            return None
        self._ensure_run_succeeded(record)
        self._ensure_quality_passed(record)
        reader_payload = getattr(record.result, "reader_payload", None)
        if reader_payload is None:
            return None
        question_identity = stable_research_id(
            "research_reader_question",
            record.run_id,
            question,
            json.dumps(request.selection, ensure_ascii=False, sort_keys=True),
            request.request_id or "",
        )
        recover = getattr(self._rag_ask_use_case, "recover_gated_answer", None)
        if not callable(recover):
            return None
        generated = recover(
            session_id=stable_research_id(
                "paper_rag_session", record.run_id, question_identity, record.paper_id, question
            ),
            paper_id=record.paper_id,
            question=question,
            tenant_id=actor_scope.tenant_id,
            user_id=actor_scope.user_id,
            memory_namespace=actor_scope.memory_namespace,
            include_interrupted=include_interrupted,
        )
        if not isinstance(generated, Mapping):
            return None
        source_refs = _reader_source_refs(reader_payload)
        return _reader_answer_projection(
            generated,
            run_id=record.run_id,
            paper_id=record.paper_id,
            question=question,
            source_refs=source_refs,
            reader_payload=reader_payload,
        )

    def _ask_paper_chunks(
        self,
        paper_id: str,
        question: str,
        *,
        request: ResearchAskInput,
        actor_scope: ResearchActorScope,
        graph_identity: ContextGraphIdentity | None,
        run_id: str | None = None,
        allowed_source_refs: list[str] | tuple[str, ...] | None = None,
        target_sections: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        normalized_paper_id = _require_text(paper_id, "paperId")
        section_index = _bounded_int(
            request.section_index,
            "sectionIndex",
            minimum=0,
        )
        limit = _bounded_int(
            request.limit,
            "limit",
            minimum=1,
            maximum=20,
        )
        try:
            rag_kwargs = {
                "section_index": section_index,
                "limit": limit,
                "generate": bool(request.generate),
                "gated": bool(request.gated),
                "tenant_id": actor_scope.tenant_id,
                "user_id": actor_scope.user_id,
                "memory_namespace": actor_scope.memory_namespace,
                "run_id": run_id,
                "allowed_source_refs": allowed_source_refs,
                "target_sections": target_sections,
            }
            provider = request.personal_memory_context
            if provider is not None:
                if not isinstance(provider, PersonalMemoryContextProvider):
                    raise ValueError("personal memory context provider is invalid")
                # Keep the authority live through retrieval and every generation retry.
                rag_kwargs["personal_memory_context_provider"] = provider
            if graph_identity is not None:
                rag_kwargs["graph_identity"] = graph_identity
            return self._rag_ask_use_case.rag_ask(
                normalized_paper_id,
                question,
                **rag_kwargs,
            )
        except ResearchServiceError:
            raise
        except ValueError as exc:
            raise ResearchServiceError(
                "invalid_request",
                str(exc),
                status_code=400,
                user_action_required=True,
            ) from exc
        except Exception as exc:
            credential_failure = credential_unavailable_cause(exc)
            if credential_failure is not None:
                raise model_credential_service_error(credential_failure) from exc
            raise ResearchServiceError(
                "research_run_failed",
                "research RAG request failed",
                status_code=500,
                details={"error_type": type(exc).__name__},
                retryable=True,
            ) from exc

    def get_trace(
        self,
        run_id: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        normalized_run_id = _require_text(run_id, "runId")
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        try:
            if isinstance(self._run_store, ResearchRunDiagnosticStore):
                record = self._run_store.get_diagnostic_by_run_id(
                    normalized_run_id,
                    identity_scope_ref=research_identity_scope_ref(
                        actor_scope.to_metadata()
                    ),
                )
            else:
                record = self._run_store.get_by_run_id(normalized_run_id)
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(exc, operation="read_by_run") from None
        if record is None and isinstance(self._run_store, ResearchRunDiagnosticStore):
            unscoped = self._run_store.get_by_run_id(normalized_run_id)
            if unscoped is not None:
                _ensure_record_visible_to_actor(unscoped, actor_scope)
        if record is None and self._run_reconciler is not None:
            record = self._run_reconciler.reconcile_run(
                normalized_run_id,
                identity_scope_ref=research_identity_scope_ref(
                    actor_scope.to_metadata()
                ),
            )
        if record is None:
            raise ResearchServiceError(
                "research_run_failed",
                f"research run not found: {normalized_run_id}",
                status_code=404,
                user_action_required=True,
            )
        _ensure_record_visible_to_actor(record, actor_scope)
        return {
            "runId": record.run_id,
            "paperId": record.paper_id,
            "status": _result_status(record.result),
            "traceRef": _trace_ref(record.result, record.run_id),
            "trace": _to_dict(getattr(record.result, "trace", None)),
            "transcript": _to_dict(getattr(record.result, "transcript", None)),
            "metadata": {
                "artifactRefs": _artifact_refs(record.result),
                "diagnostics": _to_dict(getattr(record.result, "diagnostics", {})),
            },
        }

    def read_diagnostic_artifact(
        self,
        run_id: str,
        artifact_ref: str,
        *,
        actor: ResearchActorInput | None = None,
    ) -> dict[str, Any]:
        """Read one quarantined artifact through the scoped application path.

        The method is intentionally not part of the normal paper/latest query
        surface.  It requires an exact quarantined run record, the caller's
        identity scope, the record's subject scope, and a ref already carried
        by that record before invoking the injected read-only adapter.
        """

        normalized_run_id = _require_text(run_id, "runId")
        normalized_ref = _require_text(artifact_ref, "artifactRef")
        actor_scope = _resolve_actor_input(self._ask_use_case, actor)
        identity_scope_ref = research_identity_scope_ref(actor_scope.to_metadata())
        try:
            if isinstance(self._run_store, ResearchRunDiagnosticStore):
                record = self._run_store.get_diagnostic_by_run_id(
                    normalized_run_id,
                    identity_scope_ref=identity_scope_ref,
                )
            else:
                record = None
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(
                exc,
                operation="read_by_run",
            ) from None
        if record is None:
            try:
                unscoped = self._run_store.get_by_run_id(normalized_run_id)
            except ResearchRunStoreError as exc:
                raise _run_store_service_error(
                    exc,
                    operation="read_by_run",
                ) from None
            if unscoped is not None:
                _ensure_record_visible_to_actor(unscoped, actor_scope)
            raise _diagnostic_artifact_not_found(normalized_run_id)
        _ensure_record_visible_to_actor(record, actor_scope)
        if record.disposition is not ResearchRunDisposition.QUARANTINE:
            raise _diagnostic_artifact_not_found(normalized_run_id)
        if normalized_ref not in _artifact_refs(record.result).values():
            raise _diagnostic_artifact_not_found(normalized_run_id)
        subject_scope_ref = record.subject_scope_ref
        if not isinstance(subject_scope_ref, str) or not subject_scope_ref:
            raise _diagnostic_artifact_not_found(normalized_run_id)
        reader = self._diagnostic_artifact_reader
        if reader is None:
            raise ResearchServiceError(
                "research_run_failed",
                "Research diagnostic artifact reader is not configured",
                status_code=503,
                retryable=False,
                user_action_required=True,
            )
        try:
            payload = reader.read_diagnostic_artifact(
                normalized_ref,
                identity_scope_ref=identity_scope_ref,
                subject_scope_ref=subject_scope_ref,
            )
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(
                "research_run_failed",
                "Research diagnostic artifact could not be read",
                status_code=500,
                details={"error_type": type(exc).__name__},
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping):
            raise ResearchServiceError(
                "research_run_failed",
                "Research diagnostic artifact is invalid",
                status_code=500,
                retryable=False,
            )
        return dict(payload)

    def _record_by_run_id(
        self,
        run_id: str,
        *,
        actor_scope: ResearchActorScope,
        paper_id: str | None = None,
    ) -> ResearchRunRecord:
        normalized_run_id = _require_text(run_id, "runId")
        try:
            record = self._run_store.get_by_run_id(normalized_run_id)
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(exc, operation="get_by_run_id") from None
        if record is None:
            raise ResearchServiceError(
                "research_run_not_found",
                "Research run was not found",
                status_code=404,
                user_action_required=True,
            )
        _ensure_record_visible_to_actor(record, actor_scope)
        if paper_id is not None and record.paper_id != _require_text(paper_id, "paperId"):
            raise ResearchServiceError(
                "research_run_conflict",
                "Research run does not belong to the selected paper",
                status_code=409,
                user_action_required=True,
            )
        return record

    def _record_for_paper(
        self,
        paper_id: str,
        *,
        actor_scope: ResearchActorScope,
    ) -> ResearchRunRecord:
        normalized = _require_text(paper_id, "paperId")
        try:
            records = self._run_store.list_by_paper_id(normalized)
        except ResearchRunStoreError as exc:
            raise _run_store_service_error(
                exc,
                operation="list_by_paper",
            ) from None
        for record in records:
            if (
                record.disposition is ResearchRunDisposition.ACCEPTED
                and _record_visible_to_actor(record, actor_scope)
            ):
                return record
        raise ResearchServiceError(
            "paper_not_found",
            f"paper not found: {normalized}",
            status_code=404,
            user_action_required=True,
        )

    def _ensure_run_succeeded(self, record: ResearchRunRecord) -> None:
        status = _result_status(record.result)
        if status in {
            "failed",
            "halted",
            "cancelled",
            "blocked",
            "waiting_approval",
        }:
            raise ResearchServiceError(
                "research_run_failed",
                f"research run {record.run_id} ended with status {status}",
                status_code=500,
                details={"runId": record.run_id, "status": status},
                retryable=status != "cancelled",
            )

    def _ensure_quality_passed(self, record: ResearchRunRecord) -> None:
        quality = getattr(record.result, "quality", None)
        if quality is not None and getattr(quality, "passed", None) is True:
            return
        raise ResearchServiceError(
            "quality_gate_failed",
            f"research quality gates failed for paper {record.paper_id}",
            status_code=422,
            details={
                "runId": record.run_id,
                "paperId": record.paper_id,
                "gateFailures": _quality_gate_failures(quality),
            },
            user_action_required=True,
        )

    def _ensure_run_accepted(self, record: ResearchRunRecord) -> None:
        if record.disposition is ResearchRunDisposition.ACCEPTED:
            return
        raise ResearchServiceError(
            "research_run_failed",
            f"research run {record.run_id} was quarantined",
            status_code=500,
            details={
                "runId": record.run_id,
                "status": _result_status(record.result),
            },
            retryable=False,
        )

    def _reconcile_failed_run(
        self,
        request: AnalyzePaperRequest,
        actor_scope: ResearchActorScope,
    ) -> ResearchRunRecord | None:
        if self._run_reconciler is None:
            return None
        return self._run_reconciler.reconcile_failed_run(
            request,
            identity_scope_ref=research_identity_scope_ref(
                actor_scope.to_metadata()
            ),
        )

    def _analyze_response(self, record: ResearchRunRecord) -> dict[str, Any]:
        return {
            "runId": record.run_id,
            "paperId": record.paper_id,
            "status": _result_status(record.result),
            "analysisRef": _artifact_ref(record.result, "research-analysis"),
            "readerPayloadRef": _artifact_ref(record.result, "research-reader-payload"),
            "qualityRef": _artifact_ref(record.result, "research-quality-result"),
            "traceRef": _trace_ref(record.result, record.run_id),
            "paperCardRef": _artifact_ref(record.result, "research-paper-card"),
            "metadata": {"artifactRefs": _artifact_refs(record.result)},
            **(
                {"usedPersonalMemory": used_personal_memory}
                if (used_personal_memory := _result_used_personal_memory(record.result))
                else {}
            ),
        }


class _UnconfiguredAnalyzeUseCase:
    def analyze(self, request: AnalyzePaperRequest) -> Any:
        raise ResearchServiceError(
            "research_run_failed",
            "Research analyze runtime is not configured",
            status_code=503,
            retryable=False,
            user_action_required=True,
        )


class _UnconfiguredRagAskUseCase:
    def rag_ask(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ResearchServiceError(
            "research_run_failed",
            "Research chunk RAG runtime is not configured",
            status_code=503,
            retryable=False,
            user_action_required=True,
        )


def _source_ref(command: ResearchAnalyzeInput) -> str:
    source_ref = _optional_text(command.source_url) or _optional_text(command.pdf_url)
    if source_ref is None:
        raise ResearchServiceError(
            "invalid_request",
            "sourceUrl or pdfUrl is required",
            status_code=400,
            user_action_required=True,
        )
    return source_ref


def _runtime_options(command: ResearchAnalyzeInput) -> dict[str, Any]:
    options = dict(command.options)
    if command.source_url is not None:
        options["source_url"] = command.source_url
    if command.pdf_url is not None:
        options["pdf_url"] = command.pdf_url
    if command.metadata:
        options["metadata"] = dict(command.metadata)
    return options


def _resolve_actor_scope(
    use_case: AskPaperUseCase,
    *,
    tenant_id: str | None,
    user_id: str | None,
    memory_namespace: str | None,
) -> ResearchActorScope:
    try:
        return use_case.resolve_actor_scope(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_namespace=memory_namespace,
        )
    except ValueError as exc:
        raise ResearchServiceError(
            "invalid_request",
            str(exc),
            status_code=400,
            user_action_required=True,
        ) from exc


def _resolve_actor_input(
    use_case: AskPaperUseCase,
    actor: ResearchActorInput | None,
) -> ResearchActorScope:
    actual = actor or ResearchActorInput()
    if not isinstance(actual, ResearchActorInput):
        raise ResearchServiceError(
            "invalid_request",
            "actor must be ResearchActorInput",
            status_code=400,
            user_action_required=True,
        )
    return _resolve_actor_scope(
        use_case,
        tenant_id=actual.tenant_id,
        user_id=actual.user_id,
        memory_namespace=actual.memory_namespace,
    )


def bind_research_actor_input(
    requested: ResearchActorInput,
    actor: ActorContext | None,
) -> ResearchActorInput:
    if not isinstance(requested, ResearchActorInput):
        raise TypeError("requested must be ResearchActorInput")
    if actor is not None and not isinstance(actor, ActorContext):
        raise TypeError("actor must be ActorContext or None")
    if actor is None or actor.actor_type == "anonymous":
        if any((requested.tenant_id, requested.user_id, requested.memory_namespace)):
            raise _research_actor_forbidden()
        return ResearchActorInput()

    metadata = actor.metadata if isinstance(actor.metadata, dict) else {}
    trusted_user_id = _optional_text(metadata.get("user_id"))
    if trusted_user_id is None and actor.actor_type == "user":
        trusted_user_id = _trusted_actor_scope_id(actor.actor_id)
    trusted = ResearchActorInput(
        tenant_id=_optional_text(metadata.get("tenant_id")),
        user_id=trusted_user_id,
        memory_namespace=_optional_text(metadata.get("memory_namespace")),
    )
    try:
        trusted_scope = AskPaperUseCase().resolve_actor_scope(
            tenant_id=trusted.tenant_id,
            user_id=trusted.user_id,
            memory_namespace=trusted.memory_namespace,
        )
        merged_scope = AskPaperUseCase().resolve_actor_scope(
            tenant_id=requested.tenant_id or trusted_scope.tenant_id,
            user_id=requested.user_id or trusted_scope.user_id,
            memory_namespace=(
                requested.memory_namespace or trusted_scope.memory_namespace
            ),
        )
    except ValueError:
        raise _research_actor_forbidden() from None
    if merged_scope != trusted_scope:
        raise _research_actor_forbidden()
    return ResearchActorInput(
        tenant_id=trusted_scope.tenant_id,
        user_id=trusted_scope.user_id,
        memory_namespace=trusted_scope.memory_namespace,
    )


def _trusted_actor_scope_id(actor_id: str) -> str:
    text = str(actor_id or "").strip()
    try:
        return AskPaperUseCase().resolve_actor_scope(user_id=text).user_id or ""
    except ValueError:
        return stable_research_id("research_actor", text)


def _research_actor_forbidden() -> ResearchActorAuthorizationError:
    return ResearchActorAuthorizationError(
        "forbidden",
        "Research actor scope does not match the authenticated principal",
        status_code=403,
        user_action_required=True,
    )


def _diagnostic_artifact_not_found(run_id: str) -> ResearchServiceError:
    return ResearchServiceError(
        "research_run_failed",
        f"research diagnostic artifact not found: {run_id}",
        status_code=404,
        user_action_required=True,
    )


def _ensure_record_visible_to_actor(
    record: ResearchRunRecord,
    actor_scope: ResearchActorScope,
) -> None:
    if _record_visible_to_actor(record, actor_scope):
        return
    raise ResearchServiceError(
        "paper_not_found",
        f"paper not found: {record.paper_id}",
        status_code=404,
        user_action_required=True,
    )


def _record_visible_to_actor(
    record: ResearchRunRecord,
    actor_scope: ResearchActorScope,
) -> bool:
    persisted_scope = getattr(record.result, "actor_scope", None)
    if isinstance(persisted_scope, ResearchActorScope):
        record_tenant = persisted_scope.tenant_id
        record_user = persisted_scope.user_id
        record_namespace = persisted_scope.memory_namespace
    else:
        trace = getattr(record.result, "trace", None)
        trace_metadata = getattr(trace, "metadata", None)
        if not isinstance(trace_metadata, dict) and isinstance(trace, dict):
            trace_metadata = trace.get("metadata")
        metadata = trace_metadata if isinstance(trace_metadata, dict) else {}
        record_tenant = _optional_text(metadata.get("tenant_id"))
        record_user = _optional_text(metadata.get("user_id"))
        record_namespace = _optional_text(metadata.get("memory_namespace"))
    if record_tenant is not None:
        if actor_scope.tenant_id != record_tenant:
            return False
        if record_namespace == f"research:tenant:{record_tenant}:public":
            return True
    elif record_namespace in {None, "research.public"} and record_user is None:
        return True
    if record_user is not None and actor_scope.user_id != record_user:
        return False
    if record_namespace is not None:
        return actor_scope.memory_namespace == record_namespace
    return record_user is None or actor_scope.user_id == record_user


def _bind_result_source_identity(result: Any, metadata: Mapping[str, Any]) -> Any:
    declared_revision = _optional_text(metadata.get("source_revision"))
    declared_fingerprint = _optional_text(metadata.get("source_fingerprint"))
    if declared_revision is None and declared_fingerprint is None:
        return result
    reader_payload = getattr(result, "reader_payload", None)
    document = getattr(reader_payload, "document", None)
    actual = _normalized_sha256_identity(getattr(document, "source_hash", None))
    if actual is None:
        return result
    if (
        declared_revision is not None
        and _normalized_sha256_identity(declared_revision) != actual
    ) or (
        declared_fingerprint is not None
        and _normalized_sha256_identity(declared_fingerprint) != actual
    ):
        raise ResearchServiceError(
            "source_identity_mismatch",
            "Accepted Research document does not match the declared source identity",
            status_code=409,
            user_action_required=True,
        )
    diagnostics = getattr(result, "diagnostics", None)
    if not is_dataclass(result) or not isinstance(diagnostics, Mapping):
        return result
    bound = dict(diagnostics)
    verified = f"sha256:{actual}"
    bound["desktop_source_revision"] = verified
    bound["desktop_source_fingerprint"] = verified
    return replace(result, diagnostics=bound)

def _accepted_artifacts_present(result: Any) -> bool:
    refs = set(_artifact_refs(result))
    return any("research-analysis" in ref for ref in refs) and any(
        "research-quality-result" in ref for ref in refs
    )


def _run_source_revision(result: Any) -> str | None:
    candidates = (
        getattr(getattr(result, "analysis", None), "metadata", None),
        getattr(getattr(result, "paper_card", None), "metadata", None),
        getattr(getattr(result, "reader_payload", None), "metadata", None),
        getattr(getattr(result, "trace", None), "metadata", None),
        getattr(result, "diagnostics", None),
    )
    for candidate in candidates:
        found = _find_source_revision(candidate, depth=0)
        if found is not None:
            return found
    return None


def _find_source_revision(value: Any, *, depth: int) -> str | None:
    if depth > 4:
        return None
    if isinstance(value, Mapping):
        for key in ("sourceRevision", "source_revision", "desktop_source_revision", "research_input_source_revision"):
            found = _optional_text(value.get(key))
            if found is not None:
                return found
        for key in ("research_input", "metadata", "profile", "prepared_input", "payload"):
            found = _find_source_revision(value.get(key), depth=depth + 1)
            if found is not None:
                return found
    return None


def _run_source_fingerprint(result: Any) -> str | None:
    diagnostics = getattr(result, "diagnostics", None)
    if isinstance(diagnostics, Mapping):
        value = _optional_text(diagnostics.get("desktop_source_fingerprint"))
        if value is not None:
            return value
    return None


def _safe_run_diagnostics(result: Any) -> dict[str, Any]:
    diagnostics = getattr(result, "diagnostics", None)
    if not isinstance(diagnostics, Mapping):
        return {}
    allowed: dict[str, Any] = {}
    for key in ("reason", "reason_code", "error_code", "error_type", "stage", "retryable"):
        value = diagnostics.get(key)
        if isinstance(value, (str, bool, int, float)) and len(str(value)) <= 256:
            allowed[key] = value
    used = _bounded_used_personal_memory(diagnostics.get("used_personal_memory"))
    if used:
        allowed["usedPersonalMemory"] = used
    return allowed


def _used_personal_memory(value: Mapping[str, Any]) -> list[dict[str, str | int]]:
    direct = _bounded_used_personal_memory(value.get("used_personal_memory"))
    if direct:
        return direct
    candidate = value.get("answer_candidate")
    if not isinstance(candidate, Mapping):
        return []
    metadata = candidate.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    generated = metadata.get("generated_answer_metadata")
    if isinstance(generated, Mapping):
        nested = _bounded_used_personal_memory(generated.get("used_personal_memory"))
        if nested:
            return nested
    return _bounded_used_personal_memory(metadata.get("used_personal_memory"))


def _result_used_personal_memory(result: Any) -> list[dict[str, str | int]]:
    diagnostics = getattr(result, "diagnostics", None)
    if not isinstance(diagnostics, Mapping):
        return []
    return _bounded_used_personal_memory(diagnostics.get("used_personal_memory"))


def _bounded_used_personal_memory(value: Any) -> list[dict[str, str | int]]:
    if not isinstance(value, (list, tuple)) or len(value) > 20:
        return []
    refs: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"memoryId", "revision"}:
            return []
        memory_id = item.get("memoryId")
        revision = item.get("revision")
        if (
            not isinstance(memory_id, str)
            or re.fullmatch(r"pmem_[0-9a-f]{32}", memory_id) is None
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or memory_id in seen
        ):
            return []
        seen.add(memory_id)
        refs.append({"memoryId": memory_id, "revision": revision})
    return refs


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ResearchServiceError(
            "invalid_request",
            f"{field_name} is required",
            status_code=400,
            user_action_required=True,
        )
    return text


def _catalog_scope_fingerprint(scope: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {str(key): str(value) for key, value in sorted(scope.items()) if str(value).strip()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_catalog_cursor(
    *,
    offset: int,
    query: str,
    sort: str,
    scope: Mapping[str, Any],
) -> str:
    payload = {
        "v": "research.catalog.v1",
        "offset": int(offset),
        "query": hashlib.sha256(str(query or "").strip().casefold().encode("utf-8")).hexdigest(),
        "scope": _catalog_scope_fingerprint(scope),
        "sort": sort,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_catalog_cursor(
    cursor: str | None,
    *,
    query: str,
    sort: str,
    scope: Mapping[str, Any],
) -> int:
    if not cursor:
        return 0
    try:
        padded = str(cursor) + "=" * (-len(str(cursor)) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        expected_query = hashlib.sha256(str(query or "").strip().casefold().encode("utf-8")).hexdigest()
        if (
            payload.get("v") != "research.catalog.v1"
            or payload.get("query") != expected_query
            or payload.get("scope") != _catalog_scope_fingerprint(scope)
            or payload.get("sort") != sort
            or isinstance(payload.get("offset"), bool)
            or not isinstance(payload.get("offset"), int)
            or payload["offset"] < 0
        ):
            raise ValueError
        return int(payload["offset"])
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, base64.binascii.Error) as exc:
        raise ResearchServiceError(
            "invalid_cursor",
            "catalog cursor is invalid or no longer matches the query",
            status_code=400,
            user_action_required=True,
        ) from exc


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ask_mode(value: Any) -> str:
    mode = str(value or "summary").strip().lower()
    if mode not in {"summary", "chunk_rag"}:
        raise ResearchServiceError(
            "invalid_request",
            "mode must be summary or chunk_rag",
            status_code=400,
            user_action_required=True,
        )
    return mode


def _bounded_int(
    value: Any,
    field_name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        parsed = -1
    else:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = -1
    if parsed < minimum or maximum is not None and parsed > maximum:
        suffix = f" and at most {maximum}" if maximum is not None else ""
        raise ResearchServiceError(
            "invalid_request",
            f"{field_name} must be at least {minimum}{suffix}",
            status_code=400,
            user_action_required=True,
        )
    return parsed


def _result_status(result: Any) -> str:
    return str(getattr(result, "status", None) or "unknown")


def _result_execution_error_type(result: Any) -> str | None:
    """Read physical worker exception evidence without routing from Worker data."""

    if _result_status(result) != "failed":
        return None
    diagnostics = getattr(result, "diagnostics", None)
    if not isinstance(diagnostics, Mapping):
        return None
    worker_results = diagnostics.get("worker_results")
    if not isinstance(worker_results, Mapping):
        return None
    error_types = {
        str(worker_diagnostics.get("execution_error_type")).strip()
        for worker_result in worker_results.values()
        if isinstance(worker_result, Mapping)
        and isinstance(
            worker_diagnostics := worker_result.get("diagnostics"),
            Mapping,
        )
        and isinstance(worker_diagnostics.get("execution_error_type"), str)
        and worker_diagnostics.get("execution_error_type", "").strip()
    }
    if len(error_types) != 1:
        return None
    return next(iter(error_types))


def _artifact_refs(result: Any) -> dict[str, str]:
    refs = getattr(result, "artifact_refs", {}) or {}
    if not isinstance(refs, dict):
        return {}
    return {str(key): str(value) for key, value in refs.items() if value is not None}


def _artifact_ref(result: Any, key: str) -> str | None:
    return _artifact_refs(result).get(key)


def _trace_ref(result: Any, run_id: str) -> str:
    return str(getattr(result, "trace_ref", None) or f"harness-trace://{run_id}")


def _to_dict(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _selection_targets(selection: dict[str, Any], *, reader_payload: Any) -> list[str]:
    document = getattr(reader_payload, "document", None)
    sections = list(getattr(document, "sections", []) or [])
    by_id = {str(getattr(section, "section_id", "")): section for section in sections if str(getattr(section, "section_id", ""))}
    requested: list[str] = []
    section_id = str(selection.get("sectionId") or "").strip()
    if section_id:
        if section_id not in by_id:
            raise ResearchServiceError("invalid_selection", "Selected section is not part of the accepted document", status_code=422, user_action_required=True)
        requested.append(section_id)
    page = selection.get("page") if selection.get("page") is not None else selection.get("pdfPage")
    if page is not None:
        if isinstance(page, bool) or not isinstance(page, int):
            raise ResearchServiceError("invalid_selection", "Selected page is invalid", status_code=422, user_action_required=True)
        page_matches = [
            section_id
            for section_id, section in by_id.items()
            if getattr(section, "page_start", None) is not None
            and int(getattr(section, "page_start")) <= page
            and page <= int(getattr(section, "page_end", None) or getattr(section, "page_start"))
        ]
        if not page_matches:
            raise ResearchServiceError("invalid_selection", "Selected page is not mapped to the accepted document", status_code=422, user_action_required=True)
        requested.extend(page_matches)
    selected_text = str(selection.get("selectedText") or "").strip()
    if selected_text:
        text_matches = [section_id for section_id, section in by_id.items() if selected_text in str(getattr(section, "text", ""))]
        if len(text_matches) != 1:
            raise ResearchServiceError("invalid_selection", "Selected text must map to exactly one accepted section", status_code=422, user_action_required=True)
        requested.extend(text_matches)
    return _unique_texts(requested)

def _reader_source_refs(reader_payload: Any) -> list[str]:
    lineage = getattr(reader_payload, "source_lineage", None)
    refs = list(getattr(lineage, "source_refs", []) or [])
    if refs:
        return [str(ref) for ref in refs]
    document = getattr(reader_payload, "document", None)
    lineage = getattr(document, "lineage", None)
    refs = list(getattr(lineage, "source_refs", []) or [])
    return [str(ref) for ref in refs] or ["paper://unknown/source"]


def _reader_answer_projection(
    generated: Mapping[str, Any],
    *,
    run_id: str,
    paper_id: str,
    question: str,
    source_refs: list[str],
    reader_payload: Any,
) -> dict[str, Any]:
    if generated.get("recovery_state") == "needs_action":
        return {
            "status": "interrupted",
            "recoveryState": "needs_action",
            "errorCode": str(generated.get("error_code") or "rag_execution_interrupted"),
            "journalEventCount": int(generated.get("journal_event_count") or 0),
            "lastEventType": generated.get("last_event_type"),
            "runId": run_id,
            "paperId": paper_id,
        }
    answer = generated.get("answer")
    decision = generated.get("decision") if isinstance(generated.get("decision"), Mapping) else {}
    citations = _grounded_answer_citations(
        generated.get("citations"),
        paper_id=paper_id,
        source_revision=_reader_content_revision(reader_payload),
        source_refs=set(source_refs),
        reader_payload=reader_payload,
    )
    evidence_refs = _unique_texts([
        str(citation.get("evidenceId") or "")
        for citation in citations
    ])
    decision_type = decision.get("decision_type") or decision.get("decisionType")
    transcript_id = generated.get("transcript_id")
    trace_valid = (
        isinstance(transcript_id, str)
        and 1 <= len(transcript_id) <= 256
        and re.search(r"[\s\x00-\x1f\x7f]", transcript_id) is None
    )
    raw_citations = generated.get("citations")
    citations_valid = (
        isinstance(raw_citations, list) and bool(citations)
        and len(citations) == len(raw_citations)
        and all(citation.get("valid") is True for citation in citations)
    )
    candidate = generated.get("answer_candidate")
    candidate_not_abstained = not isinstance(candidate, Mapping) or candidate.get("abstained") is False
    accepted = (
        isinstance(answer, str) and bool(answer.strip())
        and generated.get("abstained") is not True and candidate_not_abstained
        and generated.get("status") == "answered"
        and decision_type == "return_answer"
        and trace_valid and citations_valid
    )
    abstained = not accepted
    return {
        "answer": answer.strip() if accepted else None,
        "evidenceRefs": evidence_refs,
        "claims": list(generated.get("claims") or ()),
        "citations": citations,
        "confidence": 1.0 if accepted else 0.0,
        "abstained": abstained,
        "transcriptId": generated.get("transcript_id"),
        "status": generated.get("status"),
        "decision": dict(decision),
        "runId": run_id,
        "paperId": paper_id,
        "metadata": {
            "runId": run_id,
            "paperId": paper_id,
            "retrievalGoalId": stable_research_id("research_ask", run_id, question),
            "sourceRefs": list(source_refs),
            "generationMode": generated.get("generation_mode"),
            "citationLocatorVerified": citations_valid,
            "traceVerified": trace_valid,
            "recovered": generated.get("recovered") is True,
        },
        **(
            {"usedPersonalMemory": used_personal_memory}
            if (used_personal_memory := _used_personal_memory(generated))
            else {}
        ),
    }


def _reader_content_revision(reader_payload: Any) -> str:
    document = getattr(reader_payload, "document", None)
    revision = str(getattr(document, "source_hash", "") or "").strip()
    digest = _normalized_sha256_identity(revision)
    return f"sha256:{digest}" if digest else revision


def _grounded_answer_citations(
    raw: Any,
    *,
    paper_id: str,
    source_revision: str,
    source_refs: set[str],
    reader_payload: Any,
) -> list[dict[str, Any]]:
    document = getattr(reader_payload, "document", None)
    sections = list(getattr(document, "sections", []) or [])
    result: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        evidence_id = item.get("evidence_id")
        if (
            not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= 256
            or re.search(r"[\s\x00-\x1f\x7f]", evidence_id) is not None
        ):
            continue  # Never manufacture or truncate evidence identity.
        source_ref = item.get("source_ref")
        source_ref = source_ref if isinstance(source_ref, str) else ""
        spans = item.get("span_refs")
        locator_refs: list[str] = []
        complete = isinstance(spans, (list, tuple)) and bool(spans)
        source_locator = item.get("source_locator")
        for span in spans if isinstance(spans, (list, tuple)) else ():
            if not isinstance(span, str) or not span.strip():
                complete = False
            elif _parse_grounded_locator(span, source_ref=source_ref) is not None:
                locator_refs.append(span)
            elif span == item.get("chunk_id") and isinstance(source_locator, str):
                # Chunk spans are verified by Harness; their locator comes from that
                # exact accepted evidence, not from a model or an arbitrary section.
                locator_refs.append(source_locator)
            else:
                complete = False
        if source_locator is not None:
            if not isinstance(source_locator, str):
                complete = False
            elif source_locator != source_ref:
                if _parse_grounded_locator(source_locator, source_ref=source_ref) is None:
                    complete = False
                else:
                    locator_refs.append(source_locator)
        if _parse_grounded_locator(source_ref, source_ref=source_ref) is not None:
            locator_refs.append(source_ref)
        section = _section_for_grounded_spans(
            sections, source_ref=source_ref, span_refs=locator_refs
        ) if complete else None
        valid = bool(source_ref in source_refs and source_revision and section is not None)
        citation: dict[str, Any] = {
            "id": f"citation:{hashlib.sha256(evidence_id.encode('utf-8')).hexdigest()[:24]}",
            "label": str(getattr(section, "title", "") or evidence_id)[:500],
            "evidenceId": evidence_id,
            "paperId": paper_id,
            "sourceRevision": source_revision,
            "valid": valid,
        }
        if valid:
            citation["sectionId"] = str(section.section_id)
            pages = {
                locator[1] for span in locator_refs
                if (locator := _parse_grounded_locator(span, source_ref=source_ref)) is not None
                and locator[1] is not None
            }
            if len(pages) == 1:
                citation["page"] = pages.pop()
        else:
            citation["reasonCode"] = "source_span_not_verified"
        result.append(citation)
    return result


def _parse_grounded_locator(span: str, *, source_ref: str) -> tuple[str | None, int | None] | None:
    """Parse explicit locators only; do not infer a page from section bounds."""
    try:
        parsed = urlsplit(span)
        source = urlsplit(source_ref)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        if parsed._replace(query="", fragment="").geturl() != source._replace(query="", fragment="").geturl():
            return None
        components = (parsed.query, parsed.fragment)
    else:
        components = (span.lstrip("#?"),)
    sections: list[str] = []
    pages: list[str] = []
    for component in components:
        for key, value in parse_qsl(component, keep_blank_values=True):
            if key in {"section", "section_id"}:
                sections.append(value)
            elif key == "page":
                pages.append(value)
    if len(sections) > 1 or len(pages) > 1 or (not sections and not pages):
        return None
    section_id = sections[0] if sections else None
    if section_id is not None and not section_id.strip():
        return None
    if pages and (not re.fullmatch(r"[1-9][0-9]{0,5}", pages[0]) or int(pages[0]) > 100_000):
        return None
    return section_id, int(pages[0]) if pages else None


def _section_for_grounded_spans(sections: list[Any], *, source_ref: str, span_refs: list[str]) -> Any | None:
    if not source_ref or not span_refs:
        return None
    locators = [_parse_grounded_locator(span, source_ref=source_ref) for span in span_refs]
    if any(locator is None for locator in locators):
        return None
    explicit_sections = {locator[0] for locator in locators if locator is not None and locator[0] is not None}
    explicit_pages = {locator[1] for locator in locators if locator is not None and locator[1] is not None}
    if len(explicit_sections) > 1 or len(explicit_pages) > 1:
        return None
    matches = []
    for section in sections:
        if str(getattr(section, "source_ref", "") or "") != source_ref:
            continue
        section_id = str(getattr(section, "section_id", "") or "")
        if not section_id:
            continue
        start = getattr(section, "page_start", None)
        end = getattr(section, "page_end", None)
        end = start if end is None else end
        matches_all = True
        for locator in locators:
            assert locator is not None
            explicit_section, page = locator
            if explicit_section is not None and explicit_section != section_id:
                matches_all = False
                break
            if page is not None and not (
                type(start) is int and type(end) is int and 1 <= start <= page <= end <= 100_000
            ):
                matches_all = False
                break
        if matches_all:
            matches.append(section)
    return matches[0] if len(matches) == 1 else None


def _normalized_sha256_identity(value: Any) -> str | None:
    text = str(value or "").strip().lower().removeprefix("sha256:")
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else None

def _quality_gate_failures(quality: Any) -> list[dict[str, Any]]:
    failures = []
    for result in getattr(quality, "gate_results", []) or []:
        if bool(getattr(result, "passed", False)):
            continue
        failures.append(_to_dict(result))
    return failures


def _run_store_service_error(
    error: ResearchRunStoreError,
    *,
    operation: str,
) -> ResearchServiceError:
    return ResearchServiceError(
        "research_run_failed",
        "research run storage operation failed",
        status_code=500,
        details={
            "operation": operation,
            "reason": error.reason_code,
        },
        retryable=error.retryable,
    )


def model_credential_service_error(error: LLMCredentialUnavailableError) -> ResearchServiceError:
    """The account's model credential is missing or unusable; the user fixes it."""

    return ResearchServiceError(
        error.code,
        str(error),
        status_code=409,
        retryable=False,
        user_action_required=True,
    )


def _research_runtime_service_error(error: Exception) -> ResearchServiceError:
    from interfaces.composition.research_errors import ResearchCompositionError

    credential_failure = credential_unavailable_cause(error)
    if credential_failure is not None:
        return model_credential_service_error(credential_failure)
    if isinstance(error, ResearchCompositionError):
        public = error.to_public_dict()
        return ResearchServiceError(
            str(public["code"]),
            str(public["message"]),
            status_code=503,
            details={
                "capabilities": list(public["capabilities"]),
                "remediation": dict(public["remediation"]),
            },
            retryable=bool(public["retryable"]),
            user_action_required=True,
        )
    if isinstance(error, ResearchDynamicTaskPlanUnavailableError):
        return ResearchServiceError(
            "research_runtime_unavailable",
            "Research dynamic TaskPlan runtime is unavailable.",
            status_code=503,
            details={
                "capabilities": list(error.missing_capabilities),
                "remediation": {
                    "code": "restore_research_runtime_capability",
                    "message": (
                        "Restore the named Research capability and retry the request."
                    ),
                },
            },
            retryable=False,
        )
    return ResearchServiceError(
        "research_run_failed",
        "research run failed",
        status_code=500,
        details={"error_type": type(error).__name__},
        retryable=True,
    )


def _record_recovery_failure(
    primary_error: ResearchServiceError,
    recovery_error: Exception,
) -> None:
    """Surface failed diagnostic persistence without changing the API shape."""

    primary_error.add_note(
        "Research failed-run diagnostics could not be durably reconciled: "
        f"{type(recovery_error).__name__}"
    )
    LOGGER.error(
        "Research failed-run diagnostic reconciliation failed",
        exc_info=(
            type(recovery_error),
            recovery_error,
            recovery_error.__traceback__,
        ),
    )


def _unique_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


_DEFAULT_RESEARCH_RUN_STORE = InMemoryResearchRunStore()



def _qualified_type(value: Any) -> str | None:
    if value is None:
        return None
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"

__all__ = [
    "InMemoryResearchRunStore",
    "ResearchAnalyzeInput",
    "ResearchActorInput",
    "ResearchActorAuthorizationError",
    "ResearchApplicationService",
    "ResearchAskInput",
    "ResearchParseInput",
    "ResearchRagAskUseCase",
    "ResearchRunRecord",
    "ResearchRunStore",
    "ResearchServiceError",
    "bind_research_actor_input",
    "model_credential_service_error",
]
