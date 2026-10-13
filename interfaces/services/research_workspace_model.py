"""Validated user-authored workspace content; no document access is granted by references."""
from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, unquote_to_bytes, urlencode, urlsplit, urlunsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


def _clean_text(value: str) -> str:
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise ValueError("text contains unsupported control characters")
    return value


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text cannot be blank")
    return value


def _source_url(value: str) -> str:
    if not value:
        return value
    if any(c in value for c in "\r\n\\"):
        raise ValueError("source URL is unsafe")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must be an absolute HTTP(S) URL without credentials")
    return value


_READER_QUERY_KEYS = frozenset({"sourceRevision", "resumeSection", "resumePage", "returnTo"})
_READER_PAPER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


def _reader_target(value: str) -> tuple[str, dict[str, str]]:
    if not value.startswith("/") or value.startswith("//") or any(c in value for c in "\r\n\\"):
        raise ValueError("reader target is unsafe")
    parsed = urlsplit(value)
    match = re.fullmatch(
        r"/(?:design-demo/)?papers/(?P<paper>(?:[A-Za-z0-9._~!()*':-]|%[0-9A-Fa-f]{2})+)/read",
        parsed.path,
    )
    if parsed.scheme or parsed.netloc or parsed.fragment or match is None:
        raise ValueError("reader target must be a same-origin paper reader")
    try:
        paper_id = unquote_to_bytes(match.group("paper")).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("reader target paper identity is invalid") from exc
    if _READER_PAPER_ID_PATTERN.fullmatch(paper_id) is None or any(
        part in {"", ".", ".."} for part in paper_id.split("/")
    ):
        raise ValueError("reader target paper identity is unsafe")

    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in _READER_QUERY_KEYS for key, _item in pairs):
        raise ValueError("reader target contains an unsupported query")
    if len({key for key, _item in pairs}) != len(pairs):
        raise ValueError("reader target query keys must be unique")
    query = dict(pairs)
    for key, maximum in (("sourceRevision", 512), ("resumeSection", 200)):
        item = query.get(key)
        if item is not None:
            _clean_text(item)
            if not item.strip() or len(item) > maximum:
                raise ValueError(f"reader target {key} is invalid")
    resume_page = query.get("resumePage")
    if resume_page is not None and (
        re.fullmatch(r"[1-9]\d{0,4}", resume_page) is None or int(resume_page) > 10000
    ):
        raise ValueError("reader target resumePage is invalid")
    return_to = query.get("returnTo")
    if return_to is not None:
        _clean_text(return_to)
        if (
            not return_to
            or len(return_to) > 10000
            or not return_to.startswith("/")
            or return_to.startswith("//")
            or any(character in return_to for character in "\r\n\\")
        ):
            raise ValueError("reader target returnTo is unsafe")
        return_target = urlsplit(return_to)
        if return_target.scheme or return_target.netloc or return_target.username or return_target.password:
            raise ValueError("reader target returnTo must be same-origin")
    return paper_id, query


def _reader_href(value: str) -> str:
    _reader_target(value)
    return value


WorkspaceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")]
WorkspaceTime = Annotated[int, Field(gt=0, le=2**53 - 1)]
Title = Annotated[str, Field(max_length=500), AfterValidator(_clean_text), AfterValidator(_nonblank)]
Question = Annotated[str, Field(max_length=2000), AfterValidator(_clean_text)]
Notes = Annotated[str, Field(max_length=10000), AfterValidator(_clean_text)]
NotebookNotes = Annotated[str, Field(max_length=20000), AfterValidator(_clean_text)]
SourceUrl = Annotated[str, Field(max_length=2000), AfterValidator(_source_url)]
ReaderHref = Annotated[str, Field(max_length=10000), AfterValidator(_reader_href)]
ResearchMode = Literal["auto", "plan", "papers", "projects", "community", "reports"]


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(item is None and key not in {"groupId", "composerDraft", "sourceUrl", "pdfUrl", "locale", "sourceType", "objective"} for key, item in value.items()):
            raise ValueError("optional fields must be omitted rather than null")
        return value


