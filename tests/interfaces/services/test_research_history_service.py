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
