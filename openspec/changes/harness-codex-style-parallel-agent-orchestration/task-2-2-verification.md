# Task 2.2 verification — accepted 2026-09-10

Task 2.2 is checked after the final frozen-source smoke exited successfully.
This records admission evidence only, not full G2 or release acceptance.

## Contract and implementation audit

- `ParallelAgentCoordinator._group_definition` binds the complete accepted plan,
  policy, parent Graph execution, required roles, static admission policy and
  aggregate budget. Live available concurrency is not part of that identity.
- `create_group` exposes a local session only after admission publication and
  coalesces same-identity concurrent callers. `restore_group` restores the
  canonical group, wave ordinals, attempt identities and outstanding reservations
  without publishing admission or invoking execution.
- Both TaskPlan stores run shared admission validation. The durable store
  repeats validation against the canonical prefix on each publication retry;
  the event store's stream CAS protects the atomic batch. The derived admission
  index is not a second writable state source.
- A plan cannot admit a second group through another correlation. A group cannot
  admit another wave before its previous wave has durable terminal evidence.
  Wave ordinals are contiguous and bounded. Later snapshots cannot replace
  membership, policy, parent identity, join policy, limits or budget envelope.
- Historical exact batch redelivery verifies the original projection references;
  it does not overwrite the latest projection or reserve budget again.

## Added acceptance coverage

- Synchronized concurrent group and wave admission: identical and conflicting
  batches, tested through separate SQLiteEventStore instances on the same file
  and through the existing canonical test adapter. A barrier pauses each writer
  at publication after reading the same prefix. Exact redelivery must succeed
  twice with one authoritative batch; competing content must have one winner
  and one sequence conflict. Group admission reserves no attempt budget; wave
  admission leaves only the winning attempt's reservation.
- Reopen SQLite and redeliver original group/wave batches after wave completion;
  assert unchanged events, latest projection, artifact bytes and ledger count.
- Seven immutable group drift cases with recomputed checksums, exercised against
  both memory and durable stores: membership, admission policy, budget, join
  policy, parallelism, wave bound and parent physical activity. Rejection must
  leave history, projection and ledger unchanged.
- Reopen SQLite, replay admission and reconstruct the coordinator with zero
  live capacity. Restore and repeated admission must retain original waves,
  ordinal and reservation identity without event publication or executor calls.

## Checks and root-cause fixes

- First concentrated run: 136 passed, 3 failed, 204.02 seconds.
  `outputs/task-2-2-admission.xml` records this failed run. Two concurrent wave
  tests exposed the test adapter's incomplete transaction lock: sequence checks
  occurred outside the lock held only at commit. Its unit of work now owns the
  lock from context entry through exit, including rollback. The same race matrix
  also uses real SQLite WAL transactions. The third failure was a test passing
  frozen replay arrays directly to a JSON parser; it now uses `thaw_mapping`,
  as the production stage does.
- Second concentrated run: 142 passed, 1 failed, 246.38 seconds.
  `outputs/task-2-2-admission-final.xml` records this failed run despite its name.
  The remaining new restoration test omitted the coordinator's required explicit
  execution port. It now supplies a call-failing port and asserts zero calls;
  production construction guards and execution behavior are unchanged.
- OpenSpec strict validation passed before final smoke; final whitespace check
  passed. No focused tests were rerun after the last test correction: the full
  required smoke includes all four concentrated test files.
- Final `.venv/Scripts/python.exe -m scripts.dev smoke`: **exit 0**.
  Compile passed; pytest reported **3451 passed, 23 deselected, 31 warnings**
  in **2744.33 seconds (45:44)**. The warnings are dependency/API deprecations;
  source validation reported `is_valid=true`, `error_count=0`, `warning_count=0`.
  Logs: `outputs/task-2-2-smoke.log`; JUnit: `outputs/task-2-2-smoke.xml`.
- The final JUnit has 3451 cases, zero failures/errors/skips. It includes every
  case in the concentrated scope: admission transactions **44**, coordinator
  orchestration **46**, durable TaskPlan store **20**, spawn recovery audit **33**
  (143 total). All passed, including the corrected zero-execution restore case.
- Offline AgentLoop succeeded with `network_calls=0`, three LLM fixture calls
  and one tool call. Run: `test-agent-loop-3a8104bd485248f2a485bb1f0c6b3858`;
  manifest: `.newsroom/smoke/test-agent-loop-3a8104bd485248f2a485bb1f0c6b3858/manifest.json`.
- Source hashes were rechecked after successful smoke and exactly match the
  frozen values below. Only this acceptance record and the task checkbox were
  changed afterward; unrelated frontend edits remain outside this commit.

Final log SHA-256:
`61cfbe657c3f6994ce8a04f609c59ab2180c8d3377c809127d0869fa054e6963`.
Final JUnit SHA-256:
`57d09879404557d1cd31bb282eede8537b04b971b3af52477f027c73d2733c0b`.

## Frozen source hashes before final smoke

SHA-256:

- `tests/framework/harness/task_plan/test_parallel_admission_transactions.py`:
  `6b07c145c43c4f280b6543eaa268edcbdfbe2248a6c270e42db0f8c6ab882c39`
- `tests/framework/harness/task_plan/test_durable_task_plan_store.py`:
  `dfb79fbc2694bb65f2d974ce4580526e987ceba9037cf6ba335743f21a8cfe6e`

## Limits

The SQLite evidence covers single-host canonical admission, not PostgreSQL or
distributed exactly-once execution. Artifact storage in these tests remains
an immutable test adapter. Multi-pool packing, crash reconciliation of actual
children, execution metering and parent continuation retain their separately
numbered task gates. Existing unrelated frontend changes are outside this batch.
