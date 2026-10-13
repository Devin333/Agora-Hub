from __future__ import annotations

import re
from dataclasses import dataclass

from backend.research.rag.retrieval.paper_policy import classify_query_intent
from backend.research.rag.models import ResearchRetrievalGoal

_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class ResearchActorScope:
    tenant_id: str | None
    user_id: str | None
    memory_namespace: str

    def to_metadata(self) -> dict[str, str]:
        metadata = {"memory_namespace": self.memory_namespace}
        if self.tenant_id:
            metadata["tenant_id"] = self.tenant_id
        if self.user_id:
            metadata["user_id"] = self.user_id
        return metadata


class AskPaperUseCase:
    def build_retrieval_goal(self, goal: ResearchRetrievalGoal) -> ResearchRetrievalGoal:
        return goal

    def build_paper_ask_goal(
        self,
        *,
        paper_id: str,
        question: str,
        goal_id: str = "paper-rag-ask",
        memory_namespace: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        run_id: str | None = None,
        allowed_source_refs: list[str] | tuple[str, ...] | None = None,
        target_sections: list[str] | tuple[str, ...] | None = None,
    ) -> ResearchRetrievalGoal:
        intent = classify_query_intent(question)
        scope = self.resolve_actor_scope(
            tenant_id=tenant_id,
            user_id=user_id,
            memory_namespace=memory_namespace,
        )
        return ResearchRetrievalGoal(
            goal_id=goal_id,
            paper_id=paper_id,
            question=question,
            required_evidence_types=_required_evidence_types(intent),
            target_sections=_source_refs(target_sections, paper_id="") if target_sections else [],
            allowed_source_refs=_source_refs(allowed_source_refs, paper_id=paper_id),
            allowed_memory_namespaces=[scope.memory_namespace],
            metadata={
                "intent": intent,
                **({"run_id": _required_scope_text(run_id, "run_id")} if run_id else {}),
                **scope.to_metadata(),
            },
        )

    def resolve_actor_scope(
        self,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        memory_namespace: str | None = None,
    ) -> ResearchActorScope:
        tenant = _scope_id(tenant_id, "tenant_id")
        user = _scope_id(user_id, "user_id")
        namespace = _memory_namespace(
            memory_namespace,
            tenant_id=tenant,
            user_id=user,
        )
        return ResearchActorScope(
            tenant_id=tenant or None,
            user_id=user or None,
            memory_namespace=namespace,
        )


def _source_refs(values: list[str] | tuple[str, ...] | None, *, paper_id: str) -> list[str]:
    refs = [str(value).strip() for value in (values or ()) if str(value).strip()]
    if not refs:
        refs = [f"arxiv://{paper_id}", paper_id]
    return list(dict.fromkeys(refs))


def _required_scope_text(value: str | None, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _required_evidence_types(intent: str) -> list[str]:
    if intent in {"table_query", "numerical_result"}:
        return ["experiment"]
    if intent == "figure_query":
        return ["experiment"]
    if intent == "formula_query":
        return ["method"]
    if intent in {"citation_query", "contribution", "comparison"}:
        return ["claim_support"]
    return ["method"]


def _memory_namespace(
    namespace: str | None,
    *,
    tenant_id: str,
    user_id: str,
) -> str:
    requested = str(namespace or "").strip()
    if not requested:
        if tenant_id and user_id:
            requested = f"research:tenant:{tenant_id}:user:{user_id}"
        elif tenant_id:
            requested = f"research:tenant:{tenant_id}:public"
        elif user_id:
            requested = f"research:user:{user_id}"
        else:
            requested = "research.public"
    _validate_namespace(requested, tenant_id=tenant_id, user_id=user_id)
    return requested


def _validate_namespace(namespace: str, *, tenant_id: str, user_id: str) -> None:
    if not namespace.strip():
        raise ValueError("memory_namespace is required")
    if namespace.startswith("research:tenant:") and not tenant_id:
        raise ValueError("tenant_id is required for tenant memory_namespace")
    if tenant_id:
        tenant_prefix = f"research:tenant:{tenant_id}:"
        if not namespace.startswith(tenant_prefix):
            raise ValueError("memory_namespace is outside tenant scope")
        if user_id and namespace != f"{tenant_prefix}user:{user_id}":
            raise ValueError("memory_namespace is outside user scope")
        return
    if user_id and namespace != f"research:user:{user_id}":
        raise ValueError("memory_namespace is outside user scope")


def _scope_id(value: str | None, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not _SCOPE_ID_RE.fullmatch(text):
        raise ValueError(f"{field_name} contains unsupported characters")
    return text


__all__ = ["AskPaperUseCase", "ResearchActorScope"]
