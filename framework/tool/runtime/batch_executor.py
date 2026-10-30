from __future__ import annotations

from collections.abc import Callable
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import Context, copy_context
from enum import StrEnum
from typing import Any

from framework.events import TraceContext, W3CSpanContext
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.models import (
    ToolCall,
    ToolDefinitionError,
    ToolObservation,
    ToolPolicy,
    ToolResult,
    ToolStatus,
    is_default_dangerous_tool_name,
)
from framework.tool.registry.registry import ToolRegistry
from framework.tool.runtime.executor import ToolExecutor
from framework.tool.runtime.evidence import (
    ToolEvidencePersistenceError,
    ToolExecutionEvidencePort,
)


_LOGGER = logging.getLogger(__name__)
_READ_ONLY_SIDE_EFFECTS = {"", "none", "read_only"}


class BatchCompletionMode(StrEnum):
    """Controls how the batch executor handles partial failures.

    BEST_EFFORT  – (default) run all tools regardless of failures; return all results.
    STRICT       – stop on first failure; attempt compensation for completed tools.
    ANY_SUCCESS  – succeed if at least one tool succeeds.
    QUORUM       – succeed if at least *quorum* tools succeed (set via execute_batch kwarg).
    """

    BEST_EFFORT = "best_effort"
    STRICT = "strict"
    ANY_SUCCESS = "any_success"
    QUORUM = "quorum"


