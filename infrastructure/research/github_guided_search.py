"""Source-backed repository search through the existing bounded GitHub connector."""
from __future__ import annotations

import re
from time import monotonic
from datetime import UTC, datetime, timedelta
from typing import Sequence
from urllib.parse import urlencode

from backend.research.ports.guided_research import GuidedIntent, GuidedMaterial, GuidedResearchError, GuidedResult, GuidedSearchResponse
from infrastructure.external.sources.github import GithubConnector
from infrastructure.external.sources.models import SourceDefinition


_README_PATHS = ("README.md", "README.rst", "README.txt")
_MAX_RUNNABILITY_CHECKS = 10
_MAX_README_CHARS = 100_000

_INSTALL_COMMANDS = (
    re.compile(r"^(?:(?:python(?:3(?:\.\d+)*)?|py)\s+-m\s+)?pip(?:3)?\s+install\s+\S+", re.IGNORECASE),
    re.compile(r"^(?:npm\s+(?:install|ci)|pnpm\s+install|yarn\s+install|bun\s+install)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:poetry\s+install|uv\s+sync|pdm\s+install)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:conda\s+(?:install|env\s+create)|mamba\s+(?:install|env\s+create))(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:cargo\s+build|go\s+mod\s+download|docker(?:-compose|\s+compose)\s+build)(?:\s|$)", re.IGNORECASE),
)
_RUN_COMMANDS = (
    re.compile(r"^(?:python(?:3(?:\.\d+)*)?|py)\s+-m\s+(?!pip(?:\s|$))\S+", re.IGNORECASE),
    re.compile(r"^(?:python(?:3(?:\.\d+)*)?|py)\s+[^\s]+\.py(?:\s|$)", re.IGNORECASE),
    re.compile(r"^node\s+[^\s]+\.(?:js|mjs|cjs)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:npm\s+(?:start|run\s+(?:dev|start|serve))|pnpm\s+(?:run\s+)?(?:dev|start|serve)|yarn\s+(?:dev|start|serve)|bun\s+(?:run\s+)?(?:dev|start|serve))(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:poetry|uv|pdm)\s+run\s+\S+", re.IGNORECASE),
    re.compile(r"^(?:cargo\s+run|go\s+run\s+\S+|docker(?:-compose|\s+compose)\s+up)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^make\s+(?:run|dev|start|serve)(?:\s|$)", re.IGNORECASE),
    re.compile(r"^(?:streamlit\s+run\s+\S+|jupyter\s+(?:lab|notebook)|uvicorn\s+\S+|flask\s+run|fastapi\s+(?:dev|run)\s+\S+)(?:\s|$)", re.IGNORECASE),
)


class GithubGuidedSearch:
    def __init__(self, connector: GithubConnector) -> None:
        self._connector = connector
        self._source = SourceDefinition(source_id="guided-github", name="GitHub", source_type="github", url="https://api.github.com")

    def search(self, *, intent: GuidedIntent, materials: Sequence[GuidedMaterial], actor_user_id: str | None) -> GuidedSearchResponse:
        del materials, actor_user_id
        constraints = intent.constraints
        # Only connector-controlled qualifiers are added; model text cannot inject qualifiers.
        words = " ".join(word for word in intent.query.replace('"', '').split() if ":" not in word)
        query = f"{words} archived:false is:public"
        if constraints.language:
            query += f" language:{constraints.language}"
        if constraints.license:
            query += f" license:{constraints.license}"
        if constraints.recentlyActive:
            query += f" pushed:>={(datetime.now(UTC) - timedelta(days=365)).date()}"
        rows, errors = self._connector.search_repositories(self._source, query=query, limit=10)
        if errors and any(_error_type(error) != "empty_github_repositories" for error in errors):
            raise GuidedResearchError("guided_github_unavailable", "GitHub 暂时无法查询，请稍后重试。", status_code=503, retryable=True)
        eligible_rows = [row for row in rows if not row.archived and not row.disabled and row.visibility in {None, "public"}]
        if constraints.localRunnable:
            eligible_rows = self._filter_local_runnable(eligible_rows)
        results = [GuidedResult(
            id=f"github-{row.repository_id}" if row.repository_id else row.full_name,
            kind="projects", title=row.full_name, description=(row.description or "")[:2000],
            source="GitHub · 已核对安装与启动说明" if constraints.localRunnable else "GitHub", url=row.html_url, language=row.language, stars=row.stargazers_count,
        ) for row in eligible_rows]
        return GuidedSearchResponse(source="projects", results=results, total=len(results), moreHref=f"/projects?{urlencode({'q': intent.query})}")

    def _filter_local_runnable(self, rows: Sequence[object]) -> list[object]:
        verified: list[object] = []
        evidence_response_seen = False
        attempted = 0
        deadline = monotonic() + 30
        for row in rows[:_MAX_RUNNABILITY_CHECKS]:
            if monotonic() >= deadline:
                break
            attempted += 1
            readme, readable = self._read_readme(str(getattr(row, "full_name")), deadline=deadline)
            evidence_response_seen = evidence_response_seen or readable
            if readme is not None and _has_explicit_local_setup(readme[:_MAX_README_CHARS]):
                verified.append(row)
        if attempted and not evidence_response_seen:
            raise GuidedResearchError(
                "guided_github_unavailable",
                "GitHub 暂时无法核实项目的本地运行说明，请稍后重试。",
                status_code=503,
                retryable=True,
            )
        return verified

    def _read_readme(self, repository: str, *, deadline: float) -> tuple[str | None, bool]:
        for path in _README_PATHS:
            if monotonic() >= deadline:
                return None, False
            content, errors = self._connector.fetch_repository_file(
                self._source,
                repository=repository,
                path=path,
            )
            if errors:
                continue
            if content is None or content == "__directory__":
                continue
            return str(content), True
        return None, False


def _has_explicit_local_setup(readme: str) -> bool:
    commands = _readme_commands(readme)
    return any(pattern.match(command) for pattern in _INSTALL_COMMANDS for command in commands) and any(
        pattern.match(command) for pattern in _RUN_COMMANDS for command in commands
    )


def _readme_commands(readme: str) -> tuple[str, ...]:
    commands: list[str] = []
    for raw_line in readme.splitlines():
        candidates = [raw_line, *re.findall(r"`([^`\r\n]+)`", raw_line)]
        for candidate in candidates:
            line = re.sub(r"^\s*(?:(?:[-*+] |\d+[.)] )|(?:\$|>|PS>)\s*)+", "", candidate).strip().strip("`").strip()
            for segment in re.split(r"\s*(?:&&|;)\s*", line):
                if segment:
                    commands.append(segment)
    return tuple(dict.fromkeys(commands))


def _error_type(error: object) -> str:
    return str(getattr(error, "error_type", getattr(error, "code", "")))
