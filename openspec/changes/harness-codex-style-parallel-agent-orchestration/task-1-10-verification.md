# Task 1.10 verification — accepted 2026-09-10

Task 1.10 is checked after its final frozen-code checks. This document records
scoped implementation evidence, not release acceptance or proof that G1-G5 are complete.

## Implemented surfaces

- Planning observation request/receipt parsing rejects missing and unknown
  fields and inconsistent nested/outer checksums. Repeat reads and candidate
  source validation revalidate immutable receipt integrity.
- Planning call intents consume budget before tool execution. Filesystem
  admission uses a verified exclusive lock and immutable intent files. An
  intent without its terminal receipt cannot trigger duplicate execution.
  A completed intent and receipt consume one slot, not two. Offline authorized
  readers reject reservation writes.
- Tool exceptions, mismatched observations and elapsed deadlines produce
  attributable failure receipts. Receipt commit is followed by readback.
- PLAN_BUILD_INTENT / PLAN_BUILD_RECEIPT enter the canonical TaskPlan event
  store. Attempts pin request/policy identity and bounds; replay rejects
  conflicting identities, missing candidate evidence and post-acceptance builds.
- Research planning sends request-level timeout and one transport attempt to
  the actual candidate worker / OpenAI-compatible client. Harness owns retries
  with a persisted retryable failure and an independent next attempt.
- The generic builder requires an explicit generate_bounded worker entrypoint;
  it does not install a thread or invoke an unbounded generate fallback.

## Observed checks

- Initial concentrated run: 856 passed, 4 failed, 701.56 seconds. Three negative
  tests mutated the returned copy instead of the stored receipt; the fourth
  asserted the obsolete one-event pre-plan halt sequence. Tests were corrected
  to exercise stored corruption and assert intent/receipt/halt identity.
- Second concentrated run: 163 passed, 1 failed, 114.17 seconds. The remaining
  dynamic Research golden event prefix expected the old two-event sequence;
  it now asserts intent/candidate/receipt/acceptance. JUnit:
  `outputs/task-1-10-focused.xml` (this file records the failed run).
- OpenSpec strict validation and git diff whitespace check passed before the
  final concentrated run.
- Expanded concentrated run: 813 passed, 5 failed, 183.94 seconds. All five
  failures were existing structured-output evaluation digest mismatches; the
  planning and Research integration checks passed. JUnit:
  `outputs/task-1-10-final-focused.xml`.
- Root cause: project rename commit `8be01657` edited `license_disposition` in
  the frozen schema corpus without changing its bound digest. Restored that
  single line to the exact `eda6d961` evidence. The entire corpus now matches
  that original Git version; no digest, observation, metric or release record
  was changed. Both declared and recomputed digest are
  `sha256:babfc4241f3b948edd7830f2ca11b4e5b20fff3664155f5492e3dc9090466d49`.
- Public contract regression after repair: **748 passed**, exit 0, 80.21 seconds.
  Covers full `tests/framework/llm`, full `tests/framework/events`, Research
  candidate worker and filesystem planning observation tests. JUnit:
  `outputs/task-1-10-public-contracts.xml`.
- The first repository smoke was deliberately stopped after the audit found
  the standalone planning-source replay gap below. Compile passed, but this
  interrupted run is NOT a passing smoke gate (process exit -1). Its partial
  log remains `outputs/task-1-10-smoke.log`; no completed JUnit is claimed.
  Frontend changes in the shared checkout are outside this task's scope.

## Scope boundary

- Generic production
  composition remains a separate Task 3.5 requirement; the generic planning
  adapter exposes bounds explicitly and rejects an unbounded worker entrypoint.

## Replay gap found and resolved before acceptance

The initial implementation validated the original candidate's planning refs in
`TaskPlanStageRunner._replay_history()`, but standalone recovery did not pass
planning receipts to the reducer. Build-attempt checks alone proved candidate
checksum lineage, not the contents or scope of planning observations.
The follow-up batch pins source refs and planner turn in the accepted-plan
checksum and adds explicit recorded planning receipts to reducer/recovery inputs.
Stage replay forwards already-authorized receipts; standalone replay verifies
checksum, success and run/stage/turn/policy identity without live tool ports.
Negative tests cover missing, resealed cross-scope and failed evidence.

The same batch adds a persisted turn deadline to planning call intents. Later
calls receive only the remaining time, including after a store reopen. Admission
rejects an expired deadline or a backwards wall-clock observation. These changes
completed focused verification: **129 passed**, exit 0, 109.21 seconds, JUnit
`outputs/task-1-10-replay-deadline-final.xml`. The first run found one needless
queue-reader call for an empty READY set (128 passed, 1 failed); recovery now
avoids that call, retaining the strict zero-call assertion.

## Final acceptance evidence

- `python -m scripts.dev smoke --keep-going`: **exit 0**, **3427 passed,
  23 deselected, 31 dependency deprecation warnings**, pytest duration 2639.99 s.
  JUnit independently reports 3427 tests, 0 failures, 0 errors and 0 skipped.
  The 23 deselections are the repository's existing offline smoke selection;
  no tests were removed or assertions weakened for this task.
- Compile passed. Offline AgentLoop run
  `test-agent-loop-4371f3c91c2d4faeb5395a6afc6b055d` succeeded with
  `network_calls=0`; its manifest is under `.newsroom/smoke/`.
- Source validation returned `is_valid=true`, `error_count=0`, `warning_count=0`.
- All 31 implementation/test/spec files matched their pre-smoke SHA-256 hashes
  after completion. Only acceptance documentation/checklist changed afterwards.
- OpenSpec strict validation and `git diff --check` passed.

Local retained evidence and SHA-256:

| Artifact | SHA-256 |
| --- | --- |
| `outputs/task-1-10-final-smoke.log` | `ff57aa0eebb1348073358b389b2d6337f2ae5f9b9661824ce83da567c713374a` |
| `outputs/task-1-10-final-smoke.xml` | `adb046b22b3a8432ff8952dad19e293c173fc8bcf45276c924f6763bc81bb6f2` |
| `outputs/task-1-10-public-contracts.xml` | `d2d5ca6c8df41f537488b7b333008a42cfd43f024e483f6cd9c15451795ef4f3` |
| `outputs/task-1-10-replay-deadline-final.xml` | `4c9251f5f2ecde5c157ebf1575500a2b97c3acac216e68aadf18b450161265e3` |

Requirement-to-evidence mapping:

- Calls/failure accounting: intent-before-execute and shared-store budget tests
  in `test_planning_observation.py` and `test_planning_observation_store.py`.
- Timeouts: request-level transport limits, independent elapsed measurement,
  late-success rejection and persisted same-turn deadline/reopen tests.
- Retries: canonical failed receipt/new intent history, maximum call bounds,
  no retry of unconfirmed/successful/timed-out attempts, and restored runner reuse.
- Durable receipts: canonical event-store reopen/replay and filesystem intent /
  receipt identity tests; readback is mandatory before exposing committed evidence.
- Source validation: strict schema/checksum parsing, accepted-plan source binding,
  stage authorization, and standalone replay/recovery missing/cross-scope/failed
  receipt tests with live tool execution forbidden.
