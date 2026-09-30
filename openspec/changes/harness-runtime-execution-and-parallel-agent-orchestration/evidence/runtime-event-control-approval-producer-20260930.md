# Canonical control approval producer evidence (2026-09-30)

## Gap

`AgentLoop` can enter an approval wait in two ways. A tool policy denial is
owned by `ToolExecutor`, which already publishes `approval_requested`. The
successful `control.request_human_review` and `control.escalate` tools instead
return control metadata that `AgentLoop` converts into a human-review or
escalation wait. Before this change that second path existed only in the local
AgentLoop transcript because the canonical agent-loop source intentionally
filters tool and approval diagnostics to avoid duplicate producers.

## Change

- Added the scoped `agent-loop-approval` producer map. It admits only
  `agent_waiting_for_approval` as canonical `approval_requested`.
- `AgentLoopEventRecorder` selects that source only for non-`tool_approval`
  waits. Tool approvals remain exclusively owned by `ToolExecutor`.
- The canonical event carries the exact Graph execution identity, the bounded
  approval id reference, a stable diagnostic reason code, and redacted
  metadata through `RuntimeEventEmitter`.
- Projection failure remains fail-closed because the recorder does not catch
  canonical sink errors.

## Focused verification

`python -m pytest tests/framework/agent/test_agent_loop_trace_context.py tests/framework/events/test_runtime_event_projection.py -q`

Result: `40 passed`.

The regression tests prove that human-review waits append exactly one
`approval_requested` fact with Graph identity, reason code, and approval ref,
while ordinary tool-approval waits append no AgentLoop-owned duplicate.

The required pre-commit smoke also passed: compile, Harness/Research/API/
architecture integration (`3833 passed, 23 deselected, 31 warnings`), smoke
AgentLoop execution, and source validation.

## Remaining qualification boundary

This closes the discovered control-approval request producer gap. OpenSpec 3.1
still needs merged release evidence that production compositions inject the
durable publisher for every enabled turn, tool, approval-decision, context,
worker, and child producer. Deployment qualification and the repository-wide
release gates remain separate tasks.
