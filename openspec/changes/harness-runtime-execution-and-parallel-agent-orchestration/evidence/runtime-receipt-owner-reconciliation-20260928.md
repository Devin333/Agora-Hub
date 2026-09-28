# Runtime receipt and owner contract reconciliation

Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
Scope: task 1.2 owner/version reconciliation; selected task 1.3 boundary checks.
The code and assertions below supplement
`runtime-contract-binding-slice-20260928.md` and
`continuation-scope-and-lifecycle-state-20260928.md` rather than replacing their
historical findings. Those earlier records describe the code before this slice.

## Current owner versions and live read/write boundaries

`RuntimeContractBinding.current()` in `framework/harness/runtime_contract.py`
imports the following versions from the owners at runtime. Its v2 projection
adds a pointer from the nested child terminal receipt to the existing
`child_lifecycle_state` owner; it does not create a second registry, event
writer, reader policy, or storage authority. Version values below are the
actual `runtime_contract_binding()` output for this worktree.

| Surface and exact owner versions | Writer / production acceptance or reader | Immutable identity and integrity |
| --- | --- | --- |
| Execution: `execution_profile` `newsroom.execution-profile/v1`; `execution_receipt` `newsroom.execution-receipt/v1` | `framework/execution_environment/models.py` owns both; provider emits the `ExecutionOutcome` receipt through `registry.py:105-156`, which now roundtrips through `ExecutionReceipt.from_dict` before accepting it; `framework/tool/runtime/executor.py:1005-1006` attaches receipt identity/checksum to tool result metadata. | The receipt checksum now covers schema, execution/tool/Graph/operation/attempt/provider/capability, status, termination and bounded output. Registry compares the original request, admitted provider capability checksum, and returned output bytes/checksum. The existing in-memory provider return is not claimed to be a newly durable record. |
| Child: `child_agent_handle` `newsroom.child-agent-handle/v1`; `child_lifecycle_state` `newsroom.harness-child-lifecycle-state/v2`; `subagent_invocation` `newsroom.subagent-invocation/v3` | `framework/harness/subagents/supervisor.py:335,1634,2101` owns handle and terminal receipt construction/recovery; `supervisor_store.py:30,670-751` persists and reads the lifecycle state; nested receipt readers in `task_plan/attempt_history.py:365-383` and `task_plan/replay.py:2046-2057` delegate to `ChildAgentTerminalReceipt.from_dict`. | The nested terminal receipt has no independent schema: lifecycle-state v2 owns its strict fields and checksum. The durable terminal event verifies receipt child, operation, parent Graph and terminal state against the event, including when a forged receipt has been rechecksummed; the handle carries parent/child Graph, run, task, attempt, capabilities and lease ownership; the event store binds tenant scope separately. |
| Plan/task: `task_plan_runtime` `newsroom.harness-task-plan-runtime/v1`; `validated_task_plan` `newsroom.harness-task-plan/v2`; `task_instance` `newsroom.harness-task-instance/v3`; `task_plan_projection` `newsroom.harness-task-plan-projection/v4`; `task_plan_event` `newsroom.harness-task-plan-event/v3`; `task_result` `newsroom.harness-task-plan-result/v3` | `framework/harness/task_plan/schema.py` registers the exact executable writers; `task_plan/store.py:1501-1504` and `task_plan/durable_store.py:931-933` validate results before persistence; `attempt_history_index.py:389-391` validates historical plan/event/result reads. | Plan/version/stage/run, task instance, worker binding, attempt, output and gate refs/checksums are validated against the accepted plan, rather than another worker-supplied value. TaskPlan registry and owner-constant drift are checked by the read-only binding. |
| Group and capacity: `dispatch_request` `agora.harness-parallel-dispatch-request/v3`; `dispatch_result` `agora.harness-parallel-dispatch-result/v1`; `dispatch_group` `agora.harness-dispatch-group/v3`; `dispatch_wave` `agora.harness-dispatch-wave/v4`; `task_reservation` `agora.harness-task-reservation/v1`; `budget_reservation` `agora.harness-budget-reservation/v2`; `attempt_history` `newsroom.harness-task-attempt-history/v1` | `framework/harness/task_plan/parallel.py:79-84,1445-1452` validates dispatch before capacity admission; strict group/wave/reservation readers are in the same owner; `task_plan/attempt_history.py:36,208` owns attempt history serialization/readback; `framework/harness/control_plane/budget_reservation.py` owns capacity reservations. | Group/wave bind plan checksum, membership, join/budget policy and task/attempt identity; admission references the accepted plan and profile. Capacity reservations, physical child occupancy and LLM budget have separate owners and are never interchangeable. |
| SubAgent evidence: `subagent_attempt_identity` `newsroom.subagent-attempt-identity/v3`, `subagent_context` `newsroom.subagent-context-evidence/v3`, `subagent_output` `newsroom.subagent-output-document/v3`, `subagent_transcript` `newsroom.subagent-transcript/v3`, `subagent_receipt` `newsroom.subagent-transcript-receipt/v3`, `subagent_bundle` `newsroom.subagent-attempt-bundle/v3` (`framework/harness/subagents/transcript.py:25-30`) | `SubAgentTranscriptStorePort.write/read_context/read_output/verify` in `transcript.py:533-537` owns writes and verification, including exact `from_dict` readers and receipt readback. | Parent/child run, Graph, stage, plan, task, attempt, context envelope, output checksum and atomic bundle receipts remain bound by the transcript owner; merely naming the schemas does not certify every future result acceptance path (tasks 4.3/5.1). |
| LLM budget: `budget_policy`, `budget_snapshot` `newsroom.budget/v1`; `budget_event` `newsroom.budget-event/v1` | `framework/governance/budget/models.py:14-15,339,934,1169` owns exact readers; `BudgetLedger.snapshot/restore` and child tracker provider own production writes and reads. Event catalog registration uses the same `BUDGET_EVENT_SCHEMA_VERSION`. | Run/scope hierarchy, policy digest, Graph identity, reservation/operation, usage and settlement belong to BudgetLedger, separately from TaskPlan capacity and physical child occupancy. |
| Events: `event_envelope` `newsroom.event-envelope/v2`; `runtime_event_data` `newsroom.runtime-event/v1` | `framework/events/canonical.py:26,382,666,792` and `framework/events/runtime/projection.py:68,218,785` own envelope and data emission/readback; runtime data version is checked against `framework/events/schema/catalog.py`, and the registered reducer consumes the same owner schema. | Event identity, content and record checksums, Graph/activity refs and redaction stay under the canonical event runtime. Complete routing of all listed event families remains task 3.1. |
| Artifact: nested `graph_terminal` `newsroom.graph-terminal-manifest/v1`; `artifact_manifest` `newsroom.graph-terminal-manifest/v2` | `framework/harness/artifacts/terminal_manifest.py:28-29,1043-1070` owns v2 readback and v1 nested terminal verification; `infrastructure/storage/artifacts/graph_terminal.py:56,189` owns persistence. | Tenant/run/Graph/terminal status, artifact refs, execution versions and manifest hash are verified before accepting the manifest. |
| Side effects and tools: `side_effect_intent` `newsroom.harness-side-effect-intent/v2`, `side_effect_decision` `newsroom.harness-side-effect-decision/v2`, `side_effect_outcome` `newsroom.harness-side-effect-outcome/v3`; `tool_result_envelope` `newsroom.tool-result-envelope@1`, `tool_side_effect_receipt` `newsroom.tool-side-effect-receipt@1`, `harness_bound_tool_receipt` `newsroom.harness-bound-tool-side-effect-receipt@1`, `tool_side_effect_evidence` `newsroom.tool-side-effect-evidence@1` | `framework/harness/side_effects/models.py:22-24,291,564,1151`, `framework/tool/models/result_envelope.py:26-27,45-191`, and `framework/harness/runtime/tool_result_adapter.py:61-64,114-171,271-316` own these writers/readers. | Graph/run/attempt/subject/idempotency/fencing and tool/call/operation/response/gate/bound receipt checksums are checked by the actual artifact and tool owners. |
| Continuation: `parent_observation` `agora.harness-parent-observation/v1`; `parent_continuation` `newsroom.harness-parent-continuation/v1` | `framework/harness/task_plan/parallel.py:84,3694` emits typed parent observations and `agent_loop/orchestration.py:685` consumes them; `task_plan/continuation.py:19,115,132` reads a parent continuation and verifies its enclosing TaskPlan event scope before both in-memory and durable CAS append. | Group/plan/aggregate refs/checksums and original run/stage/parent turn bind observation and continuation. Full durable submission and redelivery are tasks 5.2/5.3; typed schema ownership does not imply those workflows are complete. |

