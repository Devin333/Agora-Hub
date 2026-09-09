from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


ResearchSource = Literal["papers", "projects"]
MAX_CLARIFICATION_ROUNDS = 2
MAX_GUIDED_RESULTS = 10
MAX_PREVIOUS_TURNS = 19


class GuidedResearchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        user_action_required: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.user_action_required = user_action_required
        self.details = dict(details or {})


class GuidedSourceUnavailableError(RuntimeError):
    """A configured real search source cannot currently provide verified records."""


class GuidedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GuidedConstraints(GuidedModel):
    recentYear: bool | None = None
    hasCode: bool | None = None
    paperType: Literal["survey"] | None = None
    language: Literal["python", "typescript", "javascript", "rust", "go"] | None = None
    license: Literal["MIT", "Apache-2.0", "BSD-3-Clause"] | None = None
    recentlyActive: bool | None = None
    localRunnable: bool | None = None

    def compact(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class GuidedClarification(GuidedModel):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def unique_options(self) -> "GuidedClarification":
        normalized = [item.strip() for item in self.options]
        if any(not item or len(item) > 120 for item in normalized) or len(set(normalized)) != len(normalized):
            raise ValueError("clarification options must be unique bounded text")
        object.__setattr__(self, "options", normalized)
        return self


class GuidedIntent(GuidedModel):
    summary: str = Field(min_length=1, max_length=500)
    query: str = Field(min_length=1, max_length=2000)
    sources: list[ResearchSource] = Field(min_length=1, max_length=2)
    constraints: GuidedConstraints
    clarification: GuidedClarification | None
    confirmationRequired: bool = False
    changeNotice: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def unique_sources(self) -> "GuidedIntent":
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must be unique")
        return self


class GuidedMaterial(GuidedModel):
    id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")
    kind: Literal["paper", "project", "discussion", "note", "pdf"]
    title: str = Field(min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2000)
    notes: str = Field(default="", max_length=4000)
    excerpt: str = Field(default="", max_length=6000)

    @model_validator(mode="after")
    def valid_url(self) -> "GuidedMaterial":
        if self.url is not None and not _safe_url(self.url):
            raise ValueError("material URL is invalid")
        return self


class GuidedPreviousResult(GuidedModel):
    id: str = Field(min_length=1, max_length=200)
    kind: ResearchSource
    title: str = Field(min_length=1, max_length=500)
    source: str = Field(max_length=200)
    url: str = Field(max_length=2000)

    @model_validator(mode="after")
    def valid_url(self) -> "GuidedPreviousResult":
        if not _safe_url(self.url):
            raise ValueError("previous result URL is invalid")
        return self


class GuidedPreviousTurn(GuidedModel):
    question: str = Field(min_length=1, max_length=2000)
    answers: list[str] = Field(default_factory=list, max_length=MAX_CLARIFICATION_ROUNDS)
    summary: str | None = Field(default=None, max_length=500)
    query: str | None = Field(default=None, max_length=2000)
    sources: list[ResearchSource] = Field(default_factory=list, max_length=2)
    constraints: GuidedConstraints = Field(default_factory=GuidedConstraints)
    results: list[GuidedPreviousResult] = Field(default_factory=list, max_length=20)


class GuidedIntentCommand(GuidedModel):
    question: str = Field(min_length=1, max_length=2000)
    answers: list[str] = Field(default_factory=list, max_length=MAX_CLARIFICATION_ROUNDS)
    previous: list[GuidedPreviousTurn] = Field(default_factory=list, max_length=MAX_PREVIOUS_TURNS)
    materials: list[GuidedMaterial] = Field(default_factory=list, max_length=50)
    skipClarification: bool = False
    requestedSources: list[ResearchSource] | None = Field(default=None, min_length=1, max_length=2)
    constraints: GuidedConstraints | None = None

    @model_validator(mode="after")
    def validate_requested_sources(self) -> "GuidedIntentCommand":
        if self.requestedSources and len(set(self.requestedSources)) != len(self.requestedSources):
            raise ValueError("requestedSources must be unique")
        normalized_answers = [answer.strip() for answer in self.answers]
        if any(not answer or len(answer) > 2000 for answer in normalized_answers):
            raise ValueError("answers must be bounded nonblank text")
        object.__setattr__(self, "answers", normalized_answers)
        return self


class GuidedSearchCommand(GuidedModel):
    source: ResearchSource
    intent: GuidedIntent
    materials: list[GuidedMaterial] = Field(default_factory=list, max_length=50)


class GuidedResult(GuidedModel):
    id: str = Field(min_length=1, max_length=200)
    kind: ResearchSource
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(max_length=2000)
    source: str = Field(max_length=200)
    url: str = Field(min_length=1, max_length=2000)
    href: str | None = Field(default=None, max_length=10000)
    authors: str | None = Field(default=None, max_length=1000)
    publishedAt: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=100)
    stars: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_actions(self) -> "GuidedResult":
        if not _safe_url(self.url):
            raise ValueError("result URL must be a public HTTP(S) source")
        if self.href is not None and not _safe_href(self.href):
            raise ValueError("result href must be a same-origin research page")
        return self


class GuidedSearchResponse(GuidedModel):
    source: ResearchSource
    results: list[GuidedResult] = Field(max_length=MAX_GUIDED_RESULTS)
    total: int = Field(ge=0)
    moreHref: str | None = Field(default=None, min_length=1, max_length=10000)

    @model_validator(mode="after")
    def valid_source(self) -> "GuidedSearchResponse":
        if any(item.kind != self.source for item in self.results) or (self.moreHref is not None and not _safe_href(self.moreHref)):
            raise ValueError("search response source or moreHref is invalid")
        return self


class GuidedIntentCandidatePort(Protocol):
    def generate_candidate(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


class GuidedSearchPort(Protocol):
    def search(
        self,
        *,
        intent: GuidedIntent,
        materials: Sequence[GuidedMaterial],
        actor_user_id: str | None,
    ) -> GuidedSearchResponse | Mapping[str, Any]: ...


def _safe_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password and not any(char in value for char in "\\\r\n")


def _safe_href(value: str) -> bool:
    if not value.startswith("/") or value.startswith("//") or any(char in value for char in "\\\r\n"):
        return False
    parsed = urlsplit(value)
    return (
        not parsed.scheme
        and not parsed.netloc
        and bool(re.fullmatch(r"/(?:design-demo/)?(?:papers|projects)(?:/[A-Za-z0-9_-]+(?:/read)?)?", parsed.path))
    )


__all__ = [
    "GuidedClarification",
    "GuidedConstraints",
    "GuidedIntent",
    "GuidedIntentCandidatePort",
    "GuidedIntentCommand",
    "GuidedMaterial",
    "GuidedPreviousResult",
    "GuidedPreviousTurn",
    "GuidedResearchError",
    "GuidedResult",
    "GuidedSearchCommand",
    "GuidedSearchPort",
    "GuidedSearchResponse",
    "GuidedSourceUnavailableError",
    "MAX_CLARIFICATION_ROUNDS",
    "MAX_GUIDED_RESULTS",
]
