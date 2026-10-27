from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from contextvars import copy_context
from threading import Thread
from typing import Any

from framework.harness.rag.models import FORBIDDEN_RAG_PLAN_KEYS
from framework.shared.json import stable_json_dumps, to_jsonable
from framework.shared.graph_identity import GraphExecutionIdentity


class LLMResearchRAGPlanCandidateWorker:
    """LLM-backed Research candidate worker for Harness RAG plan candidates."""

    def __init__(self, llm_call: Callable[[str], Any]) -> None:
        self._llm_call = llm_call

    def generate_candidate(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> dict[str, Any]:
        if task != "rag_plan_candidate":
            raise ValueError(f"unsupported RAG planner task: {task}")
        prompt = _planner_prompt(payload)
        kwargs: dict[str, Any] = {}
        if execution_identity is not None:
            kwargs["execution_identity"] = execution_identity
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        if max_transport_attempts is not None:
            kwargs["max_transport_attempts"] = max_transport_attempts
        response = _run_maybe_async(self._llm_call(prompt, **kwargs))
        parsed = _extract_json_object(str(response or ""))
        forbidden = _find_forbidden_fields(parsed)
        if forbidden:
            raise ValueError(
                "RAG planner LLM response contains forbidden control fields: "
                + ", ".join(forbidden)
            )
        candidate = parsed.get("candidate") or parsed.get("retrieval_plan_candidate")
        if not isinstance(candidate, dict):
            raise ValueError("RAG planner LLM response must contain a candidate object")
        return {"candidate": candidate}


def _planner_prompt(payload: dict[str, Any]) -> str:
    safe_payload = to_jsonable(payload)
    return (
        "You are a bounded RAG retrieval planner worker. "
        "Return only JSON with one top-level key named candidate. "
        "The candidate must be a RetrievalPlanCandidate for Harness validation. "
        "Do not include workflow routing, quality pass/fail, memory writes, publication, "
        "tool authorization, or halt decisions. Avoid repeated queries listed in executed_queries.\n\n"
        "Planner request JSON:\n"
        f"{stable_json_dumps(safe_payload)}\n\n"
        "Required response shape:\n"
        '{"candidate":{"candidate_id":"...","queries":[{"step_id":"...",'
        '"operation":"search_corpus","query":"...","corpus":"research-papers",'
        '"max_results":3,"metadata":{"evidence_type":"..."}}],'
        '"expected_evidence":["..."],"expected_gaps":["..."],'
        '"confidence":0.0,"metadata":{"planner":"llm"}}}'
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("RAG planner LLM response is empty")
    for candidate in (stripped, _strip_fenced_json(stripped), _slice_json_object(stripped)):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            raise ValueError("RAG planner LLM response must be a JSON object")
        return payload
    raise ValueError("RAG planner LLM response is not valid JSON")


def _strip_fenced_json(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _slice_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start:end + 1]


def _find_forbidden_fields(value: Any, *, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if key_text in FORBIDDEN_RAG_PLAN_KEYS:
                found.append(item_path)
            found.extend(_find_forbidden_fields(item, path=item_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_fields(item, path=f"{path}[{index}]"))
    return tuple(found)


def _run_maybe_async(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result: dict[str, Any] = {}

    def _target() -> None:
        try:
            result["value"] = asyncio.run(value)
        except BaseException as exc:  # pragma: no cover - re-raised in caller thread
            result["error"] = exc

    # The worker thread inherits the caller's context so request-scoped
    # bindings (model credential, budget, attempt) stay visible to the model call.
    caller_context = copy_context()
    thread = Thread(target=lambda: caller_context.run(_target), daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


__all__ = ["LLMResearchRAGPlanCandidateWorker"]
