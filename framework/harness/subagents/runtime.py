from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.harness.ref_results import HarnessResultRefAuthority
    from framework.harness.subagents.execution import (
        AdmittedChildExecution,
        HarnessChildExecutionService,
        TrustedChildExecutionOutcome,
    )

from framework.harness.context.models import ContextEnvelope
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.gates import (
    FakeSubAgentGateSuite,
    SubAgentGateResult,
    all_subagent_gates_passed,
)
from framework.harness.subagents.models import (
    SubAgentInvocation,
    SubAgentResult,
    SubAgentStatus,
)
from framework.harness.subagents.observability import (
    DEFAULT_SUBAGENT_TRANSCRIPT_OBSERVATION_SINK,
    SUBAGENT_TRANSCRIPT_RECOVERY_REUSED_TOTAL,
    SubAgentTranscriptObservation,
    SubAgentTranscriptObservationSink,
    record_subagent_transcript_observation,
)
from framework.harness.subagents.transcript import (
    SubAgentAttemptIdentity,
    SubAgentContextEvidence,
    SubAgentOutputDocument,
    SubAgentTranscript,
    SubAgentTranscriptStorePort,
    sanitize_subagent_payload,
    subagent_context_ref,
    subagent_evidence_schemas,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity


class SubAgentRuntime:
    def __init__(
        self,
        *,
        workers: dict[str, Any],
        transcript_store: SubAgentTranscriptStorePort,
        gates: FakeSubAgentGateSuite | None = None,
        observation_sink: SubAgentTranscriptObservationSink | None = (
            DEFAULT_SUBAGENT_TRANSCRIPT_OBSERVATION_SINK
        ),
        runtime_event_sink: Any | None = None,
        result_ref_authority: HarnessResultRefAuthority | None = None,
    ) -> None:
        if not isinstance(transcript_store, SubAgentTranscriptStorePort):
            raise TypeError("transcript_store must implement SubAgentTranscriptStorePort")
        self.workers = dict(workers)
        self.transcript_store = transcript_store
        if result_ref_authority is not None:
            from framework.harness.ref_results import HarnessResultRefAuthority

            if not isinstance(result_ref_authority, HarnessResultRefAuthority) or result_ref_authority.transcript_store is not transcript_store:
                raise TypeError("result_ref_authority must own this transcript store")
        self.result_ref_authority = result_ref_authority
        self.gates = gates or FakeSubAgentGateSuite()
        if observation_sink is not None and not isinstance(
            observation_sink,
            SubAgentTranscriptObservationSink,
        ):
            raise TypeError(
                "observation_sink must implement SubAgentTranscriptObservationSink"
            )
        self._observation_sink = observation_sink
        self._runtime_event_sink = runtime_event_sink

    def invoke(self, invocation: SubAgentInvocation) -> SubAgentResult:
        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        identity = subagent_attempt_identity(invocation)
        recovered = self._recover(identity)
        if recovered is not None:
            self._emit_runtime_event(invocation, "worker_status", "recovered", "subagent_transcript_reused")
            return recovered

        self._emit_runtime_event(invocation, "worker_status", "running", "worker_invocation_started")

        spec = invocation.subagent_spec
        context_result = self.gates.context_boundary.evaluate(invocation.context_envelope)
        input_result = self.gates.input_schema.evaluate(
            spec,
            {"input_refs": list(invocation.input_refs), **invocation.metadata},
        )
        if not all_subagent_gates_passed((context_result, input_result)):
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_plan_gates_failed",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_plan_gates_failed")
            return result

        worker = self.workers.get(spec.subagent_id)
        if worker is None:
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_worker_not_registered",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_worker_not_registered")
            return result
        task = {
            "invocation": invocation.to_dict(),
            "context": invocation.context_envelope.to_dict(),
            "input_refs": list(invocation.input_refs),
            "budget": spec.budget,
        }
        if self.result_ref_authority is not None:
            dependency_outputs = self.result_ref_authority.read_dependency_outputs(
                identity, input_refs=invocation.input_refs,
            )
            if dependency_outputs:
                task["dependency_outputs"] = dependency_outputs
        try:
            worker_result = _call_worker(
                worker,
                spec.subagent_id,
                task,
                execution_identity=_execution_identity_for_invocation(invocation),
            )
        except Exception:
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_worker_execution_failed",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_worker_execution_failed")
            return result
        output = dict(worker_result.output)
        try:
            requested_tools = tuple(str(tool) for tool in output.get("requested_tools", ()))
            requested_namespaces = tuple(str(namespace) for namespace in output.get("memory_namespaces", ()))
            usage = {
                "turns": int(output.get("turns_used", 1)),
                "tool_calls": len(requested_tools),
                "memory_ops": len(requested_namespaces),
            }
            sanitized_output = sanitize_subagent_payload(
                {
                    key: value
                    for key, value in output.items()
                    if key not in {"requested_tools", "memory_namespaces", "memory_write_candidates"}
                }
            )
            memory_write_candidates = _sanitize_memory_write_candidates(
                output.get("memory_write_candidates", ())
            )
        except (HarnessValidationError, TypeError, ValueError, OverflowError):
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_worker_output_invalid",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_worker_output_invalid")
            return result
        warning_count = _warning_count(worker_result.diagnostics.get("warnings", ()))
        base_result = SubAgentResult(
            invocation_id=invocation.invocation_id,
            child_run_id=invocation.child_run_id,
            subagent_id=spec.subagent_id,
            status=worker_result.status.value,
            output=sanitized_output,
            artifact_refs=worker_result.artifacts,
            memory_write_candidates=memory_write_candidates,
            # Requested tool ids are authorization input, not proof that a
            # canonical ToolRuntime call record exists.
            tool_call_refs=(),
            warnings=tuple(
                f"subagent_worker_warning_{index}"
                for index in range(1, min(warning_count, 128) + 1)
            ),
            errors=("subagent_worker_failed",) if worker_result.error else (),
            metadata={"worker_metrics": worker_result.metrics},
        )
        gate_results = (
            context_result,
            self.gates.tool_allowlist.evaluate(spec, requested_tools),
            self.gates.memory_namespace.evaluate(spec, requested_namespaces),
            self.gates.output_schema.evaluate(spec, base_result),
            self.gates.budget.evaluate(invocation, usage),
        )
        if not all_subagent_gates_passed(gate_results):
            result = self._halted_result(
                invocation,
                gate_results,
                errors=("subagent_verify_gates_failed",),
                worker_result=base_result,
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_verify_gates_failed")
            return result

        receipt = self._write_bundle(invocation, base_result, gate_results)
        final_result = _with_receipt(base_result, receipt)
        transcript_result = self.gates.transcript.evaluate(
            final_result,
            store=self._store_for(identity),
            identity=identity,
        )
        if not transcript_result.passed:
            result = self._halted_result(
                invocation,
                (*gate_results, transcript_result),
                errors=("subagent_transcript_verify_failed",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_transcript_verify_failed")
            return result
        self._emit_runtime_event(invocation, "worker_status", "succeeded", "worker_completed")
        return final_result

    def invoke_trusted(
        self,
        invocation: SubAgentInvocation,
        *,
        admission: "AdmittedChildExecution",
        execution_service: "HarnessChildExecutionService",
    ) -> SubAgentResult:
        """Execute the production child path through its concrete authority owner."""

        from framework.harness.subagents.execution import (
            AdmittedChildExecution,
            HarnessChildExecutionService,
        )

        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        if not isinstance(admission, AdmittedChildExecution):
            raise TypeError("admission must be AdmittedChildExecution")
        if not isinstance(execution_service, HarnessChildExecutionService):
            raise TypeError("execution_service must be HarnessChildExecutionService")
        recovered = self._recover(subagent_attempt_identity(invocation))
        if recovered is not None:
            evidence = execution_service.recover_evidence(admission, invocation)
            self.verify_recovered_evidence(recovered, evidence)
            self._emit_runtime_event(
                invocation,
                "worker_status",
                "recovered",
                "subagent_transcript_reused",
            )
            return recovered

        self._emit_runtime_event(
            invocation,
            "worker_status",
            "running",
            "trusted_worker_invocation_started",
        )
        context_result = self.gates.context_boundary.evaluate(invocation.context_envelope)
        input_result = self.gates.input_schema.evaluate(
            invocation.subagent_spec,
            {"input_refs": list(invocation.input_refs), **invocation.metadata},
        )
        if not all_subagent_gates_passed((context_result, input_result)):
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_plan_gates_failed",),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_plan_gates_failed")
            return result

        try:
            outcome = execution_service.execute(admission, invocation)
            base_result, gate_results = self._trusted_result(
                invocation,
                outcome,
                context_result=context_result,
                input_result=input_result,
            )
        except (HarnessValidationError, TypeError, ValueError, OverflowError) as exc:
            if (
                isinstance(exc, HarnessValidationError)
                and exc.code == "subagent_tool_evidence_recovery_required"
            ):
                # A durable scope already exists or its close is uncertain.
                # Committing a terminal transcript here would hide the
                # unresolved execution and could authorize an unsafe replay.
                raise
            retained_event = None
            retained_result = None
            try:
                from framework.harness.subagents.execution import ChildExecutionEvidenceRetainedError

                if isinstance(exc, ChildExecutionEvidenceRetainedError):
                    retained_event = exc.transcript_event(invocation)
                    retained_result = SubAgentResult(
                        invocation_id=invocation.invocation_id,
                        child_run_id=invocation.child_run_id,
                        subagent_id=invocation.subagent_spec.subagent_id,
                        status=SubAgentStatus.HALTED,
                        tool_call_refs=(exc.evidence_index.ref,),
                    )
            except ImportError:
                pass
            result = self._halted_result(
                invocation,
                (context_result, input_result),
                errors=("subagent_trusted_execution_failed",),
                worker_result=retained_result,
                trusted_event=retained_event,
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_trusted_execution_failed")
            return result

        if not all_subagent_gates_passed(gate_results):
            result = self._halted_result(
                invocation,
                gate_results,
                errors=("subagent_verify_gates_failed",),
                worker_result=base_result,
                trusted_event=outcome.transcript_event(),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_verify_gates_failed")
            return result

        receipt = self._write_bundle(
            invocation,
            base_result,
            gate_results,
            trusted_event=outcome.transcript_event(),
        )
        final_result = _with_receipt(base_result, receipt)
        transcript_result = self.gates.transcript.evaluate(
            final_result,
            store=self._store_for(subagent_attempt_identity(invocation)),
            identity=subagent_attempt_identity(invocation),
        )
        if not transcript_result.passed:
            result = self._halted_result(
                invocation,
                (*gate_results, transcript_result),
                errors=("subagent_transcript_verify_failed",),
                worker_result=base_result,
                trusted_event=outcome.transcript_event(),
            )
            self._emit_runtime_event(invocation, "worker_status", "failed", "subagent_transcript_verify_failed")
            return result
        self._emit_runtime_event(invocation, "worker_status", "succeeded", "worker_completed")
        return final_result

    def _trusted_result(
        self,
        invocation: SubAgentInvocation,
        outcome: "TrustedChildExecutionOutcome",
        *,
        context_result: SubAgentGateResult,
        input_result: SubAgentGateResult,
    ) -> tuple[SubAgentResult, tuple[SubAgentGateResult, ...]]:
        from framework.harness.subagents.execution import TrustedChildExecutionOutcome

        if not isinstance(outcome, TrustedChildExecutionOutcome) or outcome.invocation != invocation:
            raise HarnessValidationError(
                "trusted child execution outcome differs from its invocation",
                code="subagent_trusted_execution_invalid",
            )
        runner = outcome.runner_result
        output = sanitize_subagent_payload(dict(runner.output))
        memory_candidates = _sanitize_memory_write_candidates(runner.memory_candidates)
        evidence = outcome.evidence_index
        metadata = {
            "trusted_execution": outcome.transcript_event(),
            # TaskPlan settlement consumes this field.  It must come from the
            # execution owner's canonical measurement, never Runner output.
            "worker_metrics": outcome.usage.budget_usage(),
            "runner_metrics": runner.metrics.to_dict(),
            "used_tools": outcome.requested_tools,
            "used_memory_namespaces": outcome.memory_namespaces,
        }
        base_result = SubAgentResult(
            invocation_id=invocation.invocation_id,
            child_run_id=invocation.child_run_id,
            subagent_id=invocation.subagent_spec.subagent_id,
            status=(
                SubAgentStatus.SUCCEEDED
                if runner.success
                else SubAgentStatus.FAILED
            ),
            output=output,
            artifact_refs=(),
            memory_write_candidates=memory_candidates,
            tool_call_refs=(evidence.ref,),
            warnings=tuple(str(item) for item in runner.warnings[:128]),
            errors=() if runner.success else ("subagent_worker_failed",),
            metadata=metadata,
        )
        usage = {
            "turns": outcome.usage.turns,
            "tool_calls": outcome.usage.tool_calls,
            "memory_ops": outcome.usage.memory_ops,
        }
        gate_results = (
            context_result,
            input_result,
            self.gates.tool_allowlist.evaluate(
                invocation.subagent_spec,
                outcome.requested_tools,
            ),
            self.gates.memory_namespace.evaluate(
                invocation.subagent_spec,
                outcome.memory_namespaces,
            ),
            self.gates.output_schema.evaluate(invocation.subagent_spec, base_result),
            self.gates.budget.evaluate(invocation, usage),
        )
        return base_result, gate_results

    @staticmethod
    def verify_recovered_evidence(
        result: SubAgentResult,
        evidence: Any,
    ) -> None:
        trusted = result.metadata.get("trusted_execution")
        if trusted is None:
            trusted = result.metadata.get("trusted_execution_retained")
        if not isinstance(trusted, Mapping):
            raise HarnessValidationError(
                "recovered child has no trusted execution evidence",
                code="subagent_trusted_execution_invalid",
            )
        recorded = trusted.get("tool_evidence")
        if not isinstance(recorded, Mapping) or (
            recorded.get("ref"),
            recorded.get("content_checksum"),
            recorded.get("revision"),
        ) != (
            getattr(evidence, "ref", None),
            getattr(evidence, "content_checksum", None),
            getattr(evidence, "revision", None),
        ):
            raise HarnessValidationError(
                "recovered tool evidence differs from the transcript",
                code="subagent_trusted_execution_invalid",
            )

    def _emit_runtime_event(
        self,
        invocation: SubAgentInvocation,
        event_type: str,
        status: str,
        reason_code: str,
    ) -> None:
        if self._runtime_event_sink is None:
            return
        from framework.events.runtime.projection import RuntimeEventEmitter, RuntimeEventIdentity

        graph_identity = getattr(invocation.context_envelope, "task_execution_identity", None)
        RuntimeEventEmitter(
            self._runtime_event_sink,
            identity=RuntimeEventIdentity(graph_identity=graph_identity, attempt_id=invocation.invocation_id),
            source="subagent-worker-runtime",
            stream_id=invocation.parent_run_id,
        ).emit(
            event_type,
            event_id=f"worker-runtime:{invocation.invocation_id}:{event_type}:{status}",
            status=status,
            reason_code=reason_code,
            refs=(invocation.invocation_id, invocation.child_run_id),
            metadata={"subagent_id": invocation.subagent_spec.subagent_id, "status": status},
        )

    def recover(self, invocation: SubAgentInvocation) -> SubAgentResult | None:
        """Read a previously committed outcome without invoking a worker."""

        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        return self._recover(subagent_attempt_identity(invocation))

    def _recover(self, identity: SubAgentAttemptIdentity) -> SubAgentResult | None:
        store = self._store_for(identity)
        receipt = store.find_by_identity(identity)
        if receipt is None:
            return None
        store.verify(receipt)
        output = store.read_output(receipt.output_ref)
        transcript = store.read(receipt.transcript_ref)
        trusted_events = [
            item
            for item in transcript.events
            if item.get("schema_version")
            == "newsroom.trusted-child-execution/v1"
        ]
        retained_events = [
            item
            for item in transcript.events
            if item.get("schema_version")
            == "newsroom.trusted-child-execution-retained/v1"
        ]
        if (
            len(trusted_events) > 1
            or len(retained_events) > 1
            or (trusted_events and retained_events)
        ):
            raise HarnessValidationError(
                "subagent transcript contains conflicting trusted execution events",
                code="subagent_trusted_execution_invalid",
            )
        trusted_metadata: dict[str, Any] = {}
        if trusted_events:
            from framework.harness.subagents.execution import (
                TrustedChildUsage,
                validate_trusted_execution_event,
            )

            trusted = validate_trusted_execution_event(
                trusted_events[0],
                identity=identity,
                tool_call_refs=transcript.tool_call_refs,
            )
            trusted_metadata["trusted_execution"] = trusted
            usage = TrustedChildUsage.from_dict(
                trusted["usage"]
            )
            trusted_metadata["worker_metrics"] = usage.budget_usage()
            trusted_metadata["used_tools"] = tuple(trusted["used_tools"])
            trusted_metadata["used_memory_namespaces"] = tuple(
                trusted["used_memory_namespaces"]
            )
        elif retained_events:
            from framework.harness.subagents.execution import validate_retained_execution_event

            trusted_metadata["trusted_execution_retained"] = (
                validate_retained_execution_event(
                    retained_events[0],
                    identity=identity,
                    tool_call_refs=transcript.tool_call_refs,
                )
            )
        record_subagent_transcript_observation(
            self._observation_sink,
            SubAgentTranscriptObservation.from_identity(
                SUBAGENT_TRANSCRIPT_RECOVERY_REUSED_TOTAL,
                identity,
                receipt=receipt,
                value=1,
            ),
        )
        return SubAgentResult(
            invocation_id=identity.invocation_id,
            child_run_id=identity.child_run_id,
            subagent_id=identity.subagent_id,
            status=SubAgentStatus(output.status),
            output=dict(output.output),
            artifact_refs=output.artifact_refs,
            errors=tuple(transcript.errors),
            warnings=tuple(transcript.warnings),
            tool_call_refs=transcript.tool_call_refs,
            transcript_receipt=receipt,
            metadata={
                "recovered": True,
                "gate_results": [dict(item) for item in transcript.gate_results],
                **trusted_metadata,
            },
        )

    def _halted_result(
        self,
        invocation: SubAgentInvocation,
        gate_results: tuple[SubAgentGateResult, ...],
        *,
        errors: tuple[str, ...],
        worker_result: SubAgentResult | None = None,
        trusted_event: dict[str, Any] | None = None,
    ) -> SubAgentResult:
        result = worker_result or SubAgentResult(
            invocation_id=invocation.invocation_id,
            child_run_id=invocation.child_run_id,
            subagent_id=invocation.subagent_spec.subagent_id,
            status=SubAgentStatus.HALTED,
            errors=errors,
        )
        halted = SubAgentResult(
            invocation_id=result.invocation_id,
            child_run_id=result.child_run_id,
            subagent_id=result.subagent_id,
            status=SubAgentStatus.HALTED,
            output=result.output,
            artifact_refs=result.artifact_refs,
            memory_write_candidates=result.memory_write_candidates,
            tool_call_refs=result.tool_call_refs,
            warnings=result.warnings,
            errors=errors,
            metadata={
                **result.metadata,
                "gate_results": [item.to_dict() for item in gate_results],
            },
        )
        receipt = self._write_bundle(
            invocation,
            halted,
            gate_results,
            errors=errors,
            trusted_event=trusted_event,
        )
        return _with_receipt(halted, receipt)

    def _write_bundle(
        self,
        invocation: SubAgentInvocation,
        result: SubAgentResult,
        gate_results: tuple[SubAgentGateResult, ...],
        *,
        errors: tuple[str, ...] = (),
        trusted_event: dict[str, Any] | None = None,
    ):
        identity = subagent_attempt_identity(invocation)
        schemas = subagent_evidence_schemas(identity)
        context = SubAgentContextEvidence(
            identity=identity,
            context_envelope_ref=subagent_context_ref(identity),
            input_refs=invocation.input_refs,
            memory_context_refs=invocation.context_envelope.memory_context_refs,
            redaction_report={"raw_parent_messages_included": False, "sibling_history_included": False},
            schema_version=schemas.context,
        )
        output = SubAgentOutputDocument(
            identity=identity,
            status=result.status.value,
            output=result.output,
            artifact_refs=result.artifact_refs,
            error_code=(errors or result.errors)[0]
            if errors or result.errors
            else None,
            schema_version=schemas.output,
        )
        transcript = SubAgentTranscript(
            identity=identity,
            context_envelope_ref=context.context_envelope_ref,
            input_refs=invocation.input_refs,
            tool_call_refs=result.tool_call_refs,
            memory_context_refs=invocation.context_envelope.memory_context_refs,
            output_ref=output.ref,
            output_checksum=output.output_checksum,
            artifact_refs=result.artifact_refs,
            gate_results=tuple(_transcript_gate_dict(item) for item in gate_results),
            budget_snapshot=invocation.budget_snapshot.to_dict(),
            redaction_report={"raw_parent_messages_included": False, "sibling_history_included": False},
            warnings=result.warnings,
            errors=errors or result.errors,
            events=tuple(
                item
                for item in (
                    {"event_type": "subagent_invocation_planned"},
                    trusted_event,
                    {"event_type": "subagent_completed" if result.status is SubAgentStatus.SUCCEEDED else "subagent_halted"},
                )
                if item is not None
            ),
            observed_at=invocation.observed_at,
            schema_version=schemas.transcript,
        )
        store = self._store_for(identity)
        receipt = store.write(context, output, transcript)
        return store.verify(receipt)

    def _store_for(self, identity: SubAgentAttemptIdentity) -> SubAgentTranscriptStorePort:
        if self.result_ref_authority is None:
            return self.transcript_store
        return self.result_ref_authority.for_attempt(identity, allow_registration=True)


def subagent_attempt_identity(
    invocation: SubAgentInvocation,
) -> SubAgentAttemptIdentity:
    if not isinstance(invocation, SubAgentInvocation):
        raise TypeError("invocation must be SubAgentInvocation")
    return invocation.attempt_identity


def _with_receipt(result: SubAgentResult, receipt) -> SubAgentResult:
    return SubAgentResult(
        invocation_id=result.invocation_id,
        child_run_id=result.child_run_id,
        subagent_id=result.subagent_id,
        status=result.status,
        output=result.output,
        artifact_refs=result.artifact_refs,
        memory_write_candidates=result.memory_write_candidates,
        tool_call_refs=result.tool_call_refs,
        warnings=result.warnings,
        errors=result.errors,
        transcript_receipt=receipt,
        metadata=result.metadata,
    )


def _transcript_gate_dict(result: SubAgentGateResult) -> dict[str, Any]:
    return {**result.evidence_projection(), "evidence_checksum": result.evidence_checksum}


def _warning_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return min(len(value), 128)
    return 1


def _sanitize_memory_write_candidates(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise HarnessValidationError(
            "memory_write_candidates must be an array",
            code="subagent_memory_candidate_invalid",
        )
    if len(value) > 128:
        raise HarnessValidationError(
            "memory_write_candidates exceeds its item limit",
            code="subagent_memory_candidate_invalid",
        )
    candidates: list[dict[str, Any]] = []
    for candidate in value:
        if not isinstance(candidate, dict):
            raise HarnessValidationError(
                "memory_write_candidates must contain objects",
                code="subagent_memory_candidate_invalid",
            )
        candidates.append(
            sanitize_subagent_payload(
                candidate,
                field_name="memory_write_candidate",
                max_bytes=64 * 1024,
            )
        )
    return tuple(candidates)


def _call_worker(
    worker: Any,
    subagent_id: str,
    task: dict[str, Any],
    *,
    execution_identity: GraphExecutionIdentity | None,
) -> HarnessWorkerResult:
    execute = getattr(worker, "execute", None)
    if callable(execute):
        value = _invoke_worker_callable(execute, (task,), execution_identity)
    else:
        run_subagent = getattr(worker, "run_subagent", None)
        if callable(run_subagent):
            value = _invoke_worker_callable(
                run_subagent,
                (subagent_id, task, dict(task.get("budget", {}))),
                execution_identity,
            )
        elif callable(worker):
            value = _invoke_worker_callable(worker, (task,), execution_identity)
        else:
            raise HarnessValidationError("subagent worker must be callable or implement SubAgentWorkerPort")
    if not isinstance(value, HarnessWorkerResult):
        raise HarnessValidationError("subagent worker must return HarnessWorkerResult")
    return value


def _invoke_worker_callable(
    worker: Any,
    args: tuple[Any, ...],
    execution_identity: GraphExecutionIdentity | None,
) -> Any:
    if execution_identity is None or not _accepts_execution_identity(worker):
        return worker(*args)
    return worker(*args, execution_identity=execution_identity)


def _accepts_execution_identity(worker: Any) -> bool:
    try:
        parameters = inspect.signature(worker).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == "execution_identity"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _execution_identity_for_invocation(
    invocation: SubAgentInvocation,
) -> GraphExecutionIdentity | None:
    context_pack = invocation.context_envelope.context_pack
    if not isinstance(context_pack, ContextEnvelope):
        return None
    graph_identity = context_pack.graph_identity
    if graph_identity is None or not graph_identity.has_physical_activity:
        return None
    return graph_identity.to_graph_execution_identity()


__all__ = ["SubAgentRuntime", "subagent_attempt_identity"]
