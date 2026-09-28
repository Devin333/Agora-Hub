# Continuation scope and child lifecycle contract evidence

Date: 2026-09-28. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.
This is a completed implementation slice of tasks 1.2 and 1.3; neither task is
fully qualified, so their checkboxes remain open.

## Existing version owner bound

The durable child lifecycle state writer and reader in
`framework/harness/subagents/supervisor_store.py` now share the public
`CHILD_AGENT_LIFECYCLE_STATE_SCHEMA` constant
(`newsroom.harness-child-lifecycle-state/v2`). The read-only
`RuntimeContractBinding.current()` references that owner as
`child_lifecycle_state`, alongside the existing child handle owner. A real
SQLite transactional snapshot with an unsupported schema version is rejected
by the lifecycle reader with `child_event_store_corrupt`.

`ChildAgentTerminalReceipt` is reconstructed and checksum-verified inside the
versioned lifecycle state; no standalone receipt schema or second serializer
was created. `ExecutionReceipt` remains an in-memory provider return value.
`GraphExecutionIdentity` has an exact-field reader in
`framework/shared/graph_identity.py` but no standalone schema/version owner:
its carrying contracts own their respective versions. A fabricated Graph
identity version would duplicate authority.

## Continuation identity enforced at canonical acceptance

`ParentContinuation.from_dict` validates the payload schema and checksum.
`continuation_from_event` additionally compares its `run_id` and `stage_id`
with the outer `TaskPlanEvent` (or full event mapping). If a payload and event
are individually checksum-valid but refer to different scopes, acceptance
fails with `parent_continuation_scope_mismatch`. Both in-memory and durable
TaskPlan stores use the continuation append validator before CAS, and the
historical replay reducer uses the same decoder.

Tests cover mismatched run and stage independently, history replay, memory
CAS and real SQLite CAS. On rejection the event stream high watermark,
recorded history, projection/sequence, artifacts and replayed continuation
remain unchanged. Existing valid continuation roundtrips, idempotent
redelivery and `PENDING` to terminal transition tests remain part of the
focused test suites.

## Remaining qualification

Tasks 1.2 and 1.3 still require the complete runtime event routing and
reference/scope checks at actual production boundaries (3.1), verified
child/result references and gates (4.3, 5.1), and durable parent submission,
recovery and redelivery (5.2, 5.3). These facts cannot be inferred from the
schema binding or the isolated continuation CAS validator. Provider deployment
qualification and rollout remain separately open at 2.6 and 7.1-7.7.

## Local checks

- Continuation-focused suite: 82 passed; new and directly related cases: 22 passed.
- Lifecycle-state/binding-focused suite: 64 passed.
- `python -m scripts.dev compile`: passed.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: passed.
- `git diff --check`: passed.
