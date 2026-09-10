# Task 2.3 verification

## Status

Task 2.3 is **verified** and checked in the delivery checklist. This document
records scoped evidence; it does not accept G2 as a whole, G3 parent-loop
continuation, dynamic Research rollout, or production readiness.

The accepted implementation contract is in `task-2-3-implementation-plan.md`.
No feature enablement, deployment migration, or live database mutation was
performed for this increment.

## Delivered contract

- Task/plan projections and checkpoint/reducer use v4 logical readiness.
  READY has no active attempt or admission owner; only explicit admission
  increments attempts and reserves budget. TaskInstance remains v3.
- QUEUE and GROUP_WAVE ownership are durable and preserved through recovery.
  Unallocated READY cannot be republished as an admitted queue task.
- Scheduler ordering is priority, dependency depth, task id and definition
  checksum. First-fit retains that complete order and skips candidates without
  committing provisional pool or budget allocations.
- Canonical wave evidence binds selected/overflow order, per-task reasons,
  pool scope/policy/revision, reservations and ledger before/after checksums.
- Shared capacity and wave publication use one transactional state CAS in the
  underlying event unit of work. Normal verified settlement updates that same
  authority; unknown outcomes retain their reservations.
- Public commit/append methods cannot bypass fresh capacity mutation CAS.
  Exact historical redelivery validates original evidence without reserving
  again or replacing later projection/capacity state.
- Zero-selection waits preserve the complete READY set and fixed group
  deadline without an empty wave or attempt allocation. Pool-free slot waits
  represent their absent pool snapshot with null, not a fabricated pool.
  Required shared-pool policy/snapshot dependencies still fail closed online.
- Cross-wave attempt-history validation preserves immutable group identity
  without confusing later lifecycle state with the admitted snapshot.

## Acceptance evidence map

| Requirement | Direct test evidence |
| --- | --- |
| Logical readiness, exact admission, owner and version validation | `tests/framework/harness/task_plan/test_task_lifecycle_contract.py` |
| Capacity rejection preserves the last budget for a later task | `test_capacity_packing.py::test_first_fit_capacity_rejection_preserves_last_budget_for_later_task` |
| Budget rejection preserves a pool slot for a smaller later task | `test_capacity_packing.py::test_first_fit_budget_rejection_preserves_pool_for_smaller_later_task` |
| Priority/depth/id selected and overflow order survives durable wave admission, reopen and offline replay | `test_ready_order_replay.py::test_ready_order_and_capacity_overflow_survive_sqlite_offline_replay` |
| Distinct plans with independent SQLite runtimes cannot overbook one capacity scope; loser has no event/projection/budget changes | `test_parallel_admission_transactions.py::test_two_plans_compete_atomically_for_one_sqlite_capacity_revision` |
| Fresh public-entry CAS bypass is rejected; historical redelivery is idempotent | `test_parallel_admission_transactions.py` public commit, raw append and completion cases |
| Verified settlement releases capacity once; invalid or hidden release is rejected | `test_parallel_admission_transactions.py` completion cases |
| SQLite state/event/outbox transaction rollback and redelivery | `tests/infrastructure/storage/events/test_sqlite_state_batch.py` |
| Slot-only wait and explicit submission recovery retain pending identity, fixed deadline, no work and no allocations | `tests/framework/harness/agent_loop/test_orchestration_submission.py::test_capacity_wait_remains_pending_until_explicit_recovery_without_parent_progress` |
| Mixed budget/temporary wait reasons replay idempotently and invalid reasons fail | `tests/framework/harness/task_plan/test_capacity_wait_reason_replay.py` |
| Queue recovery and attempt-history integrity remain enforced | `test_task_plan_recovery.py`, `test_attempt_history_contract.py`, `test_attempt_history_runtime.py`, and existing dependency/Research regression |

The short test-file names in the table are under
`tests/framework/harness/task_plan/` unless a complete path is supplied.
The maps describe coverage, not independent execution results for each row.

## Executed checks

1. Event/storage suite: **756 passed, 81 skipped**, exit 0, 156.89 seconds.
   Log: `outputs/task-2-3-validation-20260910-130207-events.log`.
   Event and storage implementation files have not changed since that run.
2. Frozen pre-final smoke: **3531 passed, 1 failed, 23 deselected**, exit 1,
   3424.70 seconds. Its sole remaining failure was slot-only capacity waiting.
   Compile, offline AgentLoop smoke with zero network calls, and source
   validation passed. All 63 pinned Python/SQL hashes matched at completion.
   Log: `outputs/task-2-3-validation-20260910-130207-smoke.log`.
   This failed run is diagnostic evidence, not the required passing gate.
3. After repairing waiting and applying all three supplemental test patches,
   the concentrated five-file suite passed: **147 passed**, exit 0,
   560.31 seconds. It includes capacity packing, admission transactions,
   ready-order replay, wait-reason replay and orchestration submission.
   Log: `outputs/task-2-3-supplemental-final.log`.
4. Strict OpenSpec validation and `git diff --check` passed after collecting
   those changes. Repeat document/diff checks after finalizing this record.
5. **Final required smoke passed**, exit 0: **3536 passed, 23 deselected,
   31 warnings** in 3444.05 seconds. Compilation passed. Offline AgentLoop
   succeeded with zero network calls. Source validation returned is_valid=true,
   zero errors and zero warnings.
   Log: `outputs/task-2-3-final-smoke-20260910-1415.log`.
   All 64 changed/new Python/SQL hashes match the supplemental tested source
   and remained unchanged through the final smoke. Deprecation warnings from
   FastAPI/Starlette and fitz are recorded in the log; they did not fail checks.

## Boundaries and remaining work

- SQLite evidence uses real database transactions and independent runtimes.
  PostgreSQL adapter/unit coverage is not a live PostgreSQL concurrency run;
  skipped external integrations remain unverified.
- The waiting test exercises Harness orchestration dispatch/recover_submission,
  not full automatic parent AgentLoop continuation. G3 remains separate.
- Full crash/receipt reconciliation, child production composition, final joins,
  replacement replan, cancellation/fencing, rollout and rollback stay in their
  subsequent numbered tasks. No later task is accepted by this increment.
- Outputs/logs and intermediate patch files are local diagnostic artifacts,
  not source files for staging. Stage only reviewed task-scoped source, tests,
  migration and OpenSpec records after all required checks pass.
