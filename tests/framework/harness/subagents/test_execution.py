from __future__ import annotations

from dataclasses import replace

import pytest

from framework.agent.models import AgentLoopMetrics, AgentLoopResult, AgentLoopStatus
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.execution import (
    ChildExecutionEvidenceRetainedError,
    HarnessChildExecutionService,
    TrustedChildExecutionOutcome,
    TrustedChildUsage,
    validate_retained_execution_event,
    validate_trusted_execution_event,
)
from framework.harness.subagents.fake import FakeSubAgentRuntime, fake_subagent_spec
from framework.harness.subagents.models import SubAgentResult, SubAgentStatus
from framework.harness.subagents.runtime import SubAgentRuntime
from framework.harness.subagents.tool_evidence import (
    ChildToolEvidenceCompleteness,
    ChildToolEvidenceIndex,
)


def _usage() -> TrustedChildUsage:
    return TrustedChildUsage(
        turns=1,
        llm_calls=1,
        tool_calls=0,
        logical_tool_calls=0,
        memory_ops=0,
        input_tokens=11,
        output_tokens=7,
        reasoning_tokens=3,
        cached_input_tokens=2,
        tokens=21,
        time_ms=15,
        cost_microusd=None,
    )


def _index() -> ChildToolEvidenceIndex:
    return ChildToolEvidenceIndex(
        ref="artifact://child-index",
        checksum="a" * 64,
        content_checksum="sha256:" + "b" * 64,
        byte_size=128,
        revision=1,
        completeness=ChildToolEvidenceCompleteness.COMPLETE,
        disposition="NO_TOOL_REQUESTS",
        registered_logical_calls=0,
        rejected_logical_calls=0,
        admitted_physical_attempts=0,
        known_terminal_attempts=0,
        unresolved_attempts=0,
    )


def test_trusted_execution_event_binds_usage_and_evidence_to_attempt() -> None:
    invocation = FakeSubAgentRuntime(fake_subagent_spec()).build_invocation()
    outcome = TrustedChildExecutionOutcome(
        invocation=invocation,
        runner_result=AgentLoopResult(
            success=True,
            status=AgentLoopStatus.SUCCEEDED,
            iterations=1,
        ),
        evidence_index=_index(),
        usage=_usage(),
        requested_tools=(),
        memory_namespaces=(),
    )

    event = outcome.transcript_event()

    assert validate_trusted_execution_event(
        event,
        identity=invocation.attempt_identity,
        tool_call_refs=("artifact://child-index",),
    ) == event


def test_trusted_execution_event_rejects_forged_usage_or_evidence_ref() -> None:
    invocation = FakeSubAgentRuntime(fake_subagent_spec()).build_invocation()
    outcome = TrustedChildExecutionOutcome(
        invocation=invocation,
        runner_result=AgentLoopResult(
            success=True,
            status=AgentLoopStatus.SUCCEEDED,
            iterations=1,
        ),
        evidence_index=_index(),
        usage=_usage(),
        requested_tools=(),
        memory_namespaces=(),
    )
    forged = outcome.transcript_event()
    forged["usage"] = {**forged["usage"], "tool_calls": 1}

    with pytest.raises(HarnessValidationError, match="trusted execution"):
        validate_trusted_execution_event(
            forged,
            identity=invocation.attempt_identity,
            tool_call_refs=("artifact://child-index",),
        )

    with pytest.raises(HarnessValidationError, match="trusted tool evidence"):
        validate_trusted_execution_event(
            outcome.transcript_event(),
            identity=invocation.attempt_identity,
            tool_call_refs=("artifact://different-index",),
        )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"tokens": 20}, "internally inconsistent"),
        ({"cached_input_tokens": 12}, "internally inconsistent"),
    ],
)
def test_trusted_usage_rejects_inconsistent_canonical_dimensions(
    changes: dict[str, int],
    match: str,
) -> None:
    values = _usage().checksum_projection()
    values.update(changes)

    with pytest.raises(HarnessValidationError, match=match):
        TrustedChildUsage(**values)  # type: ignore[arg-type]


def test_retained_execution_event_preserves_closed_incomplete_evidence() -> None:
    invocation = FakeSubAgentRuntime(fake_subagent_spec()).build_invocation()
    index = replace(
        _index(),
        completeness=ChildToolEvidenceCompleteness.INCOMPLETE,
        disposition="UNKNOWN",
        registered_logical_calls=1,
        admitted_physical_attempts=1,
        unresolved_attempts=1,
    )
    event = ChildExecutionEvidenceRetainedError(
        index,
        RuntimeError("runner disconnected"),
    ).transcript_event(invocation)

    assert validate_retained_execution_event(
        event,
        identity=invocation.attempt_identity,
        tool_call_refs=(index.ref,),
    ) == event

    with pytest.raises(HarnessValidationError, match="tool evidence"):
        validate_retained_execution_event(
            event,
            identity=invocation.attempt_identity,
            tool_call_refs=("artifact://replacement",),
        )


