from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone as _tz
from hashlib import sha256
from typing import Any, Iterable, Iterator

from framework.llm.cache import (
    CacheLookupStatus,
    CacheMode,
    CachePreparation,
    CacheResponseValidationError,
    LLMCacheRuntime,
    SingleFlightAcquireStatus,
    SingleFlightLease,
)
from framework.llm.cache.stream import (
    LLMStreamCacheCapture,
    LLMStreamProtocolError,
    iter_cached_response_events,
)
from framework.llm.budget import (
    GlobalBudgetCheck,
    GlobalBudgetExceededError,
    GlobalBudgetTracker,
    LLMBudgetOperation,
    LLMBudgetExceededError,
    LLMBudgetGuard,
    current_llm_budget_invocation,
)
from framework.llm.clients.openai_compatible import (
    LLMProviderContextOverflow,
    LLMProviderError,
)
from framework.llm.context import (
    LLMContextAdmissionStatus,
    LLMRequestPreparer,
    PreparedLLMRequest,
    build_default_request_preparer,
)
from framework.llm.models import (
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    TokenUsage,
)
from framework.llm.redaction import redact_sensitive_values
from framework.llm.routing.cooldown import InMemoryLLMCooldownTracker, LLMCooldownState
from framework.llm.routing.deployment import ModelDeployment
from framework.llm.routing.errors import LLMRouteError
from framework.llm.routing.events import LLMRouterEvent, LLMRouterEventSink
from framework.llm.routing.route import LLMRoutingPolicy, ModelRoute
from framework.llm.structured_output import (
    LLMStructuredOutputProjectionError,
    LLMStructuredOutputSchemaError,
    ProviderSchemaProjection,
    StructuredOutputContract,
    StructuredOutputDiagnostic,
    compile_structured_output_contract,
    project_structured_output_contract,
)
from framework.shared.graph_identity import GraphExecutionIdentity


UTC = _tz.utc


@dataclass
class _RouterCacheAttempt:
    configured: bool = False
    mode: CacheMode = CacheMode.DISABLED
    preparation: CachePreparation | None = None
    response: LLMResponse | None = None
    lease: SingleFlightLease | None = None
    write_authorized: bool = True
    reason: str = "cache_not_configured"
    backend: str = "none"
    age_seconds: float | None = None
    stream_outcome_recorded: bool = False

    @property
    def eligible(self) -> bool:
        return self.preparation is not None and self.preparation.eligible

    @property
    def hit(self) -> bool:
        return self.response is not None

    def metadata(self, *, provider_call: bool) -> dict[str, Any] | None:
        if not self.configured:
            return None
        key = self.preparation.key if self.preparation is not None else None
        return {
            "llm_cache_mode": self.mode.value,
            "llm_cacheable": (
                self.preparation.eligibility.eligible
                if self.preparation is not None
                else False
            ),
            "llm_cache_hit": self.hit,
            "llm_cache_source": "cache" if self.hit else (
                "provider" if provider_call else "bypass"
            ),
            "llm_cache_reason": self.reason,
            "llm_cache_key_version": key.key_version if key is not None else None,
            "llm_cache_key_digest_prefix": key.short_digest() if key is not None else None,
            "llm_cache_age_seconds": self.age_seconds,
            "llm_cache_backend": self.backend,
            "llm_provider_call": provider_call,
            "llm_budget_cost_counted": provider_call,
            "llm_budget_request_counted": True,
            "llm_logical_request_counted": True,
            "llm_logical_request_count": 1,
        }


class _CacheReplayDeadlineExceeded(RuntimeError):
    pass


class _LLMResponseIdentityError(ValueError):
    def __init__(self, *, expected: Any, actual: Any, response: LLMResponse) -> None:
        super().__init__("LLM response Graph identity does not match request")
        self.expected = expected
        self.actual = actual
        self.response = response


@dataclass(frozen=True)
class _RouterBudgetAttempt:
    tracker: GlobalBudgetTracker | None = None
    logical_operation_id: str | None = None
    operation: LLMBudgetOperation | None = None
    preflight_check: GlobalBudgetCheck | None = None

    @property
    def operation_id(self) -> str | None:
        return self.operation.operation_id if self.operation is not None else None


class _RouterEventList(list[LLMRouterEvent]):
    def __init__(
        self,
        execution_identity: GraphExecutionIdentity | None,
    ) -> None:
        super().__init__()
        self.execution_identity = execution_identity


