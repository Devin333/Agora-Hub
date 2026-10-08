# Implementation, rollback recovery, and local verification

Date: 2026-10-02. Change: `harness-runtime-execution-and-parallel-agent-orchestration`.

This record covers the completed 1.x–6.x implementation, local verification under 7.x, and the distinction between a passing repository gate and production release qualification. The OpenSpec change remains in progress: `openspec list --json` reports 32 of 39 tasks completed.

## Implementation and recovery evidence

- Tasks 1.3 and 3.1: strict identity, checksum, tenant/scope, capability, policy, schema, transition, and canonical event admission are documented in `runtime-contract-invariant-matrix-20260930.md`, `canonical-runtime-event-tenant-scope-20260930.md`, `runtime-event-reason-code-admission-20260930.md`, and `canonical-runtime-event-agent-loop-composition-20260930.md`. Events retain bounded references and deterministic reason codes; their durable owner remains the stage/event store.
- Tasks 4.1–4.7: `ParallelAgentCoordinator` owns immutable group/wave policy, supervised dispatch, reservations, retry and recovery. An explicit serial rollback now fences new admissions before attempting a durable write, records the same checksum-bound receipt through each accepted group's original stage event sink, and preserves accepted waves' admitted parallelism. Lost sink acknowledgements retry the same receipt idempotently; missing stage event authority fails closed. Fresh coordinators restore the receipt and active waves from stage history, reject new groups, and retain offline replay without worker or supervisor calls. `test_parallel_orchestration.py` and `test_spawn_recovery_audit.py` cover these transitions and tampering, including an externally re-signed event with a corrupted inner receipt.
- Tasks 5.1–5.6: `AgentRunner` binds a separate recovery-only Harness port. With new admissions disabled, an already committed parent turn can resume from its durable conversation using `recover_submission` without LLM work or another group admission. `test_agent_runner_graph_conversation_state.py` covers enabled and recovery-only continuations; deterministic observation and result verification remain on the Harness side.
- Tasks 6.1–6.6: bounded dynamic Research branch execution, input/evidence isolation, gated aggregation, golden parity, ledger/replay checks, and negative dependency checks remain covered by the full suite and the focused Research portion of smoke. The local test execution does not constitute an allowlisted production Research run.
- Task 7.6 local rehearsal: an accepted RUNNING wave is recovered into a fresh coordinator, rolled back with its original stage's canonical event owner, and recovered again. Tests assert a stable group checksum and pinned parallel policy, checksum-bound rollback history, fenced new groups, deterministic offline inspection/replay, no extra physical child spawn, and fail-closed receipt tampering. This is a local deterministic rehearsal; deployment rollback qualification remains outstanding.

## Commands after implementation was complete

- `python -m scripts.dev test`: **8577 passed, 115 skipped, 24 deselected**, 561 warnings; 1:20:38. Exit code 0.
- `python -m scripts.dev compile`: passed. Exit code 0.
- `python -m scripts.dev smoke`: **3872 passed, 23 deselected**, 31 warnings; 1:02:49. Its offline AgentLoop command succeeded with `network_calls=0`, and source validation returned `is_valid=true`, `error_count=0`, `warning_count=0`. Exit code 0.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict`: valid. Exit code 0.
- `git diff --check`: passed.

## Release boundaries and remaining work

- 2.6: `deployment-capability-rollback-20260930.md` explains why local Docker provider admission cannot stand in for deployment-owner capability, unsupported-capability, and actual rollback receipts. No deployment-owner qualification receipts were supplied.
- 7.2: local commands above passed, but `model-aware-llm-context-preflight` 7.1–7.6, `durable-event-runtime` 9.5, and this change's canonical `harness-graph` dependency baseline are still unchecked with their respective owners. The merged release cannot be qualified on these local command results alone.
- Migration note (2026-10-08): the obsolete `harness-workflow-graph-runtime` directory was removed. Its event/replay, attempt-fencing, and side-effect qualification remains open as the canonical `harness-graph` dependency baseline in this change's task 7.2; this historical release record does not create a dependency on the deleted directory.
- 7.3–7.5: full run/stage/group/wave/capability telemetry histories, controlled generic AgentLoop allowlisted runs, and allowlisted dynamic Research runs still require real release/deployment evidence and their prerequisite gates.
- 7.6–7.7: local rollback and recovery cases now have code and repeatable tests; controlled rollback deployment rehearsal, live receipts, owner attestations, and the final deployment/residual-risk dossier are not present. Keep these release tasks unchecked until their actual evidence exists.
