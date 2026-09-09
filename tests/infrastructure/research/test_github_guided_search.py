from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.research.ports.guided_research import GuidedConstraints, GuidedIntent, GuidedResearchError
from infrastructure.external.sources.github import GithubRepositorySearchResult
from infrastructure.research.github_guided_search import GithubGuidedSearch


class FakeGithubConnector:
    def __init__(self, rows, readmes: dict[tuple[str, str], str]) -> None:
        self.rows = rows
        self.readmes = readmes
        self.file_calls: list[tuple[str, str]] = []

    def search_repositories(self, source, *, query: str, limit: int):
        del source, query, limit
        return self.rows, []

    def fetch_repository_file(self, source, *, repository: str, path: str, ref=None):
        del source, ref
        self.file_calls.append((repository, path))
        content = self.readmes.get((repository, path))
        if content is None:
            return None, [SimpleNamespace(error_type="github_file_unavailable")]
        return content, []


def _row(name: str, repository_id: int) -> GithubRepositorySearchResult:
    return GithubRepositorySearchResult(
        repository_id=repository_id,
        full_name=name,
        html_url=f"https://github.com/{name}",
        description=f"{name} description",
        language="Python",
        stargazers_count=repository_id,
        forks_count=0,
        open_issues_count=0,
        archived=False,
        disabled=False,
        visibility="public",
        topics=[],
        updated_at=None,
        score=None,
    )


def _intent(*, local_runnable: bool = True) -> GuidedIntent:
    return GuidedIntent(
        summary="Agent 项目",
        query="agent framework",
        sources=["projects"],
        constraints=GuidedConstraints(localRunnable=local_runnable),
        clarification=None,
    )


def test_local_runnable_requires_explicit_install_and_start_commands() -> None:
    rows = [_row("example/clone-only", 1), _row("example/install-only", 2), _row("example/runnable", 3)]
    connector = FakeGithubConnector(rows, {
        ("example/clone-only", "README.md"): "Installation and API docs. Clone the source code to use it locally.",
        ("example/install-only", "README.md"): "## Install\n```sh\npip install -e .\n```\nSee the API reference.",
        ("example/runnable", "README.md"): "## Local setup\n```sh\npip install -e .\npython -m agent_app\n```",
    })

    result = GithubGuidedSearch(connector).search(intent=_intent(), materials=[], actor_user_id=None)

    assert [item.title for item in result.results] == ["example/runnable"]
    assert result.total == 1


def test_local_runnable_accepts_readme_fallback_and_inline_commands() -> None:
    row = _row("example/typescript-app", 4)
    connector = FakeGithubConnector([row], {
        ("example/typescript-app", "README.rst"): (
            "Local development\n"
            "Install dependencies with `npm ci`. Then start the local server with `npm run dev`."
        ),
    })

    result = GithubGuidedSearch(connector).search(intent=_intent(), materials=[], actor_user_id="user-1")

    assert [item.title for item in result.results] == ["example/typescript-app"]
    assert connector.file_calls == [
        ("example/typescript-app", "README.md"),
        ("example/typescript-app", "README.rst"),
    ]


def test_local_runnable_reports_source_unavailable_when_no_readme_can_be_read() -> None:
    connector = FakeGithubConnector([_row("example/unavailable", 5)], {})

    with pytest.raises(GuidedResearchError) as caught:
        GithubGuidedSearch(connector).search(intent=_intent(), materials=[], actor_user_id=None)

    assert caught.value.code == "guided_github_unavailable"
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert len(connector.file_calls) == 3


def test_readme_is_not_fetched_without_local_runnable_constraint() -> None:
    connector = FakeGithubConnector([_row("example/repository", 6)], {})

    result = GithubGuidedSearch(connector).search(
        intent=_intent(local_runnable=False),
        materials=[],
        actor_user_id=None,
    )

    assert [item.title for item in result.results] == ["example/repository"]
    assert connector.file_calls == []
