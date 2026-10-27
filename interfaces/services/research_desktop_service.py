from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote, unquote

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.application.research_input_contract import RESEARCH_INPUT_SOURCE_REVISION_OPTION
from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemoryRemoteChannel,
    PersonalMemoryRemotePolicy,
    PersonalMemorySelection,
)
from backend.research.domain.personal_memory_context import (
    PersonalMemoryContext,
    PersonalMemoryContextProvider,
)
from backend.research.ports.personal_memory import (
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreError,
    PersonalMemoryStoreNotFoundError,
)
from backend.research.ports.request_admission import ResearchDesktopRequestStore
from framework.llm.clients import (
    LLMCredentialUnavailableError,
    credential_unavailable_cause,
    require_invocation_credential,
)
from interfaces.services.research_desktop_control import (
    ResearchDesktopControlError,
    canonical_request_fingerprint,
    research_desktop_request_store_from_env,
)
from interfaces.services.research_import_application import ResearchImportApplication
from interfaces.services.research_import_service import ResearchImportError, ResearchImportService
from interfaces.services.research_service import (
    model_credential_service_error,
    ResearchActorInput,
    ResearchAnalyzeInput,
    ResearchApplicationService,
    ResearchAskInput,
    ResearchParseInput,
    ResearchServiceError,
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MISSING = object()
# Source types the resolver fetches over the network; ``local``, ``manual``
# and the GitHub observation path never apply to a public paper link.
_REMOTE_SOURCE_TYPES = frozenset({"arxiv", "openreview", "doi", "crossref", "publisher"})


class ResearchDesktopServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
        user_action_required: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.user_action_required = user_action_required
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ResearchDesktopOwner:
    user_id: str
    tenant_id: str | None = None
    memory_namespace: str | None = None

    def actor(self) -> ResearchActorInput:
        return ResearchActorInput(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            memory_namespace=self.memory_namespace,
        )


def _require_personal_memory_policy(
    policy: PersonalMemoryRemotePolicy,
    *,
    owner: PersonalMemoryOwner,
    expected_revision: int,
) -> None:
    if policy.owner != owner:
        raise ResearchDesktopServiceError(
            "personal_memory_owner_mismatch",
            "Personal memory policy does not belong to the authenticated owner",
            status_code=403,
            user_action_required=True,
        )
    if policy.revision != expected_revision:
        raise ResearchDesktopServiceError(
            "personal_memory_policy_conflict",
            "Personal memory remote-processing policy changed before use",
            status_code=409,
            user_action_required=True,
            details={
                "expectedPolicyRevision": expected_revision,
                "actualPolicyRevision": policy.revision,
            },
        )
    if not policy.allows_remote_processing(
        PersonalMemoryRemoteChannel.MODEL,
        use_memory=True,
    ):
        raise ResearchDesktopServiceError(
            "personal_memory_remote_processing_disabled",
            "Personal memory remote processing is disabled",
            status_code=403,
            user_action_required=True,
        )


@dataclass(frozen=True, slots=True)
class _PersonalMemoryTaskContextProvider:
    application: PersonalMemoryService
    owner: PersonalMemoryOwner
    policy_revision: int
    selections: tuple[PersonalMemorySelection, ...]

    def current(self) -> PersonalMemoryContext:
        try:
            policy = self.application.get_policy(self.owner)
            _require_personal_memory_policy(
                policy,
                owner=self.owner,
                expected_revision=self.policy_revision,
            )
            snapshot = self.application.select_active(
                self.owner,
                memory_refs=self.selections,
            )
            current_policy = self.application.get_policy(self.owner)
            _require_personal_memory_policy(
                current_policy,
                owner=self.owner,
                expected_revision=self.policy_revision,
            )
            expected_refs = tuple(
                (selection.memory_id, selection.revision)
                for selection in self.selections
            )
            actual_refs = tuple(
                (record.memory_id, record.revision)
                for record in snapshot.records
            )
            if snapshot.owner != self.owner or actual_refs != expected_refs:
                raise ResearchDesktopServiceError(
                    "personal_memory_selection_conflict",
                    "Selected personal memory no longer matches the frozen owner and revisions",
                    status_code=409,
                    user_action_required=True,
                )
            policy = current_policy
        except ResearchDesktopServiceError:
            raise
        except (PersonalMemoryStoreNotFoundError, PersonalMemoryStoreConflictError) as exc:
            raise ResearchDesktopServiceError(
                "personal_memory_selection_conflict",
                "Selected personal memory is no longer available at the requested revision",
                status_code=409,
                user_action_required=True,
            ) from exc
        except PersonalMemoryStoreError as exc:
            raise ResearchDesktopServiceError(
                "personal_memory_unavailable",
                "Personal memory storage is unavailable",
                status_code=503,
                retryable=exc.retryable,
            ) from exc
        except (TypeError, ValueError) as exc:
            raise ResearchDesktopServiceError(
                "personal_memory_selection_conflict",
                "Selected personal memory is invalid or unavailable",
                status_code=409,
                user_action_required=True,
            ) from exc
        return PersonalMemoryContext.from_snapshot(
            snapshot,
            policy_revision=policy.revision,
        )


class ResearchDesktopApplicationService:
    """Authenticated desktop facade over existing Research application authorities."""

    def __init__(
        self,
        *,
        research: ResearchApplicationService,
        imports: ResearchImportService | None = None,
        requests: ResearchDesktopRequestStore | None = None,
        personal_memory: PersonalMemoryService | None = None,
    ) -> None:
        if not isinstance(research, ResearchApplicationService):
            raise TypeError("research must be ResearchApplicationService")
        self._research = research
        self._imports = imports or ResearchImportService()
        self._requests = requests or research_desktop_request_store_from_env()
        if personal_memory is not None and not isinstance(
            personal_memory, PersonalMemoryService
        ):
            raise TypeError("personal_memory must be PersonalMemoryService")
        self._personal_memory = personal_memory

    def _personal_memory_context(
        self,
        owner: ResearchDesktopOwner,
        request: Mapping[str, Any] | None,
    ) -> PersonalMemoryContextProvider | None:
        if request is None:
            return None
        if self._personal_memory is None:
            raise ResearchDesktopServiceError(
                "personal_memory_context_unavailable",
                "Personal memory task context is not configured",
                status_code=503,
                retryable=False,
            )
        provider = _PersonalMemoryTaskContextProvider(
            application=self._personal_memory,
            owner=_personal_memory_owner(owner),
            policy_revision=int(request["policyRevision"]),
            selections=tuple(
                PersonalMemorySelection(
                    memory_id=str(item["memoryId"]),
                    revision=int(item["revision"]),
                )
                for item in request["refs"]
            ),
        )
        provider.current()
        return provider

    def generation(self) -> str:
        return self._requests.generation()

    def rotate_generation(self) -> str:
        """Fence all pre-restore browser requests after an offline restore switch."""

        return self._requests.rotate_generation()

    def begin_business_operation(self, *, operation: str) -> str:
        return self._requests.begin_untracked_execution(operation=operation)

    def end_business_operation(self, lease_id: str) -> None:
        self._requests.end_untracked_execution(lease_id)

    def capabilities(self, owner: ResearchDesktopOwner) -> dict[str, Any]:
        _owner(owner)
        available = self._research.capability_projection()
        # Model-backed capabilities also depend on this account's credential.
        credential_gap = _model_credential_gap()
        definitions = (
            ("source", "论文来源", True, available["catalog"], False),
            ("pdf-import", "PDF 导入", True, available["parse"], False),
            ("single-paper-analysis", "单论文分析", True, available["analyze"], True),
            ("paper-question-answering", "论文问答", True, available["ask"] and available["ragAsk"], True),
            ("accepted-report", "核验报告", True, available["artifactRead"], False),
            ("rag-question-answering", "检索增强问答", False, available["ragAsk"], True),
        )
        runtime_identity = self._research.capability_runtime_identity()
        verifications = self._requests.capability_verifications(runtime_identity=runtime_identity)
        capabilities = []
        for capability_id, label, required, configured, uses_model in definitions:
            if uses_model and credential_gap is not None and configured:
                capabilities.append({
                    "id": capability_id,
                    "label": label,
                    "required": required,
                    "configured": False,
                    "reachable": False,
                    "verified": False,
                    "verifiedAt": None,
                    "provenance": None,
                    "reasonCode": credential_gap.code,
                    "message": "请先在设置中填写并保存模型 API Key。",
                })
                continue
            verification = verifications.get(capability_id) if isinstance(verifications, Mapping) else None
            verified = configured and isinstance(verification, Mapping)
            capabilities.append({
                "id": capability_id,
                "label": label,
                "required": required,
                "configured": configured,
                "reachable": verified,
                "verified": verified,
                "verifiedAt": verification.get("verifiedAt") if verified else None,
                "provenance": dict(verification.get("provenance") or {}) if verified else None,
                "reasonCode": "business_operation_verified" if verified else ("verification_unknown" if configured else "component_not_configured"),
                "message": "已由受控业务操作验证。" if verified else ("已配置，尚无本 generation 的业务验证记录。" if configured else "该运行组件尚未配置。"),
            })
        return {"generation": self.generation(), "capabilities": capabilities}

    def get_reader(self, owner: ResearchDesktopOwner, *, paper_id: str) -> dict[str, Any]:
        actual = _owner(owner)
        paper = _safe_id(paper_id, "paperId")
        actor = actual.actor()
        try:
            reader = self._research.get_reader(paper, actor=actor)
            run_id = str((reader.get("metadata") or {}).get("runId") or "")
            projection = self._research.get_run_projection(run_id, actor=actor)
            revision = projection.get("sourceRevision")
            fingerprint = projection.get("sourceFingerprint")
            if not isinstance(revision, str) or not revision or not isinstance(fingerprint, str) or not fingerprint:
                raise ResearchDesktopServiceError(
                    "source_identity_unavailable",
                    "Reader result does not contain an exact source identity",
                    status_code=422,
                    user_action_required=True,
                )
            private_source_url = None
            try:
                self._imports.resolve_completed_source(
                    user_id=actual.user_id,
                    paper_id=paper,
                    source_revision=revision,
                    source_fingerprint=fingerprint,
                )
            except ResearchImportError:
                pass
            else:
                private_source_url = _private_source_url(paper, revision, fingerprint)
            return _desktop_reader_projection(
                reader,
                paper_id=paper,
                source_revision=revision,
                source_fingerprint=fingerprint,
                run_id=run_id,
                ask_available=bool(projection.get("eligibleForAcceptedExport")),
                private_source_url=private_source_url,
            )
        except ResearchServiceError as exc:
            if exc.code not in {"paper_not_found", "analysis_not_found", "quality_gate_failed"}:
                raise _service_error(exc) from exc
        try:
            document_payload = self._research.get_document(paper, actor=actor)
            catalog_payload = self._research.get_catalog(paper, actor=actor)
        except ResearchServiceError as exc:
            raise _service_error(exc) from exc
        document = document_payload.get("document") if isinstance(document_payload.get("document"), Mapping) else {}
        revision = _document_source_revision(document)
        fingerprint = _document_source_fingerprint(document)
        private_source_url = None
        if revision is not None and fingerprint is not None:
            try:
                self._imports.resolve_completed_source(
                    user_id=actual.user_id,
                    paper_id=paper,
                    source_revision=revision,
                    source_fingerprint=fingerprint,
                )
            except ResearchImportError:
                pass
            else:
                private_source_url = _private_source_url(paper, revision, fingerprint)
        return _desktop_document_reader_projection(
            catalog_payload,
            document,
            paper_id=paper,
            source_revision=revision,
            source_fingerprint=fingerprint,
            analyze_available=self._research.capability_projection()["analyze"],
            private_source_url=private_source_url,
        )

    def get_private_source(
        self,
        owner: ResearchDesktopOwner,
        *,
        paper_id: str,
        source_revision: str,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        source = self._imports.resolve_completed_source(
            user_id=actual.user_id,
            paper_id=_safe_id(paper_id, "paperId"),
            source_revision=_required_text(source_revision, "sourceRevision", maximum=512),
            source_fingerprint=_sha256_fingerprint(source_fingerprint, "sourceFingerprint"),
        )
        return {
            "filename": str(source["filename"]),
            "contentType": "application/pdf",
            "content": self._imports.read_source(
                user_id=actual.user_id,
                import_id=str(source["importId"]),
            ),
        }

    def _record_capability(self, capability_id: str, *, operation: str) -> None:
        self._requests.record_capability_verification(
            capability_id=capability_id,
            operation=operation,
            runtime_identity=self._research.capability_runtime_identity(),
        )

    def identity(self, owner: ResearchDesktopOwner) -> dict[str, Any]:
        actual = _owner(owner)
        return {
            "owner": {
                "userId": actual.user_id,
                "tenantId": actual.tenant_id,
                "memoryNamespace": actual.memory_namespace or f"research:user:{actual.user_id}",
            },
            "generation": self.generation(),
            "requestContract": {
                "fingerprint": "sha256-canonical-json-v1",
                "requestId": "owner-scoped-stable-before-first-send",
                "notFoundRetry": "explicit-new-request-required",
            },
        }

    def submit_prepare(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
        request_fingerprint: str,
        paper_id: str,
        source_url: str,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        actual_paper = _safe_id(paper_id, "paperId")
        source = _optional_url(source_url, "sourceUrl")
        if source is None:
            raise ResearchDesktopServiceError(
                "source_url_required",
                "Public source preparation requires an explicit HTTP(S) source URL",
                status_code=422,
                user_action_required=True,
            )
        actual_source_type = _remote_source_type(source_type)
        semantic = {
            "kind": "prepare",
            "paperId": actual_paper,
            "sourceUrl": source,
            "sourceType": actual_source_type,
        }
        self._validate_request_fingerprint(semantic, request_fingerprint)
        decision = self._requests.admit(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            kind="prepare",
            fingerprint=request_fingerprint,
            semantic=semantic,
        )
        if not decision.execute:
            return self._operation_response(decision.admission)
        run_id = _run_id(actual.user_id, generation, f"prepare:{request_id}")
        try:
            self._requests.bind_authority(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                run_id=run_id,
            )
            parse_command = ResearchParseInput(
                source=source,
                source_url=source,
                source_type=actual_source_type,
                run_id=run_id,
                tenant_id=actual.tenant_id,
                user_id=actual.user_id,
                memory_namespace=actual.memory_namespace,
                metadata={
                    "desktop_request_id": request_id,
                    "desktop_generation": generation,
                    "expected_paper_id": actual_paper,
                },
            )
            parsed = self._research.parse_paper(parse_command)
            identity = self._research.recover_prepared_source(parse_command)
            if identity is None:
                raise ResearchDesktopServiceError(
                    "run_recovery_invalid",
                    "Prepared source final receipt is not durably readable",
                    status_code=503,
                )
        except Exception as exc:
            self._record_failure(actual, request_id=request_id, generation=generation, exc=exc)
            raise _service_error(exc) from exc
        parsed_paper = str(parsed.get("paperId") or parsed.get("paper_id") or actual_paper)
        prepared_paper = str(identity.get("paperId") or "")
        if parsed_paper != actual_paper or prepared_paper != actual_paper:
            error = ResearchDesktopServiceError(
                "source_paper_mismatch",
                "Prepared source does not match the requested paper",
                status_code=409,
                user_action_required=True,
            )
            self._record_failure(actual, request_id=request_id, generation=generation, exc=error)
            raise error
        self._record_capability("source", operation="prepare")
        completed = self._requests.complete(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            run_id=run_id,
            result={
                "status": "ready",
                "paperId": actual_paper,
                "sourceRevision": identity["sourceRevision"],
                "sourceFingerprint": identity["sourceFingerprint"],
            },
        )
        return self._operation_response(completed)

    def submit_analysis(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
        request_fingerprint: str,
        paper_id: str,
        source_revision: str,
        source_fingerprint: str,
        source_url: str | None = None,
        pdf_url: str | None = None,
        objective: str | None = None,
        constraints: Sequence[str] = (),
        personal_memory: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        actual_paper = _safe_id(paper_id, "paperId")
        revision = _required_text(source_revision, "sourceRevision", maximum=512)
        source_digest = _sha256_fingerprint(source_fingerprint, "sourceFingerprint")
        actual_constraints = _text_list(constraints, "constraints", maximum_items=20, maximum_length=500)
        memory_request = _personal_memory_request(personal_memory)
        semantic_objective = _optional_text(objective, "objective", maximum=2000)
        effective_objective = semantic_objective or f"Analyze paper {actual_paper}"
        source = _optional_url(source_url, "sourceUrl")
        pdf = _optional_url(pdf_url, "pdfUrl")
        imported_source: Mapping[str, Any] | None = None
        if not source and not pdf:
            try:
                imported_source = self._imports.resolve_completed_source(
                    user_id=actual.user_id,
                    paper_id=actual_paper,
                    source_revision=revision,
                    source_fingerprint=source_digest,
                )
            except ResearchImportError as exc:
                raise ResearchDesktopServiceError(
                    "source_identity_unavailable",
                    "No exact owner-scoped source is available for this paper revision",
                    status_code=422,
                    user_action_required=True,
                ) from exc
        bound_source_locator: str | None = None
        if imported_source is not None:
            try:
                bound_source_locator = self._research.resolve_analyze_source(
                    actual_paper,
                    source_revision=revision,
                    source_fingerprint=source_digest,
                    actor=actual.actor(),
                )
            except ResearchServiceError as exc:
                raise _service_error(exc) from exc
        semantic = {
            "kind": "analyze",
            "paperId": actual_paper,
            "sourceRevision": revision,
            "sourceFingerprint": source_digest,
            "sourceUrl": source,
            "pdfUrl": pdf,
            "objective": semantic_objective,
            "constraints": actual_constraints,
            **({"personalMemory": memory_request} if memory_request is not None else {}),
        }
        self._validate_request_fingerprint(semantic, request_fingerprint)
        _require_model_credential()
        decision = self._requests.admit(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            kind="analyze",
            fingerprint=request_fingerprint,
            semantic=semantic,
        )
        if not decision.execute:
            return self._operation_response(decision.admission)
        run_id = _run_id(actual.user_id, generation, request_id)
        try:
            self._requests.bind_authority(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                run_id=run_id,
            )
            memory_context = self._personal_memory_context(
                actual, memory_request
            )
            response = self._research.analyze_paper(
                ResearchAnalyzeInput(
                    paper_id=actual_paper,
                    source_url=source,
                    pdf_url=pdf or bound_source_locator,
                    run_id=run_id,
                    user_id=actual.user_id,
                    tenant_id=actual.tenant_id,
                    memory_namespace=actual.memory_namespace,
                    personal_memory_context=memory_context,
                    metadata={
                        "desktop_request_id": request_id,
                        "desktop_generation": generation,
                        "source_revision": revision,
                        "source_fingerprint": source_digest,
                        **({"import_id": imported_source["importId"]} if imported_source is not None else {}),
                    },
                    options={
                        RESEARCH_INPUT_SOURCE_REVISION_OPTION: revision,
                        "research_input_objective": effective_objective,
                        "research_input_constraints": actual_constraints,
                    },
                )
            )
        except Exception as exc:
            self._record_failure(actual, request_id=request_id, generation=generation, exc=exc)
            raise _service_error(exc) from exc
        authority_run_id = str(response.get("runId") or run_id)
        projection = self._research.get_run_projection(authority_run_id, actor=actual.actor())
        result = _analysis_operation_result(projection, source_revision=revision)
        self._record_capability("single-paper-analysis", operation="analyze")
        if not _result_identity_matches(
            projection,
            run_id=authority_run_id,
            paper_id=actual_paper,
            source_revision=revision,
        ):
            failed = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "result_identity_mismatch",
                    "message": "The analysis result does not match the frozen run and source identity",
                    "retryable": False,
                },
                needs_review=True,
            )
            return self._operation_response(failed)
        if not _personal_memory_disclosure_matches(
            memory_request,
            projection.get("diagnostics") if isinstance(projection.get("diagnostics"), Mapping) else {},
        ):
            failed = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "personal_memory_disclosure_mismatch",
                    "message": "The analysis memory receipt does not match the frozen request selection",
                    "retryable": False,
                },
                needs_review=True,
            )
            return self._operation_response(failed)
        if not _analysis_result_is_accepted(result):
            rejected = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "quality_gate_failed",
                    "message": "The authoritative Research quality gate did not accept the analysis",
                    "retryable": False,
                },
            )
            return self._operation_response(rejected)
        self._record_capability("accepted-report", operation="analyze")
        completed = self._requests.complete(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            run_id=authority_run_id,
            result=result,
        )
        return self._operation_response(completed)

    def submit_ask(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
        request_fingerprint: str,
        run_id: str,
        paper_id: str,
        source_revision: str,
        source_fingerprint: str,
        question: str,
        selection: Mapping[str, Any] | None = None,
        locale: str | None = None,
        personal_memory: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        actual_run = _safe_id(run_id, "runId")
        actual_paper = _safe_id(paper_id, "paperId")
        revision = _required_text(source_revision, "sourceRevision", maximum=512)
        source_digest = _sha256_fingerprint(source_fingerprint, "sourceFingerprint")
        actual_question = _required_text(question, "question", maximum=4000)
        actual_selection = _ask_selection(selection or {})
        actual_locale = _optional_text(locale, "locale", maximum=32)
        memory_request = _personal_memory_request(personal_memory)
        semantic = {
            "kind": "ask",
            "runId": actual_run,
            "paperId": actual_paper,
            "sourceRevision": revision,
            "sourceFingerprint": source_digest,
            "question": actual_question,
            "selection": actual_selection,
            "locale": actual_locale,
            **({"personalMemory": memory_request} if memory_request is not None else {}),
        }
        self._validate_request_fingerprint(semantic, request_fingerprint)
        self._require_run_revision(
            actual,
            run_id=actual_run,
            source_revision=revision,
            source_fingerprint=source_digest,
        )
        _require_model_credential()
        decision = self._requests.admit(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            kind="ask",
            fingerprint=request_fingerprint,
            semantic=semantic,
        )
        if not decision.execute:
            return self._operation_response(decision.admission)
        try:
            self._requests.bind_authority(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                run_id=actual_run,
            )
            memory_context = self._personal_memory_context(
                actual, memory_request
            )
            response = self._research.ask_paper(
                actual_paper,
                ResearchAskInput(
                    question=actual_question,
                    locale=actual_locale,
                    selection=actual_selection,
                    tenant_id=actual.tenant_id,
                    user_id=actual.user_id,
                    memory_namespace=actual.memory_namespace,
                    run_id=actual_run,
                    request_id=request_id,
                    personal_memory_context=memory_context,
                ),
            )
        except Exception as exc:
            self._record_failure(actual, request_id=request_id, generation=generation, exc=exc)
            raise _service_error(exc) from exc
        if response.get("abstained") is not False or response.get("status") != "answered" or not response.get("answer"):
            rejected = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": _answer_rejection_code(response),
                    "message": "The grounded answer gate did not accept an answer",
                    "retryable": False,
                    "answerTrace": _answer_trace_for_audit(response),
                },
            )
            return self._operation_response(rejected)
        result = _ask_operation_result(response, source_revision=revision)
        if not _ask_result_has_verified_citations(
            response, paper_id=actual_paper, source_revision=revision
        ):
            rejected = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "answer_citation_locator_unverified",
                    "message": "The grounded answer citation locator could not be verified",
                    "retryable": False,
                    "answerTrace": _answer_trace_for_audit(response),
                },
            )
            return self._operation_response(rejected)
        if not _result_identity_matches(
            response,
            run_id=actual_run,
            paper_id=actual_paper,
            source_revision=None,  # Ask source identity is verified on every citation above.
        ):
            failed = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "result_identity_mismatch",
                    "message": "The answer result does not match the frozen run and source identity",
                    "retryable": False,
                    "answerTrace": _answer_trace_for_audit(response),
                },
                needs_review=True,
            )
            return self._operation_response(failed)
        if not _personal_memory_disclosure_matches(memory_request, response):
            failed = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": "personal_memory_disclosure_mismatch",
                    "message": "The answer memory receipt does not match the frozen request selection",
                    "retryable": False,
                    "answerTrace": _answer_trace_for_audit(response),
                },
                needs_review=True,
            )
            return self._operation_response(failed)
        self._record_capability("paper-question-answering", operation="ask")
        completed = self._requests.complete(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            run_id=actual_run,
            result=result,
        )
        return self._operation_response(completed)

    def submit_import(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
        request_fingerprint: str,
        content: bytes,
        filename: str | None,
        source_revision: str,
        source_fingerprint: str,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        if not isinstance(content, bytes):
            raise ResearchDesktopServiceError("invalid_request", "PDF content must be bytes")
        content_digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        declared_digest = _sha256_fingerprint(source_fingerprint, "sourceFingerprint")
        if declared_digest != content_digest:
            raise ResearchDesktopServiceError(
                "source_fingerprint_mismatch",
                "Uploaded PDF does not match sourceFingerprint",
                status_code=409,
                user_action_required=True,
            )
        revision = _required_text(source_revision, "sourceRevision", maximum=512)
        actual_filename = _normalized_upload_filename(filename)
        semantic = {
            "kind": "import",
            "filename": actual_filename,
            "sizeBytes": len(content),
            "sourceRevision": revision,
            "sourceFingerprint": declared_digest,
        }
        self._validate_request_fingerprint(semantic, request_fingerprint)
        decision = self._requests.admit(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            kind="import",
            fingerprint=request_fingerprint,
            semantic=semantic,
        )
        if not decision.execute:
            return self._operation_response(decision.admission)
        import_id = _import_id(actual.user_id, generation, request_id)
        try:
            self._requests.bind_authority(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                import_id=import_id,
            )
            received = self._imports.create_received(
                user_id=actual.user_id,
                content=content,
                filename=filename,
                import_id=import_id,
                source_revision=revision,
                source_fingerprint=declared_digest,
            )
            if str(received["importId"]) != import_id:
                raise ResearchDesktopServiceError(
                    "import_authority_conflict",
                    "Import authority returned a different identity",
                    status_code=409,
                )
            result = ResearchImportApplication(self._imports, self._research).process(
                user_id=actual.user_id,
                import_id=import_id,
            )
        except Exception as exc:
            self._record_failure(actual, request_id=request_id, generation=generation, exc=exc)
            raise _service_error(exc) from exc
        if result.get("status") == "failed":
            error = result.get("error") if isinstance(result.get("error"), Mapping) else {}
            failed = self._requests.fail(
                owner_id=actual.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": str(error.get("code") or "research_parse_failed"),
                    "message": str(error.get("message") or "PDF import failed"),
                    "retryable": True,
                },
            )
            return self._operation_response(failed)
        self._record_capability("pdf-import", operation="import")
        self._record_capability("source", operation="import")
        completed = self._requests.complete(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
            import_id=import_id,
            result={**result, "sourceRevision": revision, "sourceFingerprint": declared_digest},
        )
        return self._operation_response(completed)

    def get_operation(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        admission = self._requests.get(
            owner_id=actual.user_id,
            request_id=request_id,
            generation=generation,
        )
        if admission is None:
            raise ResearchDesktopServiceError(
                "desktop_request_not_found",
                "Desktop request was not found; a new execution requires a new explicit request identity",
                status_code=404,
                user_action_required=True,
            )
        admission = self._reconcile_operation(actual, admission)
        return self._operation_response(admission)

    def _reconcile_operation(self, owner: ResearchDesktopOwner, admission: Any) -> Any:
        if admission.status in {"completed", "failed"}:
            return admission
        if admission.kind == "prepare" and admission.run_id:
            semantic = admission.semantic
            paper_id = str(semantic.get("paperId") or "")
            source_url = str(semantic.get("sourceUrl") or "")
            try:
                identity = self._research.recover_prepared_source(
                    ResearchParseInput(
                        source=source_url, source_url=source_url,
                        source_type=semantic.get("sourceType"),
                        run_id=admission.run_id, tenant_id=owner.tenant_id,
                        user_id=owner.user_id, memory_namespace=owner.memory_namespace,
                        metadata={
                            "desktop_request_id": admission.request_id,
                            "desktop_generation": admission.generation,
                            "expected_paper_id": paper_id,
                        },
                    )
                )
            except ResearchServiceError as exc:
                return self._requests.fail(
                    owner_id=owner.user_id, request_id=admission.request_id,
                    generation=admission.generation,
                    error={"code": exc.code, "message": exc.public_message, "retryable": False},
                    needs_review=exc.code == "run_recovery_invalid",
                )
            if identity is None:
                return admission
            if identity.get("paperId") != paper_id:
                return self._requests.fail(
                    owner_id=owner.user_id, request_id=admission.request_id,
                    generation=admission.generation,
                    error={
                        "code": "source_paper_mismatch",
                        "message": "Prepared source does not match the requested paper",
                        "retryable": False,
                    },
                )
            return self._requests.complete(
                owner_id=owner.user_id, request_id=admission.request_id,
                generation=admission.generation, run_id=admission.run_id,
                result=identity,
            )
        if admission.kind == "analyze" and admission.run_id:
            try:
                projection = self._research.get_run_projection(
                    admission.run_id,
                    actor=owner.actor(),
                )
            except ResearchServiceError as exc:
                if exc.code in {"research_run_not_found", "research_run_failed"}:
                    return admission
                raise _service_error(exc) from exc
            status = str(projection.get("status") or "unknown")
            if status == "succeeded":
                result = _analysis_operation_result(
                    projection,
                    source_revision=str(admission.semantic.get("sourceRevision") or ""),
                )
                memory_request = (
                    admission.semantic.get("personalMemory")
                    if isinstance(admission.semantic.get("personalMemory"), Mapping)
                    else None
                )
                if not _result_identity_matches(
                    projection,
                    run_id=admission.run_id,
                    paper_id=str(admission.semantic.get("paperId") or ""),
                    source_revision=str(admission.semantic.get("sourceRevision") or ""),
                ):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "result_identity_mismatch",
                            "message": "The analysis result does not match the frozen run and source identity",
                            "retryable": False,
                        },
                        needs_review=True,
                    )
                if not _personal_memory_disclosure_matches(
                    memory_request,
                    projection.get("diagnostics") if isinstance(projection.get("diagnostics"), Mapping) else {},
                ):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "personal_memory_disclosure_mismatch",
                            "message": "The analysis memory receipt does not match the frozen request selection",
                            "retryable": False,
                        },
                        needs_review=True,
                    )
                if not _analysis_result_is_accepted(result):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "quality_gate_failed",
                            "message": "The authoritative Research quality gate did not accept the analysis",
                            "retryable": False,
                        },
                    )
                return self._requests.complete(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    run_id=admission.run_id,
                    result=result,
                )
            if status in {"failed", "rejected", "cancelled"}:
                return self._requests.fail(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    error={
                        "code": "research_run_failed",
                        "message": "The authoritative Research run did not complete successfully",
                        "retryable": False,
                    },
                )
        if admission.kind == "ask" and admission.run_id:
            semantic = admission.semantic
            try:
                recovered = self._research.recover_paper_answer(
                    str(semantic.get("paperId") or ""),
                    ResearchAskInput(
                        question=str(semantic.get("question") or ""),
                        locale=semantic.get("locale") if isinstance(semantic.get("locale"), str) else None,
                        selection=semantic.get("selection") if isinstance(semantic.get("selection"), Mapping) else {},
                        tenant_id=owner.tenant_id,
                        user_id=owner.user_id,
                        memory_namespace=owner.memory_namespace,
                        run_id=admission.run_id,
                        request_id=admission.request_id,
                    ),
                    include_interrupted=admission.status == "needs_review",
                )
            except ResearchServiceError as exc:
                if exc.code in {"paper_not_found", "research_run_not_found", "analysis_not_found"}:
                    return admission
                raise _service_error(exc) from exc
            if isinstance(recovered, Mapping) and recovered.get("recoveryState") == "needs_action":
                return self._requests.fail(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    error={
                        "code": str(recovered.get("errorCode") or "rag_execution_interrupted"),
                        "message": "The grounded answer execution was interrupted and requires an explicit new request",
                        "retryable": False,
                    },
                    needs_review=True,
                )
            if recovered is not None:
                if recovered.get("abstained") is not False or recovered.get("status") != "answered" or not recovered.get("answer"):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": _answer_rejection_code(recovered),
                            "message": "The grounded answer gate did not accept an answer",
                            "retryable": False,
                            "answerTrace": _answer_trace_for_audit(recovered),
                        },
                    )
                revision = str(semantic.get("sourceRevision") or "")
                result = _ask_operation_result(recovered, source_revision=revision)
                if not _ask_result_has_verified_citations(
                    recovered,
                    paper_id=str(semantic.get("paperId") or ""),
                    source_revision=revision,
                ):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "answer_citation_locator_unverified",
                            "message": "The grounded answer citation locator could not be verified",
                            "retryable": False,
                            "answerTrace": _answer_trace_for_audit(recovered),
                        },
                    )
                memory_request = (
                    semantic.get("personalMemory")
                    if isinstance(semantic.get("personalMemory"), Mapping)
                    else None
                )
                if not _result_identity_matches(
                    recovered,
                    run_id=admission.run_id,
                    paper_id=str(semantic.get("paperId") or ""),
                    source_revision=None,  # Recovered ask citations carry the exact revision.
                ):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "result_identity_mismatch",
                            "message": "The answer result does not match the frozen run and source identity",
                            "retryable": False,
                        },
                        needs_review=True,
                    )
                if not _personal_memory_disclosure_matches(memory_request, recovered):
                    return self._requests.fail(
                        owner_id=owner.user_id,
                        request_id=admission.request_id,
                        generation=admission.generation,
                        error={
                            "code": "personal_memory_disclosure_mismatch",
                            "message": "The answer memory receipt does not match the frozen request selection",
                            "retryable": False,
                        },
                        needs_review=True,
                    )
                return self._requests.complete(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    run_id=admission.run_id,
                    result=result,
                )
        if admission.kind == "import" and admission.import_id:
            try:
                imported = self._imports.get(
                    user_id=owner.user_id,
                    import_id=admission.import_id,
                )
            except ResearchImportError as exc:
                if exc.code == "import_not_found":
                    return admission
                raise _service_error(exc) from exc
            import_status = str(imported.get("status") or "")
            if import_status == "completed":
                return self._requests.complete(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    import_id=admission.import_id,
                    result={
                        **imported,
                        "sourceRevision": admission.semantic.get("sourceRevision"),
                        "sourceFingerprint": admission.semantic.get("sourceFingerprint"),
                    },
                )
            if import_status == "failed":
                error = imported.get("error") if isinstance(imported.get("error"), Mapping) else {}
                return self._requests.fail(
                    owner_id=owner.user_id,
                    request_id=admission.request_id,
                    generation=admission.generation,
                    error={
                        "code": str(error.get("code") or "research_parse_failed"),
                        "message": str(error.get("message") or "PDF import failed"),
                        "retryable": False,
                    },
                )
        return admission

    def get_report(
        self,
        owner: ResearchDesktopOwner,
        *,
        run_id: str,
        source_revision: str,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        revision = _required_text(source_revision, "sourceRevision", maximum=512)
        projection = self._require_run_revision(actual, run_id=run_id, source_revision=revision)
        return _report_projection(projection, source_revision=revision)

    def save_report(
        self,
        owner: ResearchDesktopOwner,
        *,
        report_id: str,
        run_id: str,
        source_revision: str,
        title: str,
    ) -> dict[str, Any]:
        actual = _owner(owner)
        revision = _required_text(source_revision, "sourceRevision", maximum=512)
        projection = self._require_run_revision(actual, run_id=run_id, source_revision=revision)
        if not projection.get("eligibleForAcceptedExport"):
            raise ResearchDesktopServiceError(
                "report_not_accepted",
                "Only an accepted Research result can be saved as an accepted report",
                status_code=422,
                user_action_required=True,
            )
        saved = self._requests.save_report_ref(
            owner_id=actual.user_id,
            report_id=report_id,
            run_id=str(projection["runId"]),
            paper_id=str(projection["paperId"]),
            source_revision=revision,
            title=_required_text(title, "title", maximum=500),
            metadata={"qualityPassed": True, "accepted": True},
        )
        return {"reference": saved.to_dict(), "report": _report_projection(projection, source_revision=revision)}

    def get_saved_report(self, owner: ResearchDesktopOwner, *, report_id: str) -> dict[str, Any]:
        actual = _owner(owner)
        saved = self._requests.get_report_ref(owner_id=actual.user_id, report_id=report_id)
        if saved is None:
            raise ResearchDesktopServiceError("report_not_found", "Saved report was not found", status_code=404)
        projection = self._require_run_revision(
            actual,
            run_id=saved.run_id,
            source_revision=saved.source_revision,
        )
        return {
            "reference": saved.to_dict(),
            "report": _report_projection(
                projection,
                source_revision=saved.source_revision,
                report_id=saved.report_id,
                title=saved.title,
                generated_at=saved.saved_at,
            ),
        }

    def list_saved_reports(self, owner: ResearchDesktopOwner) -> dict[str, Any]:
        actual = _owner(owner)
        return {
            "reports": [
                saved.to_dict()
                for saved in self._requests.list_report_refs(owner_id=actual.user_id)
            ]
        }

    def export_report(
        self,
        owner: ResearchDesktopOwner,
        *,
        run_id: str,
        source_revision: str,
        format: str,
    ) -> dict[str, Any]:
        report = self.get_report(owner, run_id=run_id, source_revision=source_revision)
        if not report["eligibility"]["acceptedExport"]:
            raise ResearchDesktopServiceError(
                "report_not_accepted",
                "Gate-rejected or incomplete candidates cannot use accepted report export",
                status_code=422,
                user_action_required=True,
            )
        export_format = str(format or "").strip().lower()
        stem = _export_stem(str(report["paperId"]), str(report["runId"]))
        if export_format == "json":
            content = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            return {"filename": f"{stem}.json", "contentType": "application/json; charset=utf-8", "content": content}
        if export_format == "markdown":
            return {
                "filename": f"{stem}.md",
                "contentType": "text/markdown; charset=utf-8",
                "content": _report_markdown(report),
            }
        raise ResearchDesktopServiceError("invalid_export_format", "format must be markdown or json")

    def export_draft(
        self,
        owner: ResearchDesktopOwner,
        *,
        title: str,
        question: str,
        notes: str,
        materials: Sequence[Mapping[str, Any]],
        format: str,
    ) -> dict[str, Any]:
        _owner(owner)
        payload = {
            "schema": "newsroom.research-report-draft/v1",
            "status": "draft_unverified",
            "title": _required_text(title, "title", maximum=500),
            "question": _required_text(question, "question", maximum=2000),
            "notes": _optional_text(notes, "notes", maximum=30_000) or "",
            "materials": [_json_mapping(item, "material", maximum_bytes=16_000) for item in materials[:100]],
            "exportedAt": _now(),
        }
        export_format = str(format or "").strip().lower()
        stem = _export_stem(payload["title"], "draft")
        if export_format == "json":
            return {
                "filename": f"{stem}.json",
                "contentType": "application/json; charset=utf-8",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            }
        if export_format == "markdown":
            lines = [
                f"# {_markdown_text(payload['title'])}",
                "",
                "> Status: Draft / unverified. This is not an accepted Research report.",
                "",
                "## Question",
                "",
                _markdown_text(payload["question"]),
                "",
                "## Notes",
                "",
                _markdown_text(payload["notes"]),
                "",
                "## Materials",
                "",
            ]
            for item in payload["materials"]:
                lines.append(f"- {_markdown_text(str(item.get('title') or item.get('id') or 'Untitled material'))}")
            return {"filename": f"{stem}.md", "contentType": "text/markdown; charset=utf-8", "content": "\n".join(lines) + "\n"}
        raise ResearchDesktopServiceError("invalid_export_format", "format must be markdown or json")

    def _require_run_revision(
        self,
        owner: ResearchDesktopOwner,
        *,
        run_id: str,
        source_revision: str,
        source_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        try:
            projection = self._research.get_run_projection(_safe_id(run_id, "runId"), actor=owner.actor())
        except ResearchServiceError as exc:
            raise _service_error(exc) from exc
        actual_revision = projection.get("sourceRevision")
        if actual_revision is None:
            raise ResearchDesktopServiceError(
                "source_revision_unavailable",
                "Research result does not contain an exact source revision",
                status_code=422,
                user_action_required=True,
                details={"runId": projection.get("runId")},
            )
        if str(actual_revision) != source_revision:
            raise ResearchDesktopServiceError(
                "source_revision_conflict",
                "Research result belongs to a different source revision",
                status_code=409,
                user_action_required=True,
                details={"runId": projection.get("runId")},
            )
        if source_fingerprint is not None:
            actual_fingerprint = projection.get("sourceFingerprint")
            if actual_fingerprint is None:
                raise ResearchDesktopServiceError(
                    "source_fingerprint_unavailable",
                    "Research result does not contain an exact source fingerprint",
                    status_code=422,
                    user_action_required=True,
                    details={"runId": projection.get("runId")},
                )
            if str(actual_fingerprint) != source_fingerprint:
                raise ResearchDesktopServiceError(
                    "source_fingerprint_conflict",
                    "Research result belongs to different source content",
                    status_code=409,
                    user_action_required=True,
                    details={"runId": projection.get("runId")},
                )
        return projection

    def _record_failure(
        self,
        owner: ResearchDesktopOwner,
        *,
        request_id: str,
        generation: str,
        exc: Exception,
    ) -> None:
        projected = _service_error(exc)
        try:
            self._requests.fail(
                owner_id=owner.user_id,
                request_id=request_id,
                generation=generation,
                error={
                    "code": projected.code,
                    "message": str(projected),
                    "retryable": projected.retryable,
                },
            )
        except Exception:
            # The original operation error remains authoritative; callers can
            # query a still-running admission as an uncertain outcome.
            return

    @staticmethod
    def _validate_request_fingerprint(semantic: Mapping[str, Any], supplied: str) -> None:
        expected = canonical_request_fingerprint(semantic)
        actual = _sha256_fingerprint(supplied, "requestFingerprint")
        if actual != expected:
            raise ResearchDesktopServiceError(
                "request_fingerprint_mismatch",
                "requestFingerprint does not match the canonical request semantics",
                status_code=409,
                user_action_required=True,
                details={"fingerprintVersion": "sha256-canonical-json-v1"},
            )

    @staticmethod
    def _operation_response(admission: Any) -> dict[str, Any]:
        raw = admission.to_dict()
        result = raw.get("result") if isinstance(raw.get("result"), Mapping) else {}
        error = raw.get("error") if isinstance(raw.get("error"), Mapping) else {}
        status = str(raw.get("status") or "")
        if status == "admitted":
            projected_status = "waiting"
        elif status == "running":
            projected_status = "running"
        elif status == "completed":
            projected_status = "passed"
        elif status == "needs_review":
            projected_status = "needs_action"
        elif str(error.get("code") or "") in {
            "quality_gate_failed",
            "import_quality_failed",
            "answer_quality_gate_rejected",
            "answer_citation_locator_unverified",
        }:
            projected_status = "rejected"
        elif status == "failed":
            projected_status = "failed"
        else:
            projected_status = "unknown"
        semantic = raw.get("semantic") if isinstance(raw.get("semantic"), Mapping) else {}
        payload: dict[str, Any] = {
            "requestId": raw["requestId"],
            "generation": raw["generation"],
            "fingerprint": raw["fingerprint"],
            "kind": raw["kind"],
            "status": projected_status,
            "updatedAt": raw.get("updatedAt"),
            "recoverable": True,
            "automaticRetryAllowed": False,
        }
        optional = {
            "paperId": result.get("paperId") or semantic.get("paperId"),
            "sourceRevision": result.get("sourceRevision") or semantic.get("sourceRevision"),
            "importId": raw.get("importId"),
            "runId": raw.get("runId") or result.get("runId"),
            "reportId": result.get("reportId"),
            "errorCode": error.get("code"),
            "message": _operation_message(projected_status, error.get("code")),
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if result:
            payload["result"] = dict(result)
        elif isinstance(error.get("answerTrace"), Mapping):
            payload["result"] = dict(error["answerTrace"])
        return payload


def _desktop_reader_projection(
    reader: Mapping[str, Any],
    *,
    paper_id: str,
    source_revision: str,
    source_fingerprint: str,
    run_id: str,
    ask_available: bool,
    private_source_url: str | None = None,
) -> dict[str, Any]:
    paper = _paper_projection(reader.get("paper"), paper_id=paper_id)
    if private_source_url is not None:
        paper["pdfUrl"] = private_source_url
    document = reader.get("document") if isinstance(reader.get("document"), Mapping) else {}
    analysis = reader.get("analysis") if isinstance(reader.get("analysis"), Mapping) else {}
    quality_items = reader.get("quality") if isinstance(reader.get("quality"), list) else []
    return {
        "paper": paper,
        "reader": {
            "paper": paper,
            "sections": _section_projections(document),
            "aiSummary": _ai_summary_projection(analysis),
            "readerNotes": [],
            "relatedPapers": [],
            "relatedProjects": [],
            "relatedNews": [],
            "quality": _reader_quality_projection(
                paper_id,
                sections=_section_projections(document),
                analysis=analysis,
                quality_items=quality_items,
                pdf_available=private_source_url is not None,
            ),
        },
        "state": "ready",
        "aiReady": ask_available,
        "sourceRevision": source_revision,
        "sourceFingerprint": source_fingerprint,
        "runId": run_id,
        "capabilities": {"ask": ask_available, "analyze": True},
    }


def _desktop_document_reader_projection(
    catalog: Mapping[str, Any],
    document: Mapping[str, Any],
    *,
    paper_id: str,
    source_revision: str | None,
    source_fingerprint: str | None,
    analyze_available: bool,
    private_source_url: str | None = None,
) -> dict[str, Any]:
    catalog_entry = catalog.get("catalog") if isinstance(catalog.get("catalog"), Mapping) else catalog
    identity = catalog_entry.get("identity") if isinstance(catalog_entry.get("identity"), Mapping) else catalog_entry
    paper = _paper_projection(
        {**dict(identity), **_document_paper_metadata(document)},
        paper_id=paper_id,
    )
    if private_source_url is not None:
        paper["pdfUrl"] = private_source_url
    sections = _section_projections(document)
    quality_report = document.get("quality_report") if isinstance(document.get("quality_report"), Mapping) else {}
    quality_passed = quality_report.get("passed") is True
    ready = bool(sections) and str(document.get("status") or "").lower() == "parsed" and quality_passed
    exact_identity = bool(source_revision and source_fingerprint)
    state = "ready" if ready and exact_identity else "needs_review"
    reader = None
    if ready:
        reader = {
            "paper": paper,
            "sections": sections,
            "aiSummary": None,
            "readerNotes": [],
            "relatedPapers": [],
            "relatedProjects": [],
            "relatedNews": [],
            "quality": _document_quality_projection(
                paper_id,
                document=document,
                sections=sections,
                quality_report=quality_report,
                pdf_available=private_source_url is not None,
            ),
        }
    payload: dict[str, Any] = {
        "paper": paper,
        "reader": reader,
        "state": state,
        "reasonCode": None if state == "ready" else ("source_identity_unavailable" if not exact_identity else "document_quality_pending"),
        "aiReady": False,
        "capabilities": {"ask": False, "analyze": bool(analyze_available and ready and exact_identity)},
    }
    if source_revision is not None:
        payload["sourceRevision"] = source_revision
    if source_fingerprint is not None:
        payload["sourceFingerprint"] = source_fingerprint
    return payload


def _document_paper_metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "title": document.get("title"),
        "authors": document.get("authors"),
        "abstract": document.get("abstract"),
        "source": "research",
    }


def _document_quality_projection(
    paper_id: str,
    *,
    document: Mapping[str, Any],
    sections: list[dict[str, Any]],
    quality_report: Mapping[str, Any],
    pdf_available: bool,
) -> dict[str, Any]:
    coverage = quality_report.get("evidence_coverage")
    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool):
        coverage = quality_report.get("coverage")
    return {
        "paperId": paper_id,
        "pdfAvailable": pdf_available,
        "textExtracted": bool(sections),
        "summaryAvailable": False,
        "implementationVerified": False,
        "benchmarkVerified": False,
        "evidenceCoverage": (float(coverage) if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and 0 <= float(coverage) <= 1 else 0.0),
    }

def _paper_projection(value: Any, *, paper_id: str) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    title = str(raw.get("title") or raw.get("name") or paper_id).strip()[:500]
    published = raw.get("published_at") or raw.get("publishedAt") or raw.get("published_date")
    return {
        "id": paper_id,
        "slug": str(raw.get("slug") or paper_id),
        "title": title,
        "authors": _author_names(raw.get("authors")),
        "abstract": str(raw.get("abstract") or raw.get("summary") or "")[:100_000],
        "publishedAt": str(published) if published else "",
        "source": str(raw.get("source") or "research"),
        "topics": list(raw.get("topics") or []),
        "taskRefs": list(raw.get("task_refs") or raw.get("taskRefs") or []),
        "methodRefs": list(raw.get("method_refs") or raw.get("methodRefs") or []),
        "pdfUrl": _public_http_url(raw.get("pdf_url") or raw.get("pdfUrl")),
        "paperUrl": _public_http_url(raw.get("paper_url") or raw.get("paperUrl") or raw.get("canonical_url")),
        "isPublished": bool(raw.get("is_published") or raw.get("isPublished")),
    }


def _section_projections(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_sections = document.get("sections")
    if not isinstance(raw_sections, list):
        return []
    sections: list[dict[str, Any]] = []
    paper_id = str(document.get("paper_id") or document.get("paperId") or "paper")
    for index, raw in enumerate(raw_sections):
        if not isinstance(raw, Mapping):
            continue
        section_id = str(raw.get("section_id") or raw.get("sectionId") or raw.get("id") or f"section-{index + 1}")
        sections.append({
            "id": section_id[:200],
            "paperId": paper_id[:200],
            "title": str(raw.get("title") or f"Section {index + 1}")[:500],
            "level": max(1, min(6, int(raw.get("level") or 1))),
            "pageStart": _citation_page(raw.get("page_start") or raw.get("pageStart") or raw.get("page")),
            "pageEnd": _citation_page(raw.get("page_end") or raw.get("pageEnd")),
            "textExcerpt": str(raw.get("text") or raw.get("content") or raw.get("text_excerpt") or raw.get("textExcerpt") or "")[:100_000],
            "summary": str(raw.get("summary") or "")[:10_000],
            "sectionType": str(raw.get("section_type") or raw.get("sectionType") or "unknown"),
        })
    return sections


def _ai_summary_projection(analysis: Mapping[str, Any]) -> dict[str, Any] | None:
    summary = analysis.get("summary")
    if not isinstance(summary, Mapping):
        return None
    return {
        "summary": str(summary.get("core_idea") or summary.get("problem") or ""),
        "keyPoints": list(summary.get("key_contributions") or []),
        "contributions": list(analysis.get("contributions") or []),
        "limitations": list(analysis.get("limitations") or summary.get("limitations") or []),
        "confidence": float(summary.get("confidence") or 0.0),
    }


def _reader_quality_projection(
    paper_id: str,
    *,
    sections: list[dict[str, Any]],
    analysis: Mapping[str, Any],
    quality_items: list[Any],
    pdf_available: bool,
) -> dict[str, Any]:
    coverage_values = [
        float(item["score"])
        for item in quality_items
        if isinstance(item, Mapping)
        and item.get("passed") is True
        and isinstance(item.get("score"), (int, float))
        and not isinstance(item.get("score"), bool)
        and 0 <= float(item["score"]) <= 1
    ]
    return {
        "paperId": paper_id,
        "pdfAvailable": pdf_available,
        "textExtracted": bool(sections),
        "summaryAvailable": bool(analysis.get("summary")),
        "implementationVerified": False,
        "benchmarkVerified": False,
        "evidenceCoverage": min(coverage_values) if coverage_values else 0.0,
    }

def _document_source_revision(document: Mapping[str, Any]) -> str | None:
    for key in ("source_revision", "sourceRevision", "source_hash", "sourceHash"):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:512]
    metadata = document.get("metadata")
    if isinstance(metadata, Mapping):
        return _document_source_revision(metadata)
    return None


def _document_source_fingerprint(document: Mapping[str, Any]) -> str | None:
    revision = _document_source_revision(document)
    if revision is None:
        return None
    if re.fullmatch(r"sha256:[0-9a-f]{64}", revision.lower()):
        return revision.lower()
    if re.fullmatch(r"[0-9a-f]{64}", revision.lower()):
        return f"sha256:{revision.lower()}"
    return f"sha256:{hashlib.sha256(revision.encode('utf-8')).hexdigest()}"


def _author_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("name")
        if isinstance(item, str) and item.strip():
            result.append(item.strip()[:200])
    return result[:100]


def _private_source_url(paper_id: str, source_revision: str, source_fingerprint: str) -> str:
    return (
        f"/api/v1/research/desktop/papers/{quote(paper_id, safe='')}/source"
        f"?sourceRevision={quote(source_revision, safe='')}"
        f"&sourceFingerprint={quote(source_fingerprint, safe='')}"
    )


def _public_http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith(("https://", "http://")):
        return None
    return value[:4000]



def _answer_rejection_code(value: Mapping[str, Any]) -> str:
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("citationLocatorVerified") is False:
        return "answer_citation_locator_unverified"
    return "answer_quality_gate_rejected"


def _answer_trace_for_audit(value: Mapping[str, Any]) -> dict[str, Any]:
    """Persist real failure trace without exposing an unaccepted GroundedAnswer."""
    trace: dict[str, Any] = {"answerStatus": value.get("status")}
    transcript_id = value.get("transcriptId")
    if isinstance(transcript_id, str) and 1 <= len(transcript_id) <= 256:
        trace["transcriptId"] = transcript_id
    refs = value.get("evidenceRefs")
    if isinstance(refs, list):
        trace["evidenceRefs"] = [ref for ref in refs if isinstance(ref, str) and 1 <= len(ref) <= 256][:1000]
    decision = value.get("decision")
    if isinstance(decision, Mapping):
        trace["decision"] = dict(decision)
    return trace


def _ask_operation_result(value: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    citations = value.get("citations") if isinstance(value.get("citations"), list) else []
    result: dict[str, Any] = {
        "runId": str(value.get("runId") or "")[:256],
        "paperId": str(value.get("paperId") or "")[:256],
        "sourceRevision": source_revision,
        "answer": str(value.get("answer") or "")[:100_000],
        "citations": [
            {
                **_public_mapping(
                    item,
                    (
                        "id", "label", "evidenceId", "sectionId", "page",
                        "excerpt", "valid", "reasonCode",
                    ),
                ),
            }
            for item in citations[:1000]
            if isinstance(item, Mapping)
        ],
        "confidence": _public_score(value.get("confidence")),
        "evidenceRefs": list(value.get("evidenceRefs") or ()),
        "transcriptId": value.get("transcriptId"),
        "answerStatus": value.get("status"),
        **(
            {"usedPersonalMemory": used}
            if (used := _used_personal_memory_projection(value.get("usedPersonalMemory")))
            else {}
        ),
    }
    generated_at = value.get("generatedAt") or value.get("generated_at")
    if isinstance(generated_at, str) and generated_at.strip():
        result["generatedAt"] = generated_at.strip()[:100]
    return result



def _analysis_operation_result(projection: Mapping[str, Any], *, source_revision: str) -> dict[str, Any]:
    return {
        "runId": str(projection.get("runId") or ""),
        "paperId": str(projection.get("paperId") or ""),
        "status": str(projection.get("status") or "unknown"),
        "sourceRevision": source_revision,
        "accepted": projection.get("accepted") is True,
        "qualityPassed": projection.get("qualityPassed") is True,
        "eligibleForAcceptedExport": projection.get("eligibleForAcceptedExport") is True,
        "artifactStatus": str(projection.get("artifactStatus") or "missing"),
        **(
            {"traceRef": trace_ref}
            if (trace_ref := _citation_text(projection.get("traceRef"), maximum=1000))
            else {}
        ),
        **(
            {"usedPersonalMemory": used}
            if (used := _used_personal_memory_projection(
                (projection.get("diagnostics") or {}).get("usedPersonalMemory")
                if isinstance(projection.get("diagnostics"), Mapping)
                else None
            ))
            else {}
        ),
    }

def _report_projection(
    run: Mapping[str, Any],
    *,
    source_revision: str,
    report_id: str | None = None,
    title: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    analysis = run.get("analysis") if isinstance(run.get("analysis"), Mapping) else None
    quality = run.get("quality") if isinstance(run.get("quality"), Mapping) else None
    paper_id = str(run.get("paperId") or "unknown-paper")
    artifact_valid = run.get("artifactStatus") == "valid"
    accepted_export = bool(run.get("eligibleForAcceptedExport")) and artifact_valid
    summary = analysis.get("summary") if isinstance(analysis, Mapping) else None
    if accepted_export and (
        not isinstance(analysis, Mapping)
        or analysis.get("paper_id") != paper_id
        or not isinstance(summary, Mapping)
        or not str(summary.get("problem") or "").strip()
        or not str(summary.get("core_idea") or "").strip()
        or not isinstance(quality, Mapping)
        or quality.get("target_id") != paper_id
        or quality.get("passed") is not True
        or not isinstance(quality.get("score"), (int, float))
        or not isinstance(quality.get("gate_results"), list)
    ):
        raise ResearchDesktopServiceError(
            "report_projection_invalid",
            "Accepted report source is missing its typed analysis or quality result",
            status_code=409,
            user_action_required=True,
        )
    projected_analysis = (
        _public_analysis_projection(analysis)
        if isinstance(analysis, Mapping) and _analysis_contract_complete(analysis)
        else None
    )
    projected_quality = (
        _public_quality_projection(quality)
        if isinstance(quality, Mapping) and _quality_contract_complete(quality)
        else None
    )
    if accepted_export and (projected_analysis is None or projected_quality is None):
        raise ResearchDesktopServiceError(
            "report_projection_invalid",
            "Accepted report source cannot be represented by the public report contract",
            status_code=409,
            user_action_required=True,
        )
    citations = _collect_citations(
        analysis,
        paper_id=paper_id,
        source_revision=source_revision,
        artifact_valid=artifact_valid,
        trusted_locators=_trusted_report_locators(run),
    )
    return {
        "schema": "newsroom.research-report/v1",
        "runId": str(run.get("runId") or ""),
        "paperId": paper_id,
        "sourceRevision": source_revision,
        "status": str(run.get("status") or "unknown"),
        "disposition": str(run.get("disposition")) if run.get("disposition") is not None else None,
        "analysis": projected_analysis,
        "quality": projected_quality,
        "citations": citations,
        **(
            {"usedPersonalMemory": used_personal_memory}
            if (used_personal_memory := _used_personal_memory_projection(
                (run.get("diagnostics") or {}).get("usedPersonalMemory")
                if isinstance(run.get("diagnostics"), Mapping)
                else None
            ))
            else {}
        ),
        "artifactRefs": [],
        "traceRef": None,
        "diagnostics": {
            "artifactStatus": str(run.get("artifactStatus") or "missing"),
            "artifactReason": str(run.get("artifactReason") or "")[:200],
            **({"reportId": report_id} if report_id else {}),
            **({"title": title} if title else {}),
            **({"generatedAt": generated_at} if generated_at else {}),
        },
        "eligibility": {
            "accepted": bool(run.get("accepted")),
            "qualityPassed": bool(run.get("qualityPassed")),
            "acceptedExport": accepted_export,
        },
    }

def _trusted_report_locators(run: Mapping[str, Any]) -> dict[str, Any]:
    """Build citation authority only from the persisted reader document and evidence pack."""

    reader = run.get("reader")
    if not isinstance(reader, Mapping):
        return {"sections": {}, "evidence": {}}
    document = reader.get("document")
    evidence_pack = reader.get("evidence")
    sections_by_source: dict[str, list[dict[str, Any]]] = {}
    if isinstance(document, Mapping):
        sections = document.get("sections")
        for item in sections if isinstance(sections, list) else []:
            if not isinstance(item, Mapping):
                continue
            source_ref = _citation_text(item.get("source_ref") or item.get("sourceRef"), maximum=4000)
            section_id = _citation_text(item.get("section_id") or item.get("sectionId"), maximum=200)
            if source_ref is None or section_id is None:
                continue
            sections_by_source.setdefault(source_ref, []).append({
                "sectionId": section_id,
                "pageStart": _document_page(item.get("page_start") or item.get("pageStart")),
                "pageEnd": _document_page(item.get("page_end") or item.get("pageEnd")),
            })
    evidence_by_id: dict[str, dict[str, Any]] = {}
    if isinstance(evidence_pack, Mapping):
        items = evidence_pack.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping):
                continue
            evidence_id = _citation_text(item.get("evidence_id") or item.get("evidenceId"), maximum=500)
            source_ref = _citation_text(item.get("source_ref") or item.get("sourceRef"), maximum=4000)
            if evidence_id is None or source_ref is None:
                continue
            spans = item.get("span_refs") or item.get("spanRefs")
            evidence_by_id[evidence_id] = {
                "sourceRef": source_ref,
                "spanRefs": [str(value)[:4000] for value in spans if str(value).strip()][:100]
                if isinstance(spans, list) else [],
            }
    return {"sections": sections_by_source, "evidence": evidence_by_id}


def _collect_citations(
    analysis: Mapping[str, Any] | None,
    *,
    paper_id: str,
    source_revision: str,
    artifact_valid: bool,
    trusted_locators: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if analysis is None:
        return []
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    sections_by_source = trusted_locators.get("sections")
    evidence_by_id = trusted_locators.get("evidence")
    if not isinstance(sections_by_source, Mapping):
        sections_by_source = {}
    if not isinstance(evidence_by_id, Mapping):
        evidence_by_id = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            evidence_id = _citation_text(value.get("evidence_id") or value.get("evidenceId"), maximum=500)
            authority = evidence_by_id.get(evidence_id) if evidence_id is not None else None
            source_ref = _citation_text(
                value.get("source_ref") or value.get("sourceRef")
                or (authority.get("sourceRef") if isinstance(authority, Mapping) else None),
                maximum=4000,
            )
            raw_identity = evidence_id or source_ref
            if raw_identity is not None and raw_identity not in seen:
                seen.add(raw_identity)
                requested_section = _citation_text(value.get("section_id") or value.get("sectionId"), maximum=200)
                requested_page = _citation_page(value.get("page") or value.get("pdfPage"))
                spans: list[str] = []
                if isinstance(authority, Mapping) and isinstance(authority.get("spanRefs"), list):
                    spans.extend(str(item) for item in authority["spanRefs"])
                span_ref = value.get("span_ref") or value.get("spanRef")
                if isinstance(span_ref, str):
                    spans.append(span_ref)
                derived_section, derived_page = _locator_from_spans(spans)
                section_id = requested_section or derived_section
                page = requested_page if requested_page is not None else derived_page
                trusted_section = _match_document_section(
                    sections_by_source, source_ref=source_ref, section_id=section_id, page=page
                )
                if trusted_section is not None:
                    section_id = str(trusted_section["sectionId"])
                    if page is None:
                        page = trusted_section.get("pageStart")
                identity = evidence_id or f"{source_ref}|{section_id or ''}|{page if page is not None else ''}"
                digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                valid = bool(artifact_valid and source_ref and trusted_section is not None)
                citation: dict[str, Any] = {
                    "id": f"citation:{digest}",
                    "label": section_id or (f"Page {page}" if page is not None else f"Evidence {len(citations) + 1}"),
                    "paperId": paper_id,
                    "sourceRevision": source_revision,
                    "valid": valid,
                }
                if section_id is not None:
                    citation["sectionId"] = section_id
                if page is not None:
                    citation["page"] = page
                excerpt = _citation_text(value.get("quote") or value.get("text_excerpt") or value.get("textExcerpt"), maximum=10_000)
                if excerpt is not None:
                    citation["excerpt"] = excerpt
                if valid and source_ref.startswith(("https://", "http://")) and not _unsafe_locator(source_ref):
                    citation["url"] = source_ref
                if not artifact_valid:
                    citation["reasonCode"] = "artifact_not_verified"
                elif source_ref is None or source_ref not in sections_by_source:
                    citation["reasonCode"] = "source_locator_not_verified"
                elif trusted_section is None:
                    citation["reasonCode"] = "span_locator_not_verified"
                citations.append(citation)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(analysis)
    return citations[:1000]


def _locator_from_spans(values: list[str]) -> tuple[str | None, int | None]:
    for value in values:
        section_match = re.search(r"(?:section|section_id)=([^&#]+)", value)
        page_match = re.search(r"(?:^|[?&#])page=(\d+)", value)
        if section_match or page_match:
            return (
                section_match.group(1)[:200] if section_match else None,
                int(page_match.group(1)) if page_match else None,
            )
        if value and "://" not in value and len(value) <= 200:
            return value, None
    return None, None


def _match_document_section(
    sections_by_source: Mapping[str, Any],
    *,
    source_ref: str | None,
    section_id: str | None,
    page: int | None,
) -> Mapping[str, Any] | None:
    if source_ref is None:
        return None
    candidates = sections_by_source.get(source_ref)
    if not isinstance(candidates, list):
        return None
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        if section_id is not None and item.get("sectionId") != section_id:
            continue
        start = item.get("pageStart")
        end = item.get("pageEnd") if item.get("pageEnd") is not None else start
        if page is not None and (
            not isinstance(start, int) or not isinstance(end, int) or not start <= page <= end
        ):
            continue
        if section_id is not None or page is not None:
            return item
    return None


def _document_page(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 10_000 else None

def _report_markdown(report: Mapping[str, Any]) -> str:
    analysis = report.get("analysis") if isinstance(report.get("analysis"), Mapping) else {}
    quality = report.get("quality") if isinstance(report.get("quality"), Mapping) else {}
    diagnostics = report.get("diagnostics") if isinstance(report.get("diagnostics"), Mapping) else {}
    title = str(diagnostics.get("title") or _analysis_title(analysis) or f"Research report: {report.get('paperId') or ''}")
    summary = _analysis_summary(analysis) or "No accepted summary was provided."
    lines = [
        f"# {_markdown_text(title)}",
        "",
        f"- Run: `{_markdown_code(str(report.get('runId') or ''))}`",
        f"- Paper: `{_markdown_code(str(report.get('paperId') or ''))}`",
        f"- Source revision: `{_markdown_code(str(report.get('sourceRevision') or ''))}`",
        f"- Disposition: `{_markdown_code(str(report.get('disposition') or 'unknown'))}`",
        f"- Quality passed: `{str(quality.get('passed') is True).lower()}`",
        "",
        "## Summary",
        "",
        _markdown_text(summary),
    ]
    sections = (
        ("Contributions", analysis.get("contributions")),
        ("Methods", analysis.get("methods")),
        ("Experiments", analysis.get("experiments")),
        ("Limitations", analysis.get("limitations")),
        ("Reproducibility", analysis.get("reproducibility")),
        ("Related work", analysis.get("related_work") or analysis.get("relatedWork")),
    )
    for heading, values in sections:
        normalized = _markdown_items(values)
        if normalized:
            lines.extend(["", f"## {heading}", "", *[f"- {_markdown_text(item)}" for item in normalized]])
    lines.extend(["", "## Quality", ""])
    reasons = _quality_reasons(quality)
    if reasons:
        lines.extend(f"- {_markdown_text(item)}" for item in reasons)
    else:
        lines.append("- The persisted quality result passed without reported gate failures." if quality.get("passed") is True else "- No verified quality result was available.")
    lines.extend(["", "## Citations", ""])
    citations = report.get("citations")
    valid_citations = [item for item in citations if isinstance(item, Mapping) and item.get("valid") is True] if isinstance(citations, list) else []
    if valid_citations:
        for item in valid_citations:
            locator = item.get("sectionId") or (f"page {item.get('page')}" if item.get("page") is not None else "verified span")
            lines.append(f"- {_markdown_text(str(item.get('label') or item.get('id') or 'Evidence'))} ({_markdown_text(str(locator))})")
    else:
        lines.append("- No citation with a verified source and span locator was available.")
    return "\n".join(lines) + "\n"


def _analysis_title(analysis: Mapping[str, Any]) -> str | None:
    for key in ("title", "paper_title", "paperTitle"):
        value = analysis.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return None


def _markdown_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:100]:
        if isinstance(item, Mapping):
            text = item.get("text") or item.get("claim") or item.get("summary") or item.get("description")
        else:
            text = item
        if isinstance(text, str) and text.strip():
            result.append(text.strip()[:10_000])
    return result

def _analysis_summary(analysis: Mapping[str, Any] | None) -> str:
    if analysis is None:
        return ""
    summary = analysis.get("summary")
    if isinstance(summary, Mapping):
        for key in ("core_idea", "problem", "why_it_matters"):
            value = _citation_text(summary.get(key), maximum=100_000)
            if value:
                return value
    return ""


def _analysis_content(analysis: Mapping[str, Any] | None) -> str:
    if analysis is None:
        return ""
    lines: list[str] = []
    for heading, key in (
        ("Contributions", "contributions"),
        ("Methods", "methods"),
        ("Experiments", "experiments"),
        ("Limitations", "limitations"),
        ("Reproducibility", "reproducibility"),
        ("Related work", "related_work"),
    ):
        values = analysis.get(key)
        if isinstance(values, list) and values:
            lines.append(f"## {heading}")
            lines.extend(f"- {_markdown_text(str(item))}" for item in values[:100])
    return "\n\n".join(lines)[:500_000]


def _analysis_applicability(analysis: Mapping[str, Any] | None) -> list[str]:
    if analysis is None:
        return []
    values: list[str] = []
    summary = analysis.get("summary")
    if isinstance(summary, Mapping):
        limitations = summary.get("limitations")
        if isinstance(limitations, list):
            values.extend(str(item) for item in limitations)
    limitations = analysis.get("limitations")
    if isinstance(limitations, list):
        values.extend(str(item) for item in limitations)
    return [item[:2000] for item in dict.fromkeys(item.strip() for item in values if item.strip())][:100]


def _quality_status(quality: Mapping[str, Any] | None, *, run_status: str) -> str:
    if run_status in {"failed", "halted", "cancelled", "blocked"}:
        return "failed"
    if quality is None:
        return "unknown"
    passed = quality.get("passed")
    if passed is True:
        return "passed"
    if passed is False:
        return "rejected"
    return "unknown"


def _quality_score(quality: Mapping[str, Any] | None) -> float | None:
    if quality is None:
        return None
    value = quality.get("score")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1:
        return float(value)
    return None


def _quality_reasons(quality: Mapping[str, Any] | None) -> list[str]:
    if quality is None:
        return []
    reasons: list[str] = []
    gates = quality.get("gate_results") or quality.get("gateResults")
    if isinstance(gates, list):
        for gate in gates:
            if isinstance(gate, Mapping) and gate.get("passed") is False:
                code = gate.get("gate_id") or gate.get("gateId") or gate.get("name") or "quality_gate_failed"
                reasons.append(str(code)[:1000])
    flags = quality.get("quality_flags") or quality.get("qualityFlags")
    if isinstance(flags, list):
        for flag in flags:
            if isinstance(flag, Mapping):
                code = flag.get("code") or flag.get("flag")
            else:
                code = flag
            if code:
                reasons.append(str(code)[:1000])
    return list(dict.fromkeys(reasons))[:100]


def _report_title(run: Mapping[str, Any], paper_id: str) -> str:
    paper = run.get("paper")
    if isinstance(paper, Mapping):
        title = paper.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:500]
    return f"Research report: {paper_id}"[:500]


def _operation_message(status: str, error_code: Any) -> str | None:
    if status == "needs_action":
        if error_code == "personal_memory_disclosure_mismatch":
            return "The personal-memory receipt did not match the frozen request selection."
        return "The previous execution ended without a terminal receipt; review the existing run before creating a new request."
    if status == "rejected":
        if error_code == "answer_citation_locator_unverified":
            return "The answer was rejected because its citation locator could not be verified."
        return "The Research quality gate rejected this candidate."
    if status == "failed":
        return f"Research operation failed ({str(error_code or 'operation_failed')[:200]})."
    return None


def _analysis_contract_complete(value: Mapping[str, Any]) -> bool:
    summary = value.get("summary")
    return (
        isinstance(value.get("paper_id"), str)
        and bool(str(value.get("paper_id") or "").strip())
        and isinstance(summary, Mapping)
        and all(key in summary for key in (
            "problem", "core_idea", "key_contributions", "method_summary",
            "experiment_summary", "limitations", "why_it_matters", "read_next",
            "evidence_refs", "confidence", "quality_flags",
        ))
        and all(key in value for key in (
            "contributions", "methods", "experiments", "limitations",
            "reproducibility", "related_work", "claims", "evidence_pack_id", "quality",
        ))
    )


def _quality_contract_complete(value: Mapping[str, Any]) -> bool:
    return all(key in value for key in (
        "result_id", "target_id", "target_type", "passed", "score",
        "gate_results", "quality_flags",
    ))


def _public_quality_flag_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "flag_type": str(value.get("flag_type") or "")[:200],
        "severity": str(value.get("severity") or "medium"),
        "message": str(value.get("message") or "")[:2000],
        "evidenceIds": _public_evidence_ids(
            list(value.get("evidence_refs") or value.get("evidenceIds") or ())
        ),
    }


def _public_analysis_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    summary = value.get("summary") if isinstance(value.get("summary"), Mapping) else {}
    quality = value.get("quality") if isinstance(value.get("quality"), Mapping) else {}
    claims = value.get("claims") if isinstance(value.get("claims"), list) else []
    return {
        "paper_id": str(value.get("paper_id") or "")[:256],
        "summary": {
            "problem": str(summary.get("problem") or "")[:100_000],
            "core_idea": str(summary.get("core_idea") or "")[:100_000],
            "key_contributions": _public_text_list(summary.get("key_contributions"), maximum=10_000, limit=500),
            "method_summary": str(summary.get("method_summary") or "")[:100_000],
            "experiment_summary": str(summary.get("experiment_summary") or "")[:100_000],
            "limitations": _public_text_list(summary.get("limitations"), maximum=10_000, limit=500),
            "why_it_matters": str(summary.get("why_it_matters") or "")[:100_000],
            "read_next": _public_text_list(summary.get("read_next"), maximum=10_000, limit=500),
            "evidenceIds": _public_evidence_ids(list(summary.get("evidence_refs") or ())),
            "confidence": _public_score(summary.get("confidence")),
            "quality_flags": [
                _public_quality_flag_projection(item)
                for item in list(summary.get("quality_flags") or ())[:500]
                if isinstance(item, Mapping)
            ],
        },
        "contributions": _public_text_list(value.get("contributions"), maximum=10_000, limit=500),
        "methods": _public_text_list(value.get("methods"), maximum=10_000, limit=500),
        "experiments": _public_text_list(value.get("experiments"), maximum=10_000, limit=500),
        "limitations": _public_text_list(value.get("limitations"), maximum=10_000, limit=500),
        "reproducibility": _public_text_list(value.get("reproducibility"), maximum=10_000, limit=500),
        "related_work": _public_text_list(value.get("related_work"), maximum=10_000, limit=500),
        "claims": [
            {
                "claim_id": str(item.get("claim_id") or "")[:256],
                "text": str(item.get("text") or "")[:10_000],
                "claim_type": str(item.get("claim_type") or "other"),
                "section_id": (
                    str(item.get("section_id"))[:200]
                    if item.get("section_id") is not None
                    else None
                ),
                "evidence_ids": _public_evidence_ids(list(item.get("evidence_ids") or ())),
                "confidence": _public_score(item.get("confidence")),
            }
            for item in claims[:1000]
            if isinstance(item, Mapping)
        ],
        "evidence_pack_id": str(value.get("evidence_pack_id") or "")[:256],
        "quality": {
            **(
                {"evidence_item_count": int(quality["evidence_item_count"])}
                if isinstance(quality.get("evidence_item_count"), int)
                and not isinstance(quality.get("evidence_item_count"), bool)
                and int(quality["evidence_item_count"]) >= 0
                else {}
            ),
            **(
                {"llm_candidate_warnings": _public_text_list(
                    quality.get("llm_candidate_warnings"), maximum=2000, limit=500
                )}
                if isinstance(quality.get("llm_candidate_warnings"), list)
                else {}
            ),
        },
    }


def _public_quality_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    gates = value.get("gate_results") if isinstance(value.get("gate_results"), list) else []
    flags = value.get("quality_flags") if isinstance(value.get("quality_flags"), list) else []
    return {
        "result_id": str(value.get("result_id") or "")[:256],
        "target_id": str(value.get("target_id") or "")[:256],
        "target_type": str(value.get("target_type") or "")[:100],
        "passed": value.get("passed") is True,
        "score": _public_score(value.get("score")),
        "gate_results": [
            {
                "gate_name": str(item.get("gate_name") or "")[:200],
                "passed": item.get("passed") is True,
                "reasons": _public_text_list(item.get("reasons"), maximum=2000, limit=500),
                "quality_flags": [
                    _public_quality_flag_projection(flag)
                    for flag in list(item.get("quality_flags") or ())[:500]
                    if isinstance(flag, Mapping)
                ],
            }
            for item in gates[:500]
            if isinstance(item, Mapping)
        ],
        "quality_flags": [
            _public_quality_flag_projection(item)
            for item in flags[:500]
            if isinstance(item, Mapping)
        ],
    }


def _public_text_list(value: Any, *, maximum: int, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:maximum] for item in value[:limit] if isinstance(item, str)]