class ResearchConstraints(WorkspaceModel):
    recentYear: bool = False
    hasCode: bool = False
    paperType: Literal["survey"] | None = None
    language: Literal["python", "typescript", "javascript", "rust", "go"] | None = None
    license: Literal["MIT", "Apache-2.0", "BSD-3-Clause"] | None = None
    recentlyActive: bool = False
    localRunnable: bool = False


class ResearchMaterial(WorkspaceModel):
    id: WorkspaceId
    groupId: WorkspaceId | None
    kind: Literal["paper", "project", "discussion", "note", "pdf"]
    title: Title
    url: SourceUrl
    referenceId: Annotated[str, Field(max_length=200), AfterValidator(_nonblank)] | None = None
    readerHref: ReaderHref | None = None
    notes: Notes
    createdAt: WorkspaceTime
    updatedAt: WorkspaceTime

    @model_validator(mode="after")
    def validate_source_and_time(self) -> ResearchMaterial:
        if self.updatedAt < self.createdAt:
            raise ValueError("updatedAt cannot precede createdAt")
        if self.kind != "note" and not self.url and not self.readerHref and not (self.kind == "pdf" and self.referenceId):
            raise ValueError("a material must retain its source")
        return self


class ResearchReportMaterial(WorkspaceModel):
    id: WorkspaceId
    title: Annotated[str, Field(max_length=500), AfterValidator(_clean_text)]
    url: SourceUrl
    notes: Notes


class ResearchReportDraft(WorkspaceModel):
    id: WorkspaceId
    groupId: WorkspaceId | None = None
    question: Question
    title: Annotated[str, Field(max_length=2000), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    scope: Notes
    notes: Annotated[str, Field(max_length=30000), AfterValidator(_clean_text)]
    materials: list[ResearchReportMaterial] = Field(max_length=100)
    updatedAt: WorkspaceTime

    @model_validator(mode="after")
    def unique_materials(self) -> ResearchReportDraft:
        if len({m.id for m in self.materials}) != len(self.materials):
            raise ValueError("draft material ids must be unique")
        return self


class ResearchPrompt(WorkspaceModel):
    id: WorkspaceId
    name: Annotated[str, Field(max_length=80), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    question: Annotated[Question, AfterValidator(_nonblank)]
    mode: ResearchMode
    constraints: ResearchConstraints
    updatedAt: WorkspaceTime


class ResearchComposerDraft(WorkspaceModel):
    question: Question
    mode: ResearchMode
    constraints: ResearchConstraints
    groupId: WorkspaceId | None
    updatedAt: WorkspaceTime
    materialIds: list[WorkspaceId] = Field(default_factory=list, max_length=50)
    submittedSessionId: WorkspaceId | None = None

    @model_validator(mode="after")
    def unique_materials(self) -> ResearchComposerDraft:
        if len(set(self.materialIds)) != len(self.materialIds):
            raise ValueError("composer material ids must be unique")
        return self


class ResearchReaderNotebook(WorkspaceModel):
    paperId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    note: NotebookNotes
    question: Question
    fontSize: Literal[16, 18, 20, 22]
    sectionId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text)]
    pdfPage: Annotated[int, Field(gt=0, le=10000)]
    updatedAt: WorkspaceTime


