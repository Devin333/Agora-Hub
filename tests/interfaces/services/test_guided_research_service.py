from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from backend.research.application.guided_research import GuidedResearchApplication
from backend.research.ports.guided_research import (
    GuidedResearchError,
    GuidedSearchResponse,
)
from interfaces.services.guided_research_service import GuidedResearchService, ProjectGuidedSearch
from interfaces.services.research_conversation_model import ConversationTurn
from interfaces.services.research_history_service import ResearchHistorySnapshot


class _Candidate:
    def __init__(self):
        self.payload = None

    def generate_candidate(self, payload):
        self.payload = payload
        return {"summary": "继续研究第二篇", "query": "paper two", "sources": ["papers"], "constraints": {}, "clarification": None}


class _EmptySearch:
    def search(self, *, intent, materials, actor_user_id):
        source = intent.sources[0]
        return GuidedSearchResponse(source=source, results=[], total=0, moreHref=f"/{source}")


class _History:
    def read(self, *, user_id):
        assert user_id == "user-1"
        return ResearchHistorySnapshot(revision=1, visits=[], groups=[], workspace_items={
            "materials": [{"id": "mat-1", "kind": "paper", "title": "Owned paper", "url": "https://example.test/paper"}],
        })


class _NoDocuments:
    def get_document(self, paper_id, *, actor):
        raise LookupError(paper_id)


class _OwnedDocumentHistory:
    def read(self, *, user_id):
        return ResearchHistorySnapshot(revision=1, visits=[], groups=[], workspace_items={
            "materials": [{
                "id": "mat-pdf", "kind": "pdf", "title": "Owned PDF", "url": "https://example.test/paper.pdf",
                "notes": "Focus on benchmarks", "referenceId": "paper-native",
            }],
        })


class _Documents:
    def get_document(self, paper_id, *, actor):
        assert paper_id == "paper-native"
        assert actor.user_id == "user-1"
        return {"document": {"abstract": "Abstract", "sections": [{"title": "Method", "text": "Full parsed text"}]}}


@dataclass
class _IntentRequest:
    question: str
    answers: list[str]
    previous: list[ConversationTurn]
    materialIds: list[str]
    skipClarification: bool
    requestedSources: list[str] | None = None
    constraints: object | None = None
    publicSources: list[object] = field(default_factory=list)


def _turn() -> ConversationTurn:
    return ConversationTurn.model_validate({
        "id": "turn-1", "question": "找论文", "answers": [], "phase": "results",
        "intent": {"summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None},
        "searches": [{"source": "papers", "results": [{
            "id": "paper-2", "kind": "papers", "title": "Paper Two", "description": "",
            "source": "arXiv", "url": "https://arxiv.org/abs/2",
        }], "total": 1, "moreHref": "/papers"}],
        "failures": [], "events": [{"phase": "results", "at": 1}], "createdAt": 1,
    })


def test_service_resolves_owned_material_and_projects_previous_results() -> None:
    candidate = _Candidate()
    search = _EmptySearch()
    service = GuidedResearchService(
        application=GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search),
        history=_History(),
        research=_NoDocuments(),
    )
    result = service.interpret(_IntentRequest(
        question="第二篇呢？", answers=[], previous=[_turn()], materialIds=["mat-1"], skipClarification=True,
    ), user_id="user-1")
    assert result["clarification"] is None
    assert candidate.payload["materials"] == [{
        "id": "mat-1", "kind": "paper", "title": "Owned paper",
        "url": "https://example.test/paper", "notes": "", "excerpt": "",
    }]
    assert candidate.payload["previous"][0]["results"][0]["id"] == "paper-2"


def test_guest_private_material_requires_login_without_disclosing_identity() -> None:
    candidate = _Candidate()
    search = _EmptySearch()
    service = GuidedResearchService(
        application=GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search),
        history=_History(),
        research=_NoDocuments(),
    )
    with pytest.raises(GuidedResearchError) as raised:
        service.interpret(_IntentRequest(
            question="找论文", answers=[], previous=[], materialIds=["mat-1"], skipClarification=False,
        ), user_id=None)
    assert raised.value.code == "guided_material_login_required"
    assert "mat-1" not in raised.value.message


def test_owned_pdf_context_includes_notes_source_and_bounded_native_excerpt() -> None:
    candidate = _Candidate()
    search = _EmptySearch()
    service = GuidedResearchService(
        application=GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search),
        history=_OwnedDocumentHistory(), research=_Documents(),
    )
    service.interpret(_IntentRequest(
        question="结合资料找论文", answers=[], previous=[], materialIds=["mat-pdf"], skipClarification=True,
    ), user_id="user-1")
    material = candidate.payload["materials"][0]
    assert material["notes"] == "Focus on benchmarks"
    assert material["url"] == "https://example.test/paper.pdf"
    assert material["excerpt"] == "Abstract\n\nMethod\nFull parsed text"


