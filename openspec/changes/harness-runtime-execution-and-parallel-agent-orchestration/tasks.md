## 1. Contract Consolidation

- [x] 1.1 Freeze the predecessor evidence, current HEAD, worktree status, caller inventory, and exact dependency versions in a merged traceability record. See `evidence/merged-traceability-20260923.md` for every unchecked predecessor task and retained dependency blocker.
- [ ] 1.2 Reconcile execution profile, child handle, group/wave/task/attempt, receipt, budget, event, artifact, and continuation schemas into one versioned contract without duplicate authorities.
- [ ] 1.3 Add strict validators for identity, checksum, tenant/scope, capability, policy, reference, schema, and transition invariants.
- [x] 1.4 Record explicit `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`, `DEGRADED_SERIAL`, and `ENABLED_PARALLEL` composition states and static defaults.

## 2. Execution Environment And Child Supervision

- [x] 2.1 Complete provider capability admission for filesystem roots, environment allowlists, network policy, argv, process limits, timeout, cancellation, and termination confirmation. See `evidence/provider-capability-admission-20260928.md` for the local Docker capability matrix and real-container tests.
- [x] 2.2 Add process restart, tool timeout, child loss, cancellation uncertainty, and external side-effect deduplication integration scenarios.
- [x] 2.3 Complete Harness-owned child `spawn/status/wait/cancel/close`, heartbeat, lease, stale reclaim, idempotent operation, and confirmed termination behavior through real composition.
- [x] 2.4 Recover committed child results without re-invocation and fail closed for ambiguous ownership or non-idempotent side effects.
- [x] 2.5 Run architecture and production-caller scans proving every sandboxed tool and child lifecycle path enters Harness-owned ports. See `evidence/caller-boundary-scan-20260923.md`; the legacy Nougat entry remains explicitly blocked and fail-closed rather than executable outside the port.
- [ ] 2.6 Produce deployment capability evidence for every enabled provider, including unsupported capability rejection and rollback behavior.

## 3. Durable Runtime Events And Replay

- [ ] 3.1 Route turn, tool, approval, context, worker, and child lifecycle facts through the canonical durable event owner with redaction, bounded references, reason codes, and identity checks.
- [x] 3.2 Implement idempotent projection checkpoint/cursor resume and offline rebuild with zero live dependency calls. See `evidence/runtime-projection-checkpoint-replay-20260928.md`.
- [x] 3.3 Add event identity conflict, redaction, cursor, projection rebuild, replay checksum, and no-routing-authority tests. See `evidence/runtime-projection-checkpoint-replay-20260928.md`.

## 4. Parallel Group And Wave Coordinator

- [ ] 4.1 Route each accepted TaskPlan through the Harness-owned group/wave coordinator and persist immutable group membership, join policy, plan checksum, policy checksum, and budget envelope.
- [ ] 4.2 Implement deterministic multi-pool packing, all-or-nothing reservations, stable READY overflow, resource conflict fencing, and reservation settlement.
- [ ] 4.3 Route every child through real `SubAgentRuntime`/`AgentRunner` and `ToolExecutor`/`ToolBatchExecutor` with isolated context, refs, tools, memory, budget, lease, and attributable receipts.
- [ ] 4.4 Reconcile admission, spawn intent, receipt, dispatch, crash, cancellation, and recovery without duplicate confirmed children or unsafe fallback.
- [ ] 4.5 Complete stable multi-wave join, required-role completeness, deterministic merge/conflict checks, aggregate gate, and completion-order-independent final result.
- [ ] 4.6 Implement bounded retry, wave exhaustion, legal replacement replan, old-group quarantine, fail-fast sibling cancellation, reclaim, fence loss, and indeterminate handling.
- [ ] 4.7 Verify overlap, multi-wave capacity, heterogeneous packing, upstream failure, spawn crash, online recovery, offline replay, and invocation-count invariants.

## 5. Result Authority And Parent Continuation

- [ ] 5.1 Verify every result against group/wave/plan/task/attempt/binding, transcript/output/artifact/tool receipts, schema, memory/budget, and deterministic gate references.
- [ ] 5.2 Replace synchronous-only parent orchestration with durable submission identity and bounded `PENDING` receipts.
- [ ] 5.3 Implement same-parent-turn continuation with idempotent observation delivery, terminal checksum reuse, redelivery handling, and restart recovery.
- [ ] 5.4 Project summaries only from gated durable structured evidence with ordering, redaction, UTF-8 truncation, schema version, checksum, and verified spill references.
- [ ] 5.5 Preserve legacy single-child caller result/error/stop/recovery semantics with pinned capability mapping and golden fixtures.
- [ ] 5.6 Add parent contract tests for pending/resume/redelivery, partial failure/replan, exhaustion, unavailable dependencies, hidden-context isolation, and completion-order-independent replay.

## 6. Dynamic Research Integration

- [ ] 6.1 Dispatch structure, contribution, and experiment analysis through bounded multi-pool fan-out with existing per-role gates and wait-all policy.
- [ ] 6.2 Preserve document/evidence references and policy-approved tools without copying parent private context into child inputs.
- [ ] 6.3 Aggregate verified branch outputs into `analysis_branch_refs` and preserve claim verification, quality, reader, card, artifact, and publication successors.
- [ ] 6.4 Add fixed golden inputs with field-level role/ref/checksum/evidence/gate/quality/reader/card/publication parity and explicit allowed differences.
- [ ] 6.5 Cover accepted, partial-failed, cancelled, indeterminate, serial, and crash-recovered Research histories with full ledger/checksum equality and zero live replay calls.
- [ ] 6.6 Add negative dependency checks for durable transcript, artifact, tool, reference, capacity, and publication policies; serial fallback SHALL not bypass them.

## 7. Verification And Release

- [ ] 7.1 Run focused Harness, AgentLoop, tool, supervisor, Research, architecture, and source-boundary tests and fix root causes.
- [ ] 7.2 Run `python -m scripts.dev compile`, `python -m scripts.dev test`, `python -m scripts.dev smoke`, and strict OpenSpec validation for the merged change and repository. Verify retained prerequisites from `model-aware-llm-context-preflight` 7.1-7.6, `durable-event-runtime` 9.5, and `harness-workflow-graph-runtime` 1.1 before release qualification; keep their evidence with their existing owners.
- [ ] 7.3 Capture run/stage/group/wave/capability admission, wait, run, join, budget, retry, recovery, cancellation, and degraded telemetry evidence.
- [ ] 7.4 Exercise generic AgentLoop only in controlled allowlisted runs after coordinator and continuation gates pass.
- [ ] 7.5 Exercise allowlisted dynamic Research only after golden parity, quality gates, and dependency provenance pass.
- [ ] 7.6 Rehearse disablement and explicit serial rollback with active groups pinned to original policy; preserve receipts/history and verify inspection/replay.
- [ ] 7.7 Record implementation, recovery, deployment, rollback, and residual-risk evidence. The two predecessor directories were already removed in migration commit `1f4a8ae2`; retain their Git provenance without treating that migration as release qualification.
