from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, runtime_checkable


ResearchDesktopOperationKind = Literal["prepare", "analyze", "ask", "import"]
ResearchDesktopOperationStatus = Literal[
    "admitted",
    "running",
    "completed",
    "failed",
    "needs_review",
]


@dataclass(frozen=True, slots=True)
class ResearchDesktopAdmission:
    owner_id: str
    request_id: str
    generation: str
    kind: ResearchDesktopOperationKind
    fingerprint: str
    semantic: Mapping[str, Any]
    status: ResearchDesktopOperationStatus
    created_at: str
    updated_at: str
    run_id: str | None = None
    import_id: str | None = None
    result: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ownerId": self.owner_id,
            "requestId": self.request_id,
            "generation": self.generation,
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "semantic": dict(self.semantic),
            "status": self.status,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.run_id is not None:
            payload["runId"] = self.run_id
        if self.import_id is not None:
            payload["importId"] = self.import_id
        if self.result is not None:
            payload["result"] = dict(self.result)
        if self.error is not None:
            payload["error"] = dict(self.error)
        return payload


@dataclass(frozen=True, slots=True)
class ResearchDesktopAdmissionDecision:
    admission: ResearchDesktopAdmission
    created: bool
    execute: bool


@dataclass(frozen=True, slots=True)
class ResearchDesktopSavedReportRef:
    owner_id: str
    report_id: str
    run_id: str
    paper_id: str
    source_revision: str
    title: str
    saved_at: str
    updated_at: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reportId": self.report_id,
            "runId": self.run_id,
            "paperId": self.paper_id,
            "sourceRevision": self.source_revision,
            "title": self.title,
            "savedAt": self.saved_at,
            "updatedAt": self.updated_at,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class ResearchDesktopRequestStore(Protocol):
    def generation(self) -> str: ...

    def admit(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        kind: ResearchDesktopOperationKind,
        fingerprint: str,
        semantic: Mapping[str, Any],
    ) -> ResearchDesktopAdmissionDecision: ...

    def get(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
    ) -> ResearchDesktopAdmission | None: ...

    def bind_authority(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        run_id: str | None = None,
        import_id: str | None = None,
    ) -> ResearchDesktopAdmission: ...

    def complete(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        run_id: str | None = None,
        import_id: str | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> ResearchDesktopAdmission: ...

    def fail(
        self,
        *,
        owner_id: str,
        request_id: str,
        generation: str,
        error: Mapping[str, Any],
        needs_review: bool = False,
    ) -> ResearchDesktopAdmission: ...

    def record_capability_verification(
        self,
        *,
        capability_id: str,
        operation: str,
        runtime_identity: str,
    ) -> Mapping[str, Any]:
        """Record that a business operation proved a capability.

        The record is shared by every account, so it carries only the
        operation kind and never an owner's run, import or transcript id.
        """
        ...

    def capability_verifications(
        self, *, runtime_identity: str
    ) -> Mapping[str, Mapping[str, Any]]: ...

    def save_report_ref(
        self,
        *,
        owner_id: str,
        report_id: str,
        run_id: str,
        paper_id: str,
        source_revision: str,
        title: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> ResearchDesktopSavedReportRef: ...

    def get_report_ref(
        self,
        *,
        owner_id: str,
        report_id: str,
    ) -> ResearchDesktopSavedReportRef | None: ...

    def list_report_refs(self, *, owner_id: str) -> tuple[ResearchDesktopSavedReportRef, ...]: ...


__all__ = [
    "ResearchDesktopAdmission",
    "ResearchDesktopAdmissionDecision",
    "ResearchDesktopOperationKind",
    "ResearchDesktopOperationStatus",
    "ResearchDesktopRequestStore",
    "ResearchDesktopSavedReportRef",
]