def test_recovered_result_requires_same_durable_index_revision() -> None:
    invocation = FakeSubAgentRuntime(fake_subagent_spec()).build_invocation()
    event = ChildExecutionEvidenceRetainedError(
        _index(),
        RuntimeError("usage persistence failed"),
    ).transcript_event(invocation)
    result = SubAgentResult(
        invocation_id=invocation.invocation_id,
        child_run_id=invocation.child_run_id,
        subagent_id=invocation.subagent_spec.subagent_id,
        status=SubAgentStatus.HALTED,
        tool_call_refs=(_index().ref,),
        metadata={"trusted_execution_retained": event},
    )

    SubAgentRuntime.verify_recovered_evidence(result, _index())

    with pytest.raises(HarnessValidationError, match="differs from the transcript"):
        SubAgentRuntime.verify_recovered_evidence(
            result,
            replace(_index(), revision=2),
        )


def test_halted_bundle_recovers_retained_evidence_without_worker_execution() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    invocation = runtime.build_invocation()
    index = replace(
        _index(),
        completeness=ChildToolEvidenceCompleteness.INCOMPLETE,
        disposition="UNKNOWN",
        registered_logical_calls=1,
        admitted_physical_attempts=1,
        unresolved_attempts=1,
    )
    event = ChildExecutionEvidenceRetainedError(
        index,
        RuntimeError("terminal evidence unavailable"),
    ).transcript_event(invocation)
    retained_result = SubAgentResult(
        invocation_id=invocation.invocation_id,
        child_run_id=invocation.child_run_id,
        subagent_id=invocation.subagent_spec.subagent_id,
        status=SubAgentStatus.HALTED,
        tool_call_refs=(index.ref,),
    )
    halted = runtime._halted_result(
        invocation,
        (),
        errors=("subagent_trusted_execution_failed",),
        worker_result=retained_result,
        trusted_event=event,
    )

    recovered = runtime.recover(invocation)

    assert halted.status is SubAgentStatus.HALTED
    assert recovered is not None
    assert recovered.status is SubAgentStatus.HALTED
    assert recovered.tool_call_refs == (index.ref,)
    assert recovered.metadata["trusted_execution_retained"] == event


def test_usage_allows_canonical_llm_retry_count_beyond_runner_turn_count() -> None:
    class _BudgetReservation:
        attempt_allocation = {
            "max_turns": 2,
            "max_tool_calls": 0,
            "max_memory_ops": 0,
            "max_output_tokens": 20,
            "token_limit": 50,
            "time_limit_ms": 1_000,
        }

    class _Admission:
        budget_reservation = _BudgetReservation()

    result = AgentLoopResult(
        success=True,
        status=AgentLoopStatus.SUCCEEDED,
        iterations=1,
        metrics=AgentLoopMetrics(llm_calls=1),
    )
    usage = replace(_usage(), llm_calls=2, output_tokens=7, tokens=21)

    HarnessChildExecutionService._validate_usage(
        _Admission(),  # type: ignore[arg-type]
        result,
        _index(),
        usage,
    )


def test_runtime_projects_taskplan_metrics_from_trusted_usage_not_runner() -> None:
    runtime = FakeSubAgentRuntime(fake_subagent_spec())
    invocation = runtime.build_invocation()
    runner_metrics = AgentLoopMetrics(
        iterations=1,
        llm_calls=1,
        tool_calls=99,
    )
    outcome = TrustedChildExecutionOutcome(
        invocation=invocation,
        runner_result=AgentLoopResult(
            success=True,
            status=AgentLoopStatus.SUCCEEDED,
            output={"result": "ok"},
            iterations=1,
            metrics=runner_metrics,
        ),
        evidence_index=_index(),
        usage=_usage(),
        requested_tools=(),
        memory_namespaces=(),
    )
    context_result = runtime.gates.context_boundary.evaluate(
        invocation.context_envelope
    )
    input_result = runtime.gates.input_schema.evaluate(
        invocation.subagent_spec,
        {"input_refs": list(invocation.input_refs), **invocation.metadata},
    )

    child, _ = runtime._trusted_result(
        invocation,
        outcome,
        context_result=context_result,
        input_result=input_result,
    )

    assert child.metadata["worker_metrics"] == _usage().budget_usage()
    assert child.metadata["runner_metrics"]["tool_calls"] == 99
