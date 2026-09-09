from __future__ import annotations

import re

from backend.research.ports.guided_research import (
    GuidedClarification, GuidedConstraints, GuidedIntent, GuidedIntentCandidatePort,
    GuidedIntentCommand, GuidedResearchError, GuidedSearchCommand, GuidedSearchPort,
    GuidedSearchResponse, GuidedSourceUnavailableError, MAX_CLARIFICATION_ROUNDS,
    ResearchSource,
)


class GuidedResearchApplication:
    """Validate model candidates, then dispatch only confirmed deterministic searches."""

    def __init__(
        self,
        *,
        intent_candidate: GuidedIntentCandidatePort,
        paper_search: GuidedSearchPort,
        project_search: GuidedSearchPort,
    ) -> None:
        self._intent_candidate = intent_candidate
        self._search_ports: dict[ResearchSource, GuidedSearchPort] = {
            "papers": paper_search,
            "projects": project_search,
        }

    def interpret(self, command: GuidedIntentCommand) -> GuidedIntent:
        if not isinstance(command, GuidedIntentCommand):
            raise GuidedResearchError("invalid_request", "问题格式无效。", status_code=400, user_action_required=True)
        try:
            candidate = GuidedIntent.model_validate(
                self._intent_candidate.generate_candidate(
                    {
                        "question": command.question,
                        "answers": list(command.answers),
                        "previous": [turn.model_dump(mode="json", exclude_none=True) for turn in command.previous],
                        "materials": [material.model_dump(mode="json") for material in command.materials],
                        "skip_clarification": command.skipClarification,
                        "requested_sources": sorted(_effective_sources(command)),
                        "constraints": {**_previous_constraints(command), **(command.constraints or GuidedConstraints()).compact()},
                    }
                )
            )
        except GuidedResearchError:
            raise
        except Exception as exc:
            raise GuidedResearchError(
                "guided_intent_unavailable",
                "暂时无法理解这个问题，请重试或换一种说法。",
                status_code=503,
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc
        return self._gate_intent(command, candidate)

    def search(
        self,
        command: GuidedSearchCommand,
        *,
        actor_user_id: str | None,
    ) -> GuidedSearchResponse:
        if not isinstance(command, GuidedSearchCommand):
            raise GuidedResearchError("invalid_request", "查找条件无效。", status_code=400, user_action_required=True)
        if command.source not in command.intent.sources:
            raise GuidedResearchError(
                "guided_source_not_confirmed",
                "请先确认要查找的内容类型。",
                status_code=422,
                user_action_required=True,
            )
        try:
            response = self._search_ports[command.source].search(
                intent=command.intent,
                materials=command.materials,
                actor_user_id=actor_user_id,
            )
            validated = response if isinstance(response, GuidedSearchResponse) else GuidedSearchResponse.model_validate(response)
        except GuidedResearchError:
            raise
        except GuidedSourceUnavailableError as exc:
            label = "论文" if command.source == "papers" else "项目"
            raise GuidedResearchError(
                f"guided_{command.source}_source_unavailable",
                f"{label}数据源暂时不可用，请稍后重试。",
                status_code=503,
                retryable=True,
            ) from exc
        except Exception as exc:
            label = "论文" if command.source == "papers" else "项目"
            raise GuidedResearchError(
                f"guided_{command.source}_search_failed",
                f"{label}暂时查找失败，请稍后重试。",
                status_code=503,
                retryable=True,
            ) from exc
        if validated.source != command.source:
            raise GuidedResearchError("guided_search_contract_invalid", "查找结果类型不匹配。", status_code=502)
        return validated

    @staticmethod
    def _gate_intent(command: GuidedIntentCommand, candidate: GuidedIntent) -> GuidedIntent:
        if not _concise_query(candidate.query):
            raise GuidedResearchError(
                "guided_intent_candidate_rejected",
                "查找关键词不够简洁，请重试。",
                status_code=502,
                retryable=True,
            )
        required_sources = _effective_sources(command)
        if required_sources and set(candidate.sources) != required_sources:
            raise GuidedResearchError(
                "guided_intent_candidate_rejected",
                "查找类型没有按已选内容保留，请重试。",
                status_code=502,
                retryable=True,
            )
        requested_constraints = (command.constraints or GuidedConstraints()).compact()
        previous_constraints = _previous_constraints(command)
        # Omitted fields preserve the prior choice. An explicit null proposes
        # clearing a condition and must pass the visible confirmation below.
        actual_constraints = {**previous_constraints, **candidate.constraints.model_dump(mode="json", exclude_unset=True)}
        if any(actual_constraints.get(key) != value for key, value in requested_constraints.items()):
            raise GuidedResearchError(
                "guided_intent_candidate_rejected",
                "已选择的查找条件没有被保留，请重试。",
                status_code=502,
                retryable=True,
            )
        clarification = candidate.clarification
        if command.skipClarification or len(command.answers) >= MAX_CLARIFICATION_ROUNDS:
            clarification = None
        elif required_sources and clarification is not None and _asks_source_choice(clarification):
            raise GuidedResearchError(
                "guided_intent_candidate_rejected",
                "不应重复询问已经说明的内容类型。",
                status_code=502,
                retryable=True,
            )
        changed = [key for key, value in previous_constraints.items() if value and actual_constraints.get(key) != value]
        notice = "；".join(_constraint_change(key, previous_constraints[key], actual_constraints.get(key)) for key in changed)
        return candidate.model_copy(update={
            "clarification": clarification,
            "constraints": GuidedConstraints.model_validate(actual_constraints),
            "confirmationRequired": bool(changed),
            "changeNotice": f"这次将调整：{notice}。" if notice else None,
        })


def _previous_constraints(command: GuidedIntentCommand) -> dict:
    return command.previous[-1].constraints.compact() if command.previous else {}


def _effective_sources(command: GuidedIntentCommand) -> set[ResearchSource]:
    if command.requestedSources:
        return set(command.requestedSources)
    for answer in reversed(command.answers):
        if sources := _explicit_sources(answer):
            return sources
    return _explicit_sources(command.question) or (set(command.previous[-1].sources) if command.previous else set())


def _constraint_change(key: str, previous: object, current: object) -> str:
    labels = {"recentYear": "时间", "hasCode": "代码", "paperType": "论文类型", "language": "编程语言", "license": "许可证", "recentlyActive": "项目活跃度", "localRunnable": "本地运行"}
    before = {"recentYear": "最近一年", "hasCode": "需要附带代码", "paperType": "综述", "recentlyActive": "近期活跃", "localRunnable": "需要运行说明"}.get(key, str(previous))
    after = str(current) if current else "不限"
    return f"{labels[key]}从“{before}”改为“{after}”"


def _explicit_sources(text: str) -> set[ResearchSource]:
    value = text.casefold()
    sources: set[ResearchSource] = set()
    # A request for papers with open-source code is still a paper request.
    # Repository hosts and the word "open source" alone are not content types.
    value = re.sub(r"(?:不要|不看|不找|排除)\s*(?:论文|文献|项目|仓库)", "", value)
    if re.search(r"论文|文献|\bpapers?\b|arxiv", value):
        sources.add("papers")
    if re.search(r"项目|仓库|\brepo(?:sitory|s)?\b|\bprojects?\b", value):
        sources.add("projects")
    return sources


def _asks_source_choice(clarification: GuidedClarification) -> bool:
    question = clarification.question.casefold()
    options = " ".join(clarification.options).casefold()
    asks_type = bool(
        re.search(r"(?:想|要|选择|偏向|优先).{0,12}(?:论文|文献|项目|仓库)|(?:论文|文献).{0,8}(?:还是|或).{0,8}(?:项目|仓库)", question)
    )
    option_types = _explicit_sources(options)
    return asks_type or option_types == {"papers", "projects"}


def _concise_query(value: str) -> bool:
    query = value.strip()
    if len(query) > 160:
        return False
    tokens = re.findall(r"[A-Za-z0-9_.+-]+|[\u4e00-\u9fff]+", query)
    return len(tokens) <= 12 and not bool(re.search(r"请(?:帮我)?|我想要|帮我找|给我找|你能", query))


__all__ = ["GuidedResearchApplication"]
