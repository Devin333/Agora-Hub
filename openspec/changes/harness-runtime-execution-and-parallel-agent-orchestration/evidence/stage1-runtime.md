# Stage 1 Runtime Evidence

Commit: `d490adcb`

The trusted child execution path now binds the admitted Graph identity, durable input grant, child budget scope, tool evidence scope, and deterministic result gates to the real `SubAgentRuntime`/`AgentRunner`/`ToolExecutor` path. Child output schema validation remains owned by the deterministic SubAgent gate after the action loop; provider-managed structured output is not used as a substitute for that gate.

The runtime also records runner-turn evidence before tool registration, persists physical attempt and terminal evidence, supports durable parent execution context registration/reopen, preserves retryable provider fallback accounting, and rejects unclassified execution profiles before a tool handler is invoked.

Validated commands:

```text
python -m scripts.dev compile
python -m pytest tests/framework/agent/loop tests/framework/agent/test_unified_budget_governance.py -q
python -m pytest tests/framework/llm/test_router_invocation_budget.py tests/framework/llm/test_router_context_preflight.py tests/framework/llm/test_router_context_stream.py tests/framework/llm/budget -q
python -m pytest tests/framework/tool/runtime -q
python -m pytest tests/framework/harness/agent_loop tests/framework/harness/task_plan/test_parent_execution_context.py tests/framework/harness/subagents tests/framework/tool/runtime -q
```

Observed results: `67 passed`, `34 passed`, `67 passed`, and `409 passed` respectively. The focused child/evidence/budget set also passed `153` tests. `git diff --check` passed.

This evidence does not claim process restart/cancellation uncertainty deployment capability, full repository smoke, dynamic Research fan-out, golden Research parity, or rollback readiness. Those tasks remain open.