class LLMRouter:
    def __init__(
        self,
        *,
        routes: Iterable[ModelRoute],
        deployments: Iterable[ModelDeployment],
        routing_policy: LLMRoutingPolicy | None = None,
        cooldown_tracker: InMemoryLLMCooldownTracker | None = None,
        global_budget_tracker: GlobalBudgetTracker | None = None,
        now_fn: Any | None = None,
        event_sink: LLMRouterEventSink | None = None,
        cache_runtime: LLMCacheRuntime | None = None,
        request_preparer: LLMRequestPreparer | None = None,
    ) -> None:
        deployment_list = tuple(deployments)
        self._routes = {route.route_id: route for route in routes}
        self._deployments = {
            deployment.deployment_id: deployment
            for deployment in deployment_list
        }
        self._routing_policy = routing_policy
        self._cooldown_tracker = cooldown_tracker
        self._global_budget_tracker = global_budget_tracker
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._event_sink = event_sink
        self._cache_runtime = cache_runtime
        self._request_preparer = request_preparer or build_default_request_preparer(
            profile
            for profile in (
                deployment.context_profile for deployment in deployment_list
            )
            if profile is not None
        )

    def resolve_route_id(
        self,
        *,
        route_id: str | None = None,
        agent_id: str | None = None,
        task_type: str | None = None,
    ) -> str:
        resolved, trace = self._resolve_route_id_with_trace(
            route_id=route_id,
            agent_id=agent_id,
            task_type=task_type,
        )
        if not resolved:
            raise LLMRouteError(
                "LLM route could not be resolved",
                route_id=route_id or "",
                error_type="route_not_resolved",
                retryable=False,
                errors=[
                    {
                        "routing_policy_configured": self._routing_policy is not None,
                        "resolution_trace": _safe_resolution_trace(trace),
                    }
                ],
            )
        return resolved

    def manages_global_budget(self) -> bool:
        return self._global_budget_tracker is not None

    def complete_for(
        self,
        request: LLMRequest,
        *,
        route_id: str | None = None,
        agent_id: str | None = None,
        task_type: str | None = None,
    ) -> LLMResponse:
        resolved, resolution_trace = self._resolve_route_id_with_trace(
            route_id=route_id,
            agent_id=agent_id,
            task_type=task_type,
        )
        if not resolved:
            raise LLMRouteError(
                "LLM route could not be resolved",
                route_id=route_id or "",
                error_type="route_not_resolved",
                retryable=False,
                errors=[
                    {
                        "routing_policy_configured": self._routing_policy is not None,
                        "resolution_trace": _safe_resolution_trace(resolution_trace),
                    }
                ],
            )
        return self._complete(resolved, request, resolution_trace=resolution_trace)

    def complete(self, route_id: str, request: LLMRequest) -> LLMResponse:
        return self._complete(route_id, request, resolution_trace=())

    def stream_for(
        self,
        request: LLMRequest,
        *,
        route_id: str | None = None,
        agent_id: str | None = None,
        task_type: str | None = None,
    ) -> Iterator[LLMStreamEvent]:
        resolved, resolution_trace = self._resolve_route_id_with_trace(
            route_id=route_id,
            agent_id=agent_id,
            task_type=task_type,
        )
        if not resolved:
            raise LLMRouteError(
                "LLM route could not be resolved",
                route_id=route_id or "",
                error_type="route_not_resolved",
                retryable=False,
                errors=[
                    {
                        "routing_policy_configured": self._routing_policy is not None,
                        "resolution_trace": _safe_resolution_trace(resolution_trace),
                    }
                ],
            )
        return self._stream(resolved, request, resolution_trace=resolution_trace)

    def stream(self, route_id: str, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        return self._stream(route_id, request, resolution_trace=())

    def _complete(
        self,
        route_id: str,
        request: LLMRequest,
        *,
        resolution_trace: Iterable[dict[str, Any]],
    ) -> LLMResponse:
        route = self._route(route_id)
        route_events: list[LLMRouterEvent] = _RouterEventList(
            request.execution_identity,
        )
        budget_tracker = self._budget_tracker_for_request(request)
        try:
            contract = _compile_route_structured_output_contract(
                request,
                route_id=route_id,
            )
        except LLMRouteError as exc:
            self._record_structured_output_preflight_failure(
                route_events,
                route_id=route.route_id,
                error=exc,
            )
            raise
        attempted_deployments: list[str] = []
        errors: list[dict[str, Any]] = []
        deployment_chain = route.deployment_chain()
        resolution_trace = _safe_resolution_trace(resolution_trace)
        overflow_recovery_used = False
        logical_budget_operation_id = self._logical_budget_operation_id(
            request,
            route_id=route.route_id,
            streaming=False,
            tracker=budget_tracker,
        )
        self._record_structured_output_contract_compiled(
            route_events,
            route_id=route.route_id,
            contract=contract,
        )
        self._record_event(
            route_events,
            "llm_route_started",
            route.route_id,
            metadata={
                "deployment_chain": list(deployment_chain),
                "required_capabilities": list(route.required_capabilities),
                "resolution_trace": list(resolution_trace),
            },
        )

        for index, deployment_id in enumerate(deployment_chain):
            attempted_deployments.append(deployment_id)
            deployment = self._deployment(route.route_id, deployment_id, attempted_deployments, errors)
            self._record_event(
                route_events,
                "llm_deployment_attempt_started",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempt_index": index + 1,
                    "fallback_attempt": index > 0,
                },
            )
            if not deployment.enabled:
                self._record_event(
                    route_events,
                    "llm_deployment_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={"reason": "deployment_disabled", "retryable": False},
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment is disabled: {deployment_id}",
                    route_id=route.route_id,
                    error_type="deployment_disabled",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )
            missing_capabilities = deployment.capabilities.missing(route.required_capabilities)
            if missing_capabilities:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "missing_required_capability",
                        "missing_capabilities": list(missing_capabilities),
                        "retryable": False,
                    }
                )
                self._record_event(
                    route_events,
                    "llm_deployment_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "missing_required_capability",
                        "missing_capabilities": list(missing_capabilities),
                        "retryable": False,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment lacks required capabilities: {deployment_id}",
                    route_id=route.route_id,
                    error_type="missing_required_capability",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            try:
                attempt_request, _provider_projection = (
                    self._prepare_structured_output_attempt(
                        request=request,
                        contract=contract,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                        streaming=False,
                    )
                )
            except LLMStructuredOutputProjectionError as exc:
                errors.append(_projection_error_payload(deployment, exc))
                self._record_event(
                    route_events,
                    "structured_output_provider_projection_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "provider_call": False,
                        "contract_digest": contract.schema_digest,
                        "capability_revision": (
                            deployment.structured_output_capability.revision
                            if deployment.structured_output_capability is not None
                            else None
                        ),
                        "diagnostics": [
                            item.to_dict() for item in exc.diagnostics
                        ],
                    },
                )
                if index < len(deployment_chain) - 1:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason="provider_schema_ineligible",
                        errors=errors,
                    )
                    continue
                raise self._build_route_error(
                    f"LLM route {route.route_id} has no eligible structured-output deployment",
                    route_id=route.route_id,
                    error_type="provider_schema_ineligible",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            prepared, context_projection = self._prepare_context_attempt(
                request=attempt_request,
                route=route,
                deployment=deployment,
                events=route_events,
            )
            if prepared is None or not prepared.admission.provider_call_authorized:
                admission_payload = dict(context_projection["admission"])
                error_type = str(admission_payload["status"])
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "provider": deployment.provider,
                        "model": deployment.model,
                        "error_type": error_type,
                        "retryable": False,
                        "provider_call": False,
                        "context_admission": context_projection,
                    }
                )
                has_fallback = index < len(deployment_chain) - 1
                if has_fallback:
                    self._record_context_capacity_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        context_projection=context_projection,
                    )
                    continue
                raise self._build_route_error(
                    f"LLM route {route.route_id} rejected context at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=error_type,
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            prepared_request = prepared.normalized_request
            prepared_request = self._invocation_bounded_provider_request(
                prepared_request,
                tracker=budget_tracker,
            )
            try:
                logical_budget_attempt = self._reserve_global_budget_logical(
                    deployment=deployment,
                    logical_operation_id=logical_budget_operation_id,
                    attempt_index=index + 1,
                    tracker=budget_tracker,
                )
            except GlobalBudgetExceededError as exc:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "global_budget_exceeded",
                        "retryable": False,
                        "global_budget_check": exc.check.to_dict(),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_global_budget_exceeded",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "phase": "logical_admission",
                        "global_budget_check": exc.check.to_dict(),
                        "retryable": False,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded global budget before deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="global_budget_exceeded",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            try:
                cache_attempt = self._prepare_cache_attempt(
                    request=prepared_request,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    prepared=prepared,
                )
            except Exception:
                self._release_global_budget_attempt(
                    logical_budget_attempt,
                    reason="cache_preparation_failed_before_dispatch",
                )
                raise
            cache_preparation = cache_attempt.preparation
            if (
                self._cache_runtime is not None
                and cache_preparation is not None
                and self._cache_runtime.deadline_expired(cache_preparation)
            ):
                self._release_global_budget_attempt(
                    logical_budget_attempt,
                    reason="cache_deadline_before_execution",
                )
                raise self._build_cache_deadline_error(
                    cache_attempt,
                    request=request,
                    route=route,
                    deployment=deployment,
                    attempted_deployments=attempted_deployments,
                    events=route_events,
                    errors=errors,
                    resolution_trace=resolution_trace,
                    phase="cache_admission",
                )
            if cache_attempt.hit:
                return self._return_cache_hit(
                    cache_attempt,
                    request=request,
                    prepared=prepared,
                    route=route,
                    deployment=deployment,
                    attempted_deployments=attempted_deployments,
                    fallback_used=index > 0,
                    events=route_events,
                    errors=errors,
                    resolution_trace=resolution_trace,
                    attempt_index=index + 1,
                    budget_attempt=logical_budget_attempt,
                )
            self._release_global_budget_attempt(
                logical_budget_attempt,
                reason="cache_miss_before_physical_admission",
            )
            cooldown_until = self._active_cooldown_until(deployment)
            if cooldown_until is not None:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "deployment_in_cooldown",
                        "cooldown_until": _datetime_to_json(cooldown_until),
                        "retryable": True,
                    }
                )
                has_fallback = index < len(deployment_chain) - 1
                self._record_event(
                    route_events,
                    "llm_deployment_skipped",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "deployment_in_cooldown",
                        "cooldown_until": _datetime_to_json(cooldown_until),
                        "retryable": True,
                    },
                    aliases=("deployment_skipped_cooldown",),
                )
                if has_fallback:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason="deployment_in_cooldown",
                        errors=errors,
                    )
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    continue
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment is in cooldown: {deployment_id}",
                    route_id=route.route_id,
                    error_type="deployment_in_cooldown",
                    retryable=True,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )
            try:
                budget_attempt = self._reserve_global_budget_attempt(
                    prepared=prepared,
                    deployment=deployment,
                    logical_operation_id=logical_budget_operation_id,
                    attempt_index=index + 1,
                    tracker=budget_tracker,
                )
            except GlobalBudgetExceededError as exc:
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "global_budget_exceeded",
                        "retryable": False,
                        "global_budget_check": exc.check.to_dict(),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_global_budget_exceeded",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "phase": "physical_admission",
                        "global_budget_check": exc.check.to_dict(),
                        "retryable": False,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded physical budget before deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="global_budget_exceeded",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            self._record_event(
                route_events,
                "llm_provider_call_started",
                route.route_id,
                deployment=deployment,
                metadata={"provider_call": True},
            )
            try:
                response = deployment.client.complete(prepared_request)
            except LLMProviderError as exc:
                if exc.status_code is None and not exc.retryable:
                    global_failure_check = self._mark_global_budget_indeterminate(
                        budget_attempt,
                        reason="provider_dispatch_indeterminate",
                    )
                else:
                    global_failure_check = self._settle_global_budget_failure(
                        budget_attempt,
                        deployment=deployment,
                        reason=exc.error_type,
                        observed_usage=TokenUsage() if exc.retryable else None,
                    )
                error_payload = _provider_error_payload(deployment_id, exc)
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = global_failure_check.to_dict()
                cooldown_state = self._record_provider_failure(deployment_id, exc)
                if cooldown_state is not None:
                    error_payload["cooldown_state"] = cooldown_state.to_dict()
                errors.append(error_payload)
                self._record_structured_output_rejection(
                    route_events,
                    route=route,
                    deployment=deployment,
                    error=exc,
                    contract=contract,
                )
                has_fallback = index < len(deployment_chain) - 1
                self._record_event(
                    route_events,
                    "llm_deployment_attempt_failed",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "error_type": exc.error_type,
                        "error_category": _canonical_error_category(exc.error_type),
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                        "attempts": exc.attempts,
                        "provider_call": True,
                    },
                    aliases=("route_attempt_failed",),
                )
                if isinstance(exc, LLMProviderContextOverflow):
                    self._record_provider_context_overflow_event(
                        route_events,
                        route=route,
                        deployment=deployment,
                        prepared=prepared,
                        error=exc,
                    )
                    if not overflow_recovery_used and has_fallback:
                        overflow_recovery_used = True
                        self._record_context_capacity_fallback_event(
                            route_events,
                            route_id=route.route_id,
                            from_deployment=deployment,
                            to_deployment_id=deployment_chain[index + 1],
                            context_projection=prepared.to_dict(),
                            reason="provider_context_overflow",
                        )
                        self._release_cache_attempt(
                            cache_attempt,
                            route=route,
                            deployment=deployment,
                            events=route_events,
                        )
                        continue
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    raise self._build_route_error(
                        f"LLM route {route.route_id} observed provider context overflow at deployment {deployment_id}",
                        route_id=route.route_id,
                        error_type="provider_context_overflow",
                        retryable=False,
                        request=request,
                        attempted_deployments=attempted_deployments,
                        errors=errors,
                        events=route_events,
                        resolution_trace=resolution_trace,
                    ) from None
                if exc.retryable and has_fallback:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason=exc.error_type,
                        errors=errors,
                    )
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    continue
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} failed at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="provider_error",
                    retryable=exc.retryable,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            except Exception as exc:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="provider_dispatch_indeterminate",
                )
                error_payload = {
                    "deployment_id": deployment_id,
                    "error_type": "client_error",
                    "error_class": type(exc).__name__,
                    "retryable": False,
                }
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = (
                        global_failure_check.to_dict()
                    )
                errors.append(error_payload)
                self._record_event(
                    route_events,
                    "llm_deployment_attempt_failed",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "error_type": "client_error",
                        "error_class": type(exc).__name__,
                        "retryable": False,
                        "provider_call": True,
                    },
                    aliases=("route_attempt_failed",),
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} client failed at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="client_error",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            try:
                response = _bind_router_response_identity(
                    response,
                    request.execution_identity,
                )
            except _LLMResponseIdentityError as exc:
                global_budget_check = self._settle_global_budget_failure(
                    budget_attempt,
                    deployment=deployment,
                    reason="response_identity_mismatch",
                    observed_usage=exc.response.usage,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "response_identity_mismatch",
                        "retryable": False,
                        "expected_identity": _identity_payload(exc.expected),
                        "actual_identity": _identity_payload(exc.actual),
                        "global_budget_check": (
                            global_budget_check.to_dict()
                            if global_budget_check is not None
                            else None
                        ),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_deployment_attempt_failed",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "error_type": "response_identity_mismatch",
                        "retryable": False,
                        "provider_call": True,
                    },
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} received a response with mismatched Graph identity",
                    route_id=route.route_id,
                    error_type="response_identity_mismatch",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            if (
                cache_attempt.eligible
                and self._cache_runtime is not None
                and self._cache_runtime.mode.writes
            ):
                try:
                    self._cache_runtime.validate_response(
                        request=prepared_request,
                        response=response,
                    )
                except CacheResponseValidationError:
                    global_budget_check = self._settle_global_budget_attempt(
                        budget_attempt,
                        response,
                        deployment=deployment,
                        budget_check=None,
                        outcome="failed",
                    )
                    errors.append(
                        {
                            "deployment_id": deployment_id,
                            "error_type": "structured_output_validation_error",
                            "retryable": False,
                            "global_budget_check": (
                                global_budget_check.to_dict()
                                if global_budget_check is not None
                                else None
                            ),
                        }
                    )
                    self._record_event(
                        route_events,
                        "llm_deployment_attempt_failed",
                        route.route_id,
                        deployment=deployment,
                        metadata={
                            "error_type": "structured_output_validation_error",
                            "error_category": "schema_error",
                            "retryable": False,
                            "provider_call": True,
                        },
                        aliases=("route_attempt_failed",),
                    )
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    raise self._build_route_error(
                        f"LLM route {route.route_id} response validation failed at deployment {deployment_id}",
                        route_id=route.route_id,
                        error_type="provider_error",
                        retryable=False,
                        request=request,
                        attempted_deployments=attempted_deployments,
                        errors=errors,
                        events=route_events,
                        resolution_trace=resolution_trace,
                    ) from None

            self._record_structured_output_accepted(
                route_events,
                route=route,
                deployment=deployment,
                response=response,
            )

            if self._cooldown_tracker is not None:
                self._cooldown_tracker.record_success(deployment_id)
            try:
                budget_check = _check_budget(route, deployment, response)
            except LLMBudgetExceededError as exc:
                global_budget_check = self._settle_global_budget_attempt(
                    budget_attempt,
                    response,
                    deployment=deployment,
                    budget_check=exc.check,
                    outcome="failed",
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "llm_budget_exceeded",
                        "retryable": False,
                        "budget_check": exc.check.to_dict(),
                        "global_budget_check": (
                            global_budget_check.to_dict()
                            if global_budget_check is not None
                            else None
                        ),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_budget_exceeded",
                    route.route_id,
                    deployment=deployment,
                    metadata={"budget_check": exc.check.to_dict(), "retryable": False},
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded budget at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="llm_budget_exceeded",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            global_budget_check = self._settle_global_budget_attempt(
                budget_attempt,
                response,
                deployment=deployment,
                budget_check=budget_check,
            )
            if (
                self._cache_runtime is not None
                and cache_attempt.eligible
                and self._cache_runtime.mode.writes
            ):
                write_result = self._cache_runtime.write(
                    cache_attempt.preparation,
                    request=prepared_request,
                    response=response,
                    write_authorized=cache_attempt.write_authorized,
                )
                if prepared_request.output_schema is not None:
                    self._record_event(
                        route_events,
                        "structured_output_cache_validation",
                        route.route_id,
                        deployment=deployment,
                        metadata={
                            "phase": "write",
                            "outcome": (
                                "stored" if write_result.stored else "rejected"
                            ),
                            "reason": write_result.reason,
                            "provider_call": True,
                        },
                    )
                write_event = (
                    "llm_cache_write_succeeded"
                    if write_result.stored
                    else "llm_cache_write_failed"
                )
                self._record_event(
                    route_events,
                    write_event,
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        **self._cache_event_metadata(cache_attempt, provider_call=True),
                        "result": write_result.status.value,
                        "reason": write_result.reason,
                        "size_bytes": write_result.size_bytes,
                    },
                )
            self._release_cache_attempt(
                cache_attempt,
                route=route,
                deployment=deployment,
                events=route_events,
            )
            self._record_event(
                route_events,
                "llm_deployment_attempt_succeeded",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempt_index": index + 1,
                    "provider_call": True,
                    "cache_hit": False,
                    "usage": response.usage.to_dict(),
                    "preflight_global_budget_check": (
                        budget_attempt.preflight_check.to_dict()
                        if budget_attempt.preflight_check is not None
                        else None
                    ),
                    "budget_check": budget_check.to_dict() if budget_check is not None else None,
                    "global_budget_check": (
                        global_budget_check.to_dict() if global_budget_check is not None else None
                    ),
                },
            )
            self._record_event(
                route_events,
                "llm_route_completed",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempted_deployments": list(attempted_deployments),
                    "fallback_used": index > 0,
                    "provider_call": True,
                    "cache_hit": False,
                },
            )

            return _with_routing_metadata(
                response,
                request=request,
                route_id=route.route_id,
                deployment=deployment,
                attempted_deployments=attempted_deployments,
                fallback_used=index > 0,
                budget_check=budget_check,
                global_budget_check=global_budget_check,
                events=route_events,
                errors=errors,
                resolution_trace=resolution_trace,
                cache_metadata=cache_attempt.metadata(provider_call=True),
                prepared=prepared,
            )

        raise self._build_route_error(
            f"LLM route {route.route_id} has no deployments",
            route_id=route.route_id,
            error_type="empty_route",
            retryable=False,
            request=request,
            attempted_deployments=attempted_deployments,
            errors=errors,
            events=route_events,
            resolution_trace=resolution_trace,
        )

    def _stream(
        self,
        route_id: str,
        request: LLMRequest,
        *,
        resolution_trace: Iterable[dict[str, Any]],
    ) -> Iterator[LLMStreamEvent]:
        route = self._route(route_id)
        route_events: list[LLMRouterEvent] = _RouterEventList(
            request.execution_identity,
        )
        budget_tracker = self._budget_tracker_for_request(request)
        try:
            contract = _compile_route_structured_output_contract(
                request,
                route_id=route_id,
            )
        except LLMRouteError as exc:
            self._record_structured_output_preflight_failure(
                route_events,
                route_id=route.route_id,
                error=exc,
            )
            raise
        attempted_deployments: list[str] = []
        errors: list[dict[str, Any]] = []
        deployment_chain = route.deployment_chain()
        resolution_trace = _safe_resolution_trace(resolution_trace)
        overflow_recovery_used = False
        logical_budget_operation_id = self._logical_budget_operation_id(
            request,
            route_id=route.route_id,
            streaming=True,
            tracker=budget_tracker,
        )
        self._record_structured_output_contract_compiled(
            route_events,
            route_id=route.route_id,
            contract=contract,
        )
        self._record_event(
            route_events,
            "llm_route_started",
            route.route_id,
            metadata={
                "deployment_chain": list(deployment_chain),
                "required_capabilities": list(route.required_capabilities),
                "resolution_trace": list(resolution_trace),
                "stream": True,
            },
        )

        for index, deployment_id in enumerate(deployment_chain):
            attempted_deployments.append(deployment_id)
            deployment = self._deployment(
                route.route_id,
                deployment_id,
                attempted_deployments,
                errors,
            )
            self._record_event(
                route_events,
                "llm_deployment_attempt_started",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempt_index": index + 1,
                    "fallback_attempt": index > 0,
                    "stream": True,
                },
            )
            if not deployment.enabled:
                self._record_event(
                    route_events,
                    "llm_deployment_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={"reason": "deployment_disabled", "retryable": False},
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment is disabled: {deployment_id}",
                    route_id=route.route_id,
                    error_type="deployment_disabled",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )
            missing_capabilities = deployment.capabilities.missing(route.required_capabilities)
            if missing_capabilities:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "missing_required_capability",
                        "missing_capabilities": list(missing_capabilities),
                        "retryable": False,
                    }
                )
                self._record_event(
                    route_events,
                    "llm_deployment_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "missing_required_capability",
                        "missing_capabilities": list(missing_capabilities),
                        "retryable": False,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment lacks required capabilities: {deployment_id}",
                    route_id=route.route_id,
                    error_type="missing_required_capability",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            try:
                attempt_request, _provider_projection = (
                    self._prepare_structured_output_attempt(
                        request=request,
                        contract=contract,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                        streaming=True,
                    )
                )
            except LLMStructuredOutputProjectionError as exc:
                errors.append(_projection_error_payload(deployment, exc))
                self._record_event(
                    route_events,
                    "structured_output_provider_projection_rejected",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "provider_call": False,
                        "stream": True,
                        "contract_digest": contract.schema_digest,
                        "capability_revision": (
                            deployment.structured_output_capability.revision
                            if deployment.structured_output_capability is not None
                            else None
                        ),
                        "diagnostics": [
                            item.to_dict() for item in exc.diagnostics
                        ],
                    },
                )
                if index < len(deployment_chain) - 1:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason="provider_schema_ineligible",
                        errors=errors,
                    )
                    continue
                raise self._build_route_error(
                    (
                        f"LLM route {route.route_id} has no eligible "
                        "structured-output stream deployment"
                    ),
                    route_id=route.route_id,
                    error_type="provider_schema_ineligible",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            prepared, context_projection = self._prepare_context_attempt(
                request=attempt_request,
                route=route,
                deployment=deployment,
                events=route_events,
            )
            if prepared is None or not prepared.admission.provider_call_authorized:
                admission_payload = dict(context_projection["admission"])
                error_type = str(admission_payload["status"])
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "provider": deployment.provider,
                        "model": deployment.model,
                        "error_type": error_type,
                        "retryable": False,
                        "provider_call": False,
                        "context_admission": context_projection,
                    }
                )
                has_fallback = index < len(deployment_chain) - 1
                if has_fallback:
                    self._record_context_capacity_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        context_projection=context_projection,
                    )
                    continue
                raise self._build_route_error(
                    f"LLM route {route.route_id} rejected context at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=error_type,
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            prepared_request = prepared.normalized_request
            prepared_request = self._invocation_bounded_provider_request(
                prepared_request,
                tracker=budget_tracker,
            )
            try:
                logical_budget_attempt = self._reserve_global_budget_logical(
                    deployment=deployment,
                    logical_operation_id=logical_budget_operation_id,
                    attempt_index=index + 1,
                    tracker=budget_tracker,
                )
            except GlobalBudgetExceededError as exc:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "global_budget_exceeded",
                        "retryable": False,
                        "global_budget_check": exc.check.to_dict(),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_global_budget_exceeded",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "phase": "logical_admission",
                        "global_budget_check": exc.check.to_dict(),
                        "retryable": False,
                        "stream": True,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded global budget before deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="global_budget_exceeded",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            try:
                cache_attempt = self._prepare_cache_attempt(
                    request=prepared_request,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    prepared=prepared,
                )
            except Exception:
                self._release_global_budget_attempt(
                    logical_budget_attempt,
                    reason="cache_preparation_failed_before_dispatch",
                )
                raise
            cache_preparation = cache_attempt.preparation
            if (
                self._cache_runtime is not None
                and cache_preparation is not None
                and self._cache_runtime.deadline_expired(cache_preparation)
            ):
                self._release_global_budget_attempt(
                    logical_budget_attempt,
                    reason="cache_deadline_before_execution",
                )
                raise self._build_cache_deadline_error(
                    cache_attempt,
                    request=request,
                    route=route,
                    deployment=deployment,
                    attempted_deployments=attempted_deployments,
                    events=route_events,
                    errors=errors,
                    resolution_trace=resolution_trace,
                    phase="cache_admission",
                )
            if cache_attempt.hit:
                cached_response = self._return_cache_hit(
                    cache_attempt,
                    request=request,
                    prepared=prepared,
                    route=route,
                    deployment=deployment,
                    attempted_deployments=attempted_deployments,
                    fallback_used=index > 0,
                    events=route_events,
                    errors=errors,
                    resolution_trace=resolution_trace,
                    attempt_index=index + 1,
                    budget_attempt=logical_budget_attempt,
                )
                try:
                    yield from self._cache_hit_stream_events(
                        cached_response,
                        preparation=cache_preparation,
                    )
                except _CacheReplayDeadlineExceeded:
                    raise self._build_cache_deadline_error(
                        cache_attempt,
                        request=request,
                        route=route,
                        deployment=deployment,
                        attempted_deployments=attempted_deployments,
                        events=route_events,
                        errors=errors,
                        resolution_trace=resolution_trace,
                        phase="cache_replay",
                    )
                except GeneratorExit:
                    self._record_stream_cache_outcome(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                        event_type="llm_cache_stream_replay_interrupted",
                        reason="consumer_closed",
                        provider_call=False,
                    )
                    raise
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_replay_completed",
                    reason="cache_hit_replayed",
                    provider_call=False,
                )
                return

            self._release_global_budget_attempt(
                logical_budget_attempt,
                reason="cache_miss_before_physical_admission",
            )
            cooldown_until = self._active_cooldown_until(deployment)
            if cooldown_until is not None:
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "deployment_in_cooldown",
                        "cooldown_until": _datetime_to_json(cooldown_until),
                        "retryable": True,
                    }
                )
                has_fallback = index < len(deployment_chain) - 1
                self._record_event(
                    route_events,
                    "llm_deployment_skipped",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "deployment_in_cooldown",
                        "cooldown_until": _datetime_to_json(cooldown_until),
                        "retryable": True,
                    },
                    aliases=("deployment_skipped_cooldown",),
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                if has_fallback:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason="deployment_in_cooldown",
                        errors=errors,
                    )
                    continue
                raise self._build_route_error(
                    f"LLM route {route.route_id} deployment is in cooldown: {deployment_id}",
                    route_id=route.route_id,
                    error_type="deployment_in_cooldown",
                    retryable=True,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            try:
                budget_attempt = self._reserve_global_budget_attempt(
                    prepared=prepared,
                    deployment=deployment,
                    logical_operation_id=logical_budget_operation_id,
                    attempt_index=index + 1,
                    tracker=budget_tracker,
                )
            except GlobalBudgetExceededError as exc:
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "global_budget_exceeded",
                        "retryable": False,
                        "global_budget_check": exc.check.to_dict(),
                    }
                )
                self._record_event(
                    route_events,
                    "llm_global_budget_exceeded",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "phase": "physical_admission",
                        "global_budget_check": exc.check.to_dict(),
                        "retryable": False,
                        "stream": True,
                    },
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded physical budget before deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="global_budget_exceeded",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            self._record_event(
                route_events,
                "llm_provider_call_started",
                route.route_id,
                deployment=deployment,
                metadata={"provider_call": True, "stream": True},
            )
            source_iterator: Iterator[Any] | None = None
            try:
                source_iterator = iter(deployment.client.stream(prepared_request))
                first_event = LLMStreamEvent.from_any(next(source_iterator))
            except StopIteration:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="stream_terminal_missing",
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="incomplete_stream",
                    provider_call=True,
                    metadata={"protocol_reason": "empty_stream"},
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "provider_stream_empty",
                        "retryable": False,
                        "global_budget_check": (
                            global_failure_check.to_dict()
                            if global_failure_check is not None
                            else None
                        ),
                    }
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} provider stream was empty at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="provider_stream_empty",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            except LLMProviderError as exc:
                if exc.status_code is None and not exc.retryable:
                    global_failure_check = self._mark_global_budget_indeterminate(
                        budget_attempt,
                        reason="provider_dispatch_indeterminate",
                    )
                else:
                    global_failure_check = self._settle_global_budget_failure(
                        budget_attempt,
                        deployment=deployment,
                        reason=exc.error_type,
                        observed_usage=TokenUsage() if exc.retryable else None,
                    )
                self._record_structured_output_rejection(
                    route_events,
                    route=route,
                    deployment=deployment,
                    error=exc,
                    contract=contract,
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="source_interrupted",
                    provider_call=True,
                    metadata={"error_class": type(exc).__name__},
                )
                error_payload = _provider_error_payload(deployment_id, exc)
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = global_failure_check.to_dict()
                errors.append(error_payload)
                self._record_provider_failure(deployment_id, exc)
                self._record_event(
                    route_events,
                    "llm_deployment_attempt_failed",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "error_type": exc.error_type,
                        "error_category": _canonical_error_category(exc.error_type),
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                        "attempts": exc.attempts,
                        "provider_call": True,
                        "stream": True,
                    },
                    aliases=("route_attempt_failed",),
                )
                has_fallback = index < len(deployment_chain) - 1
                if isinstance(exc, LLMProviderContextOverflow):
                    self._record_provider_context_overflow_event(
                        route_events,
                        route=route,
                        deployment=deployment,
                        prepared=prepared,
                        error=exc,
                    )
                    if not overflow_recovery_used and has_fallback:
                        overflow_recovery_used = True
                        self._record_context_capacity_fallback_event(
                            route_events,
                            route_id=route.route_id,
                            from_deployment=deployment,
                            to_deployment_id=deployment_chain[index + 1],
                            context_projection=prepared.to_dict(),
                            reason="provider_context_overflow",
                        )
                        self._release_cache_attempt(
                            cache_attempt,
                            route=route,
                            deployment=deployment,
                            events=route_events,
                        )
                        continue
                elif exc.retryable and has_fallback:
                    self._record_fallback_event(
                        route_events,
                        route_id=route.route_id,
                        from_deployment=deployment,
                        to_deployment_id=deployment_chain[index + 1],
                        reason=exc.error_type,
                        errors=errors,
                    )
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    continue
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} failed while opening stream at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=(
                        "provider_context_overflow"
                        if isinstance(exc, LLMProviderContextOverflow)
                        else "provider_error"
                    ),
                    retryable=exc.retryable,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            except Exception as exc:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="provider_dispatch_indeterminate",
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="source_interrupted",
                    provider_call=True,
                    metadata={"error_class": type(exc).__name__},
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                error_payload = {
                    "deployment_id": deployment_id,
                    "error_type": "client_error",
                    "error_class": type(exc).__name__,
                    "retryable": False,
                }
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = (
                        global_failure_check.to_dict()
                    )
                errors.append(error_payload)
                raise self._build_route_error(
                    f"LLM route {route.route_id} client failed while opening stream at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="client_error",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            capture = LLMStreamCacheCapture(
                expected_execution_identity=request.execution_identity,
            )
            terminal_event: LLMStreamEvent | None = None
            visible_output = False
            try:
                first_event = capture.add(first_event)
                if first_event.event_type == "message_complete":
                    terminal_event = first_event
                else:
                    visible_output = True
                    yield first_event
                if source_iterator is None:
                    raise RuntimeError("provider stream iterator is unavailable")
                if terminal_event is None:
                    for raw_event in source_iterator:
                        event = capture.add(raw_event)
                        if event.event_type == "message_complete":
                            terminal_event = event
                            break
                        visible_output = True
                        yield event
            except GeneratorExit:
                self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="stream_consumer_closed_before_terminal",
                )
                _close_iterator(source_iterator)
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="consumer_closed",
                    provider_call=True,
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise
            except LLMProviderError as exc:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="stream_terminal_missing",
                )
                _close_iterator(source_iterator)
                self._record_structured_output_rejection(
                    route_events,
                    route=route,
                    deployment=deployment,
                    error=exc,
                    contract=contract,
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="source_interrupted",
                    provider_call=True,
                    metadata={"error_class": type(exc).__name__},
                )
                error_payload = _provider_error_payload(deployment_id, exc)
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = global_failure_check.to_dict()
                errors.append(error_payload)
                self._record_provider_failure(deployment_id, exc)
                self._record_event(
                    route_events,
                    "llm_deployment_attempt_failed",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "error_type": exc.error_type,
                        "error_category": _canonical_error_category(exc.error_type),
                        "status_code": exc.status_code,
                        "retryable": exc.retryable,
                        "attempts": exc.attempts,
                        "provider_call": True,
                        "stream": True,
                        "visible_output": visible_output,
                    },
                    aliases=("route_attempt_failed",),
                )
                if isinstance(exc, LLMProviderContextOverflow):
                    self._record_provider_context_overflow_event(
                        route_events,
                        route=route,
                        deployment=deployment,
                        prepared=prepared,
                        error=exc,
                    )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} stream failed at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=(
                        "provider_context_overflow"
                        if isinstance(exc, LLMProviderContextOverflow)
                        else "provider_error"
                    ),
                    retryable=False if visible_output else exc.retryable,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            except Exception as exc:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="stream_terminal_missing",
                )
                _close_iterator(source_iterator)
                protocol_reason = (
                    exc.reason if isinstance(exc, LLMStreamProtocolError) else None
                )
                identity_mismatch = protocol_reason == "stream_identity_mismatch"
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason=(
                        "invalid_stream_protocol"
                        if protocol_reason is not None
                        else "source_interrupted"
                    ),
                    provider_call=True,
                    metadata={
                        "protocol_reason": protocol_reason,
                        "error_class": type(exc).__name__,
                    },
                )
                error_payload = {
                    "deployment_id": deployment_id,
                    "error_type": (
                        "response_identity_mismatch"
                        if identity_mismatch
                        else "client_error"
                    ),
                    "error_class": type(exc).__name__,
                    "retryable": False,
                    "visible_output": visible_output,
                }
                if identity_mismatch:
                    error_payload.update(
                        {
                            "expected_identity": _identity_payload(
                                request.execution_identity
                            ),
                            "actual_identity": _identity_payload(
                                exc.actual_execution_identity
                            ),
                        }
                    )
                if global_failure_check is not None:
                    error_payload["global_budget_check"] = (
                        global_failure_check.to_dict()
                    )
                errors.append(error_payload)
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} stream failed at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=(
                        "response_identity_mismatch"
                        if identity_mismatch
                        else "client_error"
                    ),
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None

            if terminal_event is None:
                global_failure_check = self._mark_global_budget_indeterminate(
                    budget_attempt,
                    reason="stream_terminal_missing",
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="incomplete_stream",
                    provider_call=True,
                    metadata={"protocol_reason": "missing_message_complete"},
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "provider_stream_incomplete",
                        "retryable": False,
                        "visible_output": visible_output,
                        "global_budget_check": (
                            global_failure_check.to_dict()
                            if global_failure_check is not None
                            else None
                        ),
                    }
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} provider stream was incomplete at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="provider_stream_incomplete",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                )

            capture_result = capture.finalize()
            try:
                response = _bind_router_response_identity(
                    capture_result.response,
                    request.execution_identity,
                )
            except _LLMResponseIdentityError as exc:
                global_budget_check = self._settle_global_budget_failure(
                    budget_attempt,
                    deployment=deployment,
                    reason="response_identity_mismatch",
                    observed_usage=exc.response.usage,
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="response_identity_mismatch",
                    provider_call=True,
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": "response_identity_mismatch",
                        "retryable": False,
                        "expected_identity": _identity_payload(exc.expected),
                        "actual_identity": _identity_payload(exc.actual),
                        "global_budget_check": (
                            global_budget_check.to_dict()
                            if global_budget_check is not None
                            else None
                        ),
                    }
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} received a streamed response with mismatched Graph identity",
                    route_id=route.route_id,
                    error_type="response_identity_mismatch",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            self._record_structured_output_accepted(
                route_events,
                route=route,
                deployment=deployment,
                response=response,
            )
            write_after_exhaustion = False
            deferred_no_write_reason: tuple[str, dict[str, Any]] | None = None
            if not capture_result.cacheable:
                outcome_metadata = {}
                if capture_result.protocol_reason is not None:
                    outcome_metadata["protocol_reason"] = capture_result.protocol_reason
                deferred_no_write_reason = (capture_result.reason, outcome_metadata)
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
            if self._cooldown_tracker is not None:
                self._cooldown_tracker.record_success(deployment_id)
            try:
                budget_check = _check_budget(route, deployment, response)
            except LLMBudgetExceededError as exc:
                global_budget_check = self._settle_global_budget_attempt(
                    budget_attempt,
                    response,
                    deployment=deployment,
                    budget_check=exc.check,
                    outcome="failed",
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="budget_rejected",
                    provider_call=True,
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                error_type = "llm_budget_exceeded"
                check = exc.check.to_dict()
                errors.append(
                    {
                        "deployment_id": deployment_id,
                        "error_type": error_type,
                        "retryable": False,
                        "budget_check": check,
                        "global_budget_check": (
                            global_budget_check.to_dict()
                            if global_budget_check is not None
                            else None
                        ),
                    }
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} exceeded stream budget at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=error_type,
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            else:
                global_budget_check = self._settle_global_budget_attempt(
                    budget_attempt,
                    response,
                    deployment=deployment,
                    budget_check=budget_check,
                )

            if capture_result.cacheable and (
                self._cache_runtime is not None
                and cache_attempt.eligible
                and self._cache_runtime.mode.writes
            ):
                write_after_exhaustion = True
            elif capture_result.cacheable and cache_attempt.eligible:
                deferred_no_write_reason = (cache_attempt.reason, {})
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
            self._record_event(
                route_events,
                "llm_deployment_attempt_succeeded",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempt_index": index + 1,
                    "provider_call": True,
                    "cache_hit": False,
                    "stream": True,
                    "usage": response.usage.to_dict(),
                    "preflight_global_budget_check": (
                        budget_attempt.preflight_check.to_dict()
                        if budget_attempt.preflight_check is not None
                        else None
                    ),
                    "budget_check": budget_check.to_dict() if budget_check is not None else None,
                    "global_budget_check": (
                        global_budget_check.to_dict() if global_budget_check is not None else None
                    ),
                },
            )
            self._record_event(
                route_events,
                "llm_route_completed",
                route.route_id,
                deployment=deployment,
                metadata={
                    "attempted_deployments": list(attempted_deployments),
                    "fallback_used": index > 0,
                    "provider_call": True,
                    "cache_hit": False,
                    "stream": True,
                },
            )
            routed_response = _with_routing_metadata(
                response,
                request=request,
                route_id=route.route_id,
                deployment=deployment,
                attempted_deployments=attempted_deployments,
                fallback_used=index > 0,
                budget_check=budget_check,
                global_budget_check=global_budget_check,
                events=route_events,
                errors=errors,
                resolution_trace=resolution_trace,
                cache_metadata=cache_attempt.metadata(provider_call=True),
                prepared=prepared,
            )
            try:
                yield replace(terminal_event, metadata=routed_response.metadata)
            except GeneratorExit:
                _close_iterator(source_iterator)
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="consumer_closed",
                    provider_call=True,
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise

            try:
                if source_iterator is None:
                    raise RuntimeError("provider stream iterator is unavailable")
                trailing_raw_event = next(source_iterator)
            except StopIteration:
                pass
            except LLMProviderError as exc:
                _close_iterator(source_iterator)
                self._record_structured_output_rejection(
                    route_events,
                    route=route,
                    deployment=deployment,
                    error=exc,
                    contract=contract,
                )
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="source_interrupted",
                    provider_call=True,
                    metadata={"error_class": type(exc).__name__},
                )
                errors.append(_provider_error_payload(deployment_id, exc))
                self._record_provider_failure(deployment_id, exc)
                self._record_event(
                    route_events,
                    "llm_stream_terminal_invalidated",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "source_interrupted_after_message_complete",
                        "error_class": type(exc).__name__,
                        "provider_call": True,
                    },
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} stream failed after completion at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type=(
                        "provider_context_overflow"
                        if isinstance(exc, LLMProviderContextOverflow)
                        else "provider_error"
                    ),
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            except Exception as exc:
                _close_iterator(source_iterator)
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="source_interrupted",
                    provider_call=True,
                    metadata={"error_class": type(exc).__name__},
                )
                self._record_event(
                    route_events,
                    "llm_stream_terminal_invalidated",
                    route.route_id,
                    deployment=deployment,
                    metadata={
                        "reason": "source_interrupted_after_message_complete",
                        "error_class": type(exc).__name__,
                        "provider_call": True,
                    },
                )
                self._release_cache_attempt(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                )
                raise self._build_route_error(
                    f"LLM route {route.route_id} stream failed after completion at deployment {deployment_id}",
                    route_id=route.route_id,
                    error_type="client_error",
                    retryable=False,
                    request=request,
                    attempted_deployments=attempted_deployments,
                    errors=errors,
                    events=route_events,
                    resolution_trace=resolution_trace,
                ) from None
            else:
                try:
                    capture.add(trailing_raw_event)
                except Exception as exc:
                    protocol_reason = (
                        exc.reason
                        if isinstance(exc, LLMStreamProtocolError)
                        else "invalid_event"
                    )
                    _close_iterator(source_iterator)
                    self._record_stream_cache_outcome(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                        event_type="llm_cache_stream_not_written",
                        reason="invalid_stream_protocol",
                        provider_call=True,
                        metadata={"protocol_reason": protocol_reason},
                    )
                    self._record_event(
                        route_events,
                        "llm_stream_terminal_invalidated",
                        route.route_id,
                        deployment=deployment,
                        metadata={
                            "reason": "event_after_message_complete",
                            "protocol_reason": protocol_reason,
                            "provider_call": True,
                        },
                    )
                    self._release_cache_attempt(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                    )
                    raise self._build_route_error(
                        f"LLM route {route.route_id} emitted an event after completion at deployment {deployment_id}",
                        route_id=route.route_id,
                        error_type="invalid_stream_protocol",
                        retryable=False,
                        request=request,
                        attempted_deployments=attempted_deployments,
                        errors=errors,
                        events=route_events,
                        resolution_trace=resolution_trace,
                    ) from None

            if (
                write_after_exhaustion
                and self._cache_runtime is not None
                and cache_attempt.preparation is not None
                and self._cache_runtime.deadline_expired(cache_attempt.preparation)
            ):
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason="caller_deadline_exceeded",
                    provider_call=True,
                    metadata={"phase": "post_stream_exhaustion"},
                )
                write_after_exhaustion = False

            if deferred_no_write_reason is not None:
                reason, outcome_metadata = deferred_no_write_reason
                self._record_stream_cache_outcome(
                    cache_attempt,
                    route=route,
                    deployment=deployment,
                    events=route_events,
                    event_type="llm_cache_stream_not_written",
                    reason=reason,
                    provider_call=True,
                    metadata=outcome_metadata,
                )
            elif write_after_exhaustion:
                runtime = self._cache_runtime
                preparation = cache_attempt.preparation
                if runtime is None or preparation is None:
                    self._record_stream_cache_outcome(
                        cache_attempt,
                        route=route,
                        deployment=deployment,
                        events=route_events,
                        event_type="llm_cache_stream_not_written",
                        reason="cache_runtime_unavailable",
                        provider_call=True,
                    )
                else:
                    try:
                        runtime.validate_response(
                            request=prepared_request,
                            response=response,
                        )
                    except Exception as exc:
                        self._record_stream_cache_outcome(
                            cache_attempt,
                            route=route,
                            deployment=deployment,
                            events=route_events,
                            event_type="llm_cache_stream_not_written",
                            reason="output_contract_validation_failed",
                            provider_call=True,
                            metadata={"error_class": type(exc).__name__},
                        )
                    else:
                        if runtime.deadline_expired(preparation):
                            self._record_stream_cache_outcome(
                                cache_attempt,
                                route=route,
                                deployment=deployment,
                                events=route_events,
                                event_type="llm_cache_stream_not_written",
                                reason="caller_deadline_exceeded",
                                provider_call=True,
                                metadata={"phase": "post_validation"},
                            )
                            write_result = None
                        else:
                            try:
                                write_result = runtime.write(
                                    preparation,
                                    request=prepared_request,
                                    response=response,
                                    write_authorized=cache_attempt.write_authorized,
                                )
                            except Exception as exc:
                                self._record_stream_cache_outcome(
                                    cache_attempt,
                                    route=route,
                                    deployment=deployment,
                                    events=route_events,
                                    event_type="llm_cache_write_failed",
                                    reason="backend_error",
                                    provider_call=True,
                                    metadata={"error_class": type(exc).__name__},
                                )
                                write_result = None
                        if write_result is None:
                            self._release_cache_attempt(
                                cache_attempt,
                                route=route,
                                deployment=deployment,
                                events=route_events,
                            )
                            return
                        write_status = write_result.status.value
                        outcome_reason = (
                            write_status
                            if write_status in {"backend_error", "entry_too_large"}
                            else write_result.reason or write_status
                        )
                        outcome_metadata: dict[str, Any] = {
                            "result": write_status,
                            "size_bytes": write_result.size_bytes,
                        }
                        if prepared_request.output_schema is not None:
                            self._record_event(
                                route_events,
                                "structured_output_cache_validation",
                                route.route_id,
                                deployment=deployment,
                                metadata={
                                    "phase": "write",
                                    "outcome": (
                                        "stored"
                                        if write_result.stored
                                        else "rejected"
                                    ),
                                    "reason": write_result.reason,
                                    "provider_call": True,
                                },
                            )
                        if write_status == "backend_error" and write_result.reason:
                            outcome_metadata["error_class"] = write_result.reason
                        self._record_stream_cache_outcome(
                            cache_attempt,
                            route=route,
                            deployment=deployment,
                            events=route_events,
                            event_type=(
                                "llm_cache_write_succeeded"
                                if write_result.stored
                                else "llm_cache_write_failed"
                            ),
                            reason=outcome_reason,
                            provider_call=True,
                            metadata=outcome_metadata,
                        )
            self._release_cache_attempt(
                cache_attempt,
                route=route,
                deployment=deployment,
                events=route_events,
            )
            return

        raise self._build_route_error(
            f"LLM route {route.route_id} has no deployments",
            route_id=route.route_id,
            error_type="empty_route",
            retryable=False,
            request=request,
            attempted_deployments=attempted_deployments,
            errors=errors,
            events=route_events,
            resolution_trace=resolution_trace,
        )

    def _cache_hit_stream_events(
        self,
        response: LLMResponse,
        *,
        preparation: CachePreparation | None,
    ) -> Iterator[LLMStreamEvent]:
        chunk_size = (
            self._cache_runtime.replay_chunk_size
            if self._cache_runtime is not None
            else 1_024
        )
        for event in iter_cached_response_events(response, chunk_size=chunk_size):
            if (
                self._cache_runtime is not None
                and preparation is not None
                and self._cache_runtime.deadline_expired(preparation)
            ):
                raise _CacheReplayDeadlineExceeded
            yield event

    def _prepare_structured_output_attempt(
        self,
        *,
        request: LLMRequest,
        contract: StructuredOutputContract | None,
        route: ModelRoute,
        deployment: ModelDeployment,
        events: list[LLMRouterEvent],
        streaming: bool,
    ) -> tuple[LLMRequest, ProviderSchemaProjection | None]:
        if contract is None:
            return request, None
        capability = deployment.structured_output_capability
        if capability is None:
            raise LLMStructuredOutputProjectionError(
                "deployment has no versioned structured-output capability",
                diagnostics=(
                    StructuredOutputDiagnostic(
                        code="provider_schema_ineligible",
                        message=(
                            "deployment has no versioned structured-output capability"
                        ),
                        validator="provider_capability_missing",
                        contract_digest=contract.schema_digest,
                    ),
                ),
            )
        projection = project_structured_output_contract(
            contract,
            capability,
            policy=request.structured_output_policy,
            streaming=streaming,
        )
        supports_projection = getattr(
            deployment.client,
            "supports_structured_output_projection",
            None,
        )
        if callable(supports_projection) and not supports_projection(projection):
            raise LLMStructuredOutputProjectionError(
                "deployment adapter cannot map the selected provider projection",
                diagnostics=(
                    StructuredOutputDiagnostic(
                        code="provider_schema_ineligible",
                        message=(
                            "deployment adapter cannot map the selected provider projection"
                        ),
                        validator="provider_adapter_mapping_unsupported",
                        contract_digest=contract.schema_digest,
                    ),
                ),
            )
        self._record_event(
            events,
            "structured_output_provider_projection_selected",
            route.route_id,
            deployment=deployment,
            metadata={
                "provider_call": False,
                "stream": streaming,
                "schema_revision": contract.schema_revision,
                "schema_dialect": contract.dialect,
                **projection.to_dict(),
            },
        )
        return (
            request.with_structured_output_execution(
                contract=contract,
                projection=projection,
            ),
            projection,
        )

    def _prepare_context_attempt(
        self,
        *,
        request: LLMRequest,
        route: ModelRoute,
        deployment: ModelDeployment,
        events: list[LLMRouterEvent],
    ) -> tuple[PreparedLLMRequest | None, dict[str, Any]]:
        profile = deployment.context_profile
        if profile is None:
            admission = {
                "status": LLMContextAdmissionStatus.PROFILE_REQUIRED.value,
                "reason": "deployment has no trusted model context profile",
                "provider_call_authorized": False,
            }
            projection: dict[str, Any] = {
                "deployment_id": deployment.deployment_id,
                "provider": deployment.provider,
                "model": deployment.model,
                "profile_revision": None,
                "normalizer_revision": None,
                "token_count": None,
                "effective_budget": None,
                "payload_fingerprint": None,
                "admission": admission,
            }
            self._record_event(
                events,
                "llm_context_profile_resolved",
                route.route_id,
                deployment=deployment,
                metadata={
                    "profile_available": False,
                    "provider_call": False,
                },
            )
            self._record_event(
                events,
                "llm_context_admission_decided",
                route.route_id,
                deployment=deployment,
                metadata=projection,
            )
            return None, projection

        self._record_event(
            events,
            "llm_context_profile_resolved",
            route.route_id,
            deployment=deployment,
            metadata={
                "profile_available": True,
                "profile": profile.to_dict(),
                "provider_call": False,
            },
        )
        prepared = self._request_preparer.prepare(request, profile)
        projection = prepared.to_dict()
        self._record_event(
            events,
            "llm_request_prepared",
            route.route_id,
            deployment=deployment,
            metadata={**projection, "provider_call": False},
        )
        self._record_event(
            events,
            "llm_context_admission_decided",
            route.route_id,
            deployment=deployment,
            metadata={**projection, "provider_call": False},
        )
        return prepared, projection

    def _record_context_capacity_fallback_event(
        self,
        events: list[LLMRouterEvent],
        *,
        route_id: str,
        from_deployment: ModelDeployment,
        to_deployment_id: str,
        context_projection: dict[str, Any],
        reason: str | None = None,
    ) -> LLMRouterEvent:
        to_deployment = self._deployments.get(to_deployment_id)
        return self._record_event(
            events,
            "llm_context_capacity_fallback_selected",
            route_id,
            deployment=to_deployment,
            deployment_id=to_deployment_id,
            metadata={
                "from_deployment_id": from_deployment.deployment_id,
                "from_provider": from_deployment.provider,
                "from_model": from_deployment.model,
                "to_deployment_id": to_deployment_id,
                "to_provider": to_deployment.provider if to_deployment is not None else None,
                "to_model": to_deployment.model if to_deployment is not None else None,
                "reason": reason or context_projection["admission"]["status"],
                "context_admission": context_projection,
                "provider_call": False,
            },
        )

    def _record_provider_context_overflow_event(
        self,
        events: list[LLMRouterEvent],
        *,
        route: ModelRoute,
        deployment: ModelDeployment,
        prepared: PreparedLLMRequest,
        error: LLMProviderContextOverflow,
    ) -> LLMRouterEvent:
        return self._record_event(
            events,
            "llm_provider_context_overflow_observed",
            route.route_id,
            deployment=deployment,
            metadata={
                "prepared_request": prepared.to_dict(),
                "provider_status_code": error.status_code,
                "provider_error_code": error.provider_error_code,
                "provider_reported_limit_tokens": error.provider_reported_limit_tokens,
                "provider_reported_usage_tokens": error.provider_reported_usage_tokens,
                "provider_call": True,
            },
        )

    def _prepare_cache_attempt(
        self,
        *,
        request: LLMRequest,
        route: ModelRoute,
        deployment: ModelDeployment,
        events: list[LLMRouterEvent],
        prepared: PreparedLLMRequest,
    ) -> _RouterCacheAttempt:
        runtime = self._cache_runtime
        if runtime is None:
            return _RouterCacheAttempt()
        if runtime.mode is CacheMode.DISABLED:
            return _RouterCacheAttempt(
                configured=True,
                mode=runtime.mode,
                reason="cache_disabled",
                backend=runtime.backend_name,
            )

        preparation = runtime.prepare(
            request=request,
            deployment_id=deployment.deployment_id,
            provider=deployment.provider,
            model=deployment.model,
            prepared_identity=prepared.cache_identity(),
        )
        state = _RouterCacheAttempt(
            configured=True,
            mode=runtime.mode,
            preparation=preparation,
            reason=preparation.eligibility.reason,
            backend=runtime.backend_name,
        )
        event_metadata = self._cache_event_metadata(state, provider_call=False)
        self._record_event(
            events,
            "llm_cache_eligibility_evaluated",
            route.route_id,
            deployment=deployment,
            metadata=event_metadata,
        )
        if not preparation.eligible:
            self._record_event(
                events,
                "llm_cache_bypassed",
                route.route_id,
                deployment=deployment,
                metadata=event_metadata,
            )
            return state
        if runtime.mode is CacheMode.OBSERVE:
            state.reason = "observe_only"
            return state
        if runtime.mode is CacheMode.WRITE_ONLY:
            state.reason = "write_only"
            return state

        started_at = time.perf_counter()
        self._record_event(
            events,
            "llm_cache_lookup_started",
            route.route_id,
            deployment=deployment,
            metadata=event_metadata,
        )
        read_result = runtime.read(preparation, request=request)
        state.backend = read_result.lookup.backend
        state.reason = read_result.lookup.status.value
        state.age_seconds = read_result.lookup.age_seconds
        duration_ms = round((time.perf_counter() - started_at) * 1_000, 3)
        if request.output_schema is not None:
            self._record_event(
                events,
                "structured_output_cache_validation",
                route.route_id,
                deployment=deployment,
                metadata={
                    "phase": "read",
                    "outcome": (
                        "validated_hit"
                        if read_result.hit
                        else read_result.lookup.status.value
                    ),
                    "reason": read_result.lookup.reason,
                    "schema_digest": (
                        preparation.key.structured_output_identity.schema_digest
                        if preparation.key
                        and preparation.key.structured_output_identity is not None
                        else None
                    ),
                    "validation_duration_seconds": duration_ms / 1_000,
                    "provider_call": False,
                },
            )
        if read_result.hit:
            state.response = read_result.response
            self._record_event(
                events,
                "llm_cache_hit",
                route.route_id,
                deployment=deployment,
                metadata={
                    **self._cache_event_metadata(state, provider_call=False),
                    "duration_ms": duration_ms,
                },
            )
            return state

        lookup_event = {
            CacheLookupStatus.CORRUPT: "llm_cache_corrupt_entry",
            CacheLookupStatus.BACKEND_ERROR: "llm_cache_backend_error",
        }.get(read_result.lookup.status, "llm_cache_miss")
        self._record_event(
            events,
            lookup_event,
            route.route_id,
            deployment=deployment,
            metadata={
                **self._cache_event_metadata(state, provider_call=False),
                "duration_ms": duration_ms,
            },
        )

        admission = runtime.admit_singleflight(preparation)
        state.write_authorized = admission.write_authorized
        state.lease = admission.lease
        if admission.result.status is SingleFlightAcquireStatus.ACQUIRED:
            if admission.lease is not None:
                recheck = runtime.read(preparation, request=request)
                if recheck.hit:
                    state.response = recheck.response
                    state.reason = "hit_after_singleflight_acquire"
                    state.age_seconds = recheck.lookup.age_seconds
                    self._release_cache_attempt(
                        state,
                        route=route,
                        deployment=deployment,
                        events=events,
                    )
                    self._record_event(
                        events,
                        "llm_cache_hit",
                        route.route_id,
                        deployment=deployment,
                        metadata=self._cache_event_metadata(state, provider_call=False),
                    )
            return state

        if admission.result.status is SingleFlightAcquireStatus.BUSY:
            wait_started = time.perf_counter()
            waited = runtime.wait_for_entry(preparation, request=request)
            wait_ms = round((time.perf_counter() - wait_started) * 1_000, 3)
            state.write_authorized = False
            state.reason = waited.lookup.reason or waited.lookup.status.value
            state.age_seconds = waited.lookup.age_seconds
            self._record_event(
                events,
                "llm_cache_singleflight_waited",
                route.route_id,
                deployment=deployment,
                metadata={
                    **self._cache_event_metadata(state, provider_call=False),
                    "duration_ms": wait_ms,
                    "result": waited.lookup.status.value,
                },
            )
            if waited.hit:
                state.response = waited.response
                state.reason = "hit_after_singleflight_wait"
                self._record_event(
                    events,
                    "llm_cache_hit",
                    route.route_id,
                    deployment=deployment,
                    metadata=self._cache_event_metadata(state, provider_call=False),
                )
            return state

        state.reason = admission.result.reason or "singleflight_backend_error"
        state.write_authorized = False
        self._record_event(
            events,
            "llm_cache_backend_error",
            route.route_id,
            deployment=deployment,
            metadata={
                **self._cache_event_metadata(state, provider_call=False),
                "operation": "singleflight_acquire",
            },
        )
        return state

    def _release_cache_attempt(
        self,
        state: _RouterCacheAttempt,
        *,
        route: ModelRoute,
        deployment: ModelDeployment,
        events: list[LLMRouterEvent],
    ) -> None:
        runtime = self._cache_runtime
        if runtime is None or state.lease is None:
            return
        result = runtime.release_singleflight(state.lease)
        state.lease = None
        if result is not None and (result.backend_error or not result.released):
            self._record_event(
                events,
                "llm_cache_backend_error" if result.backend_error else "llm_cache_bypassed",
                route.route_id,
                deployment=deployment,
                metadata={
                    **self._cache_event_metadata(state, provider_call=False),
                    "operation": "singleflight_release",
                    "reason": result.reason,
                },
            )

    def _cache_event_metadata(
        self,
        state: _RouterCacheAttempt,
        *,
        provider_call: bool,
    ) -> dict[str, Any]:
        metadata = state.metadata(provider_call=provider_call) or {}
        return {
            "cache_mode": state.mode.value,
            "backend": state.backend,
            "reason_code": state.reason,
            "provider_call": provider_call,
            "key_version": metadata.get("llm_cache_key_version"),
            "key_digest_prefix": metadata.get("llm_cache_key_digest_prefix"),
        }

    def _record_stream_cache_outcome(
        self,
        state: _RouterCacheAttempt,
        *,
        route: ModelRoute,
        deployment: ModelDeployment,
        events: list[LLMRouterEvent],
        event_type: str,
        reason: str,
        provider_call: bool,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if (
            not state.configured
            or state.mode is CacheMode.DISABLED
            or state.stream_outcome_recorded
        ):
            return
        state.reason = reason
        state.stream_outcome_recorded = True
        self._record_event(
            events,
            event_type,
            route.route_id,
            deployment=deployment,
            metadata={
                **self._cache_event_metadata(state, provider_call=provider_call),
                "reason": reason,
                **dict(metadata or {}),
            },
        )

    def _build_cache_deadline_error(
        self,
        state: _RouterCacheAttempt,
        *,
        request: LLMRequest,
        route: ModelRoute,
        deployment: ModelDeployment,
        attempted_deployments: list[str],
        events: list[LLMRouterEvent],
        errors: list[dict[str, Any]],
        resolution_trace: Iterable[dict[str, Any]],
        phase: str,
    ) -> LLMRouteError:
        self._record_stream_cache_outcome(
            state,
            route=route,
            deployment=deployment,
            events=events,
            event_type=(
                "llm_cache_stream_replay_interrupted"
                if state.hit
                else "llm_cache_stream_not_written"
            ),
            reason="caller_deadline_exceeded",
            provider_call=False,
            metadata={"phase": phase},
        )
        self._release_cache_attempt(
            state,
            route=route,
            deployment=deployment,
            events=events,
        )
        errors.append(
            {
                "deployment_id": deployment.deployment_id,
                "error_type": "caller_deadline_exceeded",
                "retryable": False,
                "provider_call": False,
            }
        )
        return self._build_route_error(
            f"LLM route {route.route_id} cache operation exceeded caller deadline",
            route_id=route.route_id,
            error_type="caller_deadline_exceeded",
            retryable=False,
            request=request,
            attempted_deployments=attempted_deployments,
            errors=errors,
            events=events,
            resolution_trace=resolution_trace,
        )

    def _return_cache_hit(
        self,
        state: _RouterCacheAttempt,
        *,
        request: LLMRequest,
        prepared: PreparedLLMRequest,
        route: ModelRoute,
        deployment: ModelDeployment,
        attempted_deployments: list[str],
        fallback_used: bool,
        events: list[LLMRouterEvent],
        errors: list[dict[str, Any]],
        resolution_trace: Iterable[dict[str, Any]],
        attempt_index: int,
        budget_attempt: _RouterBudgetAttempt,
    ) -> LLMResponse:
        if state.response is None:
            raise RuntimeError("cache hit response is required")
        try:
            cached_response = _bind_router_response_identity(
                state.response,
                request.execution_identity,
            )
        except _LLMResponseIdentityError as exc:
            self._release_global_budget_attempt(
                budget_attempt,
                reason="cache_response_identity_mismatch",
            )
            self._release_cache_attempt(
                state,
                route=route,
                deployment=deployment,
                events=events,
            )
            errors.append(
                {
                    "deployment_id": deployment.deployment_id,
                    "error_type": "response_identity_mismatch",
                    "retryable": False,
                    "expected_identity": _identity_payload(exc.expected),
                    "actual_identity": _identity_payload(exc.actual),
                }
            )
            raise self._build_route_error(
                f"LLM route {route.route_id} received a cached response with mismatched Graph identity",
                route_id=route.route_id,
                error_type="response_identity_mismatch",
                retryable=False,
                request=request,
                attempted_deployments=attempted_deployments,
                errors=errors,
                events=events,
                resolution_trace=resolution_trace,
            ) from None
        self._record_structured_output_accepted(
            events,
            route=route,
            deployment=deployment,
            response=cached_response,
        )
        self._record_event(
            events,
            "llm_deployment_attempt_succeeded",
            route.route_id,
            deployment=deployment,
            metadata={
                "attempt_index": attempt_index,
                "provider_call": False,
                "cache_hit": True,
                "source_usage": cached_response.usage.to_dict(),
            },
        )
        self._record_event(
            events,
            "llm_route_completed",
            route.route_id,
            deployment=deployment,
            metadata={
                "attempted_deployments": list(attempted_deployments),
                "fallback_used": fallback_used,
                "provider_call": False,
                "cache_hit": True,
            },
        )
        cache_metadata = state.metadata(provider_call=False) or {}
        cache_metadata["llm_cache_source_usage"] = cached_response.usage.to_dict()
        source_execution_identity = cached_response.metadata.get(
            "llm_cache_source_execution_identity"
        )
        if isinstance(source_execution_identity, dict):
            cache_metadata["llm_cache_source_execution_identity"] = dict(
                source_execution_identity
            )
        cache_metadata["llm_provider_usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_input_tokens": 0,
            "total_tokens": 0,
        }
        global_budget_check = self._settle_global_budget_cache_hit(
            budget_attempt,
            observed_usage=cached_response.usage,
        )
        return _with_routing_metadata(
            cached_response,
            request=request,
            route_id=route.route_id,
            deployment=deployment,
            attempted_deployments=attempted_deployments,
            fallback_used=fallback_used,
            budget_check=None,
            global_budget_check=global_budget_check,
            events=events,
            errors=errors,
            resolution_trace=resolution_trace,
            cache_metadata=cache_metadata,
            prepared=prepared,
        )

    def _resolve_route_id_with_trace(
        self,
        *,
        route_id: str | None = None,
        agent_id: str | None = None,
        task_type: str | None = None,
    ) -> tuple[str | None, tuple[dict[str, Any], ...]]:
        if self._routing_policy is None:
            explicit_route = _optional_text(route_id)
            return explicit_route, (
                {
                    "source": "explicit_route",
                    "matched": explicit_route is not None,
                    "route_id": explicit_route,
                    "routing_policy_configured": False,
                },
            )
        return self._routing_policy.resolve_with_trace(
            route_id=route_id,
            agent_id=agent_id,
            task_type=task_type,
        )

    def _record_structured_output_contract_compiled(
        self,
        events: list[LLMRouterEvent],
        *,
        route_id: str,
        contract: StructuredOutputContract | None,
    ) -> None:
        if contract is None:
            return
        schema_bytes = len(
            json.dumps(
                contract.canonical_schema,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        self._record_event(
            events,
            "structured_output_contract_compiled",
            route_id,
            metadata={
                "schema_digest": contract.schema_digest,
                "schema_revision": contract.schema_revision,
                "schema_dialect": contract.dialect,
                "typed_adapter_revision": contract.typed_adapter_revision,
                "schema_bytes": schema_bytes,
                "provider_call": False,
            },
        )

    def _record_structured_output_preflight_failure(
        self,
        events: list[LLMRouterEvent],
        *,
        route_id: str,
        error: LLMRouteError,
    ) -> None:
        diagnostic: dict[str, Any] = {}
        if error.errors:
            raw_diagnostics = error.errors[0].get("diagnostics")
            if isinstance(raw_diagnostics, list) and raw_diagnostics:
                first = raw_diagnostics[0]
                if isinstance(first, dict):
                    diagnostic = first
        self._record_event(
            events,
            "structured_output_schema_preflight_failed",
            route_id,
            metadata={
                "issue_code": diagnostic.get("code") or error.error_type,
                "instance_path": list(diagnostic.get("instance_path") or []),
                "schema_path": list(diagnostic.get("schema_path") or []),
                "validator": diagnostic.get("validator"),
                "issue_count": 1 if diagnostic else 0,
                "provider_call": False,
            },
        )

    def _record_structured_output_rejection(
        self,
        events: list[LLMRouterEvent],
        *,
        route: ModelRoute,
        deployment: ModelDeployment,
        error: LLMProviderError,
        contract: StructuredOutputContract | None,
    ) -> None:
        event_type = {
            "structured_output_parse_error": "structured_output_decode_rejected",
            "structured_output_validation_error": (
                "structured_output_local_validation_failed"
            ),
            "structured_output_typed_validation_error": (
                "structured_output_typed_validation_failed"
            ),
        }.get(error.error_type)
        if (
            event_type == "structured_output_local_validation_failed"
            and error.diagnostics
            and error.diagnostics[0].get("code")
            == "structured_output_typed_validation_error"
        ):
            event_type = "structured_output_typed_validation_failed"
        if event_type is None:
            return
        diagnostic = error.diagnostics[0] if error.diagnostics else {}
        self._record_event(
            events,
            event_type,
            route.route_id,
            deployment=deployment,
            metadata={
                "schema_digest": contract.schema_digest if contract else None,
                "schema_revision": contract.schema_revision if contract else None,
                "schema_dialect": contract.dialect if contract else None,
                "capability_revision": (
                    deployment.structured_output_capability.revision
                    if deployment.structured_output_capability is not None
                    else None
                ),
                "projection_mode": (
                    "json_object_local_gate"
                    if deployment.structured_output_capability is not None
                    and deployment.structured_output_capability.mode == "json_object"
                    else (
                        deployment.structured_output_capability.mode
                        if deployment.structured_output_capability is not None
                        else None
                    )
                ),
                "issue_code": diagnostic.get("code"),
                "instance_path": list(diagnostic.get("instance_path") or []),
                "schema_path": list(diagnostic.get("schema_path") or []),
                "validator": diagnostic.get("validator"),
                "issue_count": len(error.diagnostics),
                "response_fingerprint": error.response_fingerprint,
                "provider_call": True,
            },
        )

    def _record_structured_output_accepted(
        self,
        events: list[LLMRouterEvent],
        *,
        route: ModelRoute,
        deployment: ModelDeployment,
        response: LLMResponse,
    ) -> None:
        validation = response.metadata.get("structured_output_validation")
        if not isinstance(validation, dict) or validation.get("validated") is not True:
            return
        self._record_event(
            events,
            "structured_output_validation_accepted",
            route.route_id,
            deployment=deployment,
            metadata={
                key: validation.get(key)
                for key in (
                    "schema_digest",
                    "schema_revision",
                    "schema_dialect",
                    "typed_adapter_revision",
                    "projection_digest",
                    "projection_mode",
                    "provider_capability_revision",
                    "response_fingerprint",
                )
            },
        )

    def _record_event(
        self,
        events: list[LLMRouterEvent],
        event_type: str,
        route_id: str,
        *,
        deployment: ModelDeployment | None = None,
        deployment_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        aliases: Iterable[str] = (),
    ) -> LLMRouterEvent:
        event = LLMRouterEvent(
            event_type=event_type,
            route_id=route_id,
            deployment_id=deployment.deployment_id if deployment is not None else deployment_id,
            provider=deployment.provider if deployment is not None else None,
            model=deployment.model if deployment is not None else None,
            metadata=redact_sensitive_values(
                {
                    **dict(metadata or {}),
                    "event_aliases": list(aliases),
                    "event_name": _event_name_alias(event_type),
                }
            ),
            occurred_at=self._now_fn(),
            execution_identity=getattr(events, "execution_identity", None),
        )
        events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)
        return event

    def _record_fallback_event(
        self,
        events: list[LLMRouterEvent],
        *,
        route_id: str,
        from_deployment: ModelDeployment,
        to_deployment_id: str,
        reason: str,
        errors: list[dict[str, Any]],
    ) -> LLMRouterEvent:
        to_deployment = self._deployments.get(to_deployment_id)
        return self._record_event(
            events,
            "llm_fallback_selected",
            route_id,
            deployment=to_deployment,
            deployment_id=to_deployment_id,
            metadata={
                "from_deployment_id": from_deployment.deployment_id,
                "from_provider": from_deployment.provider,
                "from_model": from_deployment.model,
                "to_deployment_id": to_deployment_id,
                "to_provider": to_deployment.provider if to_deployment is not None else None,
                "to_model": to_deployment.model if to_deployment is not None else None,
                "reason": reason,
                "error_count": len(errors),
                "errors": [_safe_route_error_payload(error) for error in errors],
            },
            aliases=("fallback_selected",),
        )

    def _build_route_error(
        self,
        message: str,
        *,
        route_id: str,
        error_type: str,
        retryable: bool,
        request: LLMRequest,
        attempted_deployments: list[str],
        errors: list[dict[str, Any]],
        events: list[LLMRouterEvent],
        resolution_trace: Iterable[dict[str, Any]],
    ) -> LLMRouteError:
        self._record_event(
            events,
            "llm_route_failed",
            route_id,
            metadata={
                "error_type": error_type,
                "retryable": retryable,
                "attempted_deployments": list(attempted_deployments),
                "error_count": len(errors),
            },
        )
        event_payloads = _event_payloads(events)
        safe_errors = [_safe_route_error_payload(error) for error in errors]
        manifest = _build_route_manifest(
            route_id=route_id,
            status="failed",
            request=request,
            response=None,
            selected_deployment=None,
            attempted_deployments=attempted_deployments,
            fallback_used=_fallback_count(events) > 0,
            budget_check=None,
            events=event_payloads,
            errors=safe_errors,
            resolution_trace=resolution_trace,
            error={
                "message": message,
                "error_type": error_type,
                "retryable": retryable,
            },
            global_budget_check=_last_global_budget_check(errors),
        )
        return LLMRouteError(
            message,
            route_id=route_id,
            error_type=error_type,
            retryable=retryable,
            attempted_deployments=attempted_deployments,
            errors=safe_errors,
            events=event_payloads,
            manifest=manifest,
        )

    def _route(self, route_id: str) -> ModelRoute:
        route = self._routes.get(route_id)
        if route is None:
            raise LLMRouteError(
                f"LLM route not found: {route_id}",
                route_id=route_id,
                error_type="route_not_found",
                retryable=False,
            )
        return route

    def _deployment(
        self,
        route_id: str,
        deployment_id: str,
        attempted_deployments: list[str],
        errors: list[dict[str, Any]],
    ) -> ModelDeployment:
        deployment = self._deployments.get(deployment_id)
        if deployment is None:
            raise LLMRouteError(
                f"LLM deployment not found: {deployment_id}",
                route_id=route_id,
                error_type="deployment_not_found",
                retryable=False,
                attempted_deployments=attempted_deployments,
                errors=errors,
            )
        return deployment

    def _active_cooldown_until(self, deployment: ModelDeployment) -> datetime | None:
        now = self._now_fn()
        candidates: list[datetime] = []
        if deployment.cooldown_until is not None and (
            _normalize_datetime(deployment.cooldown_until) > _normalize_datetime(now)
        ):
            candidates.append(deployment.cooldown_until)
        if self._cooldown_tracker is not None:
            dynamic_until = self._cooldown_tracker.cooldown_until(
                deployment.deployment_id,
                now=now,
            )
            if dynamic_until is not None:
                candidates.append(dynamic_until)
        if not candidates:
            return None
        return max(candidates, key=_normalize_datetime)

    def _record_provider_failure(
        self,
        deployment_id: str,
        error: LLMProviderError,
    ) -> LLMCooldownState | None:
        if self._cooldown_tracker is None or not error.retryable:
            return None
        return self._cooldown_tracker.record_failure(deployment_id, error)

    def _logical_budget_operation_id(
        self,
        request: LLMRequest,
        *,
        route_id: str,
        streaming: bool,
        tracker: GlobalBudgetTracker | None,
    ) -> str | None:
        if tracker is None:
            return None
        supplied = request.metadata.get("llm_budget_operation_id")
        if isinstance(supplied, str) and supplied.strip():
            execution_identity = tracker.execution_identity
            scope_identity = (
                execution_identity.to_dict()
                if execution_identity is not None
                else {
                    "run_id": tracker.scope.run_id,
                    "scope_id": tracker.scope.scope_id,
                }
            )
            digest = sha256(
                json.dumps(
                    {
                        "external_operation_id": supplied.strip(),
                        "route_id": route_id,
                        "streaming": streaming,
                        "scope_identity": scope_identity,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            return f"router-logical:external:{digest}"
        prefix = "router-stream" if streaming else "router-complete"
        return tracker.next_operation_identity(prefix)

    def _reserve_global_budget_attempt(
        self,
        *,
        prepared: PreparedLLMRequest,
        deployment: ModelDeployment,
        logical_operation_id: str | None,
        attempt_index: int,
        tracker: GlobalBudgetTracker | None,
    ) -> _RouterBudgetAttempt:
        invocation = current_llm_budget_invocation()
        if invocation is not None and invocation.execution_guard is not None:
            invocation.execution_guard()
        if tracker is None or logical_operation_id is None:
            return _RouterBudgetAttempt(tracker=tracker)
        tracker.require_trusted_pricing(deployment.pricing)
        operation_id = (
            f"{logical_operation_id}:attempt:{attempt_index}:"
            f"{deployment.deployment_id}"
        )
        reserved_output_tokens = prepared.effective_budget.reserved_output_tokens
        reserved_input_tokens = prepared.token_count.total_input_tokens
        available_total_tokens = tracker.available_total_tokens()
        available_output_tokens = tracker.available_output_tokens()
        if available_output_tokens is not None:
            reserved_output_tokens = min(
                reserved_output_tokens,
                available_output_tokens,
            )
        if available_total_tokens is not None:
            reserved_input_tokens = min(
                reserved_input_tokens,
                max(0, available_total_tokens - reserved_output_tokens),
            )
        operation = tracker.reserve_prepared_operation(
            operation_id=operation_id,
            idempotency_key=f"{operation_id}:reservation",
            input_tokens=reserved_input_tokens,
            output_tokens=reserved_output_tokens,
            pricing=deployment.pricing,
        )
        if isinstance(operation, GlobalBudgetCheck):
            raise GlobalBudgetExceededError(operation)
        return _RouterBudgetAttempt(
            tracker=tracker,
            logical_operation_id=logical_operation_id,
            operation=operation,
            preflight_check=tracker.check_for_operation(operation),
        )

    @staticmethod
    def _invocation_bounded_provider_request(
        request: LLMRequest,
        *,
        tracker: GlobalBudgetTracker | None,
    ) -> LLMRequest:
        """Prevent a provider client from hiding retries outside Router accounting."""

        invocation = current_llm_budget_invocation()
        if (
            tracker is None
            or invocation is None
            or invocation.tracker is not tracker
            or request.max_transport_attempts == 1
        ):
            return request
        return request.clone(max_transport_attempts=1)

    def _reserve_global_budget_logical(
        self,
        *,
        deployment: ModelDeployment,
        logical_operation_id: str | None,
        attempt_index: int,
        tracker: GlobalBudgetTracker | None,
    ) -> _RouterBudgetAttempt:
        cache_runtime = self._cache_runtime
        if (
            tracker is None
            or logical_operation_id is None
            or cache_runtime is None
            or not cache_runtime.mode.reads
        ):
            return _RouterBudgetAttempt(tracker=tracker)
        operation_id = (
            f"{logical_operation_id}:logical:{attempt_index}:"
            f"{deployment.deployment_id}"
        )
        operation = tracker.reserve_operation(
            operation_id=operation_id,
            idempotency_key=f"{operation_id}:reservation",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0,
            count_request=True,
        )
        if isinstance(operation, GlobalBudgetCheck):
            raise GlobalBudgetExceededError(operation)
        return _RouterBudgetAttempt(
            tracker=tracker,
            logical_operation_id=logical_operation_id,
            operation=operation,
            preflight_check=tracker.check_for_operation(operation),
        )

    def _settle_global_budget_attempt(
        self,
        attempt: _RouterBudgetAttempt,
        response: LLMResponse,
        *,
        deployment: ModelDeployment,
        budget_check,
        outcome: str = "succeeded",
    ):
        tracker = attempt.tracker
        if tracker is None or attempt.operation is None:
            return None
        estimated_cost_usd = (
            budget_check.estimated_cost_usd if budget_check is not None else None
        )
        return tracker.settle_operation(
            attempt.operation,
            response.usage,
            deployment.pricing,
            estimated_cost_usd=estimated_cost_usd,
            outcome=outcome,
        )

    def _settle_global_budget_cache_hit(
        self,
        attempt: _RouterBudgetAttempt,
        *,
        observed_usage: TokenUsage,
    ) -> GlobalBudgetCheck | None:
        tracker = attempt.tracker
        if tracker is None or attempt.operation is None:
            return None
        return tracker.settle_cache_hit(
            attempt.operation,
            observed_usage=observed_usage,
        )

    def _settle_global_budget_failure(
        self,
        attempt: _RouterBudgetAttempt,
        *,
        deployment: ModelDeployment,
        reason: str,
        observed_usage: TokenUsage | None = None,
    ) -> GlobalBudgetCheck | None:
        tracker = attempt.tracker
        if tracker is None or attempt.operation is None:
            return None
        if observed_usage is None:
            return tracker.mark_operation_indeterminate(
                attempt.operation,
                reason=reason,
            )
        return tracker.settle_operation(
            attempt.operation,
            observed_usage,
            deployment.pricing,
            request_dispatched=True,
            outcome="failed",
            reason_code=reason,
        )

    def _release_global_budget_attempt(
        self,
        attempt: _RouterBudgetAttempt,
        *,
        reason: str,
    ) -> GlobalBudgetCheck | None:
        tracker = attempt.tracker
        if tracker is None or attempt.operation is None:
            return None
        return tracker.release_operation(
            attempt.operation,
            reason=reason,
        )

    def _mark_global_budget_indeterminate(
        self,
        attempt: _RouterBudgetAttempt,
        *,
        reason: str,
    ) -> GlobalBudgetCheck | None:
        tracker = attempt.tracker
        if tracker is None or attempt.operation is None:
            return None
        return tracker.mark_operation_indeterminate(
            attempt.operation,
            reason=reason,
        )

    def _budget_tracker_for_request(
        self,
        request: LLMRequest,
    ) -> GlobalBudgetTracker | None:
        tracker = self._global_budget_tracker
        invocation = current_llm_budget_invocation()
        if (
            invocation is not None
            and invocation.execution_identity != request.execution_identity
        ):
            raise ValueError(
                "invocation budget Graph identity does not match the LLM request"
            )
        if invocation is not None and invocation.tracker is not None:
            if tracker is None:
                raise ValueError(
                    "invocation budget requires a budget-managed LLM router"
                )
            if not invocation.tracker.is_scope_descendant_of(tracker):
                raise ValueError(
                    "invocation budget tracker is outside the router ledger lineage"
                )
            return invocation.tracker
        if tracker is None or request.execution_identity is None:
            return tracker
        if tracker.scope.run_id != request.execution_identity.run_id:
            raise ValueError(
                "Graph request budget tracker run does not match execution identity"
            )
        return tracker.for_execution_identity(request.execution_identity)


def _provider_error_payload(deployment_id: str, error: LLMProviderError) -> dict[str, Any]:
    payload = error.to_dict()
    payload["deployment_id"] = deployment_id
    payload["model"] = payload.get("model")
    payload["provider"] = payload.get("provider")
    payload["error_category"] = _canonical_error_category(str(payload.get("error_type") or ""))
    return payload


def _projection_error_payload(
    deployment: ModelDeployment,
    error: LLMStructuredOutputProjectionError,
) -> dict[str, Any]:
    capability = deployment.structured_output_capability
    return {
        "deployment_id": deployment.deployment_id,
        "provider": deployment.provider,
        "model": deployment.model,
        "error_type": "provider_schema_ineligible",
        "error_category": "schema_error",
        "retryable": False,
        "provider_call": False,
        "capability_revision": capability.revision if capability is not None else None,
        "diagnostics": [item.to_dict() for item in error.diagnostics],
    }


def _compile_route_structured_output_contract(
    request: LLMRequest,
    *,
    route_id: str,
) -> StructuredOutputContract | None:
    if request.output_schema is None:
        return None
    existing = request.structured_output_contract()
    if existing is not None:
        return existing
    try:
        return compile_structured_output_contract(
            request.structured_output_schema_source(),
            schema_name=request.output_schema_name,
        )
    except LLMStructuredOutputSchemaError as exc:
        raise LLMRouteError(
            "LLM structured-output schema failed preflight before routing",
            route_id=route_id,
            error_type="structured_output_schema_error",
            retryable=False,
            errors=(
                {
                    "error_type": "structured_output_schema_error",
                    "error_category": "schema_error",
                    "retryable": False,
                    "provider_call": False,
                    "diagnostics": [
                        item.to_dict() for item in exc.diagnostics
                    ],
                },
            ),
        ) from exc


def _close_iterator(iterator: Iterator[Any] | None) -> None:
    if iterator is None:
        return
    close = getattr(iterator, "close", None)
    if callable(close):
        close()


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_to_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _normalize_datetime(value).isoformat().replace("+00:00", "Z")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _check_budget(
    route: ModelRoute,
    deployment: ModelDeployment,
    response: LLMResponse,
):
    if route.budget_policy is None:
        return None
    return LLMBudgetGuard(route.budget_policy).check_call(response.usage, deployment.pricing)


def _with_routing_metadata(
    response: LLMResponse,
    *,
    request: LLMRequest,
    route_id: str,
    deployment: ModelDeployment,
    attempted_deployments: list[str],
    fallback_used: bool,
    budget_check,
    global_budget_check,
    events: Iterable[LLMRouterEvent],
    errors: Iterable[dict[str, Any]],
    resolution_trace: Iterable[dict[str, Any]],
    prepared: PreparedLLMRequest,
    cache_metadata: dict[str, Any] | None = None,
) -> LLMResponse:
    metadata = dict(response.metadata)
    if cache_metadata and cache_metadata.get("llm_cache_hit"):
        metadata = _strip_prior_call_metadata(metadata)
    event_payloads = _event_payloads(events)
    manifest = _build_route_manifest(
        route_id=route_id,
        status="succeeded",
        request=request,
        response=response,
        selected_deployment=deployment,
        attempted_deployments=attempted_deployments,
        fallback_used=fallback_used,
        budget_check=budget_check,
        global_budget_check=global_budget_check,
        events=event_payloads,
        errors=errors,
        resolution_trace=resolution_trace,
        error=None,
        prepared=prepared,
    )
    if cache_metadata is not None:
        safe_cache_metadata = redact_sensitive_values(dict(cache_metadata))
        manifest["cache"] = safe_cache_metadata
        metadata.update(safe_cache_metadata)
    metrics = dict(manifest["metrics"])
    metadata.update(
        {
            "llm_route_id": route_id,
            "llm_deployment_id": deployment.deployment_id,
            "llm_provider": deployment.provider,
            "llm_model": deployment.model,
            "llm_capabilities": deployment.capabilities.to_dict(),
            "llm_fallback_used": fallback_used,
            "llm_attempted_deployments": list(attempted_deployments),
            "llm_fallback_count": metrics["fallback_count"],
            "llm_router_event_count": metrics["event_count"],
            "llm_logical_request_count": metrics["logical_request_count"],
            "llm_provider_call_count": metrics["provider_call_count"],
            "llm_cache_hit_count": metrics["cache_hit_count"],
            "llm_provider_resolution_trace": list(_safe_resolution_trace(resolution_trace)),
            "llm_router_events": event_payloads,
            "llm_route_manifest": manifest,
            "llm_prepared_request": prepared.to_dict(),
        }
    )
    if budget_check is not None:
        metadata["llm_estimated_cost_usd"] = budget_check.estimated_cost_usd
        metadata["llm_budget_check"] = budget_check.to_dict()
    if global_budget_check is not None:
        metadata["llm_global_budget_check"] = global_budget_check.to_dict()
        metadata["llm_global_budget_usage"] = global_budget_check.usage.to_dict()
        metadata["llm_budget_operation_id"] = global_budget_check.operation_id
        metadata["llm_budget_reservation_id"] = global_budget_check.reservation_id
    return replace(response, metadata=metadata)


def _bind_router_response_identity(
    value: Any,
    expected: Any,
) -> LLMResponse:
    try:
        response = LLMResponse.from_any(value)
    except Exception as exc:
        raise _LLMResponseIdentityError(
            expected=expected,
            actual=None,
            response=LLMResponse(),
        ) from exc
    actual = response.execution_identity
    if actual != expected:
        raise _LLMResponseIdentityError(
            expected=expected,
            actual=actual,
            response=response,
        )
    return response


def _identity_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        return dict(payload) if isinstance(payload, dict) else None
    return None


def _event_payloads(events: Iterable[LLMRouterEvent | dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for event in events:
        if isinstance(event, LLMRouterEvent):
            payloads.append(event.to_dict())
        else:
            payloads.append(redact_sensitive_values(dict(event)))
    return payloads


_SAFE_ROUTE_ERROR_FIELDS = frozenset(
    {
        "attempt_index",
        "attempts",
        "budget_check",
        "context_admission",
        "capability_revision",
        "cooldown_state",
        "cooldown_until",
        "deployment_id",
        "diagnostics",
        "error_category",
        "error_class",
        "error_type",
        "global_budget_check",
        "missing_capabilities",
        "model",
        "protocol_reason",
        "provider",
        "provider_call",
        "provider_reported_limit_tokens",
        "provider_reported_usage_tokens",
        "response_fingerprint",
        "expected_identity",
        "actual_identity",
        "retryable",
        "status_code",
        "visible_output",
    }
)
_SAFE_RESOLUTION_TRACE_FIELDS = frozenset({"matched", "route_id", "source"})
_SAFE_MESSAGE_ROLES = frozenset({"assistant", "developer", "system", "tool", "user"})


def _safe_resolution_trace(
    resolution_trace: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            key: value
            for key, value in dict(item).items()
            if key in _SAFE_RESOLUTION_TRACE_FIELDS
        }
        for item in resolution_trace
    )


def _safe_route_error_payload(error: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in dict(error).items()
        if key in _SAFE_ROUTE_ERROR_FIELDS
    }
    if "error_type" not in payload:
        payload["error_type"] = "redacted_error_detail"
    return redact_sensitive_values(payload)


def _safe_request_diagnostic_projection(request: LLMRequest) -> dict[str, Any]:
    message_roles = []
    for message in request.messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        message_roles.append(role if role in _SAFE_MESSAGE_ROLES else "unknown")
    return {
        "message_count": len(request.messages),
        "message_roles": message_roles,
        "tool_count": len(request.tools),
        "metadata_field_count": len(request.metadata),
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_format_present": request.response_format is not None,
        "output_schema_present": request.output_schema is not None,
    }


def _safe_response_diagnostic_projection(response: LLMResponse) -> dict[str, Any]:
    content = response.content if isinstance(response.content, str) else ""
    return {
        "content_present": response.content is not None,
        "content_char_count": len(content),
        "structured_output_present": response.structured_output is not None,
        "tool_call_count": len(response.tool_calls),
        "usage": response.usage.to_dict(),
    }


def _build_route_manifest(
    *,
    route_id: str,
    status: str,
    request: LLMRequest,
    response: LLMResponse | None,
    selected_deployment: ModelDeployment | None,
    attempted_deployments: Iterable[str],
    fallback_used: bool,
    budget_check,
    events: Iterable[dict[str, Any]],
    errors: Iterable[dict[str, Any]],
    resolution_trace: Iterable[dict[str, Any]],
    error: dict[str, Any] | None,
    global_budget_check=None,
    prepared: PreparedLLMRequest | None = None,
) -> dict[str, Any]:
    event_payloads = [dict(event) for event in events]
    errors_payload = [_safe_route_error_payload(item) for item in errors]
    attempted_deployment_list = list(attempted_deployments)
    metrics = _route_metrics(event_payloads, attempted_deployment_list)
    manifest: dict[str, Any] = {
        "schema_version": "newsroom.llm_route_manifest.v2",
        "execution_identity": (
            request.execution_identity.to_dict()
            if request.execution_identity is not None
            else None
        ),
        "status": status,
        "route_id": route_id,
        "selected_deployment_id": (
            selected_deployment.deployment_id if selected_deployment is not None else None
        ),
        "selected_provider": selected_deployment.provider if selected_deployment is not None else None,
        "selected_model": selected_deployment.model if selected_deployment is not None else None,
        "attempted_deployments": attempted_deployment_list,
        "fallback_used": fallback_used,
        "fallback_count": metrics["fallback_count"],
        "provider_resolution_trace": list(_safe_resolution_trace(resolution_trace)),
        "events": event_payloads,
        "errors": errors_payload,
        "metrics": metrics,
        "redacted_request": _safe_request_diagnostic_projection(request),
        "context_admissions": [
            dict(event.get("metadata") or {})
            for event in event_payloads
            if event.get("event_type") == "llm_context_admission_decided"
        ],
    }
    if prepared is not None:
        manifest["prepared_request"] = prepared.to_dict()
    if response is not None:
        manifest["redacted_response"] = _safe_response_diagnostic_projection(response)
    if budget_check is not None:
        manifest["budget_check"] = budget_check.to_dict()
    if global_budget_check is not None:
        if hasattr(global_budget_check, "to_dict"):
            manifest["global_budget_check"] = global_budget_check.to_dict()
            manifest["global_budget_usage"] = global_budget_check.usage.to_dict()
        elif isinstance(global_budget_check, dict):
            manifest["global_budget_check"] = dict(global_budget_check)
            usage = global_budget_check.get("usage")
            if isinstance(usage, dict):
                manifest["global_budget_usage"] = dict(usage)
    if error is not None:
        manifest["error"] = redact_sensitive_values(dict(error))
    return redact_sensitive_values(manifest)


def _route_metrics(
    events: Iterable[dict[str, Any]],
    attempted_deployments: Iterable[str],
) -> dict[str, Any]:
    event_list = [dict(event) for event in events]
    return {
        "logical_request_count": 1,
        "provider_call_count": sum(
            1
            for event in event_list
            if event.get("event_type") == "llm_provider_call_started"
        ),
        "cache_hit_count": sum(
            1 for event in event_list if event.get("event_type") == "llm_cache_hit"
        ),
        "attempt_count": len(list(attempted_deployments)),
        "event_count": len(event_list),
        "fallback_count": sum(
            1
            for event in event_list
            if event.get("event_type")
            in {"llm_fallback_selected", "llm_context_capacity_fallback_selected"}
        ),
        "provider_error_count": sum(
            1
            for event in event_list
            if event.get("event_type") == "llm_deployment_attempt_failed"
            and (event.get("metadata") or {}).get("error_type") != "client_error"
        ),
        "cooldown_skip_count": sum(
            1
            for event in event_list
            if event.get("event_type") == "llm_deployment_skipped"
            and (event.get("metadata") or {}).get("reason") == "deployment_in_cooldown"
        ),
    }


def _strip_prior_call_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    exact_keys = {
        "call_id",
        "request_id",
        "run_id",
        "span_id",
        "trace_id",
    }
    stale_prefixes = (
        "llm_budget_",
        "llm_cache_",
        "llm_call_",
        "llm_context_",
        "llm_deployment_",
        "llm_fallback_",
        "llm_global_budget_",
        "llm_logical_",
        "llm_provider_",
        "llm_prepared_",
        "llm_route_",
        "llm_router_",
    )
    return {
        key: value
        for key, value in metadata.items()
        if key not in exact_keys and not key.startswith(stale_prefixes)
    }


def _fallback_count(events: Iterable[LLMRouterEvent]) -> int:
    return sum(
        1
        for event in events
        if event.event_type
        in {"llm_fallback_selected", "llm_context_capacity_fallback_selected"}
    )


def _event_name_alias(event_type: str) -> str:
    aliases = {
        "llm_deployment_attempt_failed": "route_attempt_failed",
        "llm_fallback_selected": "fallback_selected",
        "llm_deployment_skipped": "deployment_skipped_cooldown",
    }
    return aliases.get(event_type, event_type.removeprefix("llm_"))


def _canonical_error_category(error_type: str) -> str:
    aliases = {
        "rate_limited": "rate_limit",
        "provider_timeout": "timeout",
        "provider_connection_error": "transient_network",
        "temporary_provider_error": "transient_network",
        "provider_server_error": "server_error",
        "invalid_api_key": "auth_error",
        "invalid_request_schema": "invalid_request",
        "context_length_exceeded": "context_length",
        "invalid_model": "unsupported_model",
        "provider_response_shape_invalid": "schema_error",
        "provider_stream_chunk_invalid": "schema_error",
        "tool_call_parse_error": "schema_error",
        "stream_tool_call_parse_error": "schema_error",
        "structured_output_parse_error": "schema_error",
        "provider_schema_ineligible": "schema_error",
        "structured_output_schema_error": "schema_error",
        "structured_output_validation_error": "schema_error",
    }
    return aliases.get(error_type, error_type)


def _last_global_budget_check(errors: Iterable[dict[str, Any]]):
    for error in reversed([dict(item) for item in errors]):
        check = error.get("global_budget_check")
        if check is not None:
            return check
    return None
