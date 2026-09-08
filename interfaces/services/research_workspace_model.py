"""Validated user-authored workspace content; no document access is granted by references."""
from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

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


def _reader_href(value: str) -> str:
    if not value.startswith("/") or value.startswith("//") or any(c in value for c in "\r\n\\"):
        raise ValueError("reader target is unsafe")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or not re.fullmatch(r"/(?:design-demo/)?papers/[A-Za-z0-9_-]+/read", parsed.path):
        raise ValueError("reader target must be a same-origin paper reader")
    return value


WorkspaceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")]
WorkspaceTime = Annotated[int, Field(gt=0, le=2**53 - 1)]
Title = Annotated[str, Field(max_length=500), AfterValidator(_clean_text), AfterValidator(_nonblank)]
Question = Annotated[str, Field(max_length=2000), AfterValidator(_clean_text)]
Notes = Annotated[str, Field(max_length=10000), AfterValidator(_clean_text)]
SourceUrl = Annotated[str, Field(max_length=2000), AfterValidator(_source_url)]
ReaderHref = Annotated[str, Field(max_length=10000), AfterValidator(_reader_href)]
ResearchMode = Literal["auto", "papers", "projects", "community", "reports"]


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_nulls(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(item is None and key not in {"groupId", "composerDraft"} for key, item in value.items()):
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
        if self.kind != "note" and not self.url and not self.readerHref:
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


class ReaderActivity(WorkspaceModel):
    kind: Literal["reader"]
    paperId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)]
    title: Title
    href: ReaderHref
    sectionId: Annotated[str, Field(max_length=200), AfterValidator(_clean_text), AfterValidator(_nonblank)] | None = None
    sectionTitle: Title | None = None
    pdfPage: Annotated[int, Field(gt=0, le=10000)] | None = None
    updatedAt: WorkspaceTime


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

    @model_validator(mode="after")
    def unique_items(self) -> WorkspaceItems:
        for items in (self.materials, self.reportDrafts, self.prompts):
            if len({item.id for item in items}) != len(items):
                raise ValueError("workspace item ids must be unique")
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
    # An omitted field from a prior snapshot stays omitted in its serialized form.
    return items.model_dump(exclude_unset=True)
