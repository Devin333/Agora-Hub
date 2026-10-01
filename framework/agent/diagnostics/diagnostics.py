from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from framework.agent.models import (
    AgentLoopDiagnosticSeverity,
    AgentLoopDiagnostics,
    AgentLoopIssue,
    AgentLoopMetrics,
    AgentLoopPolicy,
    AgentLoopStatus,
    AgentLoopStopReason,
    JudgeVerdict,
)
from framework.agent.models.trace import AgentLoopTrace, ToolCallTrace


@dataclass(frozen=True)
class StallDetection:
    stalled: bool
    stop_reason: AgentLoopStopReason | None = None
    summary: str | None = None
    issue: AgentLoopIssue | None = None
    repeated_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stalled": self.stalled,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "summary": self.summary,
            "issue": self.issue.to_dict() if self.issue else None,
            "repeated_count": self.repeated_count,
        }


class AgentLoopStallDetector:
    def __init__(self, policy: AgentLoopPolicy) -> None:
        self.policy = policy

    def after_tool_call(self, trace: AgentLoopTrace, call: ToolCallTrace) -> StallDetection:
        if not self.policy.stall_detection_enabled:
            return StallDetection(stalled=False)
        repeated_count = trace.count_tool_signature(call.signature.key)
        if (
            self.policy.max_repeated_tool_calls > 0
            and repeated_count > self.policy.max_repeated_tool_calls
        ):
            summary = (
                f"tool call repeated {repeated_count} times: "
                f"{call.tool_name}"
            )
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.REPEATED_TOOL_CALL_STALLED,
                summary=summary,
                repeated_count=repeated_count,
                issue=AgentLoopIssue(
                    code="repeated_tool_call",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.WARNING,
                    iteration=call.iteration,
                    tool_name=call.tool_name,
                    metadata={
                        "signature": call.signature.key,
                        "limit": self.policy.max_repeated_tool_calls,
                        "count": repeated_count,
                    },
                ),
            )
        if (
            self.policy.max_consecutive_tool_failures > 0
            and trace.consecutive_tool_failures() > self.policy.max_consecutive_tool_failures
        ):
            count = trace.consecutive_tool_failures()
            summary = f"tool failures repeated for {count} consecutive call(s)"
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.TOOL_FAILED,
                summary=summary,
                issue=AgentLoopIssue(
                    code="consecutive_tool_failures",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=call.iteration,
                    tool_name=call.tool_name,
                    metadata={
                        "limit": self.policy.max_consecutive_tool_failures,
                        "count": count,
                    },
                ),
            )
        return StallDetection(stalled=False)

    def after_parser_error(
        self,
        *,
        trace: AgentLoopTrace,
        iteration: int,
        parser_errors: int,
    ) -> StallDetection:
        if parser_errors > self.policy.max_parser_errors:
            summary = (
                "parser retry limit exceeded: "
                f"{parser_errors} > {self.policy.max_parser_errors}"
            )
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.PARSER_RETRY_EXHAUSTED,
                summary=summary,
                issue=AgentLoopIssue(
                    code="parser_retry_exhausted",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=iteration,
                    metadata={
                        "parser_errors": parser_errors,
                        "max_parser_errors": self.policy.max_parser_errors,
                        "consecutive_parser_errors": trace.consecutive_parser_errors(),
                    },
                ),
            )
        return StallDetection(stalled=False)

    def after_judge_retry(
        self,
        *,
        trace: AgentLoopTrace,
        iteration: int,
        judge_retries: int,
        empty_output_retries: int = 0,
    ) -> StallDetection:
        if trace.repeated_structured_output_rejection():
            summary = "structured-output repair repeated an unchanged rejected candidate"
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.JUDGE_RETRY_EXHAUSTED,
                summary=summary,
                issue=AgentLoopIssue(
                    code="structured_output_unchanged_retry",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=iteration,
                    metadata={
                        "judge_retries": judge_retries,
                        "max_judge_retries": self.policy.max_judge_retries,
                    },
                ),
            )
        if empty_output_retries > self.policy.max_judge_retries:
            summary = (
                "empty output retry limit exceeded: "
                f"{empty_output_retries} > {self.policy.max_judge_retries}"
            )
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.EMPTY_OUTPUT_EXHAUSTED,
                summary=summary,
                issue=AgentLoopIssue(
                    code="empty_output_exhausted",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=iteration,
                    metadata={
                        "empty_output_retries": empty_output_retries,
                        "max_judge_retries": self.policy.max_judge_retries,
                    },
                ),
            )
        if judge_retries > self.policy.max_judge_retries:
            summary = (
                "judge retry limit exceeded: "
                f"{judge_retries} > {self.policy.max_judge_retries}"
            )
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.JUDGE_RETRY_EXHAUSTED,
                summary=summary,
                issue=AgentLoopIssue(
                    code="judge_retry_exhausted",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=iteration,
                    metadata={
                        "judge_retries": judge_retries,
                        "max_judge_retries": self.policy.max_judge_retries,
                        "consecutive_judge_retries": trace.consecutive_judge_retries(),
                    },
                ),
            )
        return StallDetection(stalled=False)

    def detect(self, trace: AgentLoopTrace) -> StallDetection:
        if not self.policy.stall_detection_enabled:
            return StallDetection(stalled=False)
        if trace.consecutive_tool_failures() > self.policy.max_consecutive_tool_failures:
            count = trace.consecutive_tool_failures()
            return StallDetection(
                stalled=True,
                stop_reason=AgentLoopStopReason.TOOL_FAILED,
                summary=f"tool failures repeated for {count} consecutive call(s)",
                repeated_count=count,
            )
        return StallDetection(stalled=False)