class ResearchReaderSelection(WorkspaceModel):
    id: WorkspaceId
    paperId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sectionId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    paragraphId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    blockId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text)] | None = None
    pageNumber: Annotated[int, Field(gt=0, le=10000)] | None = None
    selectedText: Annotated[str, Field(max_length=10000), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    surroundingText: Annotated[str, Field(max_length=20000), AfterValidator(_clean_text)]
    startOffset: Annotated[int, Field(ge=0, le=10_000_000)]
    endOffset: Annotated[int, Field(gt=0, le=10_000_000)]
    noteText: Notes = ""
    explainQuestion: Question = ""
    explainAnswer: Annotated[str, Field(max_length=30000), AfterValidator(_clean_text)] | None = None
    exampleQuestion: Question = ""
    exampleAnswer: Annotated[str, Field(max_length=30000), AfterValidator(_clean_text)] | None = None
    explained: bool = False
    exampled: bool = False
    confused: bool = False
    createdAt: WorkspaceTime
    updatedAt: WorkspaceTime

    @model_validator(mode="after")
    def validate_offsets_and_time(self) -> "ResearchReaderSelection":
        if self.endOffset <= self.startOffset or self.endOffset - self.startOffset > len(self.selectedText) + 1000:
            raise ValueError("reader selection offsets are invalid")
        if self.updatedAt < self.createdAt:
            raise ValueError("updatedAt cannot precede createdAt")
        return self


class ResearchReaderEvent(WorkspaceModel):
    id: WorkspaceId
    type: Literal[
        "selection_created", "selection_discarded", "note_updated",
        "explanation_generated", "example_generated", "confusion_marked",
        "confusion_unmarked", "reader_settings_changed", "drawer_resized",
        "toc_navigated",
    ]
    paperId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    selectionId: WorkspaceId | None = None
    sectionId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text)] | None = None
    paragraphId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text)] | None = None
    pageNumber: Annotated[int, Field(gt=0, le=10000)] | None = None
    selectedText: Annotated[str, Field(max_length=10000), AfterValidator(_clean_text)] | None = None
    surroundingText: Annotated[str, Field(max_length=20000), AfterValidator(_clean_text)] | None = None
    createdAt: WorkspaceTime


class ResearchPrepareSemantic(WorkspaceModel):
    kind: Literal["prepare"]
    paperId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)]
    sourceUrl: SourceUrl
    sourceType: Annotated[str, Field(max_length=64), AfterValidator(_nonblank)] | None = None


class ResearchAnalyzeSemantic(WorkspaceModel):
    kind: Literal["analyze"]
    paperId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_nonblank)]
    sourceFingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    sourceUrl: SourceUrl | None = None
    pdfUrl: SourceUrl | None = None
    objective: Annotated[str, Field(max_length=2000), AfterValidator(_clean_text)] | None = None
    constraints: list[Annotated[str, Field(max_length=500), AfterValidator(_clean_text), AfterValidator(_nonblank)]] = Field(default_factory=list, max_length=20)


class ResearchAskSemantic(WorkspaceModel):
    kind: Literal["ask"]
    runId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)]
    paperId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_nonblank)]
    sourceFingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    question: Annotated[Question, AfterValidator(_nonblank)]
    selection: dict[str, Any] = Field(default_factory=dict)
    locale: Annotated[str, Field(max_length=32), AfterValidator(_nonblank)] | None = None


class ResearchImportSemantic(WorkspaceModel):
    kind: Literal["import"]
    filename: Annotated[str, Field(max_length=255), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sizeBytes: Annotated[int, Field(gt=0, le=50 * 1024 * 1024)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_nonblank)]
    sourceFingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


ResearchOperationSemantic = Annotated[
    ResearchPrepareSemantic | ResearchAnalyzeSemantic | ResearchAskSemantic | ResearchImportSemantic,
    Field(discriminator="kind"),
]


