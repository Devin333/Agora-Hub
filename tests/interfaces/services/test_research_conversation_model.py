from copy import deepcopy

import pytest
from pydantic import ValidationError

from interfaces.services.research_conversation_model import ResearchConversation
from interfaces.services.research_history_service import ResearchHistoryError, ResearchHistoryService


def conversation():
    return {"version": 1, "mode": "plan", "materialIds": [], "draft": "继续的问题", "turns": [{
        "id": "turn-1", "question": "Agent", "answers": [], "phase": "confirming", "searches": [], "failures": [], "createdAt": 1,
        "intent": {"summary": "Agent 如何完成任务", "query": "Agent", "sources": ["papers", "projects"], "constraints": {}, "clarification": None},
        "events": [{"phase": "understanding", "at": 1}, {"phase": "confirming", "at": 2}],
    }]}


def visit():
    return {"id": "study", "module": "papers", "question": "Agent", "href": "/design-demo/papers?question=Agent&researchSession=study", "scrollY": 420, "createdAt": 1, "updatedAt": 2, "title": "Agent", "groupId": None, "isFavorite": False, "deletedAt": None, "conversation": conversation()}


def test_conversation_restores_in_same_account_and_old_snapshots_remain_valid(tmp_path):
    service = ResearchHistoryService(tmp_path / "history.json")
    service.replace(user_id="one", revision=0, visits=[visit()], groups=[])
    restored = ResearchHistoryService(tmp_path / "history.json").read(user_id="one")
    assert restored.visits[0]["conversation"] == conversation()
    assert restored.visits[0]["scrollY"] == 420
    assert service.read(user_id="two").visits == []
    old = visit()
    old.pop("conversation")
    service.replace(user_id="one", revision=1, visits=[old], groups=[])
    assert "conversation" not in service.read(user_id="one").visits[0]


@pytest.mark.parametrize("mutation", [
    lambda c: c["turns"][0]["answers"].extend(["1", "2", "3"]),
    lambda c: c["turns"].extend([deepcopy(c["turns"][0]) for _ in range(20)]),
    lambda c: c["turns"][0]["intent"].update(sources=["papers", "papers"]),
    lambda c: c["turns"][0]["intent"].update(query=""),
    lambda c: c.update(materialIds=["source", "source"]),
    lambda c: c["turns"][0].update(executor="run-anything"),
])
def test_rejects_unbounded_or_ambiguous_client_snapshots(mutation):
    value = conversation()
    mutation(value)
    with pytest.raises(ValidationError):
        ResearchConversation.model_validate(value)


@pytest.mark.parametrize("href", ["https://evil.test/papers/a", "//evil.test/papers/a", "/papers/../../admin", "/papers/%2e%2e/admin", "/projects/a\\b"])
def test_rejects_unsafe_result_destinations(href):
    value = conversation()
    value["turns"][0]["searches"] = [{"source": "papers", "total": 1, "moreHref": "/papers", "results": [{"id": "one", "kind": "papers", "title": "Paper", "description": "", "source": "arXiv", "url": "https://arxiv.org/abs/2605.22343", "href": href}]}]
    with pytest.raises(ValidationError):
        ResearchConversation.model_validate(value)


def test_cannot_change_original_question_without_matching_visit(tmp_path):
    value = visit()
    value["conversation"]["turns"][0]["question"] = "Another research"
    with pytest.raises(ResearchHistoryError):
        ResearchHistoryService(tmp_path / "history.json").replace(user_id="one", revision=0, visits=[value], groups=[])
