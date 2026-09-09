from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import re
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from backend.projects.dto import ProjectListQuery
from backend.research.application.guided_research import GuidedResearchApplication
from backend.research.ports.guided_research import (
    GuidedConstraints,
    GuidedIntent,
    GuidedIntentCommand,
    GuidedMaterial,
    GuidedPreviousResult,
    GuidedPreviousTurn,
    GuidedResearchError,
    GuidedResult,
    GuidedSearchCommand,
    GuidedSearchResponse,
)
from interfaces.services.project_service import ProjectApplicationService
from interfaces.services.research_conversation_model import ConversationTurn
from interfaces.services.research_history_service import ResearchHistoryService
from interfaces.services.research_service import ResearchActorInput, ResearchApplicationService


class GuidedIntentRequest(Protocol):
    question: str
    answers: Sequence[str]
    previous: Sequence[ConversationTurn]
    materialIds: Sequence[str]
    skipClarification: bool
    requestedSources: Sequence[str] | None
    constraints: GuidedConstraints | None
    publicSources: Sequence[Any]


class GuidedResearchService:
    def __init__(
        self,
        *,
        application: GuidedResearchApplication,
        history: ResearchHistoryService,
        research: ResearchApplicationService,
    ) -> None:
        self._application = application
        self._history = history
        self._research = research

    def interpret(self, request: GuidedIntentRequest, *, user_id: str | None) -> dict[str, Any]:
        materials = self._resolve_materials(request.materialIds, request.publicSources, user_id=user_id)
        command = GuidedIntentCommand(
            question=request.question,
            answers=list(request.answers),
            previous=[_project_previous(turn) for turn in request.previous[-19:]],
            materials=materials,
            skipClarification=request.skipClarification,
            requestedSources=list(request.requestedSources) if request.requestedSources else None,
            constraints=request.constraints,
        )
        result = self._application.interpret(command)
        payload = result.model_dump(mode="json", exclude_none=True)
        payload["clarification"] = (
            result.clarification.model_dump(mode="json") if result.clarification is not None else None
        )
        return payload

    def search(
        self,
        *,
        source: str,
        intent: GuidedIntent,
        material_ids: Sequence[str],
        public_sources: Sequence[Any],
        user_id: str | None,
    ) -> dict[str, Any]:
        materials = self._resolve_materials(material_ids, public_sources, user_id=user_id)
        response = self._application.search(
            GuidedSearchCommand(source=source, intent=intent, materials=materials),
            actor_user_id=user_id,
        )
        return response.model_dump(mode="json", exclude_none=True)

    def _resolve_materials(
        self,
        material_ids: Sequence[str],
        public_sources: Sequence[Any],
        *,
        user_id: str | None,
    ) -> list[GuidedMaterial]:
        identifiers = list(material_ids)
        public = [_public_material(item) for item in public_sources]
        if len({item.id for item in public}) != len(public):
            raise GuidedResearchError("invalid_request", "公开资料列表包含重复内容。", status_code=400, user_action_required=True)
        if not identifiers:
            return public
        if len(set(identifiers)) != len(identifiers):
            raise GuidedResearchError("invalid_request", "资料列表包含重复内容。", status_code=400, user_action_required=True)
        if user_id is None:
            raise GuidedResearchError(
                "guided_material_login_required",
                "登录后才能在研究中使用已保存的资料。",
                status_code=401,
                user_action_required=True,
            )
        try:
            snapshot = self._history.read(user_id=user_id)
        except Exception as exc:
            raise GuidedResearchError(
                "guided_materials_unavailable",
                "暂时无法读取已保存的资料，请稍后重试。",
                status_code=503,
                retryable=True,
            ) from exc
        owned = {
            str(item.get("id")): item
            for item in snapshot.workspace_items.get("materials", [])
            if isinstance(item, Mapping) and item.get("id")
        }
        if any(identifier not in owned for identifier in identifiers):
            raise GuidedResearchError(
                "guided_material_not_found",
                "有些资料不存在或当前账号无权使用。",
                status_code=404,
                user_action_required=True,
            )
        if set(identifiers) & {item.id for item in public}:
            raise GuidedResearchError("invalid_request", "资料标识不能重复。", status_code=400, user_action_required=True)
        actor = ResearchActorInput(user_id=user_id)
        resolved: list[GuidedMaterial] = []
        for identifier in identifiers:
            item = owned[identifier]
            kind = str(item["kind"])
            reference_id = str(item.get("referenceId") or "").strip()
            excerpt = ""
            if kind in {"paper", "pdf"} and reference_id:
                try:
                    excerpt = _document_excerpt(self._research.get_document(reference_id, actor=actor))
                except Exception:
                    excerpt = ""
            if kind == "pdf" and not excerpt:
                raise GuidedResearchError("guided_pdf_unavailable", "这份 PDF 的正文暂时不可用，请完成转换后再试，或先移除这份资料。", status_code=422, user_action_required=True)
            resolved.append(GuidedMaterial(
                id=identifier,
                kind=kind,
                title=str(item["title"]),
                url=_first_public_url(item.get("url")),
                notes=str(item.get("notes") or "")[:4000],
                excerpt=excerpt,
            ))
        return [*resolved, *public]