class AgentLoopDiagnosticsBuilder:
    def __init__(self, *, agent_id: str, trace: AgentLoopTrace | None = None) -> None:
        self.agent_id = agent_id
        self.trace = trace or AgentLoopTrace(agent_id=agent_id)
        self._issues: list[AgentLoopIssue] = []

    def add_issue(
        self,
        code: str,
        message: str,
        severity: AgentLoopDiagnosticSeverity = AgentLoopDiagnosticSeverity.INFO,
        **metadata: Any,
    ) -> None:
        self._issues.append(
            AgentLoopIssue(
                code=code,
                message=message,
                severity=severity,
                metadata=dict(metadata),
            )
        )

    def build(self) -> AgentLoopDiagnostics:
        severity = _max_severity(self._issues)
        healthy = severity in {AgentLoopDiagnosticSeverity.OK, AgentLoopDiagnosticSeverity.INFO}
        return AgentLoopDiagnostics(
            agent_id=self.agent_id,
            status=AgentLoopStatus.ACCEPTED if healthy else AgentLoopStatus.FAILED,
            stop_reason=AgentLoopStopReason.FINAL_OUTPUT_ACCEPTED
            if healthy
            else AgentLoopStopReason.UNKNOWN_FAILED,
            summary="agent diagnostics built",
            healthy=healthy,
            severity=severity,
            issues=list(self._issues),
            trace_summary=self.trace.summary(),
        )

    def accepted(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        stop_reason: AgentLoopStopReason,
        verdict: JudgeVerdict | None = None,
    ) -> AgentLoopDiagnostics:
        issues = []
        suggestions = []
        if verdict is not None and verdict.feedback and verdict.feedback != "accepted":
            issues.append(
                AgentLoopIssue(
                    code="accepted_with_feedback",
                    message=verdict.feedback,
                    severity=AgentLoopDiagnosticSeverity.INFO,
                    iteration=iterations,
                )
            )
        return self._build(
            status=AgentLoopStatus.ACCEPTED,
            stop_reason=stop_reason,
            summary="agent output accepted",
            healthy=True,
            severity=AgentLoopDiagnosticSeverity.OK,
            metrics=metrics,
            iterations=iterations,
            issues=issues,
            suggestions=suggestions,
        )

    def blocked(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        stop_reason: AgentLoopStopReason,
        verdict: JudgeVerdict | None,
        issue: AgentLoopIssue | None = None,
    ) -> AgentLoopDiagnostics:
        issues = []
        if issue is not None:
            issues.append(issue)
        if verdict is not None:
            issues.extend(_issues_from_verdict(verdict, iterations))
        summary = verdict.feedback if verdict and verdict.feedback else "agent loop blocked"
        return self._build(
            status=AgentLoopStatus.BLOCKED,
            stop_reason=stop_reason,
            summary=summary,
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.BLOCKED,
            metrics=metrics,
            iterations=iterations,
            issues=issues,
            suggestions=["inspect policy violations before retrying the agent"],
        )

    def waiting_for_approval(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        tool_name: str,
        approval_id: str | None,
        approval_kind: str = "tool_approval",
        control_action: str | None = None,
        escalation_type: str | None = None,
    ) -> AgentLoopDiagnostics:
        summary = _approval_summary(
            tool_name,
            approval_kind=approval_kind,
            control_action=control_action,
            escalation_type=escalation_type,
        )
        metadata: dict[str, Any] = {
            "approval_id": approval_id,
            "approval_kind": approval_kind,
        }
        if control_action:
            metadata["control_action"] = control_action
        if escalation_type:
            metadata["escalation_type"] = escalation_type
        return self._build(
            status=AgentLoopStatus.WAITING_FOR_APPROVAL,
            stop_reason=AgentLoopStopReason.TOOL_APPROVAL_REQUIRED,
            summary=summary,
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.WARNING,
            metrics=metrics,
            iterations=iterations,
            issues=[
                AgentLoopIssue(
                    code="tool_approval_required",
                    message=summary,
                    severity=AgentLoopDiagnosticSeverity.WARNING,
                    iteration=iterations,
                    tool_name=tool_name,
                    metadata=metadata,
                )
            ],
            suggestions=[_approval_suggestion(approval_kind)],
        )

    def orchestration_pending(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        reason: str,
    ) -> AgentLoopDiagnostics:
        """Represent a durable Harness continuation without treating it as failure."""

        return self._build(
            status=AgentLoopStatus.PENDING,
            stop_reason=AgentLoopStopReason.ORCHESTRATION_PENDING,
            summary=reason,
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.INFO,
            metrics=metrics,
            iterations=iterations,
            issues=[
                AgentLoopIssue(
                    code="agent_orchestration_pending",
                    message=reason,
                    severity=AgentLoopDiagnosticSeverity.INFO,
                    iteration=iterations,
                )
            ],
            suggestions=["wait for the Harness continuation signal before resuming"],
        )

    def retry_exhausted(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        stop_reason: AgentLoopStopReason,
        verdict: JudgeVerdict | None,
        issue: AgentLoopIssue | None = None,
    ) -> AgentLoopDiagnostics:
        issues = []
        if issue is not None:
            issues.append(issue)
        if verdict is not None:
            issues.extend(_issues_from_verdict(verdict, iterations))
        summary = (
            issue.message
            if issue is not None
            else verdict.feedback
            if verdict is not None and verdict.feedback
            else "agent retry limit exhausted"
        )
        return self._build(
            status=AgentLoopStatus.RETRY_EXHAUSTED,
            stop_reason=stop_reason,
            summary=summary,
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.ERROR,
            metrics=metrics,
            iterations=iterations,
            issues=issues,
            suggestions=[
                "inspect judge feedback",
                "tighten the output schema or prompt before rerunning",
            ],
        )

    def stalled(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        detection: StallDetection,
        verdict: JudgeVerdict | None = None,
    ) -> AgentLoopDiagnostics:
        issues = []
        if detection.issue is not None:
            issues.append(detection.issue)
        if verdict is not None:
            issues.extend(_issues_from_verdict(verdict, iterations))
        return self._build(
            status=AgentLoopStatus.STALLED,
            stop_reason=detection.stop_reason or AgentLoopStopReason.MAX_ITERATIONS_EXCEEDED,
            summary=detection.summary or "agent loop stalled",
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.WARNING,
            metrics=metrics,
            iterations=iterations,
            issues=issues,
            suggestions=[
                "change strategy or reduce tool access before retrying",
                "check whether the same tool call is being repeated",
            ],
        )

    def failed(
        self,
        *,
        metrics: AgentLoopMetrics,
        iterations: int,
        stop_reason: AgentLoopStopReason,
        error: str,
    ) -> AgentLoopDiagnostics:
        return self._build(
            status=AgentLoopStatus.FAILED,
            stop_reason=stop_reason,
            summary=error,
            healthy=False,
            severity=AgentLoopDiagnosticSeverity.ERROR,
            metrics=metrics,
            iterations=iterations,
            issues=[
                AgentLoopIssue(
                    code=stop_reason.value,
                    message=error,
                    severity=AgentLoopDiagnosticSeverity.ERROR,
                    iteration=iterations,
                )
            ],
            suggestions=["inspect the exception and retry only after fixing the dependency"],
        )

    def _build(
        self,
        *,
        status: AgentLoopStatus,
        stop_reason: AgentLoopStopReason,
        summary: str,
        healthy: bool,
        severity: AgentLoopDiagnosticSeverity,
        metrics: AgentLoopMetrics,
        iterations: int,
        issues: list[AgentLoopIssue],
        suggestions: list[str],
    ) -> AgentLoopDiagnostics:
        trace_summary = self.trace.summary()
        repeated_tool_calls = len(trace_summary.get("repeated_tool_calls") or [])
        return AgentLoopDiagnostics(
            agent_id=self.agent_id,
            status=status,
            stop_reason=stop_reason,
            summary=summary,
            healthy=healthy,
            severity=severity,
            iterations=iterations,
            llm_calls=metrics.llm_calls,
            tool_calls=metrics.tool_calls,
            parser_errors=metrics.parser_errors,
            judge_retries=metrics.judge_retries,
            tool_failures=metrics.tool_failures + metrics.tool_timeouts,
            tool_blocks=metrics.tool_blocks,
            approval_requests=metrics.tool_approval_requests,
            repeated_tool_calls=repeated_tool_calls,
            issues=issues,
            suggestions=suggestions,
            trace_summary=trace_summary,
        )