## This slice: canonical receipt readers and rejection behavior

- `ExecutionReceipt` now has an exact v1 schema owned by
  `framework/execution_environment/models.py:66,960-1113`. The version is
  included in its receipt checksum. `ExecutionEnvironmentRegistry.execute`
  verifies a provider return by canonical roundtrip before accepting its
  identity/capability/output; an unsupported version or tampered checksum
  raises `ExecutionIdentityMismatchError`.
- `ChildAgentTerminalReceipt.from_dict` strictly checks the nested lifecycle
  receipt shape and checksum. The supervisor's recovery reader, TaskPlan
  attempt-history reader and parallel recovery reader all use it. Durable
  lifecycle event ingestion cross-checks a receipt against the enclosing event
  before any snapshot change; it rejects changed operation identity even if
  the forged receipt has a valid recomputed checksum.
- `RuntimeContractBinding` v2 is derived from owner constants and exposes the
  receipt's owner as well as the child receipt's existing projection owner.
  It has no persistence writer and does not grant event, budget, tool or
  publication authority. Unsupported versions and extraneous/missing fields
  are rejected; no historical receipt is silently relabeled.

## Qualification boundary

Task 1.3 still requires its full identity/checksum/tenant/scope/capability/
policy/reference/schema/transition matrix at each applicable real boundary,
including failure without unauthorized worker calls or state mutation. Tasks
3.1, 4.3, 5.1, 5.2 and 5.3 still own full event routing, child/result
reference verification and durable parent continuation, respectively. The
versioned schema reconciliation here does not claim to deliver those runtime
behaviors, provider deployment qualification (2.6), or rollout (7.x).

## Verification

- Targeted receipt, task history, replay, provider and ToolRuntime tests:
  185 passed, 2 skipped. Both skips require filesystem symlink creation,
  unavailable on this Windows platform; those two symlink cases are not
  claimed as exercised.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`:
  passed.
- `git diff --check`: passed (Git line-ending warnings only).
- Repository pre-commit `python -m scripts.dev smoke`: passed; 3765 passed,
  23 deselected in 3310.59s, followed by successful deterministic AgentLoop
  smoke and `sources validate` (`is_valid=true`, zero errors or warnings).
