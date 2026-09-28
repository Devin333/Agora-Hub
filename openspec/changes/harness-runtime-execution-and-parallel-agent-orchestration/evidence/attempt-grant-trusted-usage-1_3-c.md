# Attempt grants and trusted child usage: task 1.3 evidence slice

Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
Scope: three production-path contract tests for attempt-scoped reference grants,
parallel wave admission, and usage recovered from the trusted child execution
ledger. The owner implementation is in the earlier committed runtime and
result-acceptance slices; this record does not mark task 1.3 complete.

## Observed boundaries

- `tests/framework/harness/task_plan/test_subagent_result_lineage.py` creates two
  independently valid attempts for one accepted task, commits both canonical
  transcript/receipt bundles and both attempt-specific reference grants, then
  presents the second attempt's result as the first. The verifier rejects
  `REF_SNAPSHOT_BINDING_MISMATCH` before calling the gate or changing result,
  projection, or event state. The correctly scoped first attempt succeeds,
  and both original grants remain readable.
- The same file admits a real durable group but no `TASK_WAVE_ADMITTED` event,
  then invokes the online stage with a valid worker result. The stage rejects
  `task_plan_result_admission_mismatch` before gate evaluation or result
  persistence. The test compares the event sequence, projection, and result
  collection before and after rejection. The worker was called once; this is
  a result-admission boundary, not a promise to prevent worker invocation.
- `tests/framework/harness/agent_loop/test_parent_ref_admission.py` runs the
  actual child `AgentRunner` and graph composition with an adapter that erases
  only the runner's reported `AgentLoopMetrics`. It reads each committed
  transcript's `newsroom.trusted-child-execution/v1` fact, validates its
  identity and tool references, and compares the resulting trusted usage
  against recovered worker metrics and both persisted `TaskResultRecord.usage`
  values. LLM, token, tool-evidence, and memory-namespace observations are
  derived from that ledger rather than the underreported return value.

## Validation and remaining scope

- Focused lineage and AgentLoop test files passed before the repository gate.
- `python -m scripts.dev smoke`: 3775 passed, 23 deselected (31 warnings) in
  its test phase, followed by successful deterministic AgentLoop smoke and
  `sources validate` (`is_valid=true`, zero errors and warnings).
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`
  and `git diff --check` passed.

Task 1.3 stays unchecked: persisted gate verdict provenance, online/offline
readback of the same gate evidence, and the remaining identity/scope/policy/
reference/schema/transition matrix still need production-boundary proof.
Tasks 4.3 and 5.1 also remain unchecked until full child routing and result
verification requirements are met. This slice does not qualify provider
deployment (2.6), complete canonical event routing (3.1), or release (7.x).