class ResearchPendingOperation(WorkspaceModel):
    requestId: WorkspaceId
    generation: Annotated[str, Field(pattern=r"^gen_[0-9a-f]{32}$")]
    kind: Literal["prepare", "import", "analyze", "ask"]
    fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    semantic: ResearchOperationSemantic
    status: Literal["prepared", "waiting", "running", "needs_action", "passed", "rejected", "failed", "interrupted", "unknown"]
    paperId: Annotated[str, Field(max_length=200), AfterValidator(_nonblank)] | None = None
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_nonblank)] | None = None
    importId: Annotated[str, Field(max_length=200), AfterValidator(_nonblank)] | None = None
    runId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)] | None = None
    question: Annotated[str, Field(max_length=4000), AfterValidator(_clean_text), AfterValidator(_nonblank)] | None = None
    errorCode: Annotated[str, Field(max_length=200), AfterValidator(_nonblank)] | None = None
    createdAt: WorkspaceTime
    updatedAt: WorkspaceTime

    @model_validator(mode="after")
    def validate_semantic_identity(self) -> "ResearchPendingOperation":
        if self.updatedAt < self.createdAt:
            raise ValueError("operation updatedAt cannot precede createdAt")
        if self.semantic.kind != self.kind:
            raise ValueError("operation semantic kind must match operation kind")
        semantic_paper = getattr(self.semantic, "paperId", None)
        semantic_revision = getattr(self.semantic, "sourceRevision", None)
        semantic_question = getattr(self.semantic, "question", None)
        if self.paperId is not None and semantic_paper != self.paperId:
            raise ValueError("operation paperId must match fixed semantic")
        if self.sourceRevision is not None and semantic_revision != self.sourceRevision:
            raise ValueError("operation sourceRevision must match fixed semantic")
        if self.question is not None and semantic_question != self.question:
            raise ValueError("operation question must match fixed semantic")
        return self


class ResearchSavedReport(WorkspaceModel):
    id: WorkspaceId
    reportId: WorkspaceId | None = None
    runId: Annotated[str, Field(max_length=256), AfterValidator(_nonblank)] | None = None
    draftId: WorkspaceId | None = None
    kind: Literal["accepted", "draft"]
    title: Title
    savedAt: WorkspaceTime

    @model_validator(mode="after")
    def validate_target(self) -> "ResearchSavedReport":
        if self.kind == "accepted" and (not self.reportId or not self.runId or self.draftId is not None):
            raise ValueError("accepted saved report requires reportId and runId")
        if self.kind == "draft" and (not self.draftId or self.reportId is not None or self.runId is not None):
            raise ValueError("draft saved report requires only draftId")
        return self


