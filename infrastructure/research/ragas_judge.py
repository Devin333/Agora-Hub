"""Optional Ragas 0.4.3 semantic judge adapter for Research evaluation.

The adapter never constructs an OpenAI, LangChain, or provider client. Every
structured generation request is delegated to the injected bounded candidate
port, audited through the mandatory callback, and optionally connected to the
owning runner's shared budget hook. Ragas remains an optional dependency loaded only when
judge is called.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from backend.research.ports.evaluation_judge import (
    BoundedJudgeCandidatePort,
    CandidateUsage,
    CitationSupportCandidate,
    CitationSupportJudgeRequest,
    FactualJudgeRequest,
    FaithfulnessJudgeRequest,
    JudgeCandidateRequest,
    JudgeCapability,
    JudgeError,
    JudgeErrorCode,
    JudgeBudgetHookRecord,
    JudgeBudgetHookRequest,
    JudgeMetric,
    JudgeModality,
    JudgeObservationStatus,
    JudgeRuntimeProfile,
    JudgeScope,
    JudgeSubrequestCallbackPort,
    JudgeSubrequestRecord,
    JudgeUsageSummary,
    SemanticJudgeObservation,
    SemanticJudgeRequest,
    JudgeBudgetHookPort,
    content_digest,
)


ADAPTER_ID = "research.ragas_collections"
ADAPTER_VERSION = "1.0.0"
RAGAS_VERSION = "0.4.3"
RAGAS_PROMPT_VERSION = "ragas-collections-0.4.3"
CITATION_PROMPT_VERSION = "research-citation-support-v1"
SCHEMA_VERSION = "pydantic-json-schema-v1"
SCOPE_LIMITATION = (
    "Ragas 0.4.3 collections metrics are text-only. text_subset observations cannot "
    "satisfy required image, formula, or table review; faithfulness over actual model "
    "context does not validate source or OCR truth; reference_comparison observations "
    "compare only against the supplied reference and do not establish global factual truth."
)
EXPECTED_DEPENDENCIES = {
    "ragas": RAGAS_VERSION,
    "langchain": "0.3.27",
    "langchain-core": "0.3.79",
    "langchain-community": "0.3.31",
    "langchain-openai": "0.3.35",
    "langchain-text-splitters": "0.3.11",
    "instructor": "1.3.2",
    "openai": "1.109.1",
}

_ResponseModelT = TypeVar("_ResponseModelT", bound=BaseModel)


@dataclass(frozen=True)
class _Dependencies:
    instructor_base: type[Any]
    factual_correctness: type[Any]
    faithfulness: type[Any]
    versions: dict[str, str]


class _AdapterFailure(RuntimeError):
    def __init__(
        self,
        code: JudgeErrorCode,
        stage: str,
        message: str,
        *,
        retryable: bool,
        budget_exhausted: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable
        self.budget_exhausted = budget_exhausted


class _NoVerifiableClaims(RuntimeError):
    pass


@dataclass
class _CallState:
    request: SemanticJudgeRequest
    candidate_port: BoundedJudgeCandidatePort
    callback: JudgeSubrequestCallbackPort
    shared_budget_hook: JudgeBudgetHookPort | None
    started_at: float = field(default_factory=time.monotonic)
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    known_cost: float = 0.0
    cost_currency: str | None = None
    cost_unknown_reasons: list[str] = field(default_factory=list)
    records: list[JudgeSubrequestRecord] = field(default_factory=list)

    @property
    def shared_budget_hook_connected(self) -> bool:
        return self.shared_budget_hook is not None and self.request.shared_budget_key is not None

    def deadline_remaining(self) -> float:
        return self.request.budget.deadline_seconds - (time.monotonic() - self.started_at)

    def usage_summary(self) -> JudgeUsageSummary:
        if self.cost_unknown_reasons:
            cost = None
            currency = None
            reason = "; ".join(dict.fromkeys(self.cost_unknown_reasons))
        elif self.request_count == 0 and self.request.budget.max_cost is None:
            cost = None
            currency = None
            reason = self.request.budget.unknown_price_reason
        else:
            cost = self.known_cost
            currency = self.cost_currency or self.request.budget.currency
            reason = None
        return JudgeUsageSummary(
            requests=self.request_count,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.input_tokens + self.output_tokens,
            cost=cost,
            currency=currency,
            cost_unknown_reason=reason,
        )

    def runtime_identity(self) -> tuple[str | None, str | None, str | None, str | None]:
        identities = {
            (
                record.provider,
                record.model_id,
                record.model_revision,
                record.model_revision_unavailable_reason,
            )
            for record in self.records
            if record.provider is not None
        }
        if not identities:
            return None, None, None, None
        if len(identities) != 1:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT,
                "runtime_identity",
                "judge subrequests used inconsistent provider/model identities",
                retryable=False,
            )
        return next(iter(identities))

    def _remaining_tokens(self) -> int:
        return self.request.budget.max_total_tokens - self.input_tokens - self.output_tokens

    def _remaining_cost(self) -> float | None:
        if self.request.budget.max_cost is None:
            return None
        return self.request.budget.max_cost - self.known_cost

    async def call(self, prompt: str, response_model: type[_ResponseModelT]) -> _ResponseModelT:
        schema = response_model.model_json_schema()
        schema_name = response_model.__name__
        final_failure: _AdapterFailure | None = None
        attempts = self.request.budget.max_retries_per_subrequest + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._attempt(
                    prompt=prompt,
                    response_model=response_model,
                    schema=schema,
                    schema_name=schema_name,
                    attempt=attempt,
                )
            except _AdapterFailure as exc:
                final_failure = exc
                if not exc.retryable or attempt >= attempts:
                    break
        assert final_failure is not None
        if attempts > 1 and final_failure.retryable:
            raise _AdapterFailure(
                JudgeErrorCode.RETRY_EXHAUSTED,
                final_failure.stage,
                f"retry budget exhausted after {attempts} attempts: {final_failure}",
                retryable=False,
                budget_exhausted=final_failure.budget_exhausted,
            ) from final_failure
        raise final_failure

    async def _attempt(
        self,
        *,
        prompt: str,
        response_model: type[_ResponseModelT],
        schema: dict[str, Any],
        schema_name: str,
        attempt: int,
    ) -> _ResponseModelT:
        prompt = prompt.strip()
        deadline_remaining = self.deadline_remaining()
        remaining_tokens = self._remaining_tokens()
        remaining_cost = self._remaining_cost()
        logical_id = f"{self.request.observation_id}:{schema_name}:{len(self.records) + 1}"
        prompt_digest = content_digest(prompt)
        schema_digest = content_digest(schema)
        if deadline_remaining <= 0:
            return self._fail_before_call(
                request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                attempt=attempt, code=JudgeErrorCode.TIMEOUT,
                message="judge observation deadline was exhausted before subrequest",
            )
        if self.request_count >= self.request.budget.max_requests or remaining_tokens <= 0:
            return self._fail_before_call(
                request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                attempt=attempt, code=JudgeErrorCode.BUDGET_EXHAUSTED,
                message="judge request or token budget was exhausted before subrequest",
            )
        if remaining_cost is not None and remaining_cost < 0:
            return self._fail_before_call(
                request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                attempt=attempt, code=JudgeErrorCode.BUDGET_EXHAUSTED,
                message="judge cost budget was exhausted before subrequest",
            )
        if self.request.shared_budget_key is not None and self.shared_budget_hook is None:
            return self._fail_before_call(
                request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                attempt=attempt, code=JudgeErrorCode.BUDGET_HOOK_REJECTED,
                message="request requires a shared budget hook but no shared budget hook was injected",
            )

        timeout = min(self.request.budget.request_timeout_seconds, deadline_remaining)
        call_request = JudgeCandidateRequest(
            request_id=logical_id,
            observation_id=self.request.observation_id,
            metric=self.request.metric,
            task=f"ragas.{schema_name}",
            prompt=prompt,
            prompt_digest=prompt_digest,
            response_schema=schema,
            response_schema_digest=schema_digest,
            schema_name=schema_name,
            schema_version=SCHEMA_VERSION,
            generation_parameters=self.request.generation_parameters,
            timeout_seconds=float(timeout),
            max_transport_attempts=1,
            max_output_tokens=min(self.request.budget.max_output_tokens_per_request, remaining_tokens),
            remaining_total_tokens=remaining_tokens,
            remaining_cost=remaining_cost,
            currency=self.request.budget.currency,
        )
        if self.shared_budget_hook_connected:
            assert self.shared_budget_hook is not None
            assert self.request.shared_budget_key is not None
            try:
                permit = self.shared_budget_hook.authorize(
                    JudgeBudgetHookRequest(
                        budget_key=self.request.shared_budget_key,
                        observation_id=self.request.observation_id,
                        request_id=logical_id,
                        metric=self.request.metric,
                        attempt=attempt,
                        requested_token_limit=call_request.max_output_tokens,
                        requested_cost_limit=remaining_cost,
                        currency=self.request.budget.currency,
                        deadline_seconds_remaining=float(deadline_remaining),
                    )
                )
            except Exception as exc:
                return self._fail_before_call(
                    request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                    schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                    attempt=attempt, code=JudgeErrorCode.BUDGET_HOOK_REJECTED,
                    message=f"shared budget hook failed: {type(exc).__name__}: {exc}",
                )
            if not permit.allowed:
                return self._fail_before_call(
                    request_id=logical_id, prompt=prompt, prompt_digest=prompt_digest,
                    schema=schema, schema_digest=schema_digest, schema_name=schema_name,
                    attempt=attempt, code=JudgeErrorCode.BUDGET_HOOK_REJECTED,
                    message=permit.reason or "shared budget hook rejected judge request",
                    record_budget_hook=True,
                )

        self.request_count += 1
        started = time.monotonic()
        response = None
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self.candidate_port.generate_candidate, call_request),
                timeout=timeout,
            )
            duration_ms = float((time.monotonic() - started) * 1000.0)
            self._accept_usage(response.usage, call_request)
            if response.runtime_profile is not self.request.execution_profile:
                raise _AdapterFailure(
                    JudgeErrorCode.INVALID_OUTPUT,
                    "runtime_profile",
                    "candidate runtime profile does not match requested judge profile",
                    retryable=False,
                )
            try:
                raw_value = _decode_json_object(response.raw_output)
                parsed = response_model.model_validate(raw_value)
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                raise _AdapterFailure(
                    JudgeErrorCode.INVALID_OUTPUT,
                    "structured_output",
                    f"candidate output failed {schema_name} validation: {exc}",
                    retryable=True,
                ) from exc
            record = JudgeSubrequestRecord(
                request_id=logical_id, metric=self.request.metric, task=call_request.task,
                attempt=attempt, prompt=prompt, prompt_digest=prompt_digest,
                response_schema=schema, response_schema_digest=schema_digest,
                schema_name=schema_name, schema_version=SCHEMA_VERSION,
                raw_output=response.raw_output, parsed_output=parsed.model_dump(mode="json"),
                provider=response.provider, model_id=response.model_id,
                model_revision=response.model_revision,
                model_revision_unavailable_reason=response.model_revision_unavailable_reason,
                runtime_profile=response.runtime_profile, usage=response.usage,
                duration_ms=duration_ms, error_code=None, error_message=None, retryable=None,
            )
            self._record(record, budget_hook_error=None)
            return parsed
        except asyncio.TimeoutError as exc:
            failure = _AdapterFailure(
                JudgeErrorCode.TIMEOUT, "candidate_call",
                f"candidate call exceeded {timeout:.3f}s timeout", retryable=True,
            )
            self.cost_unknown_reasons.append("timed-out candidate call returned no provider usage")
            self._record_failure(call_request=call_request, attempt=attempt, started=started,
                                 response=response, failure=failure)
            raise failure from exc
        except _AdapterFailure as failure:
            self._record_failure(call_request=call_request, attempt=attempt, started=started,
                                 response=response, failure=failure)
            raise
        except Exception as exc:
            failure = _AdapterFailure(
                JudgeErrorCode.CANDIDATE_CALL_FAILED, "candidate_call",
                f"bounded candidate port failed: {type(exc).__name__}: {exc}", retryable=True,
            )
            self.cost_unknown_reasons.append("failed candidate call returned no provider usage")
            self._record_failure(call_request=call_request, attempt=attempt, started=started,
                                 response=response, failure=failure)
            raise failure from exc

    def _accept_usage(self, usage: CandidateUsage, request: JudgeCandidateRequest) -> None:
        # Actual provider usage is accounted before validating whether the injected
        # candidate port respected its authorization; overspend must not disappear.
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost is None:
            self.cost_unknown_reasons.append(usage.cost_unknown_reason or "provider cost unavailable")
        else:
            if self.cost_currency is not None and usage.currency != self.cost_currency:
                raise _AdapterFailure(
                    JudgeErrorCode.INVALID_OUTPUT, "usage",
                    "candidate calls reported inconsistent cost currencies", retryable=False,
                )
            self.cost_currency = usage.currency
            self.known_cost += usage.cost
            if self.request.budget.max_cost is not None and self.known_cost > self.request.budget.max_cost:
                raise _AdapterFailure(
                    JudgeErrorCode.BUDGET_EXHAUSTED, "usage",
                    "candidate port exceeded the observation cost budget",
                    retryable=False, budget_exhausted=True,
                )
        if usage.output_tokens > request.max_output_tokens:
            raise _AdapterFailure(
                JudgeErrorCode.BUDGET_EXHAUSTED, "usage",
                "candidate port exceeded the authorized output-token limit",
                retryable=False, budget_exhausted=True,
            )
        if self.input_tokens + self.output_tokens > self.request.budget.max_total_tokens:
            raise _AdapterFailure(
                JudgeErrorCode.BUDGET_EXHAUSTED, "usage",
                "candidate port exceeded the observation token budget",
                retryable=False, budget_exhausted=True,
            )

    def _record_failure(self, *, call_request: JudgeCandidateRequest, attempt: int,
                        started: float, response: Any, failure: _AdapterFailure) -> None:
        record = JudgeSubrequestRecord(
            request_id=call_request.request_id, metric=self.request.metric, task=call_request.task,
            attempt=attempt, prompt=call_request.prompt, prompt_digest=call_request.prompt_digest,
            response_schema=call_request.response_schema,
            response_schema_digest=call_request.response_schema_digest,
            schema_name=call_request.schema_name, schema_version=call_request.schema_version,
            raw_output=None if response is None else response.raw_output,
            parsed_output=None, provider=None if response is None else response.provider,
            model_id=None if response is None else response.model_id,
            model_revision=None if response is None else response.model_revision,
            model_revision_unavailable_reason=(
                None if response is None else response.model_revision_unavailable_reason
            ),
            runtime_profile=None if response is None else response.runtime_profile,
            usage=None if response is None else response.usage,
            duration_ms=float((time.monotonic() - started) * 1000.0),
            error_code=failure.code, error_message=str(failure), retryable=failure.retryable,
        )
        self._record(record, budget_hook_error=failure)

    def _fail_before_call(self, *, request_id: str, prompt: str, prompt_digest: str,
                          schema: dict[str, Any], schema_digest: str, schema_name: str,
                          attempt: int, code: JudgeErrorCode, message: str,
                          record_budget_hook: bool = False) -> Any:
        failure = _AdapterFailure(
            code, "budget_authorization", message, retryable=False,
            budget_exhausted=code is JudgeErrorCode.BUDGET_EXHAUSTED,
        )
        record = JudgeSubrequestRecord(
            request_id=request_id, metric=self.request.metric, task=f"ragas.{schema_name}",
            attempt=attempt, prompt=prompt, prompt_digest=prompt_digest,
            response_schema=schema, response_schema_digest=schema_digest,
            schema_name=schema_name, schema_version=SCHEMA_VERSION,
            raw_output=None, parsed_output=None, provider=None, model_id=None,
            model_revision=None, model_revision_unavailable_reason=None,
            runtime_profile=None, usage=None, duration_ms=0.0, error_code=code,
            error_message=message, retryable=False,
        )
        self.records.append(record)
        self.callback.on_subrequest(record)
        if record_budget_hook and self.shared_budget_hook_connected:
            assert self.shared_budget_hook is not None
            assert self.request.shared_budget_key is not None
            self.shared_budget_hook.record(JudgeBudgetHookRecord(
                budget_key=self.request.shared_budget_key,
                observation_id=self.request.observation_id, request_id=request_id,
                metric=self.request.metric, attempt=attempt, usage=None,
                error_code=code, error_message=message,
            ))
        raise failure

    def _record(self, record: JudgeSubrequestRecord,
                budget_hook_error: _AdapterFailure | None) -> None:
        self.records.append(record)
        self.callback.on_subrequest(record)
        if self.shared_budget_hook_connected:
            assert self.shared_budget_hook is not None
            assert self.request.shared_budget_key is not None
            self.shared_budget_hook.record(JudgeBudgetHookRecord(
                budget_key=self.request.shared_budget_key,
                observation_id=self.request.observation_id,
                request_id=record.request_id, metric=self.request.metric,
                attempt=record.attempt, usage=record.usage,
                error_code=None if budget_hook_error is None else budget_hook_error.code,
                error_message=None if budget_hook_error is None else str(budget_hook_error),
            ))


def _make_bridge(base: type[Any], state: _CallState) -> Any:
    class PortBackedInstructorLLM(base):
        def generate(self, prompt: str, response_model: type[_ResponseModelT]) -> _ResponseModelT:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(state.call(prompt, response_model))
            raise RuntimeError("use agenerate inside an active event loop")

        async def agenerate(self, prompt: str,
                            response_model: type[_ResponseModelT]) -> _ResponseModelT:
            return await state.call(prompt, response_model)

    return PortBackedInstructorLLM()


class RagasJudgeAdapter:
    """Text-only Ragas collections adapter using a Harness-owned candidate port."""

    def __init__(self, *, candidate_port: BoundedJudgeCandidatePort,
                 subrequest_callback: JudgeSubrequestCallbackPort,
                 shared_budget_hook: JudgeBudgetHookPort | None = None) -> None:
        self._candidate_port = candidate_port
        self._callback = subrequest_callback
        self._shared_budget_hook = shared_budget_hook
        self._capability = JudgeCapability(
            modalities=frozenset({JudgeModality.TEXT}),
            scopes=frozenset({JudgeScope.TEXT_SUBSET, JudgeScope.REFERENCE_COMPARISON}),
        )

    @property
    def capability(self) -> JudgeCapability:
        return self._capability

    async def judge(self, request: SemanticJudgeRequest) -> SemanticJudgeObservation:
        if any(modality not in self.capability.modalities for modality in request.required_modalities):
            return self._preflight_error(
                request, code=JudgeErrorCode.CAPABILITY_UNAVAILABLE, stage="capability",
                message=("required visual/table/formula evidence cannot be replaced by the "
                         "adapter's text_subset capability"),
                library_version=None,
                library_reason="Ragas was not loaded because declared capability was insufficient",
            )
        try:
            dependencies = _load_dependencies()
        except _AdapterFailure as exc:
            return self._preflight_error(
                request, code=exc.code, stage=exc.stage, message=str(exc),
                library_version=None, library_reason=str(exc),
            )

        state = _CallState(request=request, candidate_port=self._candidate_port,
                           callback=self._callback, shared_budget_hook=self._shared_budget_hook)
        bridge = _make_bridge(dependencies.instructor_base, state)
        try:
            if isinstance(request, FactualJudgeRequest):
                result, details = await self._judge_factual(request, bridge, dependencies, state)
            elif isinstance(request, FaithfulnessJudgeRequest):
                result, details = await self._judge_faithfulness(request, bridge, dependencies, state)
            elif isinstance(request, CitationSupportJudgeRequest):
                result, details = await self._judge_citation(request, bridge, state)
            else:
                raise _AdapterFailure(
                    JudgeErrorCode.INVALID_OUTPUT, "dispatch",
                    f"unsupported judge request type: {type(request).__name__}", retryable=False,
                )
            provider, model_id, revision, revision_reason = state.runtime_identity()
            needs_review = bool(details.get("requires_global_fact_review", False))
            status = (
                JudgeObservationStatus.NEEDS_REVIEW
                if needs_review
                else JudgeObservationStatus.OK
            )
            return SemanticJudgeObservation(
                observation_id=request.observation_id, metric=request.metric,
                metric_version=request.metric_version, status=status,
                value=None if needs_review else result[0],
                numerator=None if needs_review else result[1],
                denominator=None if needs_review else result[2],
                scope=request.scope, capability=self.capability,
                execution_profile=request.execution_profile, provider=provider,
                model_id=model_id, model_revision=revision,
                model_revision_unavailable_reason=revision_reason,
                adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                library_name="ragas", library_version=dependencies.versions["ragas"],
                library_unavailable_reason=None,
                prompt_version=(CITATION_PROMPT_VERSION if request.metric is JudgeMetric.CITATION_SUPPORT
                                else RAGAS_PROMPT_VERSION),
                rubric_version=(request.rubric_version if isinstance(request, CitationSupportJudgeRequest)
                                else None),
                rubric_digest=(request.rubric_digest if isinstance(request, CitationSupportJudgeRequest)
                               else None),
                generation_parameters=request.generation_parameters,
                input_digests=_input_digests(request), subrequests=tuple(state.records),
                raw_judgment=details, reasons=tuple(details.get("reasons", [])),
                usage=state.usage_summary(), evidence_references=request.evidence_references,
                error=None, shared_budget_hook_connected=state.shared_budget_hook_connected,
                scope_limitation=SCOPE_LIMITATION,
            )
        except _NoVerifiableClaims as exc:
            provider, model_id, revision, revision_reason = state.runtime_identity()
            return SemanticJudgeObservation(
                observation_id=request.observation_id, metric=request.metric,
                metric_version=request.metric_version,
                status=JudgeObservationStatus.NOT_APPLICABLE,
                value=None, numerator=None, denominator=None, scope=request.scope,
                capability=self.capability, execution_profile=request.execution_profile,
                provider=provider, model_id=model_id, model_revision=revision,
                model_revision_unavailable_reason=revision_reason,
                adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                library_name="ragas", library_version=dependencies.versions["ragas"],
                library_unavailable_reason=None, prompt_version=RAGAS_PROMPT_VERSION,
                rubric_version=None, rubric_digest=None,
                generation_parameters=request.generation_parameters,
                input_digests=_input_digests(request), subrequests=tuple(state.records),
                raw_judgment={"outcome": "no_verifiable_claims"}, reasons=(str(exc),),
                usage=state.usage_summary(), evidence_references=request.evidence_references,
                error=None, shared_budget_hook_connected=state.shared_budget_hook_connected,
                scope_limitation=SCOPE_LIMITATION,
            )
        except _AdapterFailure as exc:
            return self._runtime_error(request, dependencies, state, exc)
        except Exception as exc:
            failure = _AdapterFailure(
                JudgeErrorCode.CANDIDATE_CALL_FAILED, "ragas_metric",
                f"Ragas metric execution failed: {type(exc).__name__}: {exc}", retryable=False,
            )
            return self._runtime_error(request, dependencies, state, failure)

    async def _judge_factual(self, request: FactualJudgeRequest, bridge: Any,
                             dependencies: _Dependencies, state: _CallState
                             ) -> tuple[tuple[float, int | None, int | None], dict[str, Any]]:
        mode = {JudgeMetric.FACT_PRECISION: "precision",
                JudgeMetric.FACT_RECALL: "recall", JudgeMetric.FACT_F1: "f1"}[request.metric]
        metric = dependencies.factual_correctness(
            llm=bridge, mode=mode, atomicity=request.atomicity, coverage=request.coverage)
        metric_result = await metric.ascore(response=request.response, reference=request.reference)
        parsed = _successful_outputs(state.records)
        expected = 2 if mode == "precision" else 4
        if len(parsed) != expected:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "ragas_records",
                f"factual {mode} expected {expected} successful subrequests, got {len(parsed)}",
                retryable=False,
            )
        response_claims = _claims(parsed[0])
        if not response_claims:
            raise _NoVerifiableClaims("factual response decomposition produced no verifiable claims")
        response_verdicts = _validated_verdicts(parsed[1], response_claims)
        tp = sum(response_verdicts)
        fp = len(response_verdicts) - tp
        fn = 0
        reference_claims: list[str] = []
        reference_verdicts: list[int] = []
        if mode != "precision":
            reference_claims = _claims(parsed[2])
            if not reference_claims:
                raise _NoVerifiableClaims("reference decomposition produced no verifiable claims")
            reference_verdicts = _validated_verdicts(parsed[3], reference_claims)
            fn = len(reference_verdicts) - sum(reference_verdicts)
        raw_value = float(metric_result.value)
        if not math.isfinite(raw_value):
            raise _NoVerifiableClaims("Ragas returned a non-finite factual score")
        if mode == "precision":
            denominator = tp + fp
            if denominator == 0:
                raise _NoVerifiableClaims("factual precision has no verifiable claims")
            value = tp / denominator
            counts: tuple[int | None, int | None] = (tp, denominator)
        elif mode == "recall":
            denominator = tp + fn
            if denominator == 0:
                raise _NoVerifiableClaims("factual recall has no measurable claim denominator")
            value = tp / denominator
            counts = (tp, denominator)
        else:
            value = raw_value
            counts = (None, None)
        unsupported_reference_claims = [
            claim
            for claim, verdict in zip(response_claims, response_verdicts, strict=True)
            if verdict == 0
        ]
        requires_global_fact_review = bool(unsupported_reference_claims) and mode in {
            "precision",
            "f1",
        }
        reasons = [item["reason"] for output in parsed
                   for item in output.get("statements", [])
                   if isinstance(item, dict) and isinstance(item.get("reason"), str)]
        if requires_global_fact_review:
            reasons.append(
                "reference comparison found answer claims outside the supplied reference; "
                "those claims require independent source review and are not global factual errors"
            )
        return (value, counts[0], counts[1]), {
            "ragas_metric": "FactualCorrectness", "ragas_mode": mode,
            "ragas_metric_result": raw_value, "tp": tp, "fp": fp, "fn": fn,
            "response_claims": response_claims, "reference_claims": reference_claims,
            "response_verdicts": response_verdicts,
            "reference_verdicts": reference_verdicts,
            "unsupported_reference_claims": unsupported_reference_claims,
            "requires_global_fact_review": requires_global_fact_review,
            "reference_comparison_only": True,
            "reasons": reasons,
        }

    async def _judge_faithfulness(self, request: FaithfulnessJudgeRequest, bridge: Any,
                                  dependencies: _Dependencies, state: _CallState
                                  ) -> tuple[tuple[float, int, int], dict[str, Any]]:
        metric = dependencies.faithfulness(llm=bridge)
        metric_result = await metric.ascore(
            user_input=request.user_input, response=request.response,
            retrieved_contexts=[item.text for item in request.actual_contexts])
        parsed = _successful_outputs(state.records)
        if len(parsed) != 2:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "ragas_records",
                f"faithfulness expected 2 successful subrequests, got {len(parsed)}",
                retryable=False,
            )
        statements = _statements(parsed[0])
        if not statements:
            raise _NoVerifiableClaims("faithfulness decomposition produced no verifiable claims")
        verdicts = _validated_verdicts(parsed[1], statements)
        numerator = sum(verdicts)
        denominator = len(verdicts)
        raw_value = float(metric_result.value)
        if not math.isfinite(raw_value):
            raise _NoVerifiableClaims("Ragas returned a non-finite faithfulness score")
        reasons = [item["reason"] for item in parsed[1].get("statements", [])
                   if isinstance(item, dict) and isinstance(item.get("reason"), str)]
        return (numerator / denominator, numerator, denominator), {
            "ragas_metric": "Faithfulness", "ragas_metric_result": raw_value,
            "statements": statements, "verdicts": verdicts,
            "actual_context_ids": [item.context_id for item in request.actual_contexts],
            "reasons": reasons,
        }

    async def _judge_citation(self, request: CitationSupportJudgeRequest,
                              bridge: Any, state: _CallState
                              ) -> tuple[tuple[float, int, int], dict[str, Any]]:
        binding = request.binding
        prompt = (
            "Evaluate only whether the exact cited text supports the bound claim, including "
            "its stated object, quantity, unit, method, population, and experimental conditions. "
            "Do not use outside knowledge or any unquoted retrieval context.\n\n"
            f"Rubric version: {request.rubric_version}\nRubric:\n{request.rubric}\n\n"
            f"Claim ID: {binding.claim_id}\nClaim: {binding.claim_text}\n"
            f"Answer span: {binding.answer_span.text}\n"
            f"Citation source: {binding.citation.source_id}\n"
            f"Citation anchor: {binding.citation.anchor_id}\n"
            f"Exact cited text: {binding.citation.quoted_text}\n"
        )
        candidate = await bridge.agenerate(prompt, CitationSupportCandidate)
        if candidate.claim_id != binding.claim_id:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "citation_binding",
                "citation candidate changed the stable claim_id", retryable=False,
            )
        if candidate.cited_text != binding.citation.quoted_text:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "citation_binding",
                "citation candidate did not preserve the exact cited location text", retryable=False,
            )
        numerator = int(candidate.supported)
        return (float(numerator), numerator, 1), {
            "metric": "research_exact_citation_support", "claim_id": candidate.claim_id,
            "supported": candidate.supported, "reason": candidate.reason,
            "conditions_considered": list(candidate.conditions_considered),
            "citation_location_digest": binding.citation.quoted_text_digest,
            "reasons": [candidate.reason],
        }

    def _runtime_error(self, request: SemanticJudgeRequest, dependencies: _Dependencies,
                       state: _CallState, failure: _AdapterFailure) -> SemanticJudgeObservation:
        try:
            provider, model_id, revision, revision_reason = state.runtime_identity()
        except _AdapterFailure:
            provider = model_id = revision = revision_reason = None
        return SemanticJudgeObservation(
            observation_id=request.observation_id, metric=request.metric,
            metric_version=request.metric_version, status=JudgeObservationStatus.ERROR,
            value=None, numerator=None, denominator=None, scope=request.scope,
            capability=self.capability, execution_profile=request.execution_profile,
            provider=provider, model_id=model_id, model_revision=revision,
            model_revision_unavailable_reason=revision_reason,
            adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
            library_name="ragas", library_version=dependencies.versions["ragas"],
            library_unavailable_reason=None,
            prompt_version=(CITATION_PROMPT_VERSION if request.metric is JudgeMetric.CITATION_SUPPORT
                            else RAGAS_PROMPT_VERSION),
            rubric_version=(request.rubric_version if isinstance(request, CitationSupportJudgeRequest)
                            else None),
            rubric_digest=(request.rubric_digest if isinstance(request, CitationSupportJudgeRequest)
                           else None),
            generation_parameters=request.generation_parameters,
            input_digests=_input_digests(request), subrequests=tuple(state.records),
            raw_judgment={"error_code": failure.code.value, "stage": failure.stage,
                          "message": str(failure)},
            reasons=(failure.code.value, str(failure)), usage=state.usage_summary(),
            evidence_references=request.evidence_references,
            error=JudgeError(code=failure.code, stage=failure.stage, message=str(failure),
                             attempts=state.request_count, retryable=failure.retryable,
                             budget_exhausted=failure.budget_exhausted),
            shared_budget_hook_connected=state.shared_budget_hook_connected,
            scope_limitation=SCOPE_LIMITATION,
        )

    def _preflight_error(self, request: SemanticJudgeRequest, *, code: JudgeErrorCode,
                         stage: str, message: str, library_version: str | None,
                         library_reason: str | None) -> SemanticJudgeObservation:
        usage = JudgeUsageSummary(
            requests=0, input_tokens=0, output_tokens=0, total_tokens=0,
            cost=None, currency=None,
            cost_unknown_reason=(request.budget.unknown_price_reason
                                 or "no candidate calls were made and provider cost was not observed"),
        )
        return SemanticJudgeObservation(
            observation_id=request.observation_id, metric=request.metric,
            metric_version=request.metric_version, status=JudgeObservationStatus.ERROR,
            value=None, numerator=None, denominator=None, scope=request.scope,
            capability=self.capability, execution_profile=request.execution_profile,
            provider=None, model_id=None, model_revision=None,
            model_revision_unavailable_reason=None, adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION, library_name="ragas",
            library_version=library_version, library_unavailable_reason=library_reason,
            prompt_version=(CITATION_PROMPT_VERSION if request.metric is JudgeMetric.CITATION_SUPPORT
                            else RAGAS_PROMPT_VERSION),
            rubric_version=(request.rubric_version if isinstance(request, CitationSupportJudgeRequest)
                            else None),
            rubric_digest=(request.rubric_digest if isinstance(request, CitationSupportJudgeRequest)
                           else None),
            generation_parameters=request.generation_parameters,
            input_digests=_input_digests(request), subrequests=(),
            raw_judgment={"error_code": code.value, "stage": stage, "message": message},
            reasons=(code.value, message), usage=usage,
            evidence_references=request.evidence_references,
            error=JudgeError(code=code, stage=stage, message=message, attempts=0,
                             retryable=False, budget_exhausted=False),
            shared_budget_hook_connected=False, scope_limitation=SCOPE_LIMITATION,
        )


def _load_dependencies() -> _Dependencies:
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    os.environ["LANGSMITH_TRACING"] = "false"
    versions: dict[str, str] = {}
    missing: list[str] = []
    mismatched: list[str] = []
    for package, expected in EXPECTED_DEPENDENCIES.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            missing.append(f"{package}=={expected}")
            continue
        versions[package] = actual
        if actual != expected:
            mismatched.append(f"{package} expected {expected}, found {actual}")
    if missing:
        raise _AdapterFailure(
            JudgeErrorCode.JUDGE_UNAVAILABLE, "dependency_import",
            "optional rag-eval-judge dependencies are not installed: " + ", ".join(missing),
            retryable=False,
        )
    if mismatched:
        raise _AdapterFailure(
            JudgeErrorCode.DEPENDENCY_VERSION_MISMATCH, "dependency_version",
            "unvalidated Ragas compatibility chain: " + "; ".join(mismatched),
            retryable=False,
        )
    try:
        from ragas import _analytics as ragas_analytics
        from ragas.llms.base import InstructorBaseRagasLLM
        from ragas.metrics.collections import FactualCorrectness, Faithfulness
        ragas_analytics.do_not_track.cache_clear()
        if not ragas_analytics.do_not_track():
            raise RuntimeError("Ragas telemetry opt-out did not take effect")
    except Exception as exc:
        raise _AdapterFailure(
            JudgeErrorCode.JUDGE_UNAVAILABLE, "dependency_import",
            f"Ragas compatibility chain failed to import: {type(exc).__name__}: {exc}",
            retryable=False,
        ) from exc
    return _Dependencies(
        instructor_base=InstructorBaseRagasLLM,
        factual_correctness=FactualCorrectness,
        faithfulness=Faithfulness,
        versions=versions,
    )



class _DuplicateJsonKey(ValueError):
    pass


class _NonFiniteJsonNumber(ValueError):
    pass


def _decode_json_object(content: str) -> dict[str, Any]:
    def reject_constant(value: str) -> Any:
        raise _NonFiniteJsonNumber(value)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    value = json.loads(
        content,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(value, dict):
        raise ValueError("structured candidate must be a JSON object")
    return value

def _successful_outputs(records: list[JudgeSubrequestRecord]) -> list[dict[str, Any]]:
    return [dict(record.parsed_output) for record in records
            if record.parsed_output is not None and record.error_code is None]


def _claims(output: dict[str, Any]) -> list[str]:
    value = output.get("claims")
    if not isinstance(value, list):
        raise _AdapterFailure(
            JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
            "claim decomposition output does not contain a claims list", retryable=False,
        )
    return _strict_non_empty_strings(value, "claims")


def _statements(output: dict[str, Any]) -> list[str]:
    value = output.get("statements")
    if not isinstance(value, list):
        raise _AdapterFailure(
            JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
            "statement generator output does not contain a statements list", retryable=False,
        )
    return _strict_non_empty_strings(value, "statements")


def _strict_non_empty_strings(values: list[Any], field_name: str) -> list[str]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
                f"{field_name} must contain only non-empty strings", retryable=False,
            )
        result.append(value)
    return result


def _validated_verdicts(output: dict[str, Any], expected: list[str]) -> list[int]:
    items = output.get("statements")
    if not isinstance(items, list) or len(items) != len(expected):
        raise _AdapterFailure(
            JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
            "NLI verdict count does not match the decomposed claims", retryable=False,
        )
    verdicts: list[int] = []
    for expected_statement, item in zip(expected, items, strict=True):
        if not isinstance(item, dict):
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
                "NLI verdict item must be an object", retryable=False,
            )
        statement = item.get("statement")
        verdict = item.get("verdict")
        reason = item.get("reason")
        if statement != expected_statement:
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
                "NLI verdict statement does not preserve the decomposed claim", retryable=False,
            )
        if isinstance(verdict, bool) or not isinstance(verdict, int) or verdict not in (0, 1):
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
                "NLI verdict must be the integer 0 or 1", retryable=False,
            )
        if not isinstance(reason, str) or not reason.strip():
            raise _AdapterFailure(
                JudgeErrorCode.INVALID_OUTPUT, "cross_field_validation",
                "NLI verdict requires a non-empty reason", retryable=False,
            )
        verdicts.append(verdict)
    return verdicts


def _input_digests(request: SemanticJudgeRequest) -> dict[str, str]:
    if isinstance(request, FactualJudgeRequest):
        return {"response": request.response_digest, "reference": request.reference_digest}
    if isinstance(request, FaithfulnessJudgeRequest):
        result = {"user_input": request.user_input_digest, "response": request.response_digest}
        result.update({f"actual_context:{item.context_id}": item.text_digest
                       for item in request.actual_contexts})
        return result
    return {
        "citation_binding": request.binding_digest,
        "citation_rubric": request.rubric_digest,
        "exact_citation_text": request.binding.citation.quoted_text_digest,
    }


__all__ = ["ADAPTER_ID", "ADAPTER_VERSION", "EXPECTED_DEPENDENCIES",
           "RAGAS_VERSION", "RagasJudgeAdapter"]