class ToolBatchExecutor:
    """Execute the tool calls of one model turn through a single ToolExecutor.

    Every call runs on the same executor, so approval authority, durable
    evidence, metrics and records are shared with the caller's single-call
    path. A failure of one call never discards the observations of the
    others: tool failures are observations, and an exception escaping a call
    is isolated into an indeterminate observation for that call alone.
    Durable evidence failures stay fail-closed and are raised once every
    in-flight call has settled.
    """

    def __init__(
        self,
        executor: ToolExecutor,
        *,
        max_workers: int = 4,
        compensating_actions: dict[str, Callable[[ToolObservation], None]] | None = None,
    ) -> None:
        for capability in ("execute", "record_pre_execution_rejection"):
            if not callable(getattr(executor, capability, None)):
                raise TypeError(f"executor must provide {capability}()")
        self._executor = executor
        self._max_workers = max(1, int(max_workers))
        # Fix #5: registry of rollback callables keyed by tool name
        self._compensating_actions: dict[str, Callable[[ToolObservation], None]] = (
            compensating_actions or {}
        )
        if (
            getattr(executor, "execution_evidence", None) is not None
            and self._compensating_actions
        ):
            raise ValueError(
                "controlled tool batches require a dedicated evidence capability "
                "for compensating effects"
            )

    @classmethod
    def from_registry(
        cls,
        registry: ToolRegistry,
        *,
        artifact_manager: Any | None = None,
        run_id: str | None = None,
        secret_provider: Any | None = None,
        execution_environment: Any | None = None,
        max_workers: int = 4,
        trace_context: TraceContext | W3CSpanContext | None = None,
        compensating_actions: dict[str, Callable[[ToolObservation], None]] | None = None,
        graph_identity: GraphExecutionIdentity | None = None,
        runtime_event_sink: Any | None = None,
        execution_evidence: ToolExecutionEvidencePort | None = None,
        require_explicit_execution_profile: bool = False,
    ) -> ToolBatchExecutor:
        if graph_identity is not None and not isinstance(
            graph_identity, GraphExecutionIdentity
        ):
            raise TypeError("graph_identity must be GraphExecutionIdentity")
        if execution_evidence is not None and (
            not isinstance(execution_evidence, ToolExecutionEvidencePort)
            or getattr(execution_evidence, "is_durable", False) is not True
        ):
            raise TypeError(
                "execution_evidence must implement the durable "
                "ToolExecutionEvidencePort"
            )
        if execution_evidence is not None and compensating_actions:
            raise ValueError(
                "controlled tool batches require a dedicated evidence capability "
                "for compensating effects"
            )
        return cls(
            ToolExecutor(
                registry,
                artifact_manager=artifact_manager,
                run_id=run_id,
                secret_provider=secret_provider,
                execution_environment=execution_environment,
                trace_context=trace_context,
                graph_identity=graph_identity,
                runtime_event_sink=runtime_event_sink,
                execution_evidence=execution_evidence,
                require_explicit_execution_profile=require_explicit_execution_profile,
            ),
            max_workers=max_workers,
            compensating_actions=compensating_actions,
        )

    @property
    def executor(self) -> ToolExecutor:
        return self._executor

    def execute_batch(
        self,
        calls: list[ToolCall],
        policy: ToolPolicy | None = None,
        *,
        mode: str = "best_effort",
        max_workers: int | None = None,
        quorum: int | None = None,
    ) -> list[ToolObservation]:
        """Execute a batch of tool calls and return one observation per call.

        Args:
            calls:      Tool calls to execute, in the order the caller requested.
            policy:     Tool policy; defaults to permissive.
            mode:       One of BatchCompletionMode values.
            max_workers: Override instance-level thread pool size for this call.
                        With a single worker no call is reordered: the batch
                        runs serially in request order.
            quorum:     Required successes for QUORUM mode.
        """
        policy = policy or ToolPolicy(require_explicit_allowlist=False)
        if not calls:
            return []
        if mode not in {m.value for m in BatchCompletionMode}:
            raise ValueError(f"unsupported tool batch mode: {mode}")
        if len({call.call_id for call in calls}) != len(calls):
            raise ValueError("tool batch call_ids must be unique")

        # Budget guard: block entire batch upfront if over limit
        max_calls = _max_tool_calls_per_iteration(policy)
        if len(calls) > max_calls:
            return [
                self._executor.record_pre_execution_rejection(
                    _blocked_budget_observation(
                        call,
                        f"tool batch exceeds max_tool_calls_per_iteration of {max_calls}",
                    )
                )
                for call in calls
            ]

        if mode == BatchCompletionMode.STRICT:
            return self._execute_strict(calls, policy)

        worker_count = max(1, int(max_workers or self._max_workers))
        if worker_count == 1:
            results = self._execute_serial(calls, policy)
        else:
            # Fix #2: split into parallel-safe and unsafe instead of all-or-nothing
            safe_calls, unsafe_calls = self._split_by_parallelism(calls, policy)
            results = []
            if safe_calls:
                results.extend(
                    self._execute_parallel(safe_calls, policy, max_workers=worker_count)
                )
            if unsafe_calls:
                results.extend(self._execute_serial(unsafe_calls, policy))

            # Restore original call order
            order = {call.call_id: i for i, call in enumerate(calls)}
            results.sort(key=lambda obs: order[obs.call.call_id])

        # Fix #1: apply aggregation policy
        if mode == BatchCompletionMode.ANY_SUCCESS:
            if not any(obs.status == ToolStatus.SUCCEEDED for obs in results):
                return [
                    _failed_aggregation_observation(
                        observation,
                        "any_success: no tool succeeded",
                    )
                    for observation in results
                ]
        elif mode == BatchCompletionMode.QUORUM:
            required = max(1, int(quorum or 1))
            succeeded = sum(1 for obs in results if obs.status == ToolStatus.SUCCEEDED)
            if succeeded < required:
                return [
                    _failed_aggregation_observation(
                        observation,
                        f"quorum: only {succeeded}/{required} tools succeeded",
                    )
                    for observation in results
                ]
        return results

    # ── internal helpers ──────────────────────────────────────────────────────

    def _split_by_parallelism(
        self,
        calls: list[ToolCall],
        policy: ToolPolicy,
    ) -> tuple[list[ToolCall], list[ToolCall]]:
        """Fix #2: separate concurrency-safe calls from side-effect calls.

        Approval-gated calls always take the serial lane so that the batch can
        pause at the first pending approval instead of opening several.
        """
        registry = getattr(self._executor, "registry", None)
        safe: list[ToolCall] = []
        unsafe: list[ToolCall] = []
        for call in calls:
            try:
                if registry is None:
                    raise ToolDefinitionError("executor exposes no tool registry")
                definition = registry.get(call.tool_name).definition
                if (
                    not definition.is_dangerous
                    and not is_default_dangerous_tool_name(definition.name)
                    and definition.concurrency_safe
                    and definition.side_effect_value in _READ_ONLY_SIDE_EFFECTS
                    and not policy.requires_approval(definition)
                ):
                    safe.append(call)
                else:
                    unsafe.append(call)
            except ToolDefinitionError:
                unsafe.append(call)
        return safe, unsafe

    def _execute_parallel(
        self,
        calls: list[ToolCall],
        policy: ToolPolicy,
        *,
        max_workers: int,
    ) -> list[ToolObservation]:
        results: list[ToolObservation | None] = [None] * len(calls)
        fail_closed: list[ToolEvidencePersistenceError] = []
        worker_count = min(max_workers, len(calls))
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="news-tool-batch"
        ) as pool:
            parent_context = copy_context()
            future_to_index = {
                pool.submit(
                    self._execute_one_with_context,
                    call,
                    policy,
                    parent_context.copy(),
                ): idx
                for idx, call in enumerate(calls)
            }
            # Every future is drained: one call raising must not discard the
            # observations of calls which already ran or are still running.
            for future in as_completed(future_to_index):
                try:
                    results[future_to_index[future]] = future.result()
                except ToolEvidencePersistenceError as exc:
                    fail_closed.append(exc)
        if fail_closed:
            raise fail_closed[0]
        return [r for r in results if r is not None]

    def _execute_one_with_context(
        self,
        call: ToolCall,
        policy: ToolPolicy,
        context: Context,
    ) -> ToolObservation:
        return context.run(self._execute_one, call, policy)

    def _execute_serial(
        self, calls: list[ToolCall], policy: ToolPolicy
    ) -> list[ToolObservation]:
        results: list[ToolObservation] = []
        for index, call in enumerate(calls):
            observation = self._execute_one(call, policy)
            results.append(observation)
            if observation.status == ToolStatus.APPROVAL_REQUIRED:
                # The run pauses on this approval; later requests stay
                # uninvoked so the model can re-plan once it resumes.
                results.extend(
                    self._executor.record_pre_execution_rejection(
                        _not_invoked_observation(
                            pending,
                            "tool batch paused for approval before this request was invoked",
                        )
                    )
                    for pending in calls[index + 1 :]
                )
                break
        return results

    def _execute_strict(
        self, calls: list[ToolCall], policy: ToolPolicy
    ) -> list[ToolObservation]:
        """Fix #5: serial execution; compensate completed tools on first failure."""
        completed: list[ToolObservation] = []
        for index, call in enumerate(calls):
            obs = self._execute_one(call, policy)
            completed.append(obs)
            if obs.status != ToolStatus.SUCCEEDED:
                self._compensate(completed[:-1])  # rollback all but the failed one
                if getattr(self._executor, "execution_evidence", None) is not None:
                    completed.extend(
                        self._executor.record_pre_execution_rejection(
                            _not_invoked_observation(
                                pending,
                                "strict tool batch stopped before this request was invoked",
                            )
                        )
                        for pending in calls[index + 1 :]
                    )
                break
        return completed

    def _compensate(self, observations: list[ToolObservation]) -> None:
        """Fix #5: run compensating actions in reverse order (best-effort)."""
        for obs in reversed(observations):
            action = self._compensating_actions.get(obs.call.tool_name)
            if action is not None:
                try:
                    action(obs)
                except Exception:
                    _LOGGER.exception(
                        "tool batch compensation failed",
                        extra={
                            "tool_name": obs.call.tool_name,
                            "call_id": obs.call.call_id,
                        },
                    )

    def _execute_one(self, call: ToolCall, policy: ToolPolicy) -> ToolObservation:
        try:
            return self._executor.execute(call, policy)
        except ToolEvidencePersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate one call from its batch
            _LOGGER.exception(
                "tool call raised outside its execution boundary",
                extra={"tool_name": call.tool_name, "call_id": call.call_id},
            )
            return _escaped_exception_observation(call, exc)