def _public_score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _public_mapping(value: Mapping[str, Any], allowed: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _controlled_public_value(value[key])
        for key in allowed
        if key in value
    }


_PRIVATE_CONTRACT_KEYS = frozenset({
    "artifact_ref", "artifact_refs", "artifactRef", "artifactRefs",
    "source_ref", "source_refs", "sourceRef", "sourceRefs",
    "trace_ref", "traceRef", "path", "file_path", "filePath",
    "metadata", "lineage", "diagnostics", "transcript_artifact",
})


def _controlled_public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in _PRIVATE_CONTRACT_KEYS:
                continue
            if name in {"evidence_refs", "evidenceRefs"} and isinstance(item, list):
                result["evidenceIds"] = _public_evidence_ids(item)
                continue
            result[name] = _controlled_public_value(item)
        return result
    if isinstance(value, list):
        return [_controlled_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_controlled_public_value(item) for item in value]
    return value



def _public_evidence_ids(values: list[Any]) -> list[str]:
    result: list[str] = []
    for entry in values[:1000]:
        if isinstance(entry, Mapping):
            value = entry.get("evidence_id") or entry.get("evidenceId")
        else:
            value = entry
        if isinstance(value, str) and value.strip():
            opaque = value.strip()[:500]
        else:
            opaque = f"evidence:{hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:24]}"
        if opaque not in result:
            result.append(opaque)
    return result