class ProjectGuidedSearch:
    def __init__(self, service: ProjectApplicationService, *, public_search=None) -> None:
        self._service = service
        self._public_search = public_search

    def search(self, *, intent: GuidedIntent, materials: Sequence[GuidedMaterial], actor_user_id: str | None) -> GuidedSearchResponse:
        if intent.constraints.localRunnable is True:
            # Legacy project profiles infer local_deployable from a repository URL.
            # Only the README-backed source can substantiate this user condition.
            if self._public_search is not None:
                return self._public_search.search(intent=intent, materials=materials, actor_user_id=actor_user_id)
            raise GuidedResearchError("guided_project_setup_unavailable", "暂时无法核实项目的本地运行说明，请稍后重试。", status_code=503, retryable=True)
        payload = self._service.list_projects(ProjectListQuery(q=intent.query, page=1, page_size=200, limit=200))
        meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
        if meta.get("source") == "none" or meta.get("data_state") == "empty":
            if self._public_search is not None:
                return self._public_search.search(intent=intent, materials=materials, actor_user_id=actor_user_id)
            raise GuidedResearchError(
                "guided_projects_source_unavailable",
                "项目数据源暂时不可用，请在 Project Radar 完成后重试。",
                status_code=503,
                retryable=True,
            )
        matched: list[GuidedResult] = []
        for card in payload.get("items", []):
            if not isinstance(card, Mapping):
                continue
            project_id = str(card.get("id") or "").strip()
            if not project_id:
                continue
            try:
                detail = self._service.get_project(project_id)
            except Exception:
                detail = {}
            if not _project_matches(intent.constraints, card=card, detail=detail):
                continue
            url = _first_public_url(card.get("github_url"), card.get("canonical_url"), card.get("website_url"))
            if url is None:
                continue
            metric = card.get("metric_summary") if isinstance(card.get("metric_summary"), Mapping) else {}
            stars = metric.get("github_stars")
            tags = [str(item) for item in card.get("tags", []) if isinstance(item, str)]
            target_id = str(card.get("slug") or project_id)
            href = f"/projects/{target_id}" if re.fullmatch(r"[A-Za-z0-9_-]+", target_id) else None
            matched.append(GuidedResult(
                id=project_id,
                kind="projects",
                title=str(card.get("name") or project_id),
                description=str(card.get("tagline") or card.get("description") or ""),
                source="GitHub" if urlsplit(url).hostname == "github.com" else str(urlsplit(url).hostname),
                url=url,
                href=href,
                language=_known_language(tags),
                stars=int(stars) if isinstance(stars, int) and not isinstance(stars, bool) and stars >= 0 else None,
            ))
        return GuidedSearchResponse(
            source="projects",
            results=matched[:10],
            total=len(matched),
            moreHref=_project_more_href(intent),
        )


