from __future__ import annotations

import pytest

from backend.research.application.guided_research import GuidedResearchApplication
from backend.research.ports.guided_research import (
    GuidedConstraints,
    GuidedIntentCommand,
    GuidedPreviousTurn,
    GuidedResearchError,
    GuidedSourceUnavailableError,
)


class _Candidate:
    def __init__(self, payload):
        self.payload = payload
        self.received = None

    def generate_candidate(self, payload):
        self.received = payload
        return self.payload


class _Search:
    def search(self, **kwargs):
        source = kwargs["intent"].sources[0]
        return {"source": source, "results": [], "total": 0, "moreHref": f"/{source}"}


class _UnavailableSearch:
    def search(self, **kwargs):
        raise GuidedSourceUnavailableError("cache missing")


def _application(candidate: _Candidate) -> GuidedResearchApplication:
    search = _Search()
    return GuidedResearchApplication(intent_candidate=candidate, paper_search=search, project_search=search)


def test_interpret_passes_bounded_previous_context_to_candidate() -> None:
    candidate = _Candidate({
        "summary": "继续看第二篇论文",
        "query": "second paper evaluation",
        "sources": ["papers"],
        "constraints": {},
        "clarification": None,
    })
    command = GuidedIntentCommand.model_validate({
        "question": "第二篇更关注什么？",
        "previous": [{
            "question": "找 Agent 评测论文",
            "results": [{
                "id": "paper-2", "kind": "papers", "title": "Evaluation Two",
                "source": "arXiv", "url": "https://arxiv.org/abs/2",
            }],
        }],
    })

    result = _application(candidate).interpret(command)

    assert result.query == "second paper evaluation"
    assert candidate.received["previous"][0]["results"][0]["id"] == "paper-2"


def test_interpret_preserves_requested_source_and_constraints() -> None:
    candidate = _Candidate({
        "summary": "找近期有代码的论文",
        "query": "agent evaluation code",
        "sources": ["papers"],
        "constraints": {"recentYear": True, "hasCode": True},
        "clarification": None,
    })
    result = _application(candidate).interpret(GuidedIntentCommand(
        question="找论文",
        requestedSources=["papers"],
        constraints=GuidedConstraints(recentYear=True, hasCode=True),
    ))
    assert result.sources == ["papers"]
    assert result.constraints.recentYear is True
    assert result.constraints.hasCode is True


def test_interpret_rejects_candidate_that_drops_explicit_source() -> None:
    candidate = _Candidate({
        "summary": "项目",
        "query": "agent",
        "sources": ["projects"],
        "constraints": {},
        "clarification": None,
    })
    with pytest.raises(GuidedResearchError, match="查找类型"):
        _application(candidate).interpret(GuidedIntentCommand(question="找 Agent 论文"))


def test_requested_sources_are_an_exact_selection_not_a_subset() -> None:
    candidate = _Candidate({
        "summary": "论文和项目",
        "query": "agent",
        "sources": ["papers", "projects"],
        "constraints": {},
        "clarification": None,
    })
    with pytest.raises(GuidedResearchError, match="查找类型"):
        _application(candidate).interpret(GuidedIntentCommand(
            question="找 Agent 论文",
            requestedSources=["papers"],
        ))


def test_interpret_stops_clarifying_after_two_answers() -> None:
    candidate = _Candidate({
        "summary": "Agent 评测",
        "query": "agent evaluation",
        "sources": ["papers"],
        "constraints": {},
        "clarification": {"question": "还想看什么？", "options": ["基准", "方法"]},
    })
    result = _application(candidate).interpret(GuidedIntentCommand(
        question="Agent 评测",
        answers=["论文", "近期"],
    ))
    assert result.clarification is None


def test_explicit_paper_request_allows_useful_paper_scope_clarification() -> None:
    candidate = _Candidate({
        "summary": "找 Agent 论文",
        "query": "agent evaluation",
        "sources": ["papers"],
        "constraints": {},
        "clarification": {"question": "这些论文里更关注哪个方向？", "options": ["评测方法", "任务能力"]},
    })
    result = _application(candidate).interpret(GuidedIntentCommand(question="找 Agent 论文"))
    assert result.clarification is not None
    assert result.clarification.options == ["评测方法", "任务能力"]