def _unsafe_locator(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized.startswith(("file:", "data:", "javascript:", "vbscript:"))
        or "\\" in value
        or re.match(r"^[a-zA-Z]:[\\/]", value) is not None
    )


def _citation_text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:maximum]


def _citation_page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if 1 <= page <= 10_000 else None


def _model_credential_gap() -> LLMCredentialUnavailableError | None:
    try:
        require_invocation_credential()
    except LLMCredentialUnavailableError as exc:
        return exc
    return None


def _require_model_credential() -> None:
    """Refuse model-backed work before admission when no credential is bound.

    Checked before ``admit`` so a missing key never leaves an admitted
    request behind that can only fail.
    """

    try:
        require_invocation_credential()
    except LLMCredentialUnavailableError as exc:
        raise _service_error(model_credential_service_error(exc)) from exc


def _service_error(exc: Exception) -> ResearchDesktopServiceError:
    if isinstance(exc, ResearchDesktopServiceError):
        return exc
    credential_failure = credential_unavailable_cause(exc)
    if credential_failure is not None:
        return _service_error(model_credential_service_error(credential_failure))
    if isinstance(exc, ResearchDesktopControlError):
        return ResearchDesktopServiceError(
            exc.code,
            str(exc),
            status_code=exc.status_code,
            retryable=exc.retryable,
            user_action_required=exc.status_code < 500,
            details=exc.details,
        )
    if isinstance(exc, ResearchServiceError):
        return ResearchDesktopServiceError(
            exc.code,
            exc.public_message,
            status_code=exc.status_code,
            retryable=exc.retryable,
            user_action_required=exc.user_action_required,
            details=exc.details,
        )
    if isinstance(exc, ResearchImportError):
        return ResearchDesktopServiceError(
            exc.code,
            str(exc),
            status_code=exc.status_code,
            retryable=exc.retryable,
            user_action_required=exc.status_code < 500,
        )
    return ResearchDesktopServiceError(
        "research_desktop_operation_failed",
        "Research desktop operation failed",
        status_code=500,
        retryable=True,
        details={"errorType": type(exc).__name__},
    )


