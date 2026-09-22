from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from framework.agent.models import JudgeVerdict
from framework.events.trace import new_span_id, new_trace_id
from framework.llm.models import LLMRequest, LLMResponse, TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import to_jsonable as to_json_safe
from framework.tool import ToolObservation
from framework.agent.runtime.redaction import redact_sensitive_values


@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    arguments_hash: str
    arguments_preview: dict[str, Any]

    @classmethod
    def from_call(
        cls,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        max_preview_chars: int,
    ) -> ToolCallSignature:
        safe_arguments = redact_sensitive_values(dict(arguments))
        encoded = _stable_json_bytes(safe_arguments)
        return cls(
            tool_name=tool_name,
            arguments_hash=hashlib.sha256(encoded).hexdigest(),
            arguments_preview=_preview_mapping(safe_arguments, max_preview_chars),
        )

    @property
    def key(self) -> str:
        return f"{self.tool_name}:{self.arguments_hash}"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallSignature:
        return cls(
            tool_name=str(payload.get("tool_name") or ""),
            arguments_hash=str(payload.get("arguments_hash") or ""),
            arguments_preview=dict(payload.get("arguments_preview") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments_hash": self.arguments_hash,
            "arguments_preview": dict(self.arguments_preview),
        }


@dataclass(frozen=True)
class LLMCallTrace:
    iteration: int
    llm_call_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    provider: str | None = None
    model: str | None = None
    route_id: str | None = None
    deployment_id: str | None = None
    cache_hit: bool | None = None
    fallback_used: bool | None = None
    fallback_count: int | None = None
    router_event_count: int | None = None
    provider_resolution_trace: list[dict[str, Any]] = field(default_factory=list)
    route_manifest: dict[str, Any] | None = None
    response_chars: int = 0

    @classmethod
    def from_response(cls, iteration: int, response: LLMResponse) -> LLMCallTrace:
        metadata = dict(response.metadata)
        usage = response.usage
        return cls(
            iteration=iteration,
            llm_call_id=_optional_text(metadata.get("llm_call_id")) or f"llm_call:{iteration}",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            provider=_optional_text(metadata.get("provider") or metadata.get("llm_provider")),
            model=_optional_text(metadata.get("model") or metadata.get("llm_model")),
            route_id=_optional_text(metadata.get("llm_route_id")),
            deployment_id=_optional_text(metadata.get("llm_deployment_id")),
            cache_hit=_optional_bool(metadata.get("llm_cache_hit")),
            fallback_used=_optional_bool(metadata.get("llm_fallback_used")),
            fallback_count=_optional_int(metadata.get("llm_fallback_count")),
            router_event_count=_optional_int(metadata.get("llm_router_event_count")),
            provider_resolution_trace=[
                dict(item)
                for item in metadata.get("llm_provider_resolution_trace", [])
                if isinstance(item, dict)
            ],
            route_manifest=(
                dict(metadata["llm_route_manifest"])
                if isinstance(metadata.get("llm_route_manifest"), dict)
                else None
            ),
            response_chars=len(response.content or ""),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LLMCallTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            llm_call_id=str(payload.get("llm_call_id") or f"llm_call:{payload.get('iteration') or 0}"),
            input_tokens=int(payload.get("input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
            provider=_optional_text(payload.get("provider")),
            model=_optional_text(payload.get("model")),
            route_id=_optional_text(payload.get("route_id")),
            deployment_id=_optional_text(payload.get("deployment_id")),
            cache_hit=_optional_bool(payload.get("cache_hit")),
            fallback_used=_optional_bool(payload.get("fallback_used")),
            fallback_count=_optional_int(payload.get("fallback_count")),
            router_event_count=_optional_int(payload.get("router_event_count")),
            provider_resolution_trace=[
                dict(item)
                for item in payload.get("provider_resolution_trace", [])
                if isinstance(item, dict)
            ],
            route_manifest=(
                dict(payload["route_manifest"])
                if isinstance(payload.get("route_manifest"), dict)
                else None
            ),
            response_chars=int(payload.get("response_chars") or 0),
        )

    def token_usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "llm_call_id": self.llm_call_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
            "model": self.model,
            "route_id": self.route_id,
            "deployment_id": self.deployment_id,
            "cache_hit": self.cache_hit,
            "fallback_used": self.fallback_used,
            "fallback_count": self.fallback_count,
            "router_event_count": self.router_event_count,
            "provider_resolution_trace": [
                dict(item) for item in self.provider_resolution_trace
            ],
            "route_manifest": dict(self.route_manifest) if self.route_manifest else None,
            "response_chars": self.response_chars,
        }


@dataclass(frozen=True)
class LLMErrorTrace:
    iteration: int
    error_type: str
    error_message: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LLMErrorTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            error_type=str(payload.get("error_type") or ""),
            error_message=str(payload.get("error_message") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True)
class ParserErrorTrace:
    iteration: int
    error_type: str
    error_message: str
    content_preview: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParserErrorTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            error_type=str(payload.get("error_type") or ""),
            error_message=str(payload.get("error_message") or ""),
            content_preview=str(payload.get("content_preview") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "content_preview": self.content_preview,
        }


@dataclass(frozen=True)
class ParsedActionTrace:
    iteration: int
    action_type: str
    tool_name: str | None = None
    skill_name: str | None = None
    output_keys: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParsedActionTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            action_type=str(payload.get("action_type") or ""),
            tool_name=_optional_text(payload.get("tool_name")),
            skill_name=_optional_text(payload.get("skill_name")),
            output_keys=[str(item) for item in payload.get("output_keys", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "action_type": self.action_type,
            "tool_name": self.tool_name,
            "skill_name": self.skill_name,
            "output_keys": list(self.output_keys),
        }


@dataclass(frozen=True)
class ToolCallTrace:
    iteration: int
    tool_call_id: str
    tool_name: str
    status: str
    summary: str
    elapsed_ms: float
    signature: ToolCallSignature
    error_type: str | None = None
    error_message: str | None = None
    output_bytes: int | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    safe_for_llm: bool = True

    @classmethod
    def from_observation(
        cls,
        *,
        iteration: int,
        observation: ToolObservation,
        max_preview_chars: int,
    ) -> ToolCallTrace:
        return cls(
            iteration=iteration,
            tool_call_id=observation.call.call_id,
            tool_name=observation.call.tool_name,
            status=observation.status.value,
            summary=observation.summary,
            elapsed_ms=observation.elapsed_ms,
            signature=ToolCallSignature.from_call(
                tool_name=observation.call.tool_name,
                arguments=observation.call.arguments,
                max_preview_chars=max_preview_chars,
            ),
            error_type=observation.result.error_type,
            error_message=observation.result.error_message,
            output_bytes=observation.result.output_bytes,
            artifact_refs=[
                artifact_ref.to_dict()
                for artifact_ref in observation.result.artifact_refs
            ],
            safe_for_llm=observation.safe_for_llm,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ToolCallTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            tool_name=str(payload.get("tool_name") or ""),
            status=str(payload.get("status") or ""),
            summary=str(payload.get("summary") or ""),
            elapsed_ms=float(payload.get("elapsed_ms") or 0.0),
            signature=ToolCallSignature.from_dict(dict(payload.get("signature") or {})),
            error_type=_optional_text(payload.get("error_type")),
            error_message=_optional_text(payload.get("error_message")),
            output_bytes=_optional_int(payload.get("output_bytes")),
            artifact_refs=[
                dict(item) for item in payload.get("artifact_refs", []) if isinstance(item, dict)
            ],
            safe_for_llm=bool(payload.get("safe_for_llm", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "summary": self.summary,
            "elapsed_ms": self.elapsed_ms,
            "signature": self.signature.to_dict(),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "output_bytes": self.output_bytes,
            "artifact_refs": [dict(ref) for ref in self.artifact_refs],
            "safe_for_llm": self.safe_for_llm,
        }


@dataclass(frozen=True)
class JudgeTrace:
    iteration: int
    decision: str
    confidence: float
    feedback: str | None = None
    missing_output_keys: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    policy_violations: list[str] = field(default_factory=list)
    structured_output_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    structured_output_contract: dict[str, Any] | None = None
    response_fingerprint: str | None = None

    @classmethod
    def from_verdict(cls, iteration: int, verdict: JudgeVerdict) -> JudgeTrace:
        return cls(
            iteration=iteration,
            decision=verdict.decision.value,
            confidence=verdict.confidence,
            feedback=verdict.feedback,
            missing_output_keys=list(verdict.missing_output_keys),
            schema_errors=list(verdict.schema_errors),
            validation_errors=list(verdict.validation_errors),
            policy_violations=list(verdict.policy_violations),
            structured_output_diagnostics=[
                dict(item) for item in verdict.structured_output_diagnostics
            ],
            structured_output_contract=(
                dict(verdict.structured_output_contract)
                if verdict.structured_output_contract is not None
                else None
            ),
            response_fingerprint=verdict.response_fingerprint,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> JudgeTrace:
        return cls(
            iteration=int(payload.get("iteration") or 0),
            decision=str(payload.get("decision") or ""),
            confidence=float(payload.get("confidence") or 0.0),
            feedback=_optional_text(payload.get("feedback")),
            missing_output_keys=[str(item) for item in payload.get("missing_output_keys", [])],
            schema_errors=[str(item) for item in payload.get("schema_errors", [])],
            validation_errors=[
                str(item) for item in payload.get("validation_errors", [])
            ],
            policy_violations=[str(item) for item in payload.get("policy_violations", [])],
            structured_output_diagnostics=[
                dict(item)
                for item in payload.get("structured_output_diagnostics", [])
                if isinstance(item, dict)
            ],
            structured_output_contract=(
                dict(payload["structured_output_contract"])
                if isinstance(payload.get("structured_output_contract"), dict)
                else None
            ),
            response_fingerprint=_optional_text(payload.get("response_fingerprint")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "decision": self.decision,
            "confidence": self.confidence,
            "feedback": self.feedback,
            "missing_output_keys": list(self.missing_output_keys),
            "schema_errors": list(self.schema_errors),
            "validation_errors": list(self.validation_errors),
            "policy_violations": list(self.policy_violations),
            "structured_output_diagnostics": [
                dict(item) for item in self.structured_output_diagnostics
            ],
            "structured_output_contract": (
                dict(self.structured_output_contract)
                if self.structured_output_contract is not None
                else None
            ),
            "response_fingerprint": self.response_fingerprint,
        }


@dataclass
class IterationTrace:
    iteration: int
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    feedback: str | None = None
    prompt_hash: str | None = None
    llm_call_id: str | None = None
    llm_artifact_ref: str | None = None
    tool_observation_count_before: int = 0
    tools_available: list[str] = field(default_factory=list)
    llm_call: LLMCallTrace | None = None
    llm_error: LLMErrorTrace | None = None
    parser_error: ParserErrorTrace | None = None
    parsed_action: ParsedActionTrace | None = None
    tool_call: ToolCallTrace | None = None
    judge: JudgeTrace | None = None
    stop_candidate: str | None = None
    duration_ms: float | None = None
    _started_monotonic: float | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> IterationTrace:
        llm_call = _optional_trace(payload.get("llm_call"), LLMCallTrace.from_dict)
        item = cls(
            iteration=int(payload.get("iteration") or 0),
            trace_id=_optional_text(payload.get("trace_id")),
            span_id=_optional_text(payload.get("span_id")),
            parent_span_id=_optional_text(payload.get("parent_span_id")),
            feedback=_optional_text(payload.get("feedback")),
            prompt_hash=_optional_text(payload.get("prompt_hash")),
            llm_call_id=_optional_text(payload.get("llm_call_id")),
            llm_artifact_ref=_optional_text(payload.get("llm_artifact_ref")),
            tool_observation_count_before=int(
                payload.get("tool_observation_count_before") or 0
            ),
            tools_available=[str(tool) for tool in payload.get("tools_available", [])],
            llm_call=llm_call,
            llm_error=_optional_trace(payload.get("llm_error"), LLMErrorTrace.from_dict),
            parser_error=_optional_trace(
                payload.get("parser_error"),
                ParserErrorTrace.from_dict,
            ),
            parsed_action=_optional_trace(
                payload.get("parsed_action"),
                ParsedActionTrace.from_dict,
            ),
            tool_call=_optional_trace(payload.get("tool_call"), ToolCallTrace.from_dict),
            judge=_optional_trace(
                payload.get("judge") or payload.get("judge_result"),
                JudgeTrace.from_dict,
            ),
            stop_candidate=_optional_text(payload.get("stop_candidate")),
            duration_ms=_optional_float(payload.get("duration_ms")),
        )
        if item.llm_call_id is None and llm_call is not None:
            item.llm_call_id = llm_call.llm_call_id
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "feedback": self.feedback,
            "prompt_hash": self.prompt_hash,
            "llm_call_id": self.llm_call_id,
            "llm_artifact_ref": self.llm_artifact_ref,
            "tool_observation_count_before": self.tool_observation_count_before,
            "tools_available": list(self.tools_available),
            "llm_call": self.llm_call.to_dict() if self.llm_call else None,
            "llm_error": self.llm_error.to_dict() if self.llm_error else None,
            "parser_error": self.parser_error.to_dict() if self.parser_error else None,
            "parsed_action": self.parsed_action.to_dict() if self.parsed_action else None,
            "tool_call": self.tool_call.to_dict() if self.tool_call else None,
            "tool_calls": [self.tool_call.to_dict()] if self.tool_call else [],
            "judge": self.judge.to_dict() if self.judge else None,
            "judge_result": self.judge.to_dict() if self.judge else None,
            "stop_candidate": self.stop_candidate,
            "duration_ms": self.duration_ms,
        }


@dataclass
class AgentLoopTrace:
    agent_id: str
    execution_identity: GraphExecutionIdentity | None = None
    trace_id: str | None = None
    root_span_id: str | None = None
    iterations: list[IterationTrace] = field(default_factory=list)
    llm_calls: list[LLMCallTrace] = field(default_factory=list)
    llm_errors: list[LLMErrorTrace] = field(default_factory=list)
    parser_errors: list[ParserErrorTrace] = field(default_factory=list)
    actions: list[ParsedActionTrace] = field(default_factory=list)
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    judges: list[JudgeTrace] = field(default_factory=list)
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)

    def start_iteration(
        self,
        iteration: int,
        *,
        feedback: str | None,
        tool_observation_count_before: int,
        tools_available: list[str],
    ) -> IterationTrace:
        trace_id = self.trace_id or new_trace_id()
        if self.trace_id is None:
            self.trace_id = trace_id
        root_span_id = self.root_span_id or new_span_id()
        if self.root_span_id is None:
            self.root_span_id = root_span_id
        item = IterationTrace(
            iteration=iteration,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=root_span_id,
            feedback=feedback,
            tool_observation_count_before=tool_observation_count_before,
            tools_available=list(tools_available),
            _started_monotonic=time.perf_counter(),
        )
        self.iterations.append(item)
        return item

    def finish_iteration(self, iteration: IterationTrace) -> None:
        if iteration.duration_ms is not None:
            return
        if iteration._started_monotonic is None:
            return
        iteration.duration_ms = round(
            (time.perf_counter() - iteration._started_monotonic) * 1000,
            3,
        )

    def finish_open_iterations(self) -> None:
        for iteration in self.iterations:
            self.finish_iteration(iteration)

    def record_llm_call(self, iteration: IterationTrace, response: LLMResponse) -> LLMCallTrace:
        trace = LLMCallTrace.from_response(iteration.iteration, response)
        iteration.llm_call = trace
        iteration.llm_call_id = trace.llm_call_id
        self.llm_calls.append(trace)
        return trace

    def record_prompt(self, iteration: IterationTrace, request: LLMRequest) -> str:
        prompt_hash = hashlib.sha256(_stable_json_bytes(request.to_dict(redact=True))).hexdigest()
        iteration.prompt_hash = prompt_hash
        return prompt_hash

    def record_llm_artifact(self, iteration: IterationTrace, artifact_id: str) -> None:
        iteration.llm_artifact_ref = artifact_id

    def record_llm_error(self, iteration: IterationTrace, exc: Exception) -> LLMErrorTrace:
        trace = LLMErrorTrace(
            iteration=iteration.iteration,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        iteration.llm_error = trace
        self.llm_errors.append(trace)
        return trace

    def record_parser_error(
        self,
        iteration: IterationTrace,
        *,
        exc: Exception,
        content: str,
        max_preview_chars: int,
    ) -> ParserErrorTrace:
        trace = ParserErrorTrace(
            iteration=iteration.iteration,
            error_type=type(exc).__name__,
            error_message=str(exc),
            content_preview=_preview_text(content, max_preview_chars),
        )
        iteration.parser_error = trace
        self.parser_errors.append(trace)
        return trace

    def record_action(
        self,
        iteration: IterationTrace,
        *,
        action_type: str,
        tool_name: str | None = None,
        output: dict[str, Any] | None = None,
    ) -> ParsedActionTrace:
        trace = ParsedActionTrace(
            iteration=iteration.iteration,
            action_type=action_type,
            tool_name=tool_name,
            skill_name=tool_name if action_type == "skill_call" else None,
            output_keys=sorted((output or {}).keys()),
        )
        iteration.parsed_action = trace
        self.actions.append(trace)
        return trace

    def record_tool_call(
        self,
        iteration: IterationTrace,
        observation: ToolObservation,
        *,
        max_preview_chars: int,
    ) -> ToolCallTrace:
        trace = ToolCallTrace.from_observation(
            iteration=iteration.iteration,
            observation=observation,
            max_preview_chars=max_preview_chars,
        )
        iteration.tool_call = trace
        self.tool_calls.append(trace)
        return trace

    def record_judge(self, iteration: IterationTrace, verdict: JudgeVerdict) -> JudgeTrace:
        trace = JudgeTrace.from_verdict(iteration.iteration, verdict)
        iteration.judge = trace
        self.judges.append(trace)
        return trace

    def record_memory_candidate(self, candidate: dict[str, Any]) -> None:
        if not isinstance(candidate, dict):
            raise TypeError("memory candidate must be an object")
        self.memory_candidates.append(dict(candidate))

    def mark_stop_candidate(self, iteration: IterationTrace, reason: str) -> None:
        iteration.stop_candidate = reason

    def count_tool_signature(self, signature_key: str) -> int:
        return sum(1 for call in self.tool_calls if call.signature.key == signature_key)

    def consecutive_parser_errors(self) -> int:
        count = 0
        for iteration in reversed(self.iterations):
            if iteration.parser_error is None:
                break
            count += 1
        return count

    def consecutive_tool_failures(self) -> int:
        count = 0
        for call in reversed(self.tool_calls):
            if call.status not in {"failed", "timeout", "blocked"}:
                break
            count += 1
        return count

    def consecutive_judge_retries(self) -> int:
        count = 0
        for judge in reversed(self.judges):
            if judge.decision != "retry":
                break
            count += 1
        return count

    def repeated_structured_output_rejection(self) -> bool:
        if len(self.judges) < 2:
            return False
        previous, current = self.judges[-2:]
        if previous.decision != "retry" or current.decision != "retry":
            return False
        if not previous.response_fingerprint or (
            previous.response_fingerprint != current.response_fingerprint
        ):
            return False
        return bool(current.structured_output_diagnostics) and (
            _diagnostic_identity(previous.structured_output_diagnostics)
            == _diagnostic_identity(current.structured_output_diagnostics)
        )

    def summary(self) -> dict[str, Any]:
        repeated_signatures = {}
        for call in self.tool_calls:
            repeated_signatures[call.signature.key] = repeated_signatures.get(call.signature.key, 0) + 1
        repeated_tool_calls = [
            {
                "signature": signature,
                "count": count,
            }
            for signature, count in sorted(repeated_signatures.items())
            if count > 1
        ]
        return {
            "agent_id": self.agent_id,
            "execution_identity": (
                None
                if self.execution_identity is None
                else self.execution_identity.to_dict()
            ),
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "iteration_count": len(self.iterations),
            "llm_call_count": len(self.llm_calls),
            "llm_error_count": len(self.llm_errors),
            "parser_error_count": len(self.parser_errors),
            "action_count": len(self.actions),
            "tool_call_count": len(self.tool_calls),
            "judge_count": len(self.judges),
            "judge_retry_count": sum(1 for judge in self.judges if judge.decision == "retry"),
            "tool_status_counts": self.tool_status_counts(),
            "repeated_tool_calls": repeated_tool_calls,
        }

    def tool_status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.tool_calls:
            counts[call.status] = counts.get(call.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "execution_identity": (
                None
                if self.execution_identity is None
                else self.execution_identity.to_dict()
            ),
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "iterations": [iteration.to_dict() for iteration in self.iterations],
            "trajectory": [item.to_dict() for item in self.trajectory()],
            "memory_candidates": [dict(item) for item in self.memory_candidates],
            "summary": self.summary(),
        }

    def trajectory(self) -> list["AgentIterationTrace"]:
        self.finish_open_iterations()
        return [AgentIterationTrace.from_iteration(item) for item in self.iterations]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AgentLoopTrace:
        raw_identity = payload.get("execution_identity")
        if raw_identity is None:
            summary = payload.get("summary")
            if isinstance(summary, dict):
                raw_identity = summary.get("execution_identity")
        execution_identity = None
        if raw_identity is not None:
            try:
                execution_identity = GraphExecutionIdentity.from_dict(raw_identity)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "AgentLoopTrace execution_identity is invalid"
                ) from exc
        trace = cls(
            agent_id=str(payload.get("agent_id") or ""),
            execution_identity=execution_identity,
            trace_id=_optional_text(payload.get("trace_id")),
            root_span_id=_optional_text(payload.get("root_span_id")),
        )
        raw_candidates = payload.get("memory_candidates", [])
        if not isinstance(raw_candidates, list) or any(
            not isinstance(candidate, dict) for candidate in raw_candidates
        ):
            raise ValueError("AgentLoopTrace memory_candidates must be an array of objects")
        trace.memory_candidates = [dict(candidate) for candidate in raw_candidates]
        trace.iterations = [
            IterationTrace.from_dict(item)
            for item in payload.get("iterations", [])
            if isinstance(item, dict)
        ]
        trace.llm_calls = [
            item.llm_call for item in trace.iterations if item.llm_call is not None
        ]
        trace.llm_errors = [
            item.llm_error for item in trace.iterations if item.llm_error is not None
        ]
        trace.parser_errors = [
            item.parser_error for item in trace.iterations if item.parser_error is not None
        ]
        trace.actions = [
            item.parsed_action
            for item in trace.iterations
            if item.parsed_action is not None
        ]
        trace.tool_calls = [
            item.tool_call for item in trace.iterations if item.tool_call is not None
        ]
        trace.judges = [item.judge for item in trace.iterations if item.judge is not None]
        return trace


@dataclass(frozen=True)
class AgentIterationTrace:
    iteration: int
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    llm_request_ref: str | None = None
    llm_response_ref: str | None = None
    parsed_action: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    memory_ops: list[dict[str, Any]] = field(default_factory=list)
    decision: str | None = None
    error: dict[str, Any] | None = None
    duration_ms: float | None = None

    @classmethod
    def from_iteration(cls, iteration: IterationTrace) -> "AgentIterationTrace":
        tool_calls = [iteration.tool_call.to_dict()] if iteration.tool_call else []
        observations = (
            [_tool_observation_summary(iteration.tool_call)]
            if iteration.tool_call is not None
            else []
        )
        return cls(
            iteration=iteration.iteration,
            trace_id=iteration.trace_id,
            span_id=iteration.span_id,
            parent_span_id=iteration.parent_span_id,
            llm_request_ref=iteration.llm_artifact_ref,
            llm_response_ref=iteration.llm_artifact_ref,
            parsed_action=(
                iteration.parsed_action.to_dict()
                if iteration.parsed_action is not None
                else {}
            ),
            tool_calls=tool_calls,
            observations=observations,
            decision=_iteration_decision(iteration),
            error=_iteration_error(iteration),
            duration_ms=iteration.duration_ms,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentIterationTrace":
        return cls(
            iteration=int(payload.get("iteration") or 0),
            trace_id=_optional_text(payload.get("trace_id")),
            span_id=_optional_text(payload.get("span_id")),
            parent_span_id=_optional_text(payload.get("parent_span_id")),
            llm_request_ref=_optional_text(payload.get("llm_request_ref")),
            llm_response_ref=_optional_text(payload.get("llm_response_ref")),
            parsed_action=dict(payload.get("parsed_action") or {}),
            tool_calls=[
                dict(item)
                for item in payload.get("tool_calls", [])
                if isinstance(item, dict)
            ],
            observations=[
                dict(item)
                for item in payload.get("observations", [])
                if isinstance(item, dict)
            ],
            memory_ops=[
                dict(item)
                for item in payload.get("memory_ops", [])
                if isinstance(item, dict)
            ],
            decision=_optional_text(payload.get("decision")),
            error=dict(payload["error"]) if isinstance(payload.get("error"), dict) else None,
            duration_ms=_optional_float(payload.get("duration_ms")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "llm_request_ref": self.llm_request_ref,
            "llm_response_ref": self.llm_response_ref,
            "parsed_action": dict(self.parsed_action),
            "tool_calls": [dict(item) for item in self.tool_calls],
            "observations": [dict(item) for item in self.observations],
            "memory_ops": [dict(item) for item in self.memory_ops],
            "decision": self.decision,
            "error": dict(self.error) if self.error is not None else None,
            "duration_ms": self.duration_ms,
        }


def _stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        to_json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _preview_mapping(value: Any, max_chars: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"value": _preview_text(str(value), max_chars)}
    preview: dict[str, Any] = {}
    remaining = max(0, max_chars)
    for key, item in value.items():
        if remaining <= 0:
            preview[str(key)] = "...truncated..."
            break
        preview_value = _preview_value(item, remaining)
        preview[str(key)] = preview_value
        remaining -= len(str(preview_value))
    return preview


def _preview_value(value: Any, max_chars: int) -> Any:
    safe_value = redact_sensitive_values(value)
    if isinstance(safe_value, dict):
        return _preview_mapping(safe_value, max_chars)
    if isinstance(safe_value, list):
        items = []
        remaining = max_chars
        for item in safe_value[:5]:
            if remaining <= 0:
                items.append("...truncated...")
                break
            preview = _preview_value(item, remaining)
            items.append(preview)
            remaining -= len(str(preview))
        if len(safe_value) > len(items):
            items.append(f"...{len(safe_value) - len(items)} more item(s)")
        return items
    return _preview_text(str(safe_value), max_chars)


def _preview_text(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars <= 3:
        return value[:max_chars]
    return f"{value[: max_chars - 3]}..."


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_trace(value: Any, factory: Any) -> Any:
    if not isinstance(value, dict):
        return None
    return factory(value)


def _tool_observation_summary(call: ToolCallTrace) -> dict[str, Any]:
    return {
        "tool_call_id": call.tool_call_id,
        "tool_name": call.tool_name,
        "status": call.status,
        "summary": call.summary,
        "elapsed_ms": call.elapsed_ms,
        "error_type": call.error_type,
        "error_message": call.error_message,
        "result": {
            "error_message": call.error_message,
            "error_type": call.error_type,
        },
        "artifact_refs": [dict(ref) for ref in call.artifact_refs],
        "safe_for_llm": call.safe_for_llm,
    }


def _iteration_decision(iteration: IterationTrace) -> str | None:
    if iteration.judge is not None:
        return iteration.judge.decision
    if iteration.stop_candidate:
        return iteration.stop_candidate
    if iteration.parsed_action is not None:
        return iteration.parsed_action.action_type
    return None


def _iteration_error(iteration: IterationTrace) -> dict[str, Any] | None:
    if iteration.llm_error is not None:
        return iteration.llm_error.to_dict()
    if iteration.parser_error is not None:
        return iteration.parser_error.to_dict()
    if iteration.tool_call is not None and iteration.tool_call.error_type:
        return {
            "iteration": iteration.iteration,
            "error_type": iteration.tool_call.error_type,
            "error_message": iteration.tool_call.error_message,
        }
    return None


def _diagnostic_identity(
    diagnostics: list[dict[str, Any]],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.get("code"),
            tuple(item.get("instance_path") or ()),
            tuple(item.get("schema_path") or ()),
            item.get("validator"),
        )
        for item in diagnostics
    )
