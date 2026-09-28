from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from hashlib import sha256
from pathlib import Path
from typing import Any

from framework.shared.attempts import (
    AdmissionResult,
    AttemptIdentity,
    AttemptCancelledError,
    AttemptCapacityExhaustedError,
    AttemptContext,
    AttemptState,
    AttemptSupervisor,
    AttemptLifecycleSink,
    AttemptOutcome,
    DeadlineAdmissionPolicy,
    ExecutionLimits,
    LocalRetryBudget,
    RetryCreditLedger,
    current_attempt_context,
    derive_idempotency_key,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.execution_environment import (
    ExecutionEnvironmentError,
    ExecutionEnvironmentUnavailableError,
    ExecutionEnvironmentRegistry,
    ExecutionIdentityMismatchError,
    ExecutionMode,
    ExecutionOutcome,
    ExecutionPolicyViolationError,
    ExecutionProfile,
    ExecutionRequest,
    ResourceLimits,
    RuntimeCompositionProfileError,
)
from framework.shared.json import to_jsonable
from framework.events.propagation import (
    W3CSpanContext,
    current_trace_context,
    trace_context_scope,
)
from framework.events.trace import TraceContext
from framework.governance import CompositeAndGate, GateCheckResult
from framework.tool.governance.approval import ToolApprovalRequest
from framework.tool.governance.boundary import is_external_fetch_tool, is_restricted_agent_id
from framework.tool.governance.redaction import (
    contains_redacted_value,
    redact_sensitive_values,
    restore_redacted_booleans,
)
from framework.tool.governance.secrets import SecretProvider
from framework.tool.inspection.metrics import ToolEvent, ToolExecutionRecord, ToolMetrics
from framework.tool.models import (
    ArtifactRef,
    ToolCall,
    ToolObservation,
    ToolPermissionError,
    ToolPolicy,
    ToolPolicyTrace,
    ToolResult,
    ToolRuntimeError,
    ToolIndeterminateError,
    ToolSecretError,
    ToolStatus,
    ToolTimeoutError,
    is_default_dangerous_tool_name,
    timed_tool_call,
)
from framework.tool.registry.registry import ToolRegistry
from framework.tool.schema.validation import normalize_tool_arguments
from framework.tool.runtime.evidence import (
    ToolEvidencePersistenceError,
    ToolExecutionEvidencePort,
)


_RUNTIME_SECRETS_ARGUMENT = "_secrets"


class _ToolAttemptEventMirror:
    """Keep local ToolEvent telemetry aligned with admitted physical starts."""

    required = False

    def __init__(self, executor: "ToolExecutor", call: ToolCall) -> None:
        self._executor = executor
        self._call = call
        self._tool_started_emitted = False

    def rejected(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        idempotency_key: str,
        admission: AdmissionResult,
    ) -> None:
        if operation_kind != "tool_call":
            return
        self._executor._emit(
            "attempt_admission_rejected",
            self._call,
            {
                "operation_id": operation_id,
                "operation_kind": operation_kind,
                "idempotency_key": idempotency_key,
                "started": False,
                "reason_code": admission.reason_code,
                "deadline_calculation": {
                    key: value
                    for key, value in admission.details.items()
                    if key.endswith("until") or key.endswith("seconds")
                },
            },
        )

    def started(self, *, context: AttemptContext) -> None:
        if context.operation_kind != "tool_call":
            return
        payload = {
            "operation_id": context.operation_id,
            "operation_kind": context.operation_kind,
            "idempotency_key": context.idempotency_key,
            "started": True,
            "attempt_id": context.attempt_id,
            "local_attempt_no": context.local_attempt_no,
            "retry_credit_id": context.retry_credit_id,
            "parent_attempt_id": context.parent_attempt_id,
        }
        self._executor._emit("attempt_started", self._call, payload)

    def physical_admitted(self, identity: AttemptIdentity) -> None:
        """Emit physical-start telemetry only after required intent is durable."""

        if not self._tool_started_emitted:
            self._tool_started_emitted = True
            self._executor._emit("tool_started", self._call, {
                "attempt_id": identity.attempt_id,
                "local_attempt_no": identity.local_attempt_no,
                "operation_id": identity.operation_id,
                "idempotency_key": identity.idempotency_key,
            })

    def terminal(self, *, outcome: Any) -> None:
        context = outcome.context
        if context is None or context.operation_kind != "tool_call":
            return
        state = (
            "INDETERMINATE"
            if outcome.indeterminate or outcome.state is AttemptState.INDETERMINATE
            else (
                "SUCCEEDED"
                if outcome.state is AttemptState.SUCCEEDED
                else "TIMED_OUT"
                if outcome.state is AttemptState.TIMED_OUT
                else "FAILED"
            )
        )
        self._executor._emit(
            "attempt_terminal",
            self._call,
            {
                "operation_id": context.operation_id,
                "operation_kind": context.operation_kind,
                "idempotency_key": context.idempotency_key,
                "started": True,
                "attempt_id": context.attempt_id,
                "local_attempt_no": context.local_attempt_no,
                "retry_credit_id": context.retry_credit_id,
                "parent_attempt_id": context.parent_attempt_id,
                "state": state,
                "reason_code": getattr(outcome, "reason_code", None)
                or getattr(outcome.error, "code", None)
                or (type(outcome.error).__name__ if outcome.error else None),
                "termination_confirmed": outcome.termination_confirmed,
                "indeterminate": outcome.indeterminate,
                "elapsed_seconds": outcome.elapsed_seconds,
            },
        )


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        artifact_manager: Any | None = None,
        run_id: str | None = None,
        approval_store: Any | None = None,
        secret_provider: SecretProvider | None = None,
        trace_context: TraceContext | W3CSpanContext | None = None,
        defer_result_persistence: bool = False,
        graph_identity: GraphExecutionIdentity | Mapping[str, Any] | None = None,
        execution_environment: Any | None = None,
        execution_environment_registry: Any | None = None,
        runtime_event_sink: Any | None = None,
        runtime_event_projection: Any | None = None,
        require_explicit_execution_profile: bool = False,
        execution_evidence: ToolExecutionEvidencePort | None = None,
    ) -> None:
        self._registry = registry
        self._artifact_manager = artifact_manager
        if graph_identity is not None and not isinstance(
            graph_identity, GraphExecutionIdentity
        ):
            graph_identity = GraphExecutionIdentity.from_dict(graph_identity)
        if graph_identity is not None and run_id is not None and run_id != graph_identity.run_id:
            raise ValueError("run_id must match graph_identity.run_id")
        self._graph_identity = graph_identity
        self._run_id = graph_identity.run_id if graph_identity is not None else run_id
        self._approval_store = approval_store
        self._secret_provider = secret_provider
        self._trace_context = trace_context
        if not isinstance(defer_result_persistence, bool):
            raise TypeError("defer_result_persistence must be boolean")
        self._defer_result_persistence = defer_result_persistence
        if execution_environment is not None and execution_environment_registry is not None:
            raise ValueError(
                "provide only one of execution_environment or execution_environment_registry"
            )
        self._execution_environment = (
            execution_environment
            if execution_environment is not None
            else execution_environment_registry
        )
        if runtime_event_sink is not None and runtime_event_projection is not None:
            raise ValueError("provide only one of runtime_event_sink or runtime_event_projection")
        self._runtime_event_sink = runtime_event_sink or runtime_event_projection
        if not isinstance(require_explicit_execution_profile, bool):
            raise TypeError("require_explicit_execution_profile must be boolean")
        self._require_explicit_execution_profile = require_explicit_execution_profile
        if execution_evidence is not None and (
            not isinstance(execution_evidence, ToolExecutionEvidencePort)
            or getattr(execution_evidence, "is_durable", False) is not True
        ):
            raise TypeError(
                "execution_evidence must implement the durable "
                "ToolExecutionEvidencePort"
            )
        self._execution_evidence = execution_evidence
        self._events: list[ToolEvent] = []
        self._metrics = ToolMetrics()
        self._records: list[ToolExecutionRecord] = []

    @property
    def metrics(self) -> ToolMetrics:
        return self._metrics.snapshot()

    def list_events(self) -> list[ToolEvent]:
        return list(self._events)

    def list_records(self) -> list[ToolExecutionRecord]:
        return list(self._records)

    @property
    def defers_result_persistence(self) -> bool:
        return self._defer_result_persistence

    @property
    def execution_evidence(self) -> ToolExecutionEvidencePort | None:
        return self._execution_evidence

    def execute(self, call: ToolCall, policy: ToolPolicy | None = None) -> ToolObservation:
        call = self._bind_graph_identity(call)
        parent_context = self._trace_context or current_trace_context()
        if isinstance(parent_context, TraceContext):
            execution_context: TraceContext | W3CSpanContext = (
                parent_context
                if parent_context.tool_call_id == call.call_id
                else parent_context.child(tool_call_id=call.call_id)
            )
        elif isinstance(parent_context, W3CSpanContext):
            execution_context = parent_context.child()
        else:
            execution_context = W3CSpanContext.root()
        with trace_context_scope(execution_context):
            return self._execute_scoped(call, policy)

    def record_pre_execution_rejection(
        self,
        observation: ToolObservation,
    ) -> ToolObservation:
        """Persist a caller-owned rejection which made no physical invocation."""

        if not isinstance(observation, ToolObservation):
            raise TypeError("observation must be ToolObservation")
        if observation.status not in {
            ToolStatus.BLOCKED,
            ToolStatus.DENIED,
            ToolStatus.APPROVAL_REQUIRED,
        }:
            raise ValueError("pre-execution rejection must use a rejected status")
        call = self._bind_graph_identity(observation.call)
        definition = None
        if self._execution_evidence is not None:
            _required_evidence_write(
                "register_call",
                self._execution_evidence.register_call,
                call,
            )
            try:
                definition = self._registry.get(call.tool_name).definition
            except ToolDefinitionError:
                definition = None
            if definition is not None:
                _required_evidence_write(
                    "bind_tool",
                    self._execution_evidence.bind_tool,
                    call,
                    definition,
                )
        rejected = ToolObservation(
            call=call,
            result=_with_call(observation.result, call),
            elapsed_ms=observation.elapsed_ms,
        )
        if self._execution_evidence is not None:
            evidence_ref = _required_evidence_write(
                "commit_observation",
                self._execution_evidence.commit_observation,
                rejected,
                definition,
            )
            if not _is_canonical_evidence_ref(evidence_ref):
                raise ToolEvidencePersistenceError(
                    "tool evidence writer returned an invalid receipt reference"
                )
            rejected = ToolObservation(
                call=call,
                result=_copy_tool_result(
                    rejected.result,
                    metadata={
                        **rejected.result.metadata,
                        "tool_evidence_ref": evidence_ref,
                    },
                ),
                elapsed_ms=rejected.elapsed_ms,
            )
        self._record_observation(rejected)
        return rejected

    def _bind_graph_identity(self, call: ToolCall) -> ToolCall:
        if not isinstance(call, ToolCall):
            raise TypeError("call must be ToolCall")
        if self._graph_identity is None:
            return call
        if call.graph_identity is None:
            return replace(call, graph_identity=self._graph_identity)
        if call.graph_identity != self._graph_identity:
            raise ValueError("tool call Graph identity conflicts with executor identity")
        return call

    def _execute_scoped(
        self,
        call: ToolCall,
        policy: ToolPolicy | None = None,
    ) -> ToolObservation:
        scoped_context = current_trace_context()
        event_trace_context = (
            scoped_context
            if isinstance(scoped_context, (TraceContext, W3CSpanContext))
            else None
        )
        policy = policy or ToolPolicy(require_explicit_allowlist=False)
        started_at = datetime.now(UTC)
        policy_trace = _ToolPolicyTraceBuilder(call.tool_name)
        attempts_used = 0
        last_attempt_context: AttemptContext | None = None
        resolved_definition: Any | None = None
        execution_receipt_metadata: dict[str, Any] = {}

        if self._execution_evidence is not None:
            _required_evidence_write(
                "register_call",
                self._execution_evidence.register_call,
                call,
            )

        def record_attempt(attempt: int, context: AttemptContext) -> None:
            nonlocal attempts_used, last_attempt_context
            attempts_used = max(attempts_used, int(attempt))
            last_attempt_context = context

        def invoke() -> ToolResult:
            nonlocal attempts_used, last_attempt_context, resolved_definition
            registered = self._registry.get(call.tool_name)
            resolved_definition = registered.definition
            if self._execution_evidence is not None:
                _required_evidence_write(
                    "bind_tool",
                    self._execution_evidence.bind_tool,
                    call,
                    resolved_definition,
                )
            policy_trace.risk_level = _risk_level(registered.definition)
            policy_trace.requires_approval = _requires_approval(registered.definition, policy)
            policy_trace.add("tool.resolve", "compatibility", True, f"tool resolved: {call.tool_name}")

            if (
                call.graph_identity is not None
                and call.tool_name in {"memory.recall", "memory.explain"}
                and registered.graph_identity is None
            ):
                policy_trace.add("tool.graph_identity", "safety", False, "Graph memory requires execution-bound registration")
                raise ToolPermissionError("Graph memory requires execution-bound registration")
            if registered.graph_identity is not None and call.graph_identity != registered.graph_identity:
                policy_trace.add(
                    "tool.graph_identity",
                    "safety",
                    False,
                    "tool requires the exact Graph activity identity bound at registration",
                )
                raise ToolPermissionError(
                    "tool call Graph identity does not match its registered Graph activity"
                )
            if registered.graph_identity is not None:
                policy_trace.add(
                    "tool.graph_identity",
                    "safety",
                    True,
                    "tool call matches its registered Graph activity identity",
                )

            if not policy.allows(call.tool_name):
                policy_trace.add("tool.permission", "safety", False, f"tool is not allowed: {call.tool_name}")
                raise ToolPermissionError(
                    f"agent {call.requested_by_agent_id} is not allowed to call {call.tool_name}"
                )
            policy_trace.add("tool.permission", "safety", True, "tool allowed by policy")

            restricted_agent_result = _restricted_agent_boundary_gate(call)
            if restricted_agent_result is not None:
                policy_trace.add(
                    "tool.boundary",
                    "safety",
                    False,
                    restricted_agent_result.error_message or "restricted agent boundary blocked",
                )
                return restricted_agent_result
            policy_trace.add("tool.boundary", "safety", True, "agent boundary passed")

            dangerous_result = _dangerous_gate(registered.definition, policy)
            if dangerous_result is not None:
                policy_trace.add(
                    "tool.risk",
                    "safety",
                    False,
                    dangerous_result.error_message or "dangerous tool blocked",
                )
                return dangerous_result
            policy_trace.add("tool.risk", "safety", True, "risk gate passed")

            if _RUNTIME_SECRETS_ARGUMENT in call.arguments:
                policy_trace.add(
                    "tool.arguments.reserved",
                    "safety",
                    False,
                    f"reserved tool argument is not allowed: {_RUNTIME_SECRETS_ARGUMENT}",
                )
                raise ToolRuntimeError(
                    f"reserved tool argument is not allowed: {_RUNTIME_SECRETS_ARGUMENT}"
                )

            arguments = normalize_tool_arguments(registered.definition, call.arguments)
            self._emit("tool_args_validated", call)
            policy_trace.add("tool.arguments", "compatibility", True, "arguments validated")

            approval_result = self._approval_gate(call, registered.definition, policy)
            if approval_result is not None:
                policy_trace.approval_granted = False
                policy_trace.add(
                    "tool.approval",
                    "safety",
                    False,
                    approval_result.output_summary or "tool approval required",
                    severity="warning",
                )
                return approval_result
            if policy_trace.requires_approval:
                policy_trace.approval_granted = True
            policy_trace.add("tool.approval", "safety", True, "approval gate passed")

            max_attempts = _max_attempts(registered.definition, policy)
            execution_receipt_metadata.clear()
            raw_profile = registered.definition.metadata.get("execution_profile")
            if raw_profile is None:
                arguments = _arguments_with_secrets(
                    arguments,
                    registered.definition,
                    self._secret_provider,
                )
                policy_trace.add("tool.secrets", "safety", True, "secret injection passed")
            else:
                # Named secret handles are resolved by the physical provider;
                # raw secret values must never enter a sandbox argv or env.
                policy_trace.add(
                    "tool.secrets",
                    "safety",
                    True,
                    "sandbox execution delegates secret handles to the provider",
                )
            physical_executor = self._resolve_executor(
                registered,
                call,
                arguments,
                receipt_holder=execution_receipt_metadata,
            )
            raw_output, attempts_used, last_attempt_context = _invoke_with_retry(
                _trace_scoped_executor(physical_executor, scoped_context),
                arguments,
                _timeout_seconds(registered.definition, policy),
                max_attempts,
                call.tool_name,
                record_attempt,
                definition=registered.definition,
                policy=policy,
                idempotency_key=_tool_idempotency_key(call),
                operation_id=_tool_idempotency_key(call),
                attempt_event_sink=_ToolAttemptEventMirror(self, call),
                execution_evidence=self._execution_evidence,
                call=call if self._execution_evidence is not None else None,
            )
            policy_trace.add(
                "tool.retry",
                "resource",
                True,
                "retry policy completed",
                metadata={
                    "max_attempts": max_attempts,
                    "attempts": attempts_used,
                    "retry_count": max(0, attempts_used - 1),
                },
            )
            result_contract = registered.definition.result_persistence
            result_contract.validate_output(
                raw_output,
                tool_name=registered.definition.name,
                output_schema=registered.definition.output_schema,
            )
            safe_output = _safe_tool_output(
                raw_output,
                result_contract.media_type,
            )
            if contains_redacted_value(safe_output):
                self._emit("tool_result_redacted", call, {"redacted": True})
                policy_trace.add("tool.redaction", "safety", True, "output redacted")
            else:
                policy_trace.add("tool.redaction", "safety", True, "output did not require redaction")
            output_bytes = _result_size_bytes(
                safe_output,
                result_contract.media_type,
            )
            output_guardrail_result = self._output_size_guard(
                call,
                registered.definition,
                output_bytes,
            )
            if output_guardrail_result is not None:
                policy_trace.add(
                    "tool.output_size",
                    "resource",
                    False,
                    output_guardrail_result.error_message or "output size gate failed",
                    metadata={"output_bytes": output_bytes},
                )
                return _copy_tool_result(
                    output_guardrail_result,
                    **_attempt_result_fields(last_attempt_context),
                )
            policy_trace.add(
                "tool.output_size",
                "resource",
                True,
                "output size gate passed",
                metadata={"output_bytes": output_bytes},
            )
            tool_result = self._tool_result(
                call,
                safe_output,
                policy,
                output_bytes,
                media_type=result_contract.media_type,
            )
            if execution_receipt_metadata:
                tool_result = _copy_tool_result(
                    tool_result,
                    metadata={**tool_result.metadata, **execution_receipt_metadata},
                )
            return _copy_tool_result(
                tool_result,
                **_attempt_result_fields(last_attempt_context),
            )

        self._emit(
            "tool_call_requested",
            call,
            {"argument_keys": sorted(str(key) for key in call.arguments)},
        )
        try:
            result, elapsed_ms = timed_tool_call(invoke)
        except ToolEvidencePersistenceError:
            raise
        except ToolPermissionError as exc:
            policy_trace.add("tool.permission.error", "safety", False, str(exc))
            result = ToolResult(
                status=ToolStatus.BLOCKED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                call_id=call.call_id,
                tool_name=call.tool_name,
            )
            elapsed_ms = 0.0
        except ToolTimeoutError as exc:
            policy_trace.add("tool.timeout", "resource", False, str(exc))
            result = ToolResult(
                status=ToolStatus.TIMEOUT,
                error_type=type(exc).__name__,
                error_message=str(exc),
                call_id=call.call_id,
                tool_name=call.tool_name,
                termination_confirmed=exc.termination_confirmed,
                indeterminate=exc.indeterminate,
                attempt_id=exc.attempt_id,
                idempotency_key=exc.idempotency_key,
                operation_id=exc.operation_id,
                operation_kind=exc.operation_kind,
                local_attempt_no=exc.local_attempt_no,
                retry_credit_id=exc.retry_credit_id,
                metadata={
                    "termination_confirmed": exc.termination_confirmed,
                    "indeterminate": exc.indeterminate,
                },
            )
            elapsed_ms = 0.0
        except ToolIndeterminateError as exc:
            policy_trace.add("tool.indeterminate", "safety", False, str(exc))
            result = ToolResult(
                status=ToolStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                call_id=call.call_id,
                tool_name=call.tool_name,
                termination_confirmed=exc.termination_confirmed,
                indeterminate=exc.indeterminate,
                attempt_id=exc.attempt_id,
                idempotency_key=exc.idempotency_key,
                operation_id=exc.operation_id,
                operation_kind=exc.operation_kind,
                local_attempt_no=exc.local_attempt_no,
                retry_credit_id=exc.retry_credit_id,
                metadata={
                    "termination_confirmed": exc.termination_confirmed,
                    "indeterminate": exc.indeterminate,
                    "cause_type": exc.cause_type,
                },
            )
            elapsed_ms = 0.0
        except Exception as exc:
            policy_trace.add("tool.execution", "compatibility", False, str(exc))
            execution_error_metadata = (
                {
                    "reason_code": exc.reason_code,
                    "details": dict(exc.details),
                    "execution_error_type": type(exc).__name__,
                }
                if isinstance(exc, ExecutionEnvironmentError)
                else {}
            )
            if isinstance(exc, ExecutionEnvironmentError):
                execution_error_metadata["execution_environment_halt"] = True
            result = ToolResult(
                status=ToolStatus.FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                call_id=call.call_id,
                tool_name=call.tool_name,
                metadata=execution_error_metadata,
                **_attempt_result_fields(last_attempt_context),
            )
            elapsed_ms = 0.0

        if execution_receipt_metadata:
            result = _copy_tool_result(
                result,
                metadata={**result.metadata, **execution_receipt_metadata},
            )

        result = _standardize_tool_result(
            _with_tool_gate(_with_duration(_with_call(result, call), elapsed_ms), call),
            call=call,
            policy_trace=policy_trace,
            trace_context=event_trace_context,
            retry_count=max(0, attempts_used - 1),
        )
        if result.metadata.get("execution_environment_halt"):
            result = _copy_tool_result(
                result,
                error_type=str(
                    result.metadata.get("reason_code")
                    or "execution_environment_unavailable"
                ),
                error_message=result.error_message,
            )
        if resolved_definition is not None:
            metadata = {
                **result.metadata,
                "resolved_tool_id": resolved_definition.tool_id,
            }
            result_fields: dict[str, Any] = {"metadata": metadata}
            if (
                resolved_definition.side_effect_value.casefold()
                not in {"", "none", "read_only"}
                and result.status
                not in {
                    ToolStatus.BLOCKED,
                    ToolStatus.DENIED,
                    ToolStatus.APPROVAL_REQUIRED,
                }
                and result.idempotency_key is None
            ):
                logical_idempotency_key = _tool_idempotency_key(call)
                result_fields.update(
                    idempotency_key=logical_idempotency_key,
                    operation_id=logical_idempotency_key,
                    operation_kind="tool_call",
                    termination_confirmed=True,
                )
            result = _copy_tool_result(
                result,
                **result_fields,
            )
        observation = ToolObservation(call=call, result=result, elapsed_ms=elapsed_ms)
        if self._execution_evidence is not None:
            evidence_ref = _required_evidence_write(
                "commit_observation",
                self._execution_evidence.commit_observation,
                observation,
                resolved_definition,
            )
            if not _is_canonical_evidence_ref(evidence_ref):
                raise ToolEvidencePersistenceError(
                    "tool evidence writer returned an invalid receipt reference"
                )
            observation = ToolObservation(
                call=observation.call,
                result=_copy_tool_result(
                    observation.result,
                    metadata={
                        **observation.result.metadata,
                        "tool_evidence_ref": evidence_ref,
                    },
                ),
                elapsed_ms=observation.elapsed_ms,
            )
        self._record_observation(observation)
        self._records.append(
            _execution_record(
                observation,
                self._events,
                started_at,
                trace_context=event_trace_context,
            )
        )
        return observation

    def _resolve_executor(
        self,
        registered: Any,
        call: ToolCall,
        arguments: dict[str, Any],
        *,
        receipt_holder: dict[str, Any] | None = None,
    ) -> Any:
        """Select the physical execution boundary declared by the tool.

        Existing tools remain trusted only when they do not declare an
        ``execution_profile``.  A tool that declares ``sandboxed_process`` can
        never fall back to its in-process callable: a missing registry or an
        invalid request is a typed fail-closed error.
        """
        metadata = registered.definition.metadata
        raw_profile = metadata.get("execution_profile")
        if raw_profile is None:
            if self._require_explicit_execution_profile:
                raise RuntimeCompositionProfileError(
                    "tool must explicitly declare trusted_in_process or sandboxed_process execution",
                    details={
                        "tool_id": registered.definition.tool_id,
                        "missing": ["execution_profile"],
                    },
                )
            return registered.executor
        try:
            profile = (
                raw_profile
                if isinstance(raw_profile, ExecutionProfile)
                else ExecutionProfile.from_dict(raw_profile)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeCompositionProfileError(
                f"tool {call.tool_name} has an invalid execution profile",
                details={
                    "tool_id": registered.definition.tool_id,
                    "profile_error_type": type(exc).__name__,
                },
            ) from exc
        if profile.mode is ExecutionMode.TRUSTED_IN_PROCESS:
            return registered.executor
        registry = self._execution_environment
        if registry is None:
            raise ExecutionEnvironmentUnavailableError(
                "sandboxed tool execution requires a Harness ExecutionEnvironment",
                details={
                    "tool_id": registered.definition.tool_id,
                    "missing": ["execution_environment"],
                },
            )
        # Physical profiles must route through the Harness-owned registry so
        # provider capability admission cannot be bypassed by a duck-typed
        # object that exposes only ``execute``.
        if not isinstance(registry, ExecutionEnvironmentRegistry):
            raise ExecutionEnvironmentUnavailableError(
                "sandboxed tool execution requires an ExecutionEnvironmentRegistry",
                details={
                    "tool_id": registered.definition.tool_id,
                    "missing": ["execution_environment_registry"],
                },
            )
        if not hasattr(registry, "execute"):
            raise ExecutionEnvironmentUnavailableError(
                "execution environment does not expose execute",
                details={
                    "tool_id": registered.definition.tool_id,
                    "missing": ["execution_environment.execute"],
                },
            )

        execution_spec = metadata.get("execution")
        if not isinstance(execution_spec, Mapping):
            raise ExecutionPolicyViolationError(
                "sandboxed tool must declare an execution object with image and argv",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "missing": ["execution"],
                },
            )
        graph_identity = call.graph_identity or self._graph_identity
        if graph_identity is None:
            raise ExecutionIdentityMismatchError(
                "sandboxed tool execution requires an exact Graph identity",
                details={
                    "tool_id": registered.definition.tool_id,
                    "missing": ["graph_identity"],
                },
            )
        image = execution_spec.get("image")
        argv = execution_spec.get("argv")
        if isinstance(argv, str):
            argv = (argv,)
        if not isinstance(argv, (tuple, list)):
            raise ExecutionPolicyViolationError(
                "sandboxed tool execution argv must be an array",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "field": "argv",
                },
            )
        if any(not isinstance(token, str) for token in argv):
            raise ExecutionPolicyViolationError(
                "sandboxed tool execution argv must contain strings",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "field": "argv",
                },
            )
        # Never serialize the complete argument object into argv: even when
        # secret injection is disabled, command lines are observable by other
        # processes.  Providers receive named ``secret_handles`` separately.
        if any(token == "{arguments}" or "{secret" in token.casefold() for token in argv):
            raise ExecutionPolicyViolationError(
                "sandbox execution cannot expand raw arguments or secrets into argv",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "field": "argv",
                },
            )
        argv = tuple(argv)
        raw_secret_handles = execution_spec.get("secret_handles", ())
        if isinstance(raw_secret_handles, (str, bytes)) or not isinstance(raw_secret_handles, (tuple, list)):
            raise ExecutionPolicyViolationError(
                "sandboxed tool secret_handles must be an array",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "field": "secret_handles",
                },
            )
        declared_secret_handles = tuple(raw_secret_handles)
        required_secret_names = tuple(getattr(registered.definition, "required_secret_names", ()) or ())
        if required_secret_names and not set(required_secret_names).issubset(set(declared_secret_handles)):
            raise ExecutionPolicyViolationError(
                "sandboxed tool must declare every required secret as a named handle",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "field": "secret_handles",
                },
            )
        attempt_context = current_attempt_context()
        operation_id = (
            attempt_context.operation_id
            if attempt_context is not None and attempt_context.operation_id
            else _tool_idempotency_key(call)
        )
        attempt_id = (
            attempt_context.attempt_id
            if attempt_context is not None and attempt_context.attempt_id
            else f"{operation_id}:attempt-1"
        )
        execution_id = f"exec:{operation_id}:{attempt_id}"
        timeout_seconds = execution_spec.get("timeout_seconds")
        if timeout_seconds is None:
            timeout_seconds = registered.definition.timeout_seconds
        cancellation_grace_seconds = execution_spec.get(
            "cancellation_grace_seconds"
        )
        if cancellation_grace_seconds is None:
            cancellation_grace_seconds = (
                registered.definition.cancellation_grace_seconds
            )
        if cancellation_grace_seconds is None:
            cancellation_grace_seconds = 5.0
        try:
            request = ExecutionRequest(
                execution_id=execution_id,
                tool_id=registered.definition.tool_id,
                graph_identity=graph_identity,
                operation_id=operation_id,
                attempt_id=attempt_id,
                profile=profile,
                image=str(image or ""),
                argv=argv,
                read_roots=_execution_paths(execution_spec.get("read_roots", ()), "read_roots"),
                write_roots=_execution_paths(execution_spec.get("write_roots", ()), "write_roots"),
                working_directory=execution_spec.get("working_directory"),
                environment=dict(execution_spec.get("environment", {})),
                secret_handles=declared_secret_handles,
                resource_limits=ResourceLimits(
                    **dict(execution_spec.get("resource_limits", {}))
                ),
                timeout_seconds=timeout_seconds,
                cancellation_grace_seconds=float(cancellation_grace_seconds),
                approval_evidence_ref=execution_spec.get("approval_evidence_ref"),
                budget_ref=execution_spec.get("budget_ref"),
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionPolicyViolationError(
                "sandboxed tool execution specification is not admitted",
                details={
                    "tool_id": registered.definition.tool_id,
                    "violation": "sandbox_execution_spec_invalid",
                    "spec_error_type": type(exc).__name__,
                },
            ) from exc

        def execute(_: dict[str, Any]) -> Any:
            try:
                execution_context = current_attempt_context()
                outcome = registry.execute(
                    request,
                    cancellation=(
                        execution_context.cancel_event
                        if execution_context is not None
                        else None
                    ),
                )
            except ExecutionEnvironmentError:
                raise
            if not isinstance(outcome, ExecutionOutcome):
                raise ToolRuntimeError("execution environment returned an invalid outcome")
            receipt = outcome.receipt
            if not receipt.matches_request(request):
                raise ToolRuntimeError("execution receipt identity does not match request")
            provider_capabilities = getattr(registry, "capabilities", None)
            provider_checksum = getattr(provider_capabilities, "checksum", None)
            if provider_checksum is not None and receipt.provider_capability_checksum != provider_checksum:
                raise ToolRuntimeError(
                    "execution receipt capability checksum does not match provider"
                )
            if receipt_holder is not None:
                receipt_holder.update(
                    {
                        "execution_id": receipt.execution_id,
                        "execution_receipt_ref": f"execution-receipt://{receipt.execution_id}",
                        "execution_receipt_checksum": receipt.receipt_checksum,
                        "execution_capability_checksum": receipt.provider_capability_checksum,
                        "execution_provider_id": receipt.provider_id,
                        "execution_status": receipt.status.value,
                        "execution_reason_code": receipt.reason_code,
                        "termination_confirmed": receipt.termination_confirmed,
                    }
                )
            if receipt.status.value in {"rejected", "indeterminate", "timed_out", "cancelled", "failed"}:
                error = ToolRuntimeError(
                    f"sandbox execution {receipt.status.value}: {receipt.reason_code}"
                )
                if receipt.status.value == "indeterminate":
                    raise ToolIndeterminateError(
                        str(error),
                        attempt_id=attempt_id,
                        idempotency_key=operation_id,
                        operation_id=operation_id,
                        operation_kind="tool_call",
                        local_attempt_no=1,
                        retry_credit_id=None,
                        cause_type=receipt.reason_code,
                    )
                raise error
            if not receipt.termination_confirmed:
                raise ToolIndeterminateError(
                    "sandbox execution succeeded without termination confirmation",
                    attempt_id=attempt_id,
                    idempotency_key=operation_id,
                    operation_id=operation_id,
                    operation_kind="tool_call",
                    local_attempt_no=1,
                    retry_credit_id=None,
                    cause_type="termination_unconfirmed",
                )
            if outcome.output is not None and receipt.output_checksum is None:
                raise ToolRuntimeError(
                    "sandbox execution output is missing its receipt checksum"
                )
            if outcome.output is not None:
                output_bytes = (
                    outcome.output
                    if isinstance(outcome.output, bytes)
                    else str(outcome.output).encode("utf-8")
                )
                expected_checksum = "sha256:" + sha256(output_bytes).hexdigest()
                if receipt.output_checksum != expected_checksum:
                    raise ToolRuntimeError(
                        "sandbox execution output checksum does not match its receipt"
                    )
                if receipt.output_bytes != len(output_bytes):
                    raise ToolRuntimeError(
                        "sandbox execution output size does not match its receipt"
                    )
            output = outcome.output
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            if isinstance(output, str) and registered.definition.result_persistence.media_type.endswith("json"):
                try:
                    return json.loads(output)
                except json.JSONDecodeError:
                    return output
            return output

        return execute

    def _tool_result(
        self,
        call: ToolCall,
        safe_output: Any,
        policy: ToolPolicy,
        output_bytes: int | None = None,
        *,
        media_type: str = "application/json",
    ) -> ToolResult:
        output_bytes = (
            output_bytes
            if output_bytes is not None
            else _result_size_bytes(safe_output, media_type)
        )
        artifact_spill = "inline"
        if (
            not self._defer_result_persistence
            and policy.spill_large_results_to_artifact
            and output_bytes > policy.max_result_chars_inline
        ):
            artifact_spill = "missing_context"
            if self._artifact_manager is None or self._run_id is None:
                self._emit(
                    "tool_output_guardrail_failed",
                    call,
                    {
                        "reason": "artifact_context_required",
                        "output_bytes": output_bytes,
                        "max_result_chars_inline": policy.max_result_chars_inline,
                    },
                )
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error_type="ToolRuntimeError",
                    error_message=(
                        "tool output exceeded max_result_chars_inline and no "
                        "artifact context is configured"
                    ),
                    output_bytes=output_bytes,
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    metadata={"artifact_spill": artifact_spill},
                    media_type=media_type,
                )
            if media_type != "application/json" and not media_type.endswith("+json"):
                return ToolResult(
                    status=ToolStatus.FAILED,
                    error_type="ToolRuntimeError",
                    error_message=(
                        "legacy ArtifactManager spill supports JSON only; "
                        "configure Harness result materialization"
                    ),
                    output_bytes=output_bytes,
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    metadata={"artifact_spill": "unsupported_media"},
                    media_type=media_type,
                )
            artifact_spill = "spilled"
            if call.graph_identity is not None:
                relative_path = (
                    "node_instances/"
                    f"{call.graph_identity.node_instance_id}/activities/"
                    f"{call.graph_identity.activity_id}/attempt-"
                    f"{call.graph_identity.attempt}/tool_results/{call.call_id}.json"
                )
            else:
                relative_path = f"tool_results/{call.call_id}.json"
            artifact_payload = {
                "call": call.to_dict(),
                "output": safe_output,
                "output_bytes": output_bytes,
                "graph_identity": (
                    call.graph_identity.to_dict()
                    if call.graph_identity is not None
                    else None
                ),
            }
            path = _write_json_artifact(self._artifact_manager, self._run_id, relative_path, artifact_payload)
            artifact_bytes = path.read_bytes()
            artifact_ref = ArtifactRef(
                artifact_id=f"tool_result:{call.call_id}",
                relative_path=relative_path,
                size_bytes=len(artifact_bytes),
                checksum=sha256(artifact_bytes).hexdigest(),
            )
            return ToolResult(
                status=ToolStatus.SUCCEEDED,
                output=None,
                output_summary=f"Tool result spilled to artifact: {relative_path}",
                artifact_refs=[artifact_ref],
                artifacts=[artifact_ref],
                output_bytes=output_bytes,
                call_id=call.call_id,
                tool_name=call.tool_name,
                metadata={
                    "artifact_spill": artifact_spill,
                    "graph_identity": (
                        call.graph_identity.to_dict()
                        if call.graph_identity is not None
                        else None
                    ),
                },
                media_type=media_type,
            )
        if self._defer_result_persistence:
            artifact_spill = "deferred"
        return ToolResult(
            status=ToolStatus.SUCCEEDED,
            output=safe_output,
            output_bytes=output_bytes,
            call_id=call.call_id,
            tool_name=call.tool_name,
            metadata={
                "artifact_spill": artifact_spill,
                "graph_identity": (
                    call.graph_identity.to_dict()
                    if call.graph_identity is not None
                    else None
                ),
            },
            media_type=media_type,
        )

    def _output_size_guard(self, call: ToolCall, definition: Any, output_bytes: int) -> ToolResult | None:
        max_result_bytes = getattr(definition, "max_result_bytes", None)
        if max_result_bytes is None or output_bytes <= max_result_bytes:
            return None
        payload = {
            "reason": "max_result_bytes_exceeded",
            "output_bytes": output_bytes,
            "max_result_bytes": max_result_bytes,
        }
        self._emit("tool_output_guardrail_failed", call, payload)
        return ToolResult(
            status=ToolStatus.FAILED,
            error_type="ToolRuntimeError",
            error_message=(
                f"tool output exceeded max_result_bytes for {definition.name}: "
                f"{output_bytes} > {max_result_bytes}"
            ),
            output_bytes=output_bytes,
            call_id=call.call_id,
            tool_name=call.tool_name,
        )

    def _record_observation(self, observation: ToolObservation) -> None:
        self._metrics.record(observation)
        if observation.status == ToolStatus.SUCCEEDED:
            self._emit("tool_succeeded", observation.call, _result_event_payload(observation))
            if observation.result.artifact_refs:
                self._emit("tool_result_spilled", observation.call, _result_event_payload(observation))
        elif observation.status == ToolStatus.FAILED:
            self._emit("tool_failed", observation.call, _result_event_payload(observation))
        elif observation.status in {ToolStatus.BLOCKED, ToolStatus.DENIED}:
            self._emit("tool_call_blocked", observation.call, _result_event_payload(observation))
        elif observation.status == ToolStatus.APPROVAL_REQUIRED:
            self._emit("tool_approval_required", observation.call, _result_event_payload(observation))
        elif observation.status == ToolStatus.TIMEOUT:
            self._emit("tool_timeout", observation.call, _result_event_payload(observation))
        self._emit(
            "tool_observation_created",
            observation.call,
            _bounded_observation_event_payload(observation),
        )

    def _emit(self, event_type: str, call: ToolCall, payload: dict[str, Any] | None = None) -> ToolEvent:
        scoped_context = current_trace_context()
        event = ToolEvent.from_trace(
            event_type=event_type,
            tool_name=call.tool_name,
            tool_call_id=call.call_id,
            payload=payload or {},
            trace_context=(
                scoped_context
                if isinstance(scoped_context, (TraceContext, W3CSpanContext))
                else None
            ),
            graph_identity=call.graph_identity,
        )
        self._events.append(event)
        self._emit_runtime_event(event_type, call, payload or {})
        return event

    def _emit_runtime_event(
        self,
        event_type: str,
        call: ToolCall,
        payload: dict[str, Any],
    ) -> None:
        sink = self._runtime_event_sink
        if sink is None:
            return
        from framework.events.runtime.projection import (
            RuntimeEventEnvelope,
            RuntimeEventIdentity,
            RuntimeEventType,
        )

        mapping = {
            "tool_call_requested": RuntimeEventType.TOOL_REQUESTED,
            "tool_started": RuntimeEventType.EXECUTION_STARTED,
            "attempt_started": RuntimeEventType.EXECUTION_STARTED,
            "tool_approval_required": RuntimeEventType.APPROVAL_REQUESTED,
            "tool_succeeded": RuntimeEventType.EXECUTION_TERMINAL,
            "tool_failed": RuntimeEventType.EXECUTION_TERMINAL,
            "tool_timeout": RuntimeEventType.TIMEOUT,
            "tool_call_blocked": RuntimeEventType.RUNTIME_ERROR,
            "attempt_admission_rejected": RuntimeEventType.RUNTIME_ERROR,
            "attempt_terminal": RuntimeEventType.EXECUTION_TERMINAL,
        }
        canonical_type = mapping.get(event_type, RuntimeEventType.RUNTIME_ERROR)
        identity = RuntimeEventIdentity(
            graph_identity=call.graph_identity,
            activity_id=(call.graph_identity.activity_id if call.graph_identity else None),
            attempt_id=(payload.get("attempt_id") or payload.get("operation_id")),
            node_id=(call.graph_identity.node_id if call.graph_identity else None),
            node_instance_id=(
                call.graph_identity.node_instance_id if call.graph_identity else None
            ),
        )
        status = payload.get("status")
        if status is None:
            status = {
                "tool_succeeded": "succeeded",
                "tool_failed": "failed",
                "tool_timeout": "timed_out",
                "tool_call_blocked": "rejected",
                "attempt_admission_rejected": "rejected",
            }.get(event_type)
        operation_identity = (
            payload.get("attempt_id")
            or payload.get("operation_id")
            or _tool_idempotency_key(call)
        )
        stable_event_id = "tool-runtime:" + sha256(
            f"{call.call_id}|{event_type}|{operation_identity}|{status}|{payload.get('reason_code') or payload.get('reason')}".encode("utf-8")
        ).hexdigest()
        event = RuntimeEventEnvelope(
            event_id=stable_event_id,
            event_type=canonical_type,
            occurred_at=datetime.now(UTC),
            identity=identity,
            status=str(status) if status is not None else None,
            reason_code=payload.get("reason_code") or payload.get("reason"),
            stream_id=call.graph_identity.run_id if call.graph_identity else None,
            refs=tuple(
                str(value)
                for key, value in payload.items()
                if key.endswith("_ref") and isinstance(value, str)
            ),
            checksums=(
                {
                    key: str(payload[key])
                    for key in ("execution_receipt_checksum", "execution_capability_checksum")
                    if isinstance(payload.get(key), str)
                    and payload[key].startswith("sha256:")
                }
            ),
            metadata={
                "tool_name": call.tool_name,
                "tool_call_id": call.call_id,
                "argument_keys": payload.get("argument_keys"),
                "status": status,
                "execution_receipt_ref": payload.get("execution_receipt_ref"),
                "execution_receipt_checksum": payload.get("execution_receipt_checksum"),
                "execution_capability_checksum": payload.get("execution_capability_checksum"),
                "execution_provider_id": payload.get("execution_provider_id"),
                "termination_confirmed": payload.get("termination_confirmed"),
                "execution_environment_halt": payload.get("execution_environment_halt"),
            },
            source="tool",
        )
        try:
            if callable(sink):
                sink(event)
            elif hasattr(sink, "append"):
                sink.append(event)
            elif hasattr(sink, "publish"):
                sink.publish(event)
            else:
                raise TypeError("runtime event sink must be callable or expose append/publish")
        except Exception:
            # Runtime event projection is a required canonical boundary when
            # supplied; make failures visible instead of silently losing facts.
            raise

    def _approval_gate(self, call: ToolCall, definition: Any, policy: ToolPolicy) -> ToolResult | None:
        if not policy.requires_approval(definition):
            return None

        reason = f"Tool requires approval before execution: {definition.name}"
        approval_id = None
        if self._approval_store is not None:
            request = ToolApprovalRequest(
                tool_call=call,
                tool_name=definition.name,
                side_effect=_side_effect_value(definition),
                reason=reason,
                risk_level=_risk_level(definition),
                run_id=self._run_id,
                graph_identity=call.graph_identity,
                agent_id=call.requested_by_agent_id or None,
            )
            stored = self._approval_store.upsert_approval(request.to_worker_approval_request())
            approval_id = getattr(stored, "approval_id", None)

        return ToolResult(
            status=ToolStatus.APPROVAL_REQUIRED,
            output_summary=reason,
            approval_id=approval_id,
            call_id=call.call_id,
            tool_name=call.tool_name,
        )