def _owner(value: ResearchDesktopOwner) -> ResearchDesktopOwner:
    if not isinstance(value, ResearchDesktopOwner):
        raise TypeError("owner must be ResearchDesktopOwner")
    return ResearchDesktopOwner(
        user_id=_safe_id(value.user_id, "userId"),
        tenant_id=_optional_text(value.tenant_id, "tenantId", maximum=256),
        memory_namespace=_optional_text(value.memory_namespace, "memoryNamespace", maximum=512),
    )


def _run_id(owner_id: str, generation: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{owner_id}\0{generation}\0{request_id}".encode("utf-8")).hexdigest()
    return f"research-desktop-{digest[:40]}"



def _import_id(owner_id: str, generation: str, request_id: str) -> str:
    digest = hashlib.sha256(
        f"{owner_id}\0{generation}\0{request_id}\0import".encode("utf-8")
    ).hexdigest()
    return f"imp_{digest}"

def _safe_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ResearchDesktopServiceError("invalid_request", f"{field} is invalid", user_action_required=True)
    return text


def _required_text(value: Any, field: str, *, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(ord(char) < 0x20 and char not in "\n\t" for char in text):
        raise ResearchDesktopServiceError("invalid_request", f"{field} is invalid", user_action_required=True)
    return text



def _normalized_upload_filename(value: Any) -> str:
    encoded = _optional_text(value, "filename", maximum=1500) or "paper.pdf"
    decoded = unquote(encoded)
    name = Path(decoded.replace("\\", "/")).name.strip() or "paper.pdf"
    if len(name) > 255 or any(ord(char) < 0x20 for char in name):
        raise ResearchDesktopServiceError("invalid_request", "filename is invalid", user_action_required=True)
    return name if name.casefold().endswith(".pdf") else f"{name}.pdf"


def _personal_memory_owner(owner: ResearchDesktopOwner) -> PersonalMemoryOwner:
    namespace = str(owner.memory_namespace or "").strip()
    if not namespace:
        namespace = f"research:user:{owner.user_id}"
    return PersonalMemoryOwner(
        user_id=owner.user_id,
        tenant_id=owner.tenant_id,
        namespace=namespace,
    )


def _personal_memory_request(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"refs", "policyRevision"}:
        raise ResearchDesktopServiceError(
            "invalid_request",
            "personalMemory must contain exactly refs and policyRevision",
            user_action_required=True,
        )
    policy_revision = value.get("policyRevision")
    refs = value.get("refs")
    if (
        isinstance(policy_revision, bool)
        or not isinstance(policy_revision, int)
        or policy_revision < 1
        or not isinstance(refs, list)
        or not 1 <= len(refs) <= 20
    ):
        raise ResearchDesktopServiceError(
            "invalid_request",
            "personalMemory policy revision or refs are invalid",
            user_action_required=True,
        )
    normalized: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for item in refs:
        if not isinstance(item, Mapping) or set(item) != {"memoryId", "revision"}:
            raise ResearchDesktopServiceError(
                "invalid_request",
                "personalMemory refs must contain memoryId and revision",
                user_action_required=True,
            )
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
            raise ResearchDesktopServiceError(
                "invalid_request",
                "personalMemory refs are invalid or duplicated",
                user_action_required=True,
            )
        seen.add(memory_id)
        normalized.append({"memoryId": memory_id, "revision": revision})
    return {"refs": normalized, "policyRevision": policy_revision}


def _used_personal_memory_receipt(
    value: Any,
) -> list[dict[str, str | int]] | None:
    try:
        normalized = _personal_memory_request(
            {"refs": value, "policyRevision": 1}
        )
    except ResearchDesktopServiceError:
        return None
    return list(normalized["refs"]) if normalized is not None else None


def _used_personal_memory_projection(value: Any) -> list[dict[str, str | int]]:
    return _used_personal_memory_receipt(value) or []


def _personal_memory_disclosure_matches(
    request: Mapping[str, Any] | None,
    result: Mapping[str, Any],
) -> bool:
    raw = result.get("usedPersonalMemory", _MISSING)
    if request is None:
        return raw is _MISSING
    if raw is _MISSING:
        return False
    expected = request.get("refs")
    actual = _used_personal_memory_receipt(raw)
    return isinstance(expected, list) and actual == expected


def _result_identity_matches(
    result: Mapping[str, Any],
    *,
    run_id: str,
    paper_id: str,
    source_revision: str | None,
) -> bool:
    return (
        str(result.get("runId") or "") == run_id
        and str(result.get("paperId") or "") == paper_id
        and (
            source_revision is None
            or str(result.get("sourceRevision") or "") == source_revision
        )
    )


def _analysis_result_is_accepted(result: Mapping[str, Any]) -> bool:
    return (
        result.get("status") == "succeeded"
        and result.get("accepted") is True
        and result.get("qualityPassed") is True
        and result.get("eligibleForAcceptedExport") is True
        and result.get("artifactStatus") == "valid"
    )


def _ask_result_has_verified_citations(
    value: Mapping[str, Any],
    *,
    paper_id: str,
    source_revision: str,
) -> bool:
    if value.get("status") != "answered" or value.get("abstained") is not False:
        return False
    transcript_id = value.get("transcriptId")
    if (
        not isinstance(transcript_id, str) or not 1 <= len(transcript_id) <= 256
        or re.search(r"[\s\x00-\x1f\x7f]", transcript_id) is not None
    ):
        return False
    decision = value.get("decision")
    if not isinstance(decision, Mapping) or decision.get("decision_type") != "return_answer":
        return False
    citations = value.get("citations")
    if not isinstance(citations, list) or not citations or len(citations) > 1000:
        return False
    evidence_refs: list[str] = []
    for item in citations:
        if not isinstance(item, Mapping) or item.get("valid") is not True:
            return False
        if str(item.get("paperId") or "") != paper_id:
            return False
        if str(item.get("sourceRevision") or "") != source_revision:
            return False
        evidence_id = item.get("evidenceId")
        if (
            not isinstance(evidence_id, str) or not 1 <= len(evidence_id) <= 256
            or re.search(r"[\s\x00-\x1f\x7f]", evidence_id) is not None
        ):
            return False
        if evidence_id not in evidence_refs:
            evidence_refs.append(evidence_id)
        section_id = str(item.get("sectionId") or "").strip()
        page = item.get("page")
        valid_page = (
            not isinstance(page, bool)
            and isinstance(page, int)
            and 1 <= page <= 100_000
        )
        if not section_id and not valid_page:
            return False
    return value.get("evidenceRefs") == evidence_refs


def _ask_selection(value: Mapping[str, Any]) -> dict[str, Any]:
    selection = _json_mapping(value, "selection", maximum_bytes=32_000)
    allowed = {"sectionId", "sectionIndex", "page", "pdfPage", "selectedText"}
    unknown = sorted(set(selection) - allowed)
    if unknown:
        raise ResearchDesktopServiceError("invalid_request", "selection contains unsupported fields", user_action_required=True, details={"fields": unknown})
    for field in ("sectionIndex", "page", "pdfPage"):
        number = selection.get(field)
        if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 0 or number > 10000):
            raise ResearchDesktopServiceError("invalid_request", f"selection.{field} must be a bounded integer", user_action_required=True)
    if "sectionId" in selection:
        selection["sectionId"] = _required_text(selection["sectionId"], "selection.sectionId", maximum=200)
    if "selectedText" in selection:
        selection["selectedText"] = _required_text(selection["selectedText"], "selection.selectedText", maximum=10000)
    return selection

def _optional_text(value: Any, field: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, field, maximum=maximum)


def _remote_source_type(value: Any) -> str | None:
    """Accept only source types that resolve over the network.

    Local paths are reached through the owner-scoped PDF import; letting a
    caller name ``local`` here would make the server read its own files.
    """

    text = _optional_text(value, "sourceType", maximum=64)
    if text is None:
        return None
    if text not in _REMOTE_SOURCE_TYPES:
        raise ResearchDesktopServiceError(
            "source_type_unsupported",
            "sourceType must name a remote paper source",
            status_code=422,
            user_action_required=True,
        )
    return text


def _optional_url(value: Any, field: str) -> str | None:
    text = _optional_text(value, field, maximum=4000)
    if text is None:
        return None
    if not (text.startswith("https://") or text.startswith("http://")):
        raise ResearchDesktopServiceError("invalid_request", f"{field} uses an unsupported scheme", user_action_required=True)
    return text


def _sha256_fingerprint(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", text):
        raise ResearchDesktopServiceError("invalid_request", f"{field} must be sha256:<hex>", user_action_required=True)
    return text


def _text_list(values: Sequence[str], field: str, *, maximum_items: int, maximum_length: int) -> list[str]:
    if isinstance(values, (str, bytes)) or len(values) > maximum_items:
        raise ResearchDesktopServiceError("invalid_request", f"{field} is invalid", user_action_required=True)
    return [_required_text(item, field, maximum=maximum_length) for item in values]


def _json_mapping(value: Mapping[str, Any], field: str, *, maximum_bytes: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchDesktopServiceError("invalid_request", f"{field} must be an object", user_action_required=True)
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ResearchDesktopServiceError("invalid_request", f"{field} must be JSON serializable", user_action_required=True) from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ResearchDesktopServiceError("invalid_request", f"{field} is too large", user_action_required=True)
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise ResearchDesktopServiceError("invalid_request", f"{field} must be an object", user_action_required=True)
    return decoded


def _export_stem(paper_id: str, suffix: str) -> str:
    base = _SAFE_FILENAME.sub("-", paper_id).strip(".-_")[:80] or "research-report"
    tail = _SAFE_FILENAME.sub("-", suffix).strip(".-_")[:48]
    return f"{base}-{tail}" if tail else base


def _markdown_text(value: str) -> str:
    return value.replace("\x00", "").replace("<", "&lt;").replace(">", "&gt;")


def _markdown_code(value: str) -> str:
    return value.replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "ResearchDesktopApplicationService",
    "ResearchDesktopOwner",
    "ResearchDesktopServiceError",
]
