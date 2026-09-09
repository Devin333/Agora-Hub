"""Bounded untrusted conversation snapshots, not an authorization or search source."""
from __future__ import annotations

import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from interfaces.services.research_workspace_model import (
    ResearchConstraints, SourceUrl, WorkspaceId, WorkspaceTime, _clean_text, _nonblank,
)

Source = Literal["papers", "projects"]
Phase = Literal["understanding", "clarifying", "confirming", "searching", "results", "stopped", "error"]
Text = Annotated[str, AfterValidator(_clean_text)]
RequiredText = Annotated[Text, AfterValidator(_nonblank)]


class ConversationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_optional_nulls(cls, value):
        if isinstance(value, dict) and any(v is None and k != "clarification" for k, v in value.items()):
            raise ValueError("optional fields must be omitted rather than null")
        return value


class Clarification(ConversationModel):
    question: RequiredText = Field(max_length=300)
    options: list[Annotated[RequiredText, Field(max_length=120)]] = Field(min_length=2, max_length=3)


class ConversationIntent(ConversationModel):
    summary: RequiredText = Field(max_length=500)
    query: RequiredText = Field(max_length=2000)
    sources: list[Source] = Field(min_length=1, max_length=2)
    constraints: ResearchConstraints
    clarification: Clarification | None
    confirmationRequired: bool = False
    changeNotice: Text | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be unique")
        return self


def _result_href(value: str) -> str:
    url = urlsplit(value)
    if not value.startswith("/") or value.startswith("//") or re.search(r"[\\\r\n]", value) or not re.fullmatch(r"/(?:design-demo/)?(?:papers|projects)(?:/[A-Za-z0-9_-]+(?:/read)?)?", url.path):
        raise ValueError("result path must be a same-origin research page")
    return value


ResultHref = Annotated[str, Field(max_length=10000), AfterValidator(_result_href)]


class ConversationResult(ConversationModel):
    id: RequiredText = Field(max_length=200)
    kind: Source
    title: RequiredText = Field(max_length=500)
    description: Text = Field(max_length=2000)
    source: Text = Field(max_length=200)
    url: Annotated[SourceUrl, AfterValidator(_nonblank)]
    href: ResultHref | None = None
    authors: Text | None = Field(default=None, max_length=1000)
    publishedAt: Text | None = Field(default=None, max_length=100)
    language: Text | None = Field(default=None, max_length=100)
    stars: int | None = Field(default=None, ge=0)


class ConversationSearch(ConversationModel):
    source: Source
    results: list[ConversationResult] = Field(max_length=10)
    total: int = Field(ge=0)
    moreHref: ResultHref | None = None

    @model_validator(mode="after")
    def matching_source(self):
        if any(item.kind != self.source for item in self.results):
            raise ValueError("result source mismatch")
        return self


class ConversationFailure(ConversationModel):
    source: Source
    message: RequiredText = Field(max_length=500)


class ConversationEvent(ConversationModel):
    phase: Phase
    at: WorkspaceTime


class ConversationTurn(ConversationModel):
    id: WorkspaceId
    question: RequiredText = Field(max_length=2000)
    materialIds: list[WorkspaceId] | None = Field(default=None, max_length=50)
    answers: list[Annotated[RequiredText, Field(max_length=2000)]] = Field(max_length=2)
    phase: Phase
    intent: ConversationIntent | None = None
    requestedSources: list[Source] | None = Field(default=None, min_length=1, max_length=2)
    requestedConstraints: ResearchConstraints | None = None
    searches: list[ConversationSearch] = Field(max_length=2)
    failures: list[ConversationFailure] = Field(max_length=2)
    error: Text | None = Field(default=None, max_length=500)
    events: list[ConversationEvent] = Field(min_length=1, max_length=50)
    createdAt: WorkspaceTime

    @model_validator(mode="after")
    def unique_searches(self):
        if self.materialIds is not None and len(set(self.materialIds)) != len(self.materialIds):
            raise ValueError("turn material IDs must be unique")
        if len({item.source for item in self.searches}) != len(self.searches):
            raise ValueError("search sources must be unique")
        return self


class ResearchConversation(ConversationModel):
    version: Literal[1]
    mode: Literal["auto", "plan"]
    materialIds: list[WorkspaceId] = Field(max_length=50)
    turns: list[ConversationTurn] = Field(min_length=1, max_length=20)
    draft: Text = Field(max_length=2000)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.materialIds)) != len(self.materialIds) or len({turn.id for turn in self.turns}) != len(self.turns):
            raise ValueError("conversation identities must be unique")
        return self
