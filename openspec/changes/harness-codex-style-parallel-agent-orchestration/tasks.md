# Delivery Gates

Aligned to PRD revision `e9a81f7d`. The previous checklist had 16/43 checked
items with partial implementation evidence recorded in `verification.md`.
Those checks do not prove expanded PRD contracts. Retained checks below cover
only unchanged, focused surfaces; each gate requires all its own exit evidence.

## 1. G1 Contract

- [x] 1.1 Align proposal, design, capability specs and this checklist with the PRD, including online recovery/offline replay separation, canonical states, defaults and independent G1-G5 acceptance; pass strict OpenSpec validation.
- [x] 1.2 Use one Agent/Harness `ParentObservationLimits` contract with canonical `max_observation_bytes`, PRD defaults, strict parsing and matching serialization/checksum tests; remove competing field aliases.
- [ ] 1.3 Complete versioned group/wave/task/attempt schemas, canonical `REPLAN_PENDING` and `BLOCKED_DEPENDENCY`, typed wave outcomes, stable identity/checksum and transition validation.
- [ ] 1.4 Implement durable candidate dedup keyed by run/stage/parent-turn/action-correlation plus candidate checksum, stable group identity, terminal reuse and conflict rejection across restart.
- [ ] 1.5 Establish shared `RefAuthority` validation for input/result/planning refs and memory namespaces, including owner/tenant/stage/run/access/type/checksum/allowlist and read-only sharing policy.
- [ ] 1.6 Define per-task multi-pool capacity demand, versioned pool reservations, resource conflict keys and fenced mutation lifecycle; reject missing or stale required capacity policy.
- [ ] 1.7 Define versioned token/time/tool/cost budget allocations and ledger settlement; prove consumed/released/outstanding invariants, no partial reservation and idempotent retry/cancel/recovery accounting.
- [ ] 1.8 Preserve complete accepted/rejected/failed/cancelled/indeterminate/reclaimed/quarantined attempt history and group/wave/plan/task/binding/receipt identity in result verification and replay.
- [ ] 1.9 Extend canonical events/checkpoints with spawn intent/receipt, ledger, complete history index, continuation and aggregate/observation checksums; reject corrupt/conflicting histories.
- [ ] 1.10 Complete bounded planning calls/timeouts/retries/failure accounting with durable receipts and strict source-observation validation.

## 2. G2 Coordinator

- [x] 2.1 Connect `TaskPlanStageRunner` to the Harness-owned group/wave coordinator port instead of its implicit synchronous worker loop.
- [ ] 2.2 Complete immutable group admission and single-active-wave admission transaction with durable, idempotent membership, policy and budget-envelope pinning.
- [ ] 2.3 Implement deterministic first-fit multi-pool packing, all-or-nothing task reservations, stable overflow READY order and pool evidence in wave checksums.
- [x] 2.4 Commit wave admission, ledger and `TASK_ATTEMPT_SPAWN_INTENT` atomically before supervisor spawn; persist per-task confirmed/unknown receipts using unique operation keys.
- [ ] 2.5 Reconcile crashes around admission/intent/receipt/dispatch using audited supervisor status; never duplicate confirmed children or treat missing receipts as proof of no spawn.
- [ ] 2.6 Route each child through the real `SubAgentRuntime`/`AgentRunner` and `ToolExecutor`/`ToolBatchExecutor`, with isolated refs/context/tool/memory/budget/lease and attributable durable tool receipts.
- [ ] 2.7 Verify every child result against group/wave/plan/task/attempt/binding, readable transcript/output/artifact/tool receipts, schema, memory/budget and deterministic gates before acceptance.
- [x] 2.8 Propagate terminal upstream failure to unadmitted transitive `BLOCKED_DEPENDENCY` descendants, release allocations and finish wait-all without spawning blocked tasks.
- [ ] 2.9 Complete stable multi-wave join, required-role completeness, deterministic merge/conflict checks, aggregate gate and one final group result independent of completion order.
- [ ] 2.10 Complete bounded new-attempt retry, wave exhaustion and legal replacement replan with new plan/group/correlation identity; quarantine old-group late receipts.
- [ ] 2.11 Complete fail-fast admission closure, sibling cancel receipt/lease waiting, reclaim, resource-scoped fence loss and indeterminate handling without unconfirmed side-effect replay.
- [x] 2.12 Preserve the explicit serial adapter and fail closed when parallel execution requires a missing adapter; never silently select serial fallback.
- [ ] 2.13 Prove monotonic interval overlap, three-tasks/two-slots multi-wave join, heterogeneous packing, upstream failure, spawn crash matrix and online/offline recovery boundaries with invocation-count assertions.