def _result_event_payload(observation: ToolObservation) -> dict[str, Any]:
    return {
        "status": observation.status.value,
        "elapsed_ms": observation.elapsed_ms,
        "error_type": observation.result.error_type,
        "error_message": redact_sensitive_values(
            observation.result.error_message
        ),
        "output_bytes": observation.result.output_bytes,
        "artifact_refs": [artifact_ref.to_dict() for artifact_ref in observation.result.artifact_refs],
        "termination_confirmed": observation.result.termination_confirmed,
        "indeterminate": observation.result.indeterminate,
        "attempt_id": observation.result.attempt_id,
        "operation_id": observation.result.operation_id,
        "operation_kind": observation.result.operation_kind,
        "local_attempt_no": observation.result.local_attempt_no,
        "retry_credit_id": observation.result.retry_credit_id,
        "reason_code": observation.result.metadata.get("reason_code"),
        "execution_receipt_ref": observation.result.metadata.get("execution_receipt_ref"),
        "execution_receipt_checksum": observation.result.metadata.get("execution_receipt_checksum"),
        "execution_capability_checksum": observation.result.metadata.get("execution_capability_checksum"),
        "execution_provider_id": observation.result.metadata.get("execution_provider_id"),
        "execution_environment_halt": observation.result.metadata.get("execution_environment_halt"),
    }