def max_iterations_detection(iterations: int, policy: AgentLoopPolicy) -> StallDetection:
    summary = f"agent reached max_iterations={policy.max_iterations}"
    return StallDetection(
        stalled=True,
        stop_reason=AgentLoopStopReason.MAX_ITERATIONS_EXCEEDED,
        summary=summary,
        issue=AgentLoopIssue(
            code="max_iterations_exceeded",
            message=summary,
            severity=AgentLoopDiagnosticSeverity.WARNING,
            iteration=iterations,
            metadata={"max_iterations": policy.max_iterations},
        ),
    )


def _max_severity(issues: list[AgentLoopIssue]) -> AgentLoopDiagnosticSeverity:
    if not issues:
        return AgentLoopDiagnosticSeverity.OK
    order = {
        AgentLoopDiagnosticSeverity.OK: 0,
        AgentLoopDiagnosticSeverity.INFO: 1,
        AgentLoopDiagnosticSeverity.WARNING: 2,
        AgentLoopDiagnosticSeverity.ERROR: 3,
        AgentLoopDiagnosticSeverity.BLOCKED: 4,
    }
    return max((issue.severity for issue in issues), key=lambda item: order[item])


def _issues_from_verdict(verdict: JudgeVerdict, iteration: int) -> list[AgentLoopIssue]:
    issues: list[AgentLoopIssue] = []
    for key in verdict.missing_output_keys:
        issues.append(
            AgentLoopIssue(
                code="missing_output_key",
                message=f"missing output key: {key}",
                severity=AgentLoopDiagnosticSeverity.ERROR,
                iteration=iteration,
                metadata={"output_key": key},
            )
        )
    for error in verdict.schema_errors:
        issues.append(
            AgentLoopIssue(
                code="schema_error",
                message=error,
                severity=AgentLoopDiagnosticSeverity.ERROR,
                iteration=iteration,
            )
        )
    for error in verdict.validation_errors:
        issues.append(
            AgentLoopIssue(
                code="validation_error",
                message=error,
                severity=AgentLoopDiagnosticSeverity.WARNING,
                iteration=iteration,
            )
        )
    for violation in verdict.policy_violations:
        issues.append(
            AgentLoopIssue(
                code="policy_violation",
                message=violation,
                severity=AgentLoopDiagnosticSeverity.BLOCKED,
                iteration=iteration,
            )
        )
    return issues


def _approval_summary(
    tool_name: str,
    *,
    approval_kind: str,
    control_action: str | None,
    escalation_type: str | None,
) -> str:
    if approval_kind == "human_review":
        return f"human review requested by {tool_name}"
    if approval_kind == "escalation":
        if escalation_type:
            return f"human escalation requested by {tool_name}: {escalation_type}"
        return f"human escalation requested by {tool_name}"
    if control_action:
        return f"{control_action} approval required for {tool_name}"
    return f"tool approval required for {tool_name}"


def _approval_suggestion(approval_kind: str) -> str:
    if approval_kind in {"human_review", "escalation"}:
        return "resume after the required human approval decision is recorded"
    return "resume after the required tool approval is recorded"
