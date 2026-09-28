# Canonical TaskResult owner admission, task 1.3 slice B

This slice validates the exact TaskResult owner record at both TaskPlan store acceptance paths before any result-key, history, projection, artifact, or duplicate lookup. Task 1.3 remains open for the remaining reference, gate verdict, transition, and event boundaries; task 3.1 remains open for cross-owner durable routing.

## Reproduced owner gap and repair

A typed result with modified content and an unchanged `result_checksum` was already rejected on a first append by a downstream history reader, but surfaced the lower-level `task_plan_checksum_mismatch` diagnostic. In the in-memory store, after a canonical result had been committed, the same altered typed record could instead pass the duplicate fast path without rejection. SQLite likewise lacked a shared owner check before entering its duplicate/history lookup.

`validate_task_result_owner_contract` now uses the exact runtime-bound `TaskResultRecord` type and schema, canonical `from_dict(to_dict())` checksum reader, and canonical object equality. Both the in-memory and SQLite `append_result` entry points call it before result-key/history/projection/artifact lookups and duplicate returns. The existing full plan/binding/attempt/transition checks still run at their original boundaries. Canonical duplicate submissions remain idempotent; self-consistent wrong-binding and conflicting-duplicate records retain their domain diagnostics.

## Physical and negative evidence

The real SQLite event runtime/store test observes the accepted plan, projection, result list, artifact adapter, and event-stream high watermark before and after rejected first appends and forged duplicates. Every observed state remains unchanged. It reopens SQLite to confirm an accepted plan with no result after rejection, then submits a canonical result and reopens again to read it back. A canonical duplicate changes neither committed state nor high watermark. The in-memory scheduler admission test verifies the same forged-duplicate rejection and no-side-effect boundary. Runtime contract tests cover canonical records, stale checksums, and duck-typed result objects.

The existing durable store design may leave an unreachable immutable artifact if publication fails after an artifact write; this slice does not claim cross-store atomicity. Its rejection checks occur before any artifact write.

## Verification

- Focused runtime contract, in-memory TaskPlan, and SQLite store suite: 89 passed.
- Parallel, admission-transaction, and attempt-history regression suite: 135 passed.
- `python -m scripts.dev smoke`: 3772 passed, 23 deselected; AgentLoop smoke succeeded with zero network calls; source validation reported zero errors and zero warnings.
- `openspec validate harness-runtime-execution-and-parallel-agent-orchestration --strict` and `git diff --check`: passed.

Task 1.3 remains unchecked until the remaining contracts, especially durable gate verdicts and event/continuation transitions, are fully verified.