def _execution_record(
    observation: ToolObservation,
    events: list[ToolEvent],
    started_at: datetime,
    *,
    trace_context: TraceContext | W3CSpanContext | None = None,
) -> ToolExecutionRecord:
    call_events = [
        event.event_type
        for event in events
        if event.tool_call_id == observation.call.call_id
    ]
    return ToolExecutionRecord(
        tool_call=observation.call,
        tool_result=observation.result,
        validation_passed="tool_args_validated" in call_events,
        guardrails_passed=observation.status not in {ToolStatus.BLOCKED, ToolStatus.DENIED},
        approval_required=observation.status == ToolStatus.APPROVAL_REQUIRED,
        approval_id=observation.result.approval_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        events=call_events,
        trace_id=trace_context.trace_id if trace_context is not None else None,
        span_id=trace_context.span_id if trace_context is not None else None,
        parent_span_id=(
            trace_context.parent_span_id if trace_context is not None else None
        ),
        gate_result=observation.result.gate_result,
        policy_trace=_tool_policy_trace_to_dict(observation.result.policy_trace),
        error_envelope=observation.result.error_envelope,
        retry_count=observation.result.retry_count,
        timeout=observation.result.timeout,
        graph_identity=observation.call.graph_identity,
    )


def _tool_policy_trace_to_dict(policy_trace: Any) -> dict[str, Any] | None:
    if policy_trace is None:
        return None
    to_dict = getattr(policy_trace, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return {str(key): value for key, value in payload.items()}
    if isinstance(policy_trace, Mapping):
        return {str(key): value for key, value in policy_trace.items()}
    return None


def _dangerous_gate(definition: Any, policy: ToolPolicy) -> ToolResult | None:
    if _is_dangerous_tool(definition) and not policy.allow_dangerous_tools:
        return ToolResult(
            status=ToolStatus.BLOCKED,
            error_type="ToolPermissionError",
            error_message=f"dangerous tool is not allowed: {definition.name}",
            tool_name=definition.name,
        )
    return None


def _restricted_agent_boundary_gate(call: ToolCall) -> ToolResult | None:
    if (
        call.requested_by_agent_id
        and is_restricted_agent_id(call.requested_by_agent_id)
        and is_external_fetch_tool(call.tool_name)
    ):
        return ToolResult(
            status=ToolStatus.BLOCKED,
            error_type="ToolPermissionError",
            error_message=(
                "restricted agent is not allowed to call configured boundary "
                f"tool: {call.tool_name}"
            ),
            call_id=call.call_id,
            tool_name=call.tool_name,
        )
    return None


def _is_dangerous_tool(definition: Any) -> bool:
    name = str(getattr(definition, "name", ""))
    if is_default_dangerous_tool_name(name):
        return True
    return bool(getattr(definition, "is_dangerous", False))


def _with_duration(result: ToolResult, elapsed_ms: float) -> ToolResult:
    return result.with_duration(elapsed_ms)


def _with_call(result: ToolResult, call: ToolCall) -> ToolResult:
    if (
        result.call_id == call.call_id
        and result.tool_name == call.tool_name
        and result.graph_identity == call.graph_identity
    ):
        return result
    return _copy_tool_result(
        result,
        call_id=result.call_id or call.call_id,
        tool_name=result.tool_name or call.tool_name,
        graph_identity=result.graph_identity or call.graph_identity,
    )


def _with_tool_gate(result: ToolResult, call: ToolCall) -> ToolResult:
    if result.gate_result is not None:
        return result
    accepted_statuses = {
        ToolStatus.SUCCEEDED,
        ToolStatus.SKIPPED,
        ToolStatus.APPROVAL_REQUIRED,
    }
    checks = [
        GateCheckResult(
            check_id="tool.status",
            dimension=(
                "safety"
                if result.status in {ToolStatus.BLOCKED, ToolStatus.DENIED}
                else "resource"
                if result.status is ToolStatus.TIMEOUT
                else "compatibility"
            ),
            passed=result.status in accepted_statuses,
            reason=result.error_message or "",
        )
    ]
    if result.status == ToolStatus.APPROVAL_REQUIRED:
        checks.append(
            GateCheckResult(
                check_id="tool.approval",
                dimension="safety",
                passed=False,
                severity="warning",
                reason=result.output_summary or "tool approval required",
            )
        )
    if result.status == ToolStatus.FAILED and result.error_type == "ToolRuntimeError":
        checks.append(
            GateCheckResult(
                check_id="tool.output",
                dimension="resource",
                passed=False,
                reason=result.error_message or "tool output gate failed",
                metadata={"output_bytes": result.output_bytes},
            )
        )
    gate = CompositeAndGate(f"tool:{call.call_id}:gate").evaluate(
        checks,
        metadata={"tool_name": call.tool_name, "call_id": call.call_id},
    )
    return _copy_tool_result(
        result,
        gate_result=gate.to_dict(),
    )


def _standardize_tool_result(
    result: ToolResult,
    *,
    call: ToolCall,
    policy_trace: "_ToolPolicyTraceBuilder",
    trace_context: TraceContext | W3CSpanContext | None,
    retry_count: int,
) -> ToolResult:
    artifact_spill = result.metadata.get("artifact_spill")
    if artifact_spill == "spilled":
        policy_trace.add(
            "tool.artifact_spill",
            "artifact",
            True,
            "tool output spilled to artifact",
            metadata={"artifact_refs": [ref.to_dict() for ref in result.artifact_refs]},
        )
    elif artifact_spill == "missing_context":
        policy_trace.add(
            "tool.artifact_spill",
            "artifact",
            False,
            result.error_message or "artifact context required",
        )
    elif artifact_spill == "deferred":
        policy_trace.add(
            "tool.artifact_spill",
            "artifact",
            True,
            "result persistence delegated to Harness",
        )
    else:
        policy_trace.add("tool.artifact_spill", "artifact", True, "inline output retained")
    return _copy_tool_result(
        result,
        policy_trace=policy_trace.to_trace(result),
        retry_count=retry_count,
        timeout=result.status == ToolStatus.TIMEOUT,
        trace_id=trace_context.trace_id if trace_context is not None else None,
        span_id=trace_context.span_id if trace_context is not None else None,
        parent_span_id=(
            trace_context.parent_span_id if trace_context is not None else None
        ),
        redacted_output=_safe_tool_output(result.output, result.media_type),
    )


def _copy_tool_result(result: ToolResult, **overrides: Any) -> ToolResult:
    values = {
        "status": result.status,
        "output": result.output,
        "output_summary": result.output_summary,
        "artifact_refs": list(result.artifact_refs),
        "artifacts": list(result.artifacts),
        "error_type": result.error_type,
        "error_message": result.error_message,
        "approval_id": result.approval_id,
        "redacted": result.redacted,
        "output_bytes": result.output_bytes,
        "duration_ms": result.duration_ms,
        "metadata": dict(result.metadata),
        "graph_identity": result.graph_identity,
        "call_id": result.call_id,
        "tool_name": result.tool_name,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "gate_result": result.gate_result,
        "redacted_output": result.redacted_output,
        "policy_trace": result.policy_trace,
        "retry_count": result.retry_count,
        "timeout": result.timeout,
        "termination_confirmed": result.termination_confirmed,
        "indeterminate": result.indeterminate,
        "attempt_id": result.attempt_id,
        "idempotency_key": result.idempotency_key,
        "operation_id": result.operation_id,
        "operation_kind": result.operation_kind,
        "local_attempt_no": result.local_attempt_no,
        "retry_credit_id": result.retry_credit_id,
        "trace_id": result.trace_id,
        "span_id": result.span_id,
        "parent_span_id": result.parent_span_id,
        "error_envelope": result.error_envelope,
        "media_type": result.media_type,
    }
    values.update(overrides)
    return ToolResult(**values)


def _has_side_effects(side_effect: str) -> bool:
    return side_effect not in {"", "none", "read_only"}


def _side_effect_value(definition: Any) -> str:
    value = getattr(definition, "side_effect_value", None)
    if value is not None:
        return str(value)
    side_effect = getattr(definition, "side_effect", "")
    return getattr(side_effect, "value", side_effect)


def _risk_level(definition: Any) -> str:
    side_effect = _side_effect_value(definition)
    if definition.is_dangerous or side_effect == "destructive":
        return "critical"
    if side_effect in {"publishing", "writes_external_state", "external_write"}:
        return "high"
    if side_effect in {"writes_local_state", "local_write"}:
        return "medium"
    if definition.requires_approval:
        return "medium"
    return "low"


def _requires_approval(definition: Any, policy: ToolPolicy) -> bool:
    return policy.requires_approval(definition)


class _ToolPolicyTraceBuilder:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.risk_level = "unknown"
        self.requires_approval = False
        self.approval_granted: bool | None = None
        self.checks: list[GateCheckResult] = []

    def add(
        self,
        check_id: str,
        dimension: str,
        passed: bool,
        reason: str,
        *,
        severity: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.checks.append(
            GateCheckResult(
                check_id=check_id,
                dimension=dimension,
                passed=passed,
                severity=severity or ("info" if passed else "error"),
                reason=reason,
                metadata=dict(metadata or {}),
            )
        )

    def to_trace(self, result: ToolResult) -> ToolPolicyTrace:
        allowed = result.status not in {ToolStatus.BLOCKED, ToolStatus.DENIED}
        failed_reasons = [check.reason for check in self.checks if not check.passed]
        return ToolPolicyTrace(
            tool_name=result.tool_name or self.tool_name,
            allowed=allowed,
            risk_level=self.risk_level,
            requires_approval=self.requires_approval,
            approval_granted=self.approval_granted,
            checks=[check.to_dict() for check in self.checks],
            reason="; ".join(reason for reason in failed_reasons if reason)
            or result.error_message
            or result.output_summary
            or "tool execution completed",
        )


def _timeout_seconds(definition: Any, policy: ToolPolicy) -> float | None:
    if definition.timeout_seconds is not None:
        return definition.timeout_seconds
    return policy.timeout_seconds_default


def _max_attempts(definition: Any, policy: ToolPolicy) -> int:
    attempts = definition.max_attempts
    if attempts is None:
        attempts = policy.max_attempts_default
    return max(1, int(attempts))


def _arguments_with_secrets(arguments: dict[str, Any], definition: Any, secret_provider: SecretProvider | None) -> dict[str, Any]:
    required_secret_names = list(getattr(definition, "required_secret_names", []))
    if not required_secret_names:
        return arguments
    if secret_provider is None:
        raise ToolSecretError(
            f"missing secret provider for tool {definition.name}; "
            f"required secrets: {', '.join(required_secret_names)}"
        )
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for secret_name in required_secret_names:
        value = secret_provider.get_secret(secret_name)
        if value:
            resolved[secret_name] = value
        else:
            missing.append(secret_name)
    if missing:
        raise ToolSecretError(
            f"missing required secrets for tool {definition.name}: {', '.join(missing)}"
        )
    return {**arguments, _RUNTIME_SECRETS_ARGUMENT: resolved}


def _invoke_with_retry(
    executor: Any,
    arguments: dict[str, Any],
    timeout_seconds: float | None,
    max_attempts: int,
    tool_name: str,
    attempt_callback: Any | None = None,
    *,
    definition: Any | None = None,
    policy: ToolPolicy | None = None,
    idempotency_key: str | None = None,
    operation_id: str | None = None,
    attempt_event_sink: AttemptLifecycleSink | None = None,
    execution_evidence: ToolExecutionEvidencePort | None = None,
    call: ToolCall | None = None,
) -> tuple[Any, int, AttemptContext]:
    parent_context = current_attempt_context()
    logical_idempotency_key = idempotency_key or f"tool:{tool_name}"
    resolved_operation_id = operation_id or logical_idempotency_key
    resolved_policy = policy or ToolPolicy(require_explicit_allowlist=False)
    local_budget = LocalRetryBudget(max_attempts=max_attempts)
    execution_limits = _tool_execution_limits(
        parent_context=parent_context,
        policy=resolved_policy,
        max_attempts=max_attempts,
        idempotency_key=logical_idempotency_key,
    )
    admission_policy = _tool_admission_policy(
        definition=definition,
        policy=resolved_policy,
        timeout_seconds=timeout_seconds,
        tool_name=tool_name,
    )
    supervisor = AttemptSupervisor(
        cancellation_grace_seconds=admission_policy.cancellation_grace_seconds
    )

    if (execution_evidence is None) is not (call is None):
        raise ValueError("execution_evidence and call must be provided together")

    def finalize_tool_attempt(
        outcome: AttemptOutcome[Any],
    ) -> AttemptOutcome[Any]:
        retry_safe = _retry_is_safe(definition)
        error = outcome.error
        if outcome.state is AttemptState.TIMED_OUT:
            if retry_safe:
                finalized = outcome
            else:
                finalized = replace(
                    outcome,
                    indeterminate=True,
                    reason_code=outcome.reason_code or "tool_timeout_indeterminate",
                )
        elif outcome.state is not AttemptState.FAILED or error is None:
            finalized = outcome
        elif isinstance(error, AttemptCancelledError):
            finalized = replace(
                outcome,
                state=AttemptState.TIMED_OUT,
                timed_out=True,
                indeterminate=not retry_safe,
                reason_code=error.code,
            )
        elif retry_safe or _failure_is_known_to_have_no_effect(definition, error):
            finalized = outcome
        else:
            finalized = replace(
                outcome,
                state=AttemptState.INDETERMINATE,
                indeterminate=True,
                reason_code="tool_effect_indeterminate",
            )
        if execution_evidence is not None and call is not None:
            _required_evidence_write(
                "record_attempt_terminal",
                execution_evidence.record_attempt_terminal,
                call,
                finalized,
            )
        return finalized

    def prepare_tool_attempt(identity: AttemptIdentity) -> None:
        if execution_evidence is not None and call is not None:
            _required_evidence_write(
                "admit_attempt",
                execution_evidence.admit_attempt,
                call,
                identity,
            )
        physical_admitted = getattr(attempt_event_sink, "physical_admitted", None)
        if callable(physical_admitted):
            try:
                physical_admitted(identity)
            except BaseException:  # noqa: BLE001 - local telemetry is optional
                pass

    has_physical_admission_hook = callable(
        getattr(attempt_event_sink, "physical_admitted", None)
    )

    while local_budget.remaining > 0:
        supervised = supervisor.run(
            lambda: executor(arguments),
            timeout_seconds=timeout_seconds,
            idempotency_key=logical_idempotency_key,
            operation_id=resolved_operation_id,
            operation_kind="tool_call",
            local_budget=local_budget,
            admission_policy=admission_policy,
            execution_limits=execution_limits,
            parent_context=parent_context,
            finalize=finalize_tool_attempt,
            prepare=(
                prepare_tool_attempt
                if execution_evidence is not None or has_physical_admission_hook
                else None
            ),
            event_sink=attempt_event_sink,
        )

        if supervised.state is AttemptState.REJECTED:
            if supervised.error is None:
                raise ToolRuntimeError("tool attempt admission was rejected without an error")
            raise supervised.error

        context = _started_attempt_context(supervised, tool_name=tool_name)
        attempt = context.local_attempt_no
        if callable(attempt_callback):
            attempt_callback(attempt, context)

        if supervised.state is AttemptState.SUCCEEDED:
            return supervised.value, attempt, context
        if supervised.state is AttemptState.FAILED:
            error = supervised.error
            if getattr(error, "code", None) == "attempt_preparation_failed":
                raise ToolEvidencePersistenceError(
                    "tool physical intent could not be committed before execution"
                ) from error
            if isinstance(error, AttemptCancelledError):
                raise _tool_timeout_error(
                    tool_name=tool_name,
                    timeout_seconds=timeout_seconds,
                    context=supervised.context,
                    termination_confirmed=True,
                    indeterminate=False,
                ) from None
            if error is None:
                raise ToolRuntimeError("tool attempt failed without an error")
            if isinstance(error, AttemptCapacityExhaustedError):
                raise error
            known_no_effect = _failure_is_known_to_have_no_effect(
                definition,
                error,
            )
            if not known_no_effect and not _retry_is_safe(definition):
                if parent_context is not None:
                    parent_context.mark_descendant_indeterminate()
                raise ToolIndeterminateError(
                    f"tool {tool_name} failed after an external effect could not be reconciled",
                    attempt_id=context.attempt_id,
                    idempotency_key=context.idempotency_key,
                    operation_id=context.operation_id or context.idempotency_key,
                    operation_kind=context.operation_kind,
                    local_attempt_no=context.local_attempt_no,
                    retry_credit_id=context.retry_credit_id,
                    cause_type=type(error).__name__,
                ) from error
            if local_budget.remaining == 0:
                raise error
            continue

        if supervised.state is AttemptState.INDETERMINATE:
            if parent_context is not None:
                parent_context.mark_descendant_indeterminate()
            error = supervised.error
            raise ToolIndeterminateError(
                f"tool {tool_name} has an indeterminate descendant effect",
                attempt_id=context.attempt_id,
                idempotency_key=context.idempotency_key,
                operation_id=context.operation_id or context.idempotency_key,
                operation_kind=context.operation_kind,
                local_attempt_no=context.local_attempt_no,
                retry_credit_id=context.retry_credit_id,
                cause_type=(
                    type(error).__name__
                    if error is not None
                    else "AttemptIndeterminateError"
                ),
            ) from error

        timeout_is_safe = _retry_is_safe(definition)
        indeterminate = supervised.indeterminate or not timeout_is_safe
        if parent_context is not None:
            if not supervised.termination_confirmed:
                parent_context.mark_descendant_unconfirmed()
            elif indeterminate:
                parent_context.mark_descendant_indeterminate()
        timeout_error = _tool_timeout_error(
            tool_name=tool_name,
            timeout_seconds=timeout_seconds,
            context=supervised.context,
            termination_confirmed=supervised.termination_confirmed,
            indeterminate=indeterminate,
        )
        if (
            local_budget.remaining == 0
            or not supervised.termination_confirmed
            or indeterminate
        ):
            raise timeout_error
    raise RuntimeError(f"tool {tool_name} retry loop exited unexpectedly")


def _invoke_with_timeout(
    executor: Any,
    arguments: dict[str, Any],
    timeout_seconds: float | None,
    tool_name: str,
) -> Any:
    local_budget = LocalRetryBudget(max_attempts=1)
    execution_limits = ExecutionLimits(
        execution_id=f"standalone-tool:{tool_name}",
        retry_credits=RetryCreditLedger(max_total_retries=0),
    )
    supervised = AttemptSupervisor().run(
        lambda: executor(arguments),
        timeout_seconds=timeout_seconds,
        idempotency_key=f"tool:{tool_name}",
        operation_id=f"tool:{tool_name}",
        operation_kind="tool_call",
        local_budget=local_budget,
        execution_limits=execution_limits,
    )
    if supervised.state is AttemptState.REJECTED:
        if supervised.error is None:
            raise ToolRuntimeError("tool attempt admission was rejected without an error")
        raise supervised.error
    if supervised.state is AttemptState.SUCCEEDED:
        return supervised.value
    if supervised.state is AttemptState.FAILED:
        if supervised.error is None:
            raise ToolRuntimeError("tool attempt failed without an error")
        raise supervised.error
    raise _tool_timeout_error(
        tool_name=tool_name,
        timeout_seconds=timeout_seconds,
        context=supervised.context,
        termination_confirmed=supervised.termination_confirmed,
        indeterminate=supervised.indeterminate,
    )


def _tool_idempotency_key(call: ToolCall) -> str:
    parent_context = current_attempt_context()
    if parent_context is not None:
        child_id = str(call.metadata.get("idempotency_key") or call.call_id)
        return derive_idempotency_key(
            parent_context.idempotency_key,
            "tool",
            child_id,
        )
    configured = call.metadata.get("idempotency_key")
    return str(configured or f"tool:{call.call_id}")


def _required_evidence_write(
    operation: str,
    writer: Any,
    *args: Any,
) -> Any:
    try:
        return writer(*args)
    except ToolEvidencePersistenceError:
        raise
    except BaseException as exc:  # noqa: BLE001 - fail closed at persistence boundary
        raise ToolEvidencePersistenceError(
            f"required tool evidence write failed: {operation}"
        ) from exc


def _is_canonical_evidence_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 2048
    )


def _trace_scoped_executor(
    executor: Any,
    context: TraceContext | W3CSpanContext | None,
) -> Any:
    def execute(arguments: dict[str, Any]) -> Any:
        with trace_context_scope(context):
            return executor(arguments)

    return execute


def _retry_is_safe(definition: Any | None) -> bool:
    if definition is None:
        return True
    side_effect = _side_effect_value(definition).casefold()
    if side_effect in {"", "none", "read_only"}:
        return True
    metadata = dict(getattr(definition, "metadata", {}) or {})
    return bool(
        metadata.get("idempotent") is True
        and metadata.get("reconciliation_supported") is True
    )


def _failure_is_known_to_have_no_effect(
    definition: Any | None,
    error: BaseException,
) -> bool:
    if definition is None:
        return False
    metadata = dict(getattr(definition, "metadata", {}) or {})
    configured = metadata.get("no_effect_error_types") or []
    if not isinstance(configured, list):
        return False
    error_types = {base.__name__ for base in type(error).__mro__}
    return bool(error_types.intersection(str(value) for value in configured))


def _tool_timeout_error(
    *,
    tool_name: str,
    timeout_seconds: float | None,
    context: AttemptContext,
    termination_confirmed: bool,
    indeterminate: bool,
) -> ToolTimeoutError:
    timeout_text = (
        f"{float(timeout_seconds):g} seconds"
        if timeout_seconds is not None
        else "its deadline"
    )
    return ToolTimeoutError(
        f"tool {tool_name} exceeded timeout of {timeout_text}",
        attempt_id=context.attempt_id,
        idempotency_key=context.idempotency_key,
        operation_id=context.operation_id,
        operation_kind=context.operation_kind,
        local_attempt_no=context.local_attempt_no,
        retry_credit_id=context.retry_credit_id,
        termination_confirmed=termination_confirmed,
        indeterminate=indeterminate,
    )


def _tool_execution_limits(
    *,
    parent_context: AttemptContext | None,
    policy: ToolPolicy,
    max_attempts: int,
    idempotency_key: str,
) -> ExecutionLimits:
    if parent_context is not None and parent_context.execution_limits is not None:
        return parent_context.execution_limits
    max_total_retries = (
        policy.max_total_retries
        if policy.max_total_retries is not None
        else max(0, max_attempts - 1)
    )
    return ExecutionLimits(
        execution_id=f"standalone:{idempotency_key}",
        retry_credits=RetryCreditLedger(max_total_retries=max_total_retries),
    )


def _tool_admission_policy(
    *,
    definition: Any | None,
    policy: ToolPolicy,
    timeout_seconds: float | None,
    tool_name: str,
) -> DeadlineAdmissionPolicy:
    min_start_window = getattr(definition, "min_start_window_seconds", None)
    cancellation_grace = getattr(
        definition,
        "cancellation_grace_seconds",
        None,
    )
    completion_reserve = getattr(
        definition,
        "completion_reserve_seconds",
        None,
    )
    return DeadlineAdmissionPolicy(
        timeout_seconds=timeout_seconds,
        min_start_window_seconds=(
            policy.min_start_window_seconds
            if min_start_window is None
            else float(min_start_window)
        ),
        cancellation_grace_seconds=(
            policy.cancellation_grace_seconds
            if cancellation_grace is None
            else float(cancellation_grace)
        ),
        completion_reserve_seconds=(
            policy.completion_reserve_seconds
            if completion_reserve is None
            else float(completion_reserve)
        ),
        admission_details={"tool_name": tool_name},
    )


def _started_attempt_context(
    outcome: Any,
    *,
    tool_name: str,
) -> AttemptContext:
    if outcome.context is None or not outcome.started:
        raise ToolRuntimeError(
            f"tool {tool_name} produced an execution outcome without an attempt context"
        )
    return outcome.context


def _attempt_result_fields(
    context: AttemptContext | None,
) -> dict[str, Any]:
    if context is None:
        return {}
    return {
        "attempt_id": context.attempt_id,
        "idempotency_key": context.idempotency_key,
        "operation_id": context.operation_id,
        "operation_kind": context.operation_kind,
        "local_attempt_no": context.local_attempt_no,
        "retry_credit_id": context.retry_credit_id,
        "termination_confirmed": True,
    }


def _result_size_bytes(value: Any, media_type: str) -> int:
    normalized = str(media_type).strip().casefold()
    if normalized == "application/json" or normalized.endswith("+json"):
        return len(
            json.dumps(
                to_jsonable(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    if normalized.startswith("text/"):
        if not isinstance(value, str):
            raise ToolRuntimeError("text tool result must be a string")
        return len(value.encode("utf-8"))
    if not isinstance(value, (bytes, bytearray)):
        raise ToolRuntimeError("binary tool result must be bytes")
    return len(value)


def _execution_paths(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise ToolRuntimeError(f"sandbox execution {field_name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise ToolRuntimeError(f"sandbox execution {field_name} must contain strings")
    return tuple(value)


def _safe_tool_output(value: Any, media_type: str) -> Any:
    if value is None:
        return None
    normalized = str(media_type).strip().casefold()
    if normalized == "application/json" or normalized.endswith("+json"):
        return restore_redacted_booleans(redact_sensitive_values(value), value)
    if normalized.startswith("text/"):
        if not isinstance(value, str):
            raise ToolRuntimeError("text tool result must be a string")
        return redact_sensitive_values(value)
    if not isinstance(value, (bytes, bytearray)):
        raise ToolRuntimeError("binary tool result must be bytes")
    return bytes(value)


def _bounded_observation_event_payload(
    observation: ToolObservation,
) -> dict[str, Any]:
    result = observation.result
    return {
        **_result_event_payload(observation),
        "error_message": redact_sensitive_values(result.error_message),
        "summary": redact_sensitive_values(observation.summary[:2048]),
        "media_type": result.media_type,
        "policy_trace": redact_sensitive_values(
            _tool_policy_trace_to_dict(result.policy_trace)
        ),
        "gate_result": (
            redact_sensitive_values(dict(result.gate_result))
            if result.gate_result is not None
            else None
        ),
        "retry_count": result.retry_count,
        "timeout": result.timeout,
    }


def _write_json_artifact(artifact_manager: Any, run_id: str, relative_path: str, payload: dict[str, Any]) -> Path:
    path = artifact_manager.write_json(run_id, relative_path, payload)
    if isinstance(path, Path):
        return path
    return Path(path)