def _max_tool_calls_per_iteration(policy: ToolPolicy) -> int:
    return max(0, int(policy.max_tool_calls_per_iteration))


def _blocked_budget_observation(call: ToolCall, message: str) -> ToolObservation:
    return ToolObservation(
        call=call,
        result=ToolResult(
            status=ToolStatus.BLOCKED,
            error_type="ToolPermissionError",
            error_message=message,
            call_id=call.call_id,
            tool_name=call.tool_name,
        ),
        elapsed_ms=0.0,
    )


def _not_invoked_observation(call: ToolCall, message: str) -> ToolObservation:
    return ToolObservation(
        call=call,
        result=ToolResult(
            status=ToolStatus.BLOCKED,
            error_type="ToolBatchNotInvoked",
            error_message=message,
            call_id=call.call_id,
            tool_name=call.tool_name,
            metadata={"execution_disposition": "not_invoked"},
        ),
        elapsed_ms=0.0,
    )


def _escaped_exception_observation(call: ToolCall, exc: Exception) -> ToolObservation:
    """Observation for an exception which escaped the executor's own boundary.

    The executor converts tool failures into results itself, so an escaping
    exception happened around the invocation: whether the tool took effect is
    unknown, which the observation states instead of guessing.
    """

    return ToolObservation(
        call=call,
        result=ToolResult(
            status=ToolStatus.FAILED,
            error_type=type(exc).__name__,
            error_message=str(exc) or type(exc).__name__,
            call_id=call.call_id,
            tool_name=call.tool_name,
            graph_identity=call.graph_identity,
            indeterminate=True,
            metadata={
                "execution_disposition": "indeterminate",
                "indeterminate": True,
            },
        ),
        elapsed_ms=0.0,
    )


def _failed_aggregation_observation(
    observation: ToolObservation,
    message: str,
) -> ToolObservation:
    """Fix #1: synthetic failure observation for unmet aggregation policy."""
    source_ref = observation.result.metadata.get("tool_evidence_ref")
    return ToolObservation(
        call=observation.call,
        result=ToolResult(
            status=ToolStatus.FAILED,
            error_type="BatchAggregationError",
            error_message=message,
            call_id=observation.call.call_id,
            tool_name=observation.call.tool_name,
            graph_identity=observation.call.graph_identity,
            metadata={
                "source_tool_status": observation.status.value,
                **(
                    {"source_tool_evidence_ref": source_ref}
                    if isinstance(source_ref, str) and source_ref
                    else {}
                ),
            },
        ),
        elapsed_ms=observation.elapsed_ms,
        raw_result=observation.result,
    )
