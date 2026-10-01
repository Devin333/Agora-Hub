from __future__ import annotations

from enum import Enum


class AgentLoopStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    ACCEPTED = "accepted"
    RETRY_EXHAUSTED = "retry_exhausted"
    BLOCKED = "blocked"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    STALLED = "stalled"
    FAILED = "failed"
    STOPPED = "stopped"
    MAX_ITERATIONS = "max_iterations"

    def is_terminal(self) -> bool:
        return self in {
            AgentLoopStatus.SUCCEEDED,
            AgentLoopStatus.ACCEPTED,
            AgentLoopStatus.RETRY_EXHAUSTED,
            AgentLoopStatus.BLOCKED,
            AgentLoopStatus.WAITING_FOR_APPROVAL,
            AgentLoopStatus.STALLED,
            AgentLoopStatus.FAILED,
            AgentLoopStatus.STOPPED,
            AgentLoopStatus.MAX_ITERATIONS,
        }


class AgentLoopStopReason(str, Enum):
    FINAL_ANSWER = "final_answer"
    MAX_ITERATIONS = "max_iterations"
    TOOL_ERROR = "tool_error"
    LLM_ERROR = "llm_error"
    POLICY_DENIED = "policy_denied"
    STALLED = "stalled"
    FINAL_OUTPUT_ACCEPTED = "final_output_accepted"
    CONTROL_OUTPUT_ACCEPTED = "control_output_accepted"
    JUDGE_BLOCKED = "judge_blocked"
    SECRET_BLOCKED = "secret_blocked"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TOOL_BUDGET_EXCEEDED = "tool_budget_exceeded"
    AGENT_POLICY_BLOCKED = "agent_policy_blocked"
    REPEATED_TOOL_CALL_STALLED = "repeated_tool_call_stalled"
    PARSER_RETRY_EXHAUSTED = "parser_retry_exhausted"
    JUDGE_RETRY_EXHAUSTED = "judge_retry_exhausted"
    EMPTY_OUTPUT_EXHAUSTED = "empty_output_exhausted"
    MAX_ITERATIONS_EXCEEDED = "max_iterations_exceeded"
    LLM_FAILED = "llm_failed"
    GLOBAL_BUDGET_EXCEEDED = "global_budget_exceeded"
    TOOL_FAILED = "tool_failed"
    UNKNOWN_FAILED = "unknown_failed"
    ORCHESTRATION_PENDING = "orchestration_pending"


class AgentLoopEventType(str, Enum):
    AGENT_STARTED = "agent_started"
    ITERATION_STARTED = "iteration_started"
    LLM_CALL = "llm_call"
    LLM_STREAM_EVENT = "llm_stream_event"
    LLM_CALL_FAILED = "llm_call_failed"
    ACTION_PARSED = "action_parsed"
    PARSER_ERROR = "parser_error"
    TOOL_CALL = "tool_call"
    TOOL_OBSERVATION = "tool_observation"
    TOOL_BUDGET_BLOCKED = "tool_budget_blocked"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    REPEATED_TOOL_CALL_DETECTED = "repeated_tool_call_detected"
    SUBAGENT_DELEGATION_REQUESTED = "subagent_delegation_requested"
    SUBAGENT_COMPLETED = "subagent_completed"
    SUBAGENT_FAILED = "subagent_failed"
    JUDGE_ACCEPT = "judge_accept"
    JUDGE_RETRY = "judge_retry"
    JUDGE_BLOCK = "judge_block"
    STRUCTURED_OUTPUT_REPAIR_REQUESTED = "structured_output_repair_requested"
    STRUCTURED_OUTPUT_VALIDATION_ACCEPTED = "structured_output_validation_accepted"
    STRUCTURED_OUTPUT_REPAIR_BUDGET_EXHAUSTED = (
        "structured_output_repair_budget_exhausted"
    )
    FINAL_OUTPUT = "final_output"
    AGENT_WAITING_FOR_APPROVAL = "agent_waiting_for_approval"
    AGENT_BLOCKED = "agent_blocked"
    AGENT_STALLED = "agent_stalled"
    AGENT_RETRY_EXHAUSTED = "agent_retry_exhausted"
    AGENT_FAILED = "agent_failed"
    AGENT_COMPLETED = "agent_completed"


class AgentLoopDiagnosticSeverity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKED = "blocked"


class JudgeDecision(str, Enum):
    ACCEPT = "accept"
    RETRY = "retry"
    ESCALATE = "escalate"
    BLOCK = "block"