def test_missing_private_pdf_is_not_silently_ignored():
    candidate = _Candidate()
    search = _EmptySearch()
    service = GuidedResearchService(application=GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search), history=_OwnedDocumentHistory(), research=_NoDocuments())
    with pytest.raises(GuidedResearchError) as raised:
        service.interpret(_IntentRequest(question="结合 PDF 找论文", answers=[], previous=[], materialIds=["mat-pdf"], skipClarification=True), user_id="user-1")
    assert raised.value.code == "guided_pdf_unavailable"
    assert candidate.payload is None


@dataclass
class _PublicSource:
    id: str
    kind: str
    title: str
    url: str
    notes: str


def test_guest_public_note_is_untrusted_context_without_private_lookup() -> None:
    candidate = _Candidate()
    search = _EmptySearch()
    service = GuidedResearchService(
        application=GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search),
        history=_History(), research=_NoDocuments(),
    )
    service.interpret(_IntentRequest(
        question="按笔记找论文", answers=[], previous=[], materialIds=[], skipClarification=True,
        publicSources=[_PublicSource("note-1", "note", "My note", "", "agent benchmark")],
    ), user_id=None)
    assert candidate.payload["materials"][0]["notes"] == "agent benchmark"


class _Projects:
    def list_projects(self, query):
        return {"items": [{
            "id": "project-1", "slug": "project-one", "name": "Project One", "tagline": "Agent tooling",
            "github_url": "https://github.com/example/project", "tags": ["python"],
            "metric_summary": {"github_stars": 123}, "updated_at": "2026-09-01T00:00:00+00:00",
        }], "meta": {"source": "artifact", "data_state": "ready"}}

    def get_project(self, project_id):
        return {"tool_profile": {"license": "MIT", "local_deployable": True}}


def test_project_search_applies_verified_project_constraints() -> None:
    from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent
    result = ProjectGuidedSearch(_Projects()).search(
        intent=GuidedIntent(summary="项目", query="agent", sources=["projects"], constraints=GuidedConstraints(language="python", license="MIT", hasCode=True), clarification=None),
        materials=[], actor_user_id=None,
    )
    assert result.total == 1
    assert result.results[0].stars == 123
    assert result.results[0].href == "/projects/project-one"
    assert result.moreHref is None


def test_project_more_results_preserve_supported_filters() -> None:
    from urllib.parse import parse_qs, urlsplit
    from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent

    intent = GuidedIntent(summary="Python 项目", query="agent", sources=["projects"], constraints=GuidedConstraints(language="python", license="MIT"), clarification=None)
    result = ProjectGuidedSearch(_Projects()).search(intent=intent, materials=[], actor_user_id=None)
    assert parse_qs(urlsplit(result.moreHref).query) == {"q": ["agent"], "language": ["python"], "license": ["MIT"]}


def test_project_code_filter_excludes_websites_without_a_repository() -> None:
    from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent

    class Websites(_Projects):
        def list_projects(self, query):
            payload = super().list_projects(query)
            payload["items"][0].pop("github_url")
            payload["items"][0]["website_url"] = "https://example.com/project"
            return payload

    intent = GuidedIntent(summary="有代码的项目", query="agent", sources=["projects"], constraints=GuidedConstraints(hasCode=True), clarification=None)
    assert ProjectGuidedSearch(Websites()).search(intent=intent, materials=[], actor_user_id=None).results == []
    assert ProjectGuidedSearch(_Projects()).search(intent=intent, materials=[], actor_user_id=None).total == 1


def test_local_setup_never_trusts_a_legacy_deployable_flag() -> None:
    from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent
    intent = GuidedIntent(summary="可以本地运行的项目", query="agent", sources=["projects"], constraints=GuidedConstraints(localRunnable=True), clarification=None)
    with pytest.raises(GuidedResearchError, match="本地运行说明"):
        ProjectGuidedSearch(_Projects()).search(intent=intent, materials=[], actor_user_id=None)
    source = _EmptySearch()
    result = ProjectGuidedSearch(_Projects(), public_search=source).search(intent=intent, materials=[], actor_user_id=None)
    assert result.total == 0


class _EmptyProjects:
    def list_projects(self, query):
        return {"items": [], "meta": {"source": "none", "data_state": "empty"}}


def test_project_search_reports_missing_source_instead_of_empty_match() -> None:
    from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent
    with pytest.raises(GuidedResearchError) as raised:
        ProjectGuidedSearch(_EmptyProjects()).search(
            intent=GuidedIntent(summary="项目", query="agent", sources=["projects"], constraints=GuidedConstraints(), clarification=None),
            materials=[], actor_user_id=None,
        )
    assert raised.value.code == "guided_projects_source_unavailable"
