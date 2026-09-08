from __future__ import annotations

import logging
import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

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
    section_index: int = 0
    limit: int = 5
    generate: bool = False
    gated: bool = True


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
        graph_identity: ContextGraphIdentity | None = None,
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
        if self._run_reconciler is not None:
            self._run_reconciler.reconcile_pending()

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

        record = self._record_for_paper(paper_id, actor_scope=actor_scope)
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
        goal = self._ask_use_case.build_retrieval_goal(
            ResearchRetrievalGoal(
                goal_id=stable_research_id("research_ask", record.run_id, question),
                paper_id=record.paper_id,
                question=question,
                required_evidence_types=["claim_support"],
                target_sections=_selection_targets(request.selection),
                allowed_source_refs=_reader_source_refs(reader_payload),
                allowed_memory_namespaces=[actor_scope.memory_namespace],
                constraints={"locale": request.locale or "en", "paper_only": True},
                metadata={"run_id": record.run_id, **actor_scope.to_metadata()},
            )
        )
        evidence_refs = _answer_evidence_refs(analysis, reader_payload, request.selection)
        return {
            "answer": _answer_from_analysis(question, analysis),
            "evidenceRefs": evidence_refs,
            "confidence": _answer_confidence(analysis),
            "traceRef": _trace_ref(record.result, record.run_id),
            "metadata": {
                "runId": record.run_id,
                "paperId": record.paper_id,
                "retrievalGoalId": goal.goal_id,
                "sourceRefs": goal.allowed_source_refs,
            },
        }

    def _ask_paper_chunks(
        self,
        paper_id: str,
        question: str,
        *,
        request: ResearchAskInput,
        actor_scope: ResearchActorScope,
        graph_identity: ContextGraphIdentity | None,
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
            }
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


def _selection_targets(selection: dict[str, Any]) -> list[str]:
    raw = selection.get("sectionIds") or selection.get("section_ids") or selection.get("targetRefs") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def _reader_source_refs(reader_payload: Any) -> list[str]:
    lineage = getattr(reader_payload, "source_lineage", None)
    refs = list(getattr(lineage, "source_refs", []) or [])
    if refs:
        return [str(ref) for ref in refs]
    document = getattr(reader_payload, "document", None)
    lineage = getattr(document, "lineage", None)
    refs = list(getattr(lineage, "source_refs", []) or [])
    return [str(ref) for ref in refs] or ["paper://unknown/source"]


def _answer_from_analysis(question: str, analysis: Any) -> str:
    summary = getattr(analysis, "summary", None)
    lowered = question.casefold()
    if summary is None:
        return "No grounded research summary is available for this paper."
    if any(token in lowered for token in ("method", "方法", "approach")):
        return str(getattr(summary, "method_summary", None) or getattr(summary, "core_idea", ""))
    if any(token in lowered for token in ("experiment", "实验", "benchmark")):
        return str(getattr(summary, "experiment_summary", None) or "No experiment summary is available.")
    if any(token in lowered for token in ("limitation", "局限", "risk")):
        limitations = list(getattr(summary, "limitations", []) or [])
        return "; ".join(str(item) for item in limitations) or "No limitations were recorded."
    return str(getattr(summary, "core_idea", None) or getattr(summary, "problem", ""))


def _answer_evidence_refs(analysis: Any, reader_payload: Any, selection: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    summary = getattr(analysis, "summary", None)
    for item in getattr(summary, "evidence_refs", []) or []:
        evidence_id = getattr(item, "evidence_id", None)
        source_ref = getattr(item, "source_ref", None)
        refs.append(str(evidence_id or source_ref))
    evidence = getattr(reader_payload, "evidence", None)
    for item in getattr(evidence, "items", []) or []:
        refs.append(str(getattr(item, "evidence_id", "") or getattr(item, "source_ref", "")))
    selected_refs = selection.get("sourceRefs") or selection.get("source_refs")
    if isinstance(selected_refs, str):
        selected_refs = [selected_refs]
    if selected_refs:
        refs.extend(str(item) for item in selected_refs)
    return _unique_texts(refs)


def _answer_confidence(analysis: Any) -> float:
    summary = getattr(analysis, "summary", None)
    confidence = getattr(summary, "confidence", None)
    try:
        return max(0.0, min(float(confidence), 1.0))
    except (TypeError, ValueError):
        return 0.0


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


def _research_runtime_service_error(error: Exception) -> ResearchServiceError:
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
]