def _project_more_href(intent: GuidedIntent) -> str | None:
    # The module only accepts language/license filters with identical semantics.
    # Do not offer a continuation that silently discards other confirmed filters.
    constraints = intent.constraints
    if constraints.hasCode or constraints.recentlyActive or constraints.localRunnable:
        return None
    query = {"q": intent.query}
    if constraints.language:
        query["language"] = constraints.language
    if constraints.license:
        query["license"] = constraints.license
    return f"/projects?{urlencode(query)}"


def _project_previous(turn: ConversationTurn) -> GuidedPreviousTurn:
    intent = turn.intent
    results: list[GuidedPreviousResult] = []
    for search in turn.searches:
        for item in search.results:
            results.append(GuidedPreviousResult(id=item.id, kind=item.kind, title=item.title, source=item.source, url=item.url))
    return GuidedPreviousTurn(
        question=turn.question,
        answers=list(turn.answers),
        summary=intent.summary if intent else None,
        query=intent.query if intent else None,
        sources=list(intent.sources) if intent else [],
        constraints=GuidedConstraints.model_validate(intent.constraints.model_dump(mode="json")) if intent else GuidedConstraints(),
        results=results[:20],
    )


def _public_material(value: Any) -> GuidedMaterial:
    kind = str(getattr(value, "kind", ""))
    url = _first_public_url(getattr(value, "url", ""))
    if kind != "note" and url is None:
        raise GuidedResearchError(
            "invalid_public_source",
            "公开资料需要有效的来源链接。",
            status_code=400,
            user_action_required=True,
        )
    return GuidedMaterial(
        id=str(getattr(value, "id", "")),
        kind=kind,
        title=str(getattr(value, "title", "")),
        url=url,
        notes=str(getattr(value, "notes", ""))[:4000],
    )


def _document_excerpt(payload: Mapping[str, Any]) -> str:
    document = payload.get("document") if isinstance(payload.get("document"), Mapping) else {}
    parts: list[str] = []
    abstract = document.get("abstract")
    if isinstance(abstract, str) and abstract.strip():
        parts.append(abstract.strip())
    sections = document.get("sections") if isinstance(document.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, Mapping):
            continue
        title = str(section.get("title") or "").strip()
        text = str(section.get("text") or "").strip()
        if title or text:
            parts.append("\n".join(item for item in (title, text) if item))
        if sum(len(item) for item in parts) >= 6000:
            break
    return "\n\n".join(parts)[:6000]


def _project_matches(constraints: GuidedConstraints, *, card: Mapping[str, Any], detail: Mapping[str, Any]) -> bool:
    profile = detail.get("tool_profile") if isinstance(detail.get("tool_profile"), Mapping) else {}
    if constraints.hasCode is True:
        repository = _first_public_url(card.get("github_url"))
        if repository is None or urlsplit(repository).hostname != "github.com":
            return False
    if constraints.license and str(profile.get("license") or "").casefold() != constraints.license.casefold():
        return False
    tags = [str(item).casefold() for item in card.get("tags", []) if isinstance(item, str)]
    if constraints.language and constraints.language.casefold() not in tags:
        return False
    if constraints.recentlyActive is True:
        try:
            updated = datetime.fromisoformat(str(card.get("updated_at")).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=UTC)
            if updated < datetime.now(UTC) - timedelta(days=365):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _first_public_url(*values: Any) -> str | None:
    for raw in values:
        value = str(raw or "").strip()
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password and not any(char in value for char in "\\\r\n"):
            return value
    return None


def _known_language(tags: Sequence[str]) -> str | None:
    known = {"python", "typescript", "javascript", "rust", "go"}
    return next((tag for tag in tags if tag.casefold() in known), None)


__all__ = ["GuidedResearchService", "ProjectGuidedSearch", "GuidedConstraints", "GuidedIntent", "GuidedResearchError"]
