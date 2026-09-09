from __future__ import annotations

from fastapi.testclient import TestClient

from interfaces.api import create_app


class _Guided:
    def __init__(self) -> None:
        self.calls = []

    def interpret(self, payload, *, user_id):
        self.calls.append(("intent", payload, user_id))
        return {
            "summary": "找 Agent 评测论文",
            "query": "agent evaluation",
            "sources": ["papers"],
            "constraints": {"hasCode": True},
            "clarification": None,
        }

    def search(self, *, source, intent, material_ids, public_sources, user_id):
        self.calls.append(("search", source, intent, material_ids, public_sources, user_id))
        return {
            "source": "papers",
            "results": [{
                "id": "paper-1", "kind": "papers", "title": "Agent Evaluation",
                "description": "Real catalog result", "source": "arXiv",
                "url": "https://arxiv.org/abs/1", "href": "/papers/paper-1/read",
            }],
            "total": 1,
            "moreHref": "/papers?question=agent",
        }


def test_guided_intent_route_returns_exact_data_envelope() -> None:
    service = _Guided()
    client = TestClient(create_app(guided_research_service_factory=lambda: service, audit_emitter_factory=None))

    response = client.post("/api/v1/research/guided/intent", json={
        "question": "找有代码的 Agent 评测论文",
        "answers": [], "previous": [], "materialIds": [], "skipClarification": False,
        "publicSources": [],
        "requestedSources": ["papers"], "constraints": {"hasCode": True},
    })

    assert response.status_code == 200
    assert response.json()["data"] == {
        "summary": "找 Agent 评测论文", "query": "agent evaluation", "sources": ["papers"],
        "constraints": {"hasCode": True}, "clarification": None,
    }
    assert service.calls[0][2] is None


def test_guided_search_route_is_independent_per_source() -> None:
    service = _Guided()
    client = TestClient(create_app(guided_research_service_factory=lambda: service, audit_emitter_factory=None))

    response = client.post("/api/v1/research/guided/search", json={
        "source": "papers",
        "intent": {"summary": "找论文", "query": "agent", "sources": ["papers"], "constraints": {}, "clarification": None},
        "materialIds": [],
        "publicSources": [],
    })

    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["href"] == "/papers/paper-1/read"
    assert service.calls[0][1] == "papers"


def test_guided_intent_rejects_unbounded_or_unsafe_previous_metadata() -> None:
    service = _Guided()
    client = TestClient(create_app(guided_research_service_factory=lambda: service, audit_emitter_factory=None))

    response = client.post("/api/v1/research/guided/intent", json={
        "question": "第二篇呢？", "answers": [], "materialIds": [], "skipClarification": False,
        "previous": [{
            "id": "turn-1", "question": "找论文", "answers": [], "phase": "results",
            "searches": [{"source": "papers", "results": [{
                "id": "paper-1", "kind": "papers", "title": "Paper", "description": "", "source": "x",
                "url": "https://user:secret@example.test/paper",
            }], "total": 1, "moreHref": "/papers"}],
            "failures": [], "events": [{"phase": "results", "at": 1}], "createdAt": 1,
        }],
    })

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert service.calls == []
