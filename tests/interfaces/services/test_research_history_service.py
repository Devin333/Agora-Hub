from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from interfaces.services.research_history_service import (
    ResearchHistoryConflictError,
    ResearchHistoryError,
    ResearchHistoryService,
)


def _visit(*, index: int = 1, module: str = "papers", user_question: str = "Agent") -> dict:
    return {
        "id": f"{module}:{index}",
        "module": module,
        "question": user_question,
        "href": f"/{'design-demo/' if module == 'papers' else ''}{module}?question={user_question}",
        "scrollY": 0,
        "createdAt": index,
        "updatedAt": index,
        "title": "Agent research",
        "groupId": None,
        "isFavorite": False,
        "deletedAt": None,
    }


def test_history_is_persistent_and_isolated_by_authenticated_user(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    service.replace(user_id="user-a", revision=0, visits=[_visit()], groups=[])

    reopened = ResearchHistoryService(tmp_path / "history.json")
    assert reopened.read(user_id="user-a").visits[0]["question"] == "Agent"
    assert reopened.read(user_id="user-b").visits == []


def test_replace_uses_compare_and_swap_revision(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    current = service.replace(user_id="user-a", revision=0, visits=[_visit()], groups=[])

    with pytest.raises(ResearchHistoryConflictError) as error:
        service.replace(user_id="user-a", revision=0, visits=[], groups=[])

    assert error.value.details == {"revision": current.revision}
    assert service.read(user_id="user-a").visits


def test_concurrent_writes_do_not_lose_revision_or_corrupt_file(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    barrier = Barrier(8)

    def write_one(index: int) -> str:
        snapshot = service.read(user_id="user-a")
        barrier.wait()
        try:
            service.replace(user_id="user-a", revision=snapshot.revision, visits=[_visit(index=index)], groups=[])
            return "committed"
        except ResearchHistoryConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(write_one, range(1, 9)))

    assert outcomes.count("committed") == 1
    assert outcomes.count("conflict") == 7
    assert service.read(user_id="user-a").revision == 1


def test_limits_and_session_identity_are_enforced(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    with pytest.raises(ResearchHistoryError, match="more than 2000"):
        service.replace(user_id="user-a", revision=0, visits=[{}] * 2001, groups=[])

    visit = _visit()
    visit["href"] += "&researchSession=different-id"
    with pytest.raises(ResearchHistoryError, match="researchSession"):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("href", "https://evil.example/papers?question=Agent"),
        ("href", "/projects?question=Different"),
        ("module", "unknown"),
        ("question", ""),
    ],
)
def test_schema_and_href_validation_rejects_unsafe_history(tmp_path: Path, field: str, value: str) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit()
    visit[field] = value

    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])


def test_history_rejects_unknown_owner_field_and_group_reference(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit()
    visit["owner"] = "attacker"
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])

    visit = _visit()
    visit["groupId"] = "missing"
    with pytest.raises(ResearchHistoryError):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])

def test_reader_activity_round_trips_exact_version_and_safe_opaque_identity(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    service = ResearchHistoryService(path)
    visit = _visit(module="papers")
    revision = "sha256:" + "a" * 64
    visit["activity"] = {
        "kind": "reader",
        "paperId": "doi:10.1000/research-v2",
        "sourceRevision": revision,
        "title": "A paper",
        "href": (
            "/papers/doi%3A10.1000%2Fresearch-v2/read"
            f"?sourceRevision={revision.replace(':', '%3A')}"
            "&resumeSection=methods&resumePage=3"
        ),
        "sectionId": "methods",
        "sectionTitle": "Methods",
        "pdfPage": 3,
        "updatedAt": 2,
    }

    saved = service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])
    reopened = ResearchHistoryService(path)
    activity = reopened.read(user_id="user-a").visits[0]["activity"]

    assert activity["paperId"] == "doi:10.1000/research-v2"
    assert activity["sourceRevision"] == revision
    assert activity["sectionId"] == "methods"
    assert activity["pdfPage"] == 3

    visit["activity"]["updatedAt"] = 3
    updated = reopened.replace(
        user_id="user-a", revision=saved.revision, visits=[visit], groups=[]
    )
    assert updated.revision == saved.revision + 1
    assert updated.visits[0]["activity"]["href"] == visit["activity"]["href"]