class ReaderActivity(WorkspaceModel):
    kind: Literal["reader"]
    paperId: Annotated[str, Field(max_length=256), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    sourceRevision: Annotated[str, Field(max_length=512), AfterValidator(_clean_text), AfterValidator(_nonblank)] | None = None
    title: Title
    href: ReaderHref
    sectionId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)] | None = None
    sectionTitle: Title | None = None
    pdfPage: Annotated[int, Field(gt=0, le=10000)] | None = None
    updatedAt: WorkspaceTime

    @model_validator(mode="before")
    @classmethod
    def degrade_unversioned_locator(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("sourceRevision") is not None:
            return value
        degraded = dict(value)
        for key in ("sectionId", "sectionTitle", "pdfPage"):
            degraded.pop(key, None)
        href = degraded.get("href")
        if isinstance(href, str):
            _reader_target(href)
            parsed = urlsplit(href)
            query = [
                (key, item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                if key not in {"sourceRevision", "resumeSection", "resumePage"}
            ]
            degraded["href"] = urlunsplit(("", "", parsed.path, urlencode(query), ""))
        return degraded

    @model_validator(mode="after")
    def validate_exact_reader_identity(self) -> "ReaderActivity":
        paper_id, query = _reader_target(self.href)
        if paper_id != self.paperId:
            raise ValueError("reader activity href must match its paper identity")
        if self.sourceRevision is None and any(
            item is not None for item in (self.sectionId, self.sectionTitle, self.pdfPage)
        ):
            raise ValueError("precise reader activity requires a source revision")
        href_revision = query.get("sourceRevision")
        if href_revision is not None and href_revision != self.sourceRevision:
            raise ValueError("reader activity href must match its source revision")
        href_section = query.get("resumeSection")
        if href_section is not None and self.sectionId is not None and href_section != self.sectionId:
            raise ValueError("reader activity href must match its resume section")
        href_page = query.get("resumePage")
        if href_page is not None and self.pdfPage is not None and int(href_page) != self.pdfPage:
            raise ValueError("reader activity href must match its resume page")
        if (href_section is not None or href_page is not None) and self.sourceRevision is None:
            raise ValueError("precise reader href requires a source revision")
        return self


class ReportActivity(WorkspaceModel):
    kind: Literal["report"]
    draftId: WorkspaceId
    title: Title
    updatedAt: WorkspaceTime


ResearchActivity = Annotated[ReaderActivity | ReportActivity, Field(discriminator="kind")]
activity_adapter = TypeAdapter(ResearchActivity)


class WorkspaceItems(WorkspaceModel):
    materials: list[ResearchMaterial] = Field(default_factory=list, max_length=1000)
    reportDrafts: list[ResearchReportDraft] = Field(default_factory=list, max_length=100)
    prompts: list[ResearchPrompt] = Field(default_factory=list, max_length=100)
    composerDraft: ResearchComposerDraft | None = None
    readerNotebooks: list[ResearchReaderNotebook] = Field(default_factory=list, max_length=1000)
    readerSelections: list[ResearchReaderSelection] = Field(default_factory=list, max_length=5000)
    readerEvents: list[ResearchReaderEvent] = Field(default_factory=list, max_length=10000)
    pendingOperations: list[ResearchPendingOperation] = Field(default_factory=list, max_length=500)
    savedReports: list[ResearchSavedReport] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_items(self) -> WorkspaceItems:
        for items in (self.materials, self.reportDrafts, self.prompts, self.savedReports):
            if len({item.id for item in items}) != len(items):
                raise ValueError("workspace item ids must be unique")
        if len({item.paperId for item in self.readerNotebooks}) != len(self.readerNotebooks):
            raise ValueError("reader notebook paper ids must be unique")
        if len({item.id for item in self.readerSelections}) != len(self.readerSelections):
            raise ValueError("reader selection ids must be unique")
        if len({item.id for item in self.readerEvents}) != len(self.readerEvents):
            raise ValueError("reader event ids must be unique")
        selection_bindings = {(item.paperId, item.sourceRevision, item.id) for item in self.readerSelections}
        if any(
            event.selectionId is not None
            and (event.paperId, event.sourceRevision, event.selectionId) not in selection_bindings
            for event in self.readerEvents
        ):
            raise ValueError("reader events must reference a selection in the same paper revision")
        if len({item.requestId for item in self.pendingOperations}) != len(self.pendingOperations):
            raise ValueError("pending operation request ids must be unique")
        return self


def validate_workspace_items(raw: Any, *, group_ids: set[str], visits: list[dict[str, Any]]) -> dict[str, Any]:
    items = WorkspaceItems.model_validate(raw)
    grouped = [*items.materials, *items.reportDrafts, *([items.composerDraft] if items.composerDraft else [])]
    if any(item.groupId is not None and item.groupId not in group_ids for item in grouped):
        raise ValueError("workspace item groupId must reference an existing group")
    drafts = {draft.id for draft in items.reportDrafts}
    materials = {material.id for material in items.materials}
    if items.composerDraft and any(material_id not in materials for material_id in items.composerDraft.materialIds):
        raise ValueError("composer materials must reference owned workspace materials")
    for visit in visits:
        activity = visit.get("activity")
        if activity and activity["kind"] == "report" and activity["draftId"] not in drafts:
            raise ValueError("report activity must reference an owned report draft")
    for saved in items.savedReports:
        if saved.kind == "draft" and saved.draftId not in drafts:
            raise ValueError("saved draft report must reference an owned report draft")
    # An omitted field from a prior snapshot stays omitted in its serialized form.
    return items.model_dump(exclude_unset=True)