def test_intent_rejects_instruction_sentence_as_search_query() -> None:
    candidate = _Candidate({
        "summary": "找论文",
        "query": "请帮我找一些最近一年有代码的 Agent 评测论文",
        "sources": ["papers"],
        "constraints": {},
        "clarification": None,
    })
    with pytest.raises(GuidedResearchError, match="关键词"):
        _application(candidate).interpret(GuidedIntentCommand(question="找 Agent 论文"))


def test_search_projects_source_unavailable_to_public_error() -> None:
    from backend.research.ports.guided_research import GuidedIntent, GuidedSearchCommand

    candidate = _Candidate({})
    application = GuidedResearchApplication(
        intent_candidate=candidate, paper_search=_Search(), project_search=_UnavailableSearch(),
    )
    with pytest.raises(GuidedResearchError) as raised:
        application.search(GuidedSearchCommand(
            source="projects",
            intent=GuidedIntent(summary="项目", query="agent", sources=["projects"], constraints=GuidedConstraints(), clarification=None),
        ), actor_user_id=None)
    assert raised.value.code == "guided_projects_source_unavailable"


def test_followup_preserves_omitted_conditions_and_source_in_application_gate() -> None:
    candidate = _Candidate({"summary": "类似研究", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None})
    previous = GuidedPreviousTurn(question="找论文", sources=["papers"], constraints=GuidedConstraints(recentYear=True, hasCode=True))
    result = _application(candidate).interpret(GuidedIntentCommand(question="再找几篇类似的", previous=[previous]))
    assert result.constraints.recentYear is True
    assert result.constraints.hasCode is True
    assert result.confirmationRequired is False
    assert candidate.received["requested_sources"] == ["papers"]
    candidate.payload["sources"] = ["projects"]
    with pytest.raises(GuidedResearchError, match="查找类型"):
        _application(candidate).interpret(GuidedIntentCommand(question="再找几篇类似的", previous=[previous]))


def test_relaxing_a_previous_condition_requires_visible_confirmation() -> None:
    candidate = _Candidate({"summary": "时间不限", "query": "agent", "sources": ["papers"], "constraints": {"recentYear": False}, "clarification": None})
    previous = GuidedPreviousTurn(question="找论文", sources=["papers"], constraints=GuidedConstraints(recentYear=True, hasCode=True))
    result = _application(candidate).interpret(GuidedIntentCommand(question="时间不限", previous=[previous]))
    assert result.constraints.recentYear is False
    assert result.constraints.hasCode is True
    assert result.confirmationRequired is True
    assert "最近一年" in result.changeNotice and "不限" in result.changeNotice


def test_explicitly_clearing_a_language_requires_confirmation() -> None:
    candidate = _Candidate({"summary": "不限编程语言", "query": "agent", "sources": ["projects"], "constraints": {"language": None}, "clarification": None})
    previous = GuidedPreviousTurn(question="找 Python 项目", sources=["projects"], constraints=GuidedConstraints(language="python", license="MIT"))
    result = _application(candidate).interpret(GuidedIntentCommand(question="编程语言不限", previous=[previous]))
    assert result.constraints.language is None
    assert result.constraints.license == "MIT"
    assert result.confirmationRequired is True
    assert "python" in result.changeNotice and "不限" in result.changeNotice


@pytest.mark.parametrize(("question", "answers", "source"), [
    ("找有开源代码和 GitHub 链接的 Agent 论文", [], "papers"),
    ("不要论文，找 Agent 项目", [], "projects"),
    ("找 Agent 论文", ["改成找项目"], "projects"),
])
def test_source_choice_respects_content_type_and_latest_clarification(question, answers, source) -> None:
    candidate = _Candidate({"summary": "Agent 相关资料", "query": "agent", "sources": [source], "constraints": {}, "clarification": None})
    result = _application(candidate).interpret(GuidedIntentCommand(question=question, answers=answers))
    assert result.sources == [source]