def test_legacy_reader_activity_without_version_degrades_to_non_precise_resume(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit(module="papers")
    visit["activity"] = {
        "kind": "reader", "paperId": "2601.01234", "title": "A paper",
        "href": (
            "/papers/2601.01234/read?sourceRevision=untrusted-old-value"
            "&resumeSection=methods&resumePage=3&returnTo=%2Fpapers%3Fq%3Dbounded"
        ),
        "sectionId": "methods", "sectionTitle": "Methods", "pdfPage": 3, "updatedAt": 2,
    }

    saved = service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])
    activity = saved.visits[0]["activity"]

    assert "sourceRevision" not in activity
    assert "sectionId" not in activity
    assert "sectionTitle" not in activity
    assert "pdfPage" not in activity
    assert "sourceRevision=" not in activity["href"]
    assert "resumeSection=" not in activity["href"]
    assert "resumePage=" not in activity["href"]
    assert "returnTo=" in activity["href"]


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/papers/2601.01234/read",
        "//evil.example/papers/2601.01234/read",
        "/papers/../read",
        "/papers/%2e%2e/read",
        "/papers/%2e%2e%2Fsecret/read",
        "/papers/%252e%252e/read",
        "/papers/%00paper/read",
        "/papers/paper%5Cescape/read",
        "/papers/%GG/read",
        "/papers/other-paper/read",
        "/papers/2601.01234/read?sourceRevision=wrong",
        "/papers/2601.01234/read?sourceRevision=" + "sha256%3A" + "a" * 64 + "&sourceRevision=duplicate",
        "/papers/2601.01234/read?owner=forged",
        "/papers/2601.01234/read?returnTo=https%3A%2F%2Fevil.example%2Fpapers",
        "/papers/2601.01234/read?resumePage=10001",
        "/papers/2601.01234/read#fragment",
    ],
)
def test_reader_activity_rejects_unsafe_or_mismatched_targets(tmp_path: Path, href: str) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit(module="papers")
    visit["activity"] = {
        "kind": "reader", "paperId": "2601.01234",
        "sourceRevision": "sha256:" + "a" * 64, "title": "A paper",
        "href": href, "updatedAt": 2,
    }

    with pytest.raises(ResearchHistoryError, match="activity is invalid"):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])

def test_legacy_reader_activity_cannot_normalize_an_external_target(tmp_path: Path) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit(module="papers")
    visit["activity"] = {
        "kind": "reader", "paperId": "2601.01234", "title": "A paper",
        "href": "https://evil.example/papers/2601.01234/read", "updatedAt": 2,
    }
    with pytest.raises(ResearchHistoryError, match="activity is invalid"):
        service.replace(user_id="user-a", revision=0, visits=[visit], groups=[])


@pytest.mark.parametrize("paper_id", ["2601.01234", "doi:10.1000/paper-1", "p" * 256])
def test_reader_identity_boundary_round_trips_through_cas(tmp_path: Path, paper_id: str) -> None:
    from urllib.parse import quote

    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit()
    visit["activity"] = {
        "kind": "reader", "paperId": paper_id, "title": "A paper",
        "href": f"/papers/{quote(paper_id, safe='')}/read", "updatedAt": 2,
    }

    saved = service.replace(user_id="alice", revision=0, visits=[visit], groups=[])

    assert saved.visits[0]["activity"]["paperId"] == paper_id
    assert service.read(user_id="alice").visits == saved.visits


@pytest.mark.parametrize(
    "patch",
    [
        {"sourceRevision": ""},
        {"sourceRevision": "r" * 513},
        {"sourceRevision": "rev\x00"},
        {"href": "/papers/2601.01234/read?resumeSection=other"},
        {"href": "/papers/2601.01234/read?resumePage=4"},
    ],
)
def test_reader_exact_resume_rejects_unbounded_revision_and_mismatched_locator(
    tmp_path: Path, patch: dict,
) -> None:
    service = ResearchHistoryService(tmp_path / "history.json")
    visit = _visit()
    visit["activity"] = {
        "kind": "reader", "paperId": "2601.01234", "title": "A paper",
        "href": "/papers/2601.01234/read", "sourceRevision": "revision-1",
        "sectionId": "methods", "pdfPage": 3, "updatedAt": 2, **patch,
    }

    with pytest.raises(ResearchHistoryError, match="activity is invalid"):
        service.replace(user_id="alice", revision=0, visits=[visit], groups=[])
