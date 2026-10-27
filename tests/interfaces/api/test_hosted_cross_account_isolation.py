"""Cross-account regressions for the hosted research service.

Every request runs through the real hosted boundary, research composition and
stores, fully offline. Bob signs in to the same server as Alice and knows every
identifier Alice's responses expose; none of them may reach Alice's data.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.composition.research import reset_default_research_runtime
from interfaces.services.research_desktop_control import canonical_request_fingerprint
from tests.interfaces.api.hosted_research_support import (
    configure_research_without_llm,
    content_fingerprint,
    forbid_network,
    import_pdf,
    selectable_text_pdf,
)
from tests.interfaces.api.hosted_support import build_test_hosted_runtime, mutation_headers, sign_in

_PASSWORD = "correct-horse-battery-staple"
_MEMORY_PATH = "/api/v1/research/desktop/memory"
_ARTIFACT_REF = re.compile(r"artifact://research/([a-z][a-z0-9-]*)/([0-9a-f]{64})")


@pytest.fixture
def hosted(monkeypatch, tmp_path):
    configure_research_without_llm(monkeypatch, tmp_path)
    monkeypatch.setattr("interfaces.services.paper_rag_factory.preload_reranker", lambda: None)
    runtime = build_test_hosted_runtime(tmp_path)
    alice = sign_in(runtime, "alice", _PASSWORD)
    bob = sign_in(runtime, "bob", _PASSWORD)
    reset_default_research_runtime()
    app = create_app(hosted_runtime=runtime, audit_emitter_factory=None)
    try:
        with TestClient(app) as client, monkeypatch.context() as network_guard:
            forbid_network(network_guard)
            generation = client.get("/api/v1/research/desktop/identity", headers=alice).json()["data"][
                "generation"
            ]
            yield client, alice, bob, generation
    finally:
        reset_default_research_runtime()


def _alice_paper(client: TestClient, alice: dict[str, str], generation: str) -> tuple[dict, bytes]:
    pdf = selectable_text_pdf("alice-private-paper")
    operation = import_pdf(
        client,
        headers=alice,
        generation=generation,
        request_id="alice-private-import",
        filename="alice-private.pdf",
        source_revision="alice-revision",
        content=pdf,
    )
    return operation, pdf


def _error_code(response) -> str:
    return response.json()["error"]["code"]


def test_another_accounts_paper_is_indistinguishable_from_a_missing_one(hosted) -> None:
    client, alice, bob, generation = hosted
    operation, pdf = _alice_paper(client, alice, generation)
    paper_id = operation["result"]["paperId"]
    source_params = {"sourceRevision": "alice-revision", "sourceFingerprint": content_fingerprint(pdf)}

    owned = {
        "document": client.get(f"/api/v1/research/papers/{paper_id}/document", headers=alice),
        "reader": client.get(f"/api/v1/research/desktop/papers/{paper_id}/reader", headers=alice),
        "source": client.get(
            f"/api/v1/research/desktop/papers/{paper_id}/source", headers=alice, params=source_params
        ),
    }
    attacked = {
        "document": client.get(f"/api/v1/research/papers/{paper_id}/document", headers=bob),
        "reader": client.get(f"/api/v1/research/desktop/papers/{paper_id}/reader", headers=bob),
        "source": client.get(
            f"/api/v1/research/desktop/papers/{paper_id}/source", headers=bob, params=source_params
        ),
    }

    assert {name: response.status_code for name, response in owned.items()} == dict.fromkeys(owned, 200)
    assert {name: response.status_code for name, response in attacked.items()} == dict.fromkeys(attacked, 404)
    for response in attacked.values():
        assert "alice-private" not in response.text
        assert pdf not in response.content
    # Source listings answer an unknown paper with an empty list, so another
    # account's paper must look exactly like that.
    owned_sources = client.get(f"/api/v1/research/papers/{paper_id}/sources", headers=alice)
    attacked_sources = client.get(f"/api/v1/research/papers/{paper_id}/sources", headers=bob)
    assert len(owned_sources.json()["data"]["sources"]) == 1
    assert attacked_sources.status_code == 200
    assert attacked_sources.json()["data"]["sources"] == []
    assert "alice-private" not in attacked_sources.text


def test_another_accounts_operation_status_is_not_found(hosted) -> None:
    client, alice, bob, generation = hosted
    _alice_paper(client, alice, generation)
    path = "/api/v1/research/desktop/operations/alice-private-import"

    owned = client.get(path, headers=alice, params={"generation": generation})
    attacked = client.get(path, headers=bob, params={"generation": generation})

    assert owned.status_code == 200
    assert owned.json()["data"]["status"] == "passed"
    assert attacked.status_code == 404
    assert _error_code(attacked) == "desktop_request_not_found"


def test_artifact_digests_from_another_account_are_not_found(hosted) -> None:
    client, alice, bob, generation = hosted
    operation, _pdf = _alice_paper(client, alice, generation)
    document = client.get(f"/api/v1/research/papers/{operation['result']['paperId']}/document", headers=alice)
    refs = sorted(set(_ARTIFACT_REF.findall(document.text)))
    assert refs, "the parsed document must expose its artifact references"

    for artifact_type, digest in refs:
        path = f"/api/v1/research/artifacts/{artifact_type}/{digest}"
        owned = client.get(path, headers=alice)
        attacked = client.get(path, headers=bob, params={"includePayload": "true"})
        assert owned.status_code == 200, (artifact_type, owned.text)
        assert attacked.status_code == 404, (artifact_type, attacked.text)
        assert _error_code(attacked) == "artifact_not_found"
        # Naming Alice's identity in the query cannot borrow her scope.
        alice_id = client.get("/api/v1/auth/session", headers=alice).json()["data"]["session"]["user"]["userId"]
        forged = client.get(path, headers=bob, params={"userId": alice_id, "includePayload": "true"})
        assert forged.status_code in {400, 403, 404}, (artifact_type, forged.text)
        assert "payload" not in forged.text


def test_personal_memory_and_history_stay_with_their_owner(hosted) -> None:
    client, alice, bob, _generation = hosted
    created = client.post(
        _MEMORY_PATH,
        headers=mutation_headers(alice),
        json={
            "creation": "confirmed",
            "request_id": "alice-memory-create",
            "content": "Alice prefers ablation tables first.",
            "category": "reading_preference",
            "source_refs": [{"source_type": "user_input", "source_ref": "user://desktop/settings"}],
        },
    )
    assert created.status_code == 200, created.text
    memory_id = created.json()["data"]["record"]["memory_id"]
    saved = client.put(
        "/api/v1/research/history",
        headers=mutation_headers(alice),
        json={
            "revision": 0,
            "visits": [
                {
                    "id": "papers:alice",
                    "module": "papers",
                    "question": "Alice private question",
                    "href": "/papers?question=Alice%20private%20question",
                    "scrollY": 0,
                    "createdAt": 1,
                    "updatedAt": 1,
                    "title": "Alice private question",
                    "groupId": None,
                    "isFavorite": False,
                    "deletedAt": None,
                }
            ],
            "groups": [],
        },
    )
    assert saved.status_code == 200, saved.text

    bob_memories = client.get(_MEMORY_PATH, headers=bob)
    bob_memory = client.get(f"{_MEMORY_PATH}/{memory_id}", headers=bob)
    bob_history = client.get("/api/v1/research/history", headers=bob)
    bob_reports = client.get("/api/v1/research/desktop/reports", headers=bob)

    assert bob_memories.status_code == 200
    assert bob_memories.json()["data"]["records"] == []
    assert bob_memory.status_code == 404
    assert bob_history.json()["data"] == {"revision": 0, "visits": [], "groups": []}
    assert bob_reports.json()["data"] == {"reports": []}
    alice_history = client.get("/api/v1/research/history", headers=alice).json()["data"]
    assert [visit["id"] for visit in alice_history["visits"]] == ["papers:alice"]


@pytest.mark.parametrize(
    ("source_type", "source_url"),
    (
        ("local", "https://papers.example.org/paper.pdf"),
        ("manual", "https://papers.example.org/paper.pdf"),
        ("github", "https://github.com/example/repository"),
    ),
)
def test_source_preparation_never_reads_server_files(hosted, source_type, source_url) -> None:
    client, alice, _bob, generation = hosted
    semantic = {"kind": "prepare", "paperId": "paper-local", "sourceUrl": source_url, "sourceType": source_type}

    response = client.post(
        "/api/v1/research/desktop/operations/prepare",
        headers=mutation_headers(alice),
        json={
            "requestId": f"prepare-{source_type}-0001",
            "generation": generation,
            "requestFingerprint": canonical_request_fingerprint(semantic),
            "paperId": "paper-local",
            "sourceUrl": source_url,
            "sourceType": source_type,
        },
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "source_type_unsupported"


def test_capability_provenance_names_no_account(hosted) -> None:
    client, alice, bob, generation = hosted
    _alice_paper(client, alice, generation)
    alice_id = client.get("/api/v1/auth/session", headers=alice).json()["data"]["session"]["user"]["userId"]

    capabilities = client.get("/api/v1/research/desktop/capabilities", headers=bob)

    assert capabilities.status_code == 200
    verified = [item for item in capabilities.json()["data"]["capabilities"] if item["verified"]]
    assert {item["id"] for item in verified} >= {"source", "pdf-import"}
    for item in verified:
        assert item["provenance"] == {"verificationKind": "business_operation", "operation": "import"}
    assert alice_id not in capabilities.text
    assert "alice-private-import" not in capabilities.text