## 3. G3 AgentLoop

- [x] 3.1 Parse bounded multi-child `delegate_batch` candidates and reject forbidden top-level/child control fields without introducing Agent-to-Harness reverse imports or Agent-owned threads/queues.
- [ ] 3.2 Replace synchronous-only orchestration with durable submission identity and bounded `PENDING` receipts; do not advance parent reasoning while waiting.
- [ ] 3.3 Implement durable same-parent-turn continuation with idempotent observation id/version delivery and terminal checksum reuse across restart/redelivery.
- [ ] 3.4 Project summaries only from gated durable structured evidence and typed diagnostics; fix ordering/redaction/UTF-8 truncation/version/checksum and spill oversize detail to verified refs while retaining identity/continuation.
- [ ] 3.5 Complete real generic production composition and negative dependency checks for coordinator, binding registry, supervisor, durable stores, artifact verifier, authorized tools and observation policy; no fake fallback.
- [ ] 3.6 Verify legacy one-logical-task adaptation with old caller golden result/error/stop_reason/diagnostics/trace fixtures, unique pinned capability mapping, cancellation and recovery.
- [ ] 3.7 Add full parent contract tests for pending/resume/redelivery, partial failure/replan, exhaustion, unavailable dependencies, hidden-context isolation and completion-order-independent replay.
- [x] 3.8 Preserve the two-stage Harness-authorized planning request/receipt/candidate flow, deny side-effect planning tools, and replay observations from recorded receipts without live tool calls.

## 4. G4 Research

- [x] 4.1 Wire the dynamic Research graph factory to the actual group/wave coordinator and `ChildAgentSupervisor`.
- [x] 4.2 Retain `document`/`evidence_pack` references and policy-approved tools without copying parent private context into child inputs.
- [ ] 4.3 Dispatch structure/contribution/experiments through bounded multi-pool fan-out with all existing per-role gates, attributable real tool receipts and pinned wait-all.
- [x] 4.4 Aggregate accepted role results deterministically into `analysis_branch_refs` and preserve fixed claim verification, quality, reader/card and publication successors.
- [x] 4.5 Preserve static Research as default and regression coverage that required-role/quality failures produce no downstream success or publication.
- [ ] 4.6 Complete production negative dependency checks, including durable transcript/artifact/tool/ref/capacity policy; explicit serial fallback cannot bypass these dependencies.
- [ ] 4.7 Add fixed golden inputs with field-level role/ref/checksum/evidence/gate/quality/reader/card/publication parity assertions and an explicit allowed-difference list.
- [ ] 4.8 Cover accepted/partial-failed/cancelled/indeterminate/serial/crash-recovered Research histories with full history/ledger/checksum equality and zero live replay calls.

## 5. G5 Release

- [ ] 5.1 Pass focused Harness/AgentLoop/tool/supervisor/Research/architecture tests, required repository compile/smoke/source checks and strict OpenSpec validation; record final exits and remaining limitations before code commits.
- [ ] 5.2 Expose independent `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`, `DEGRADED_SERIAL` and `ENABLED_PARALLEL` states without switching the static default.
- [ ] 5.3 Capture run/stage/group/wave/capability-linked admission/wait/run/join/budget/retry/recovery telemetry and alert evidence.
- [ ] 5.4 Exercise generic AgentLoop only in controlled allowlisted runs after G3 passes, retaining reproducible continuation and replay evidence.
- [ ] 5.5 Exercise allowlisted dynamic Research only after G4 passes, documenting golden parity, gates, failures and production dependency provenance.
- [ ] 5.6 Rehearse feature disablement/explicit serial fallback with running groups pinned to their original policy; preserve receipts/history, settle reservations and verify post-rollback inspection/replay.
- [ ] 5.7 Record G1-G5 evidence separately, including recovery and rollback artifacts and rollout decisions; do not equate feature enablement or passing structural validation with production readiness.
