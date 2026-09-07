# Implementation Verification

## Status

The change is partially implemented. Checked tasks have focused code and test
evidence; unchecked tasks must not be treated as delivered merely because their
types or entrypoints exist. Generic orchestration remains disabled by default.
This record is not production rollout approval.

The PRD revision `e9a81f7d` expands the contracts. Proposal/design/specs/tasks
now follow G1-G5. The former 16/43 task count is historical, not acceptance of
durable submission/continuation, spawn reconciliation, multi-pool packing or
the expanded golden/replay matrix. No feature flag was enabled by this update.

## Verified Surface

- The Agent-owned orchestration contracts remove the reverse dependency from
  `framework/agent` into Harness while retaining strict request/result parsing.
- Parent delegation covers multi-child candidates, forbidden top-level and child
  control fields, fan-out limits, legacy mapping, bounded joined observations,
  redaction, failed/cancelled/indeterminate/halted outcomes, and turn exhaustion.
- `TaskPlanStageRunner` uses the supervised group/wave coordinator. Focused
  tests prove overlapping child execution, multi-wave join, explicit serial
  fallback, and fail-closed handling when the adapter is unavailable.
- Planning observation tests cover allowlisting, read-only authorization,
  durable receipt integrity, source-ref scope, and replay without live tools.
- Dynamic Research tests retain its fixed roles, aggregation contract,
  claim/quality/publication boundary, static default, and recorded replay path.
- G1 observation limits now use one Agent-owned data contract across AgentLoop,
  Harness and TaskPlanPolicy: 8 tasks, 2048 summary bytes, 16 diagnostics,
  16 refs and `max_observation_bytes=16384`. Old/unknown fields are rejected
  before dispatch; UTF-8 limits and policy checksum roundtrips are tested.
- Windows artifact containment compares equivalent DOS/extended DOS and
  UNC/extended UNC paths without removing their operational long-path prefix.
  Atomic I/O, directory traversal and locks use the corresponding namespace;
  escape/device/reparse defenses and POSIX root semantics are retained.

## Checks

The checks in this section precede the candidate-submission foundation below.
They remain historical evidence, not validation of subsequent edits.

- Combined TaskPlan, AgentLoop, dynamic Research, composition and architecture
  regression: 467 passed, 4 deprecation warnings.
- Agent, tool, and orchestration composition regression, including new parent
  boundary tests: 201 passed, 2 skipped.
- Strict OpenSpec validation: passed.
- PRD-aligned observation/AgentLoop/TaskPlan/composition regression: 155 passed.
- Artifact and transcript boundary regression: 159 passed, 3 skipped.
- Agent and tool regression on the final tree: 202 passed, 2 skipped.
- Earlier required full smoke: 2420 passed, 1 failed due to Windows namespace
  mismatch during concurrent filesystem transcript creation. This was not a
  passed commit gate.
- Repaired-tree required full `python -m scripts.dev smoke`: exit 0. Compile
  passed; Harness/Research/API/service/composition/architecture suite reported
  2475 passed, 23 deselected, 23 deprecation warnings in 842.69 seconds.
- The following offline AgentLoop smoke also succeeded with 3 LLM fixture calls,
  1 tool call and 0 network calls. Evidence manifest:
  `.newsroom/smoke/test-agent-loop-b3bc1b3de37b481c9a288f9035348490/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec validation and `git diff --check`: passed on the updated tree.

These are checks of the current partial implementation, not proof of unchecked
G1-G5 scenarios. No production enablement, external-provider qualification or
rollback rehearsal is claimed. The current checklist has 10/46 completed items.

## Candidate Submission Foundation

This increment implements a scoped part of 1.4; 1.4 remains unchecked.

- `AgentOrchestrationRequest` v2 requires a trusted `parent_turn_id`. AgentLoop
  derives it from agent/activity/iteration identity, independently of the model's
  action correlation. Missing fields, old schema and unknown aliases are rejected.
- `CandidateSubmission` pins run/stage/parent-turn/action-correlation, the source
  action checksum, immutable candidate reference, original acceptance time and
  stable submission/plan identities. The record is committed in the canonical
  candidate-built event; an unreferenced artifact is not authoritative admission.
- Memory and durable stores atomically reuse equal admissions. Durable admission
  rechecks dedup using the same history snapshot used to allocate its sequence,
  then relies on event-stream CAS. Tests force a second writer to win between
  the first lookup and artifact completion; only one submission event survives.
- The stage binds the initial plan to the submitted candidate and original time.
  Restart after candidate admission but before plan acceptance reads that candidate
  without calling the candidate builder again.
- Accepted-plan success and failure outcomes are recorded with checksums and
  reused after reopening the runtime/store, without new events or worker calls.
  Reuse validates the complete history, causal admission, gated aggregate and
  terminal plan/projection identity. Conflicting payloads return
  `CANDIDATE_IDEMPOTENCY_CONFLICT` without old result refs or projection mutation.
- The canonical catalog now registers the existing group/wave event vocabulary
  with closed nested payload schemas. TaskPlan imports that vocabulary rather
  than maintaining a second list. Real durable lifecycle payloads, missing fields
  and forbidden nested control fields are tested.
- Expanded event regression exposed a reducer-audit classification defect:
  `inspect.getclosurevars` includes import/attribute names matching module globals.
  Dependency checks now examine actual global reads. Forbidden imports and
  captured capabilities remain rejected with the existing strict assertions.

Increment checks:

- Submission/store/replay/runtime/schema regression: 104 passed.
- Events, AgentLoop and orchestration composition regression: 538 passed.
- Strict OpenSpec validation: passed.
- Required full `python -m scripts.dev smoke`: exit 0. Compile passed; the
  Harness/Research/API/service/composition/architecture suite reported
  `2537 passed, 23 deselected, 23 warnings` in 904.54 seconds. The offline
  AgentLoop smoke produced 3 fixture LLM calls, 1 tool call and 0 network calls;
  source validation reported `is_valid=true`, 0 errors and 0 warnings.
- Parallel lifecycle/replay regression: 34 passed. This covers shared group/wave
  transition validation, typed terminal outcomes, terminal reservation checksum
  readback, failed-group event parity and rejection of corrupt durable transitions.

The store can distinguish multiple submission identities, but stage execution
still owns one plan chain per run/stage. A different turn or correlation is
therefore rejected as `task_plan_submission_scope_unavailable`, never aliased to
old results. Multiple submission execution scopes, concurrent active-execution
coalescing, pre-plan rejection outcome reuse, `PENDING` receipts and durable
same-parent-turn continuation are still acceptance work. No G1/G3/G5 gate or
production readiness is claimed by these focused checks.

## Remaining Acceptance Work

### Spawn Recovery Audit Increment

This increment advances 2.5; its full acceptance checkbox remains open. The
current checklist is 12/46, superseding the earlier historical counts above.

- Online spawn reconciliation records one correlated `RECOVERY_STATUS_READ`
  before each supervisor query and a `RECOVERY_RECONCILED` or `RECOVERY_HALTED`
  conclusion. Canonical schemas require group/wave/task/attempt/operation and
  recovery identity; run/stage/plan identity is carried by the TaskPlan envelope.
- A complete wave and all intents are validated before any live read or local
  admission mutation. Restart consumes verified replay group/wave snapshots;
  terminal groups cannot be reopened, and the existing dispatch lock serializes
  admission with recovery. Confirmed worker handles are retained, not respawned.
- Receipt caches are updated only after append succeeds. An interrupted audit
  conclusion can be retried from its recorded status read without another live
  call. Identical receipt delivery is reused; conflicting status, child or task
  identity is rejected before canonical append, including same-batch conflicts.
- Group admission also rolls back its in-memory session when the durable
  admission append fails, so a retry cannot be suppressed by a ghost group.
- Coordinator recovery now validates and reuses a terminal `TaskResultRecord`
  held by the controlled child adapter after a parent-side append interruption;
  mismatched result identity is rejected before the child is closed. The
  supervisor terminal event also persists a checksum-bound result envelope, so
  a fresh supervisor/coordinator can recover the typed task result without
  invoking a live worker; resolver-backed results must match the same checksum.
- When a verified parent result is already recovered, a confirmed terminal child
  receipt (including a child-side FAILED/CANCELLED state) no longer blocks the
  group join; the child is closed and the verified parent result remains the
  source of truth.
- The stage now invokes admission reconciliation before its normal scheduling
  loop. It rehydrates both admitted and already-dispatched nonterminal waves
  from canonical history. No new wave, budget charge or child is created by
  this path.
- The stage now invokes coordinator result recovery on every parallel loop,
  including when the parent result store is empty. A checksum-bound terminal
  child result can therefore be written back through the normal TaskPlan
  result transition after a parent-side crash; failure-attempt history is used
  for idempotent suppression so retry scheduling is not duplicated.
- Supervised child join now revalidates task id, task-instance id, attempt, and
  frozen plan identity before accepting a result, preventing a terminal child
  from injecting an outcome from another attempt or plan.
- `TaskReservation` now validates every multi-pool capacity allocation as a
  concrete pool id with a positive integer quantity, so malformed zero,
  negative, boolean, and string allocations fail closed before wave checksum
  or replay.
- Pool packing now carries each admitted pool's versioned policy checksum into
  `TaskReservation`, `DispatchWave.wave_id`, the canonical event schema, and
  replay state transitions. Allocation and policy evidence must match exactly,
  so a policy-version change produces a new wave identity and cannot reuse an
  older reservation history.
- `ParallelDispatchRequest` now fails closed when task capacity demands have no
  pool policy, omit a dispatched task, or reference an unavailable pool; the
  coordinator cannot interpret missing capacity policy as unlimited capacity.
- Supervised child budget envelopes now carry a versioned reservation schema,
  owner scope, deterministic operation key, parent allocation, attempt
  allocation, ledger version, and canonical reservation checksum. This binds
  the metadata sent to the child admission boundary. The same envelope is now
  required in `TASK_ATTEMPT_SPAWN_INTENT` and is validated by offline replay;
  tampered checksums and key mismatches fail closed. The broader durable
  TaskPlan ledger/settlement work remains explicitly open.
- Offline reduction verifies every audit against its admitted spawn operation,
  status-read identity and receipt. Audited confirmations are the only route
  for reopening an indeterminate group at this boundary. Unknown outcomes keep
  their outstanding reservations; they are not evidence of released capacity.
- Canonical-store/checkpoint tests exercise crashes before receipt commit,
  before dispatch commit and after dispatch commit, with one dispatch fact and
  zero live supervisor calls during replay. Additional tests cover partial or
  duplicate intent sets, audit write failures, conflicting/untrackable handles,
  terminal groups, receipt redelivery and corrupted recovery evidence.

Increment checks before final commit:

- Spawn recovery audit suite: 33 passed, including a fresh coordinator/process
  restart that re-reads child status and reuses durable receipts.
- Broader TaskPlan/AgentLoop/supervisor regression: 307 passed; focused spawn
  and receipt recovery regression: 61 passed.
- Final required repository smoke: passed (`2653 passed, 23 deselected`, plus
  smoke AgentLoop artifact and source validation).
- Strict OpenSpec validation: passed.

Remaining 2.5/2.13 work includes auditing the older wait/close recovery path,
durable supervisor restoration beyond terminal result envelopes, and
ledger/admission conflict recovery. This increment proves admission, dispatch,
and terminal child-result repair, not successful whole-stage completion after
every crash.
G1-G5, rollout, and production readiness remain unproven.

### Per-Attempt TaskBudget Ledger Increment

This increment implements part of 1.7. It does not complete the expanded
token/time/tool/cost contract. Task 2.4 has been reopened: readiness ledger
reservation and wave/intent admission still use separate commits. The current
checklist is 11/46; the earlier 12/46 count overstated that atomic boundary.

- `TaskPlanBudgetLedger` is the sole authority for the four existing TaskBudget
  dimensions: turns, tool calls, memory operations and output tokens. Its
  versioned, checksummed records retain full TaskInstance identity, deterministic
  reservation keys, actual reservation/settlement revisions and terminal result
  checksums. It does not reinterpret the separate LLM BudgetLedger dimensions.
- Every dimension enforces consumed + released + outstanding <= the pinned
  stage allocation. Batch reservations are pure and all-or-nothing. Retry uses
  a new attempt and cannot reuse settled/released allocation; failure history
  remains present. Missing usage is conservatively charged at its allocation.
- Reservation and result settlement travel through the existing TaskPlan
  event/projection transactions. Durable write-failure/reopen tests prove no
  partial readiness/result accounting. In-memory result commits now roll back
  both events, result indexing and projection after injected append failure.
- Duplicate reservation, result and unstarted dependency-block release are
  idempotent only for identical evidence. Wrong attempt/owner/allocation,
  conflicting aliases, overage, inconsistent usage partitions, overlapping
  retry histories, invalid revisions and corrupt checksums fail closed.
- READY recovery reuses existing budget and parallelism admission, including
  single-slot and exactly-full-envelope cases. It cannot use slots reserved by
  another READY attempt to admit new tasks.
- Offline replay and checkpoint roundtrips retain identical attempt ledger
  contents without invoking workers. Replay reducer is now v3 because budget
  projection semantics changed; v2 reports/checkpoints are rejected. Golden
  checksum updates include new field-level accounting assertions. Existing
  v2 checkpoints need canonical-history rebuild, not a version-label rewrite.
- Released-budget counters are available to bounded observability; full ledger
  records are not added as metric labels or parent-observation content.

Increment checks:

- Full TaskPlan regression: `272 passed` in 122.18 seconds.
- Canonical event/deterministic replay regression: `463 passed` in 50.75 seconds.
- Required repository smoke: exit 0; compilation passed and the full
  Harness/Research/API/service/composition/architecture suite reported
  `2678 passed, 23 deselected, 23 warnings` in 1107.45 seconds.
- Offline AgentLoop smoke passed with 3 fixture LLM calls, 1 tool call and
  0 network calls. Evidence manifest:
  `.newsroom/smoke/test-agent-loop-da0d102d4ad04bd88c65a408fad2b997/manifest.json`.
- Source validation: `is_valid=true`, 0 errors and 0 warnings.
- Strict OpenSpec validation and `git diff --check`: passed.

These checks qualify this scoped code increment for commit, not PRD completion
or production release. The 23 suite warnings concern existing FastAPI/Starlette
deprecations; the CLI also reports its existing PyMuPDF import deprecation.

Remaining scope: time/cost allocation, shared child-envelope/TaskPlan ledger
authority, atomic ledger + wave + intent admission, and complete cancellation,
indeterminate, lease/reclaim and rollback settlement across group histories.
Unknown outcomes are not accepted as terminal ledger results. This increment
does not prove complete cancellation or side-effect recovery accounting.
No G1-G5 gate or production rollout approval is claimed.

### Atomic Attempt Admission Increment

This increment completes task 2.4 with the required repository smoke passing.
The checklist is now 12/46; no complete delivery gate is claimed.

- `TaskPlanStorePort.commit_events` commits typed event/projection pairs in
  one batch with a projection-checksum CAS. Each historical prefix keeps its
  own projection reference; a duplicate batch remains idempotent after later
  transitions. Partial histories and mismatched projections fail closed.
- Stage reservation now occurs only for the coordinator's selected wave.
  `TASK_READY`, the per-attempt ledger updates, `TASK_WAVE_ADMITTED`, and all
  supervised spawn intents are one transaction. Failure at readiness, wave,
  intent or projection-artifact publication leaves the prior event history,
  task states and ledger unchanged and starts no child.
- `ParallelDispatchRequest` v2 requires the authoritative budget snapshot and
  exact accepted TaskInstances. Child budget envelopes use each attempt's
  actual immutable reservation revision. Wave admission binds the before/after
  ledger checksums; live recovery verifies its intent budget against that ledger.
- Task dispatch/start facts follow the confirmed per-task spawn receipts.
  Serial and explicitly test-only transports also record wave dispatch before
  invoking their worker. Result settlement remains owned by TaskPlan result
  transactions, not a second coordinator accounting implementation.
- Offline replay checks the complete contiguous wave/intent batch and its
  READY ledger evidence. Recomputed but unbacked budget checksums, changed
  allocation/revision, missing intents and interleaved history are rejected.
  Historical batch retry validates original immutable artifacts without writes;
  it cannot silently recreate a missing committed projection.

Increment checks:

- TaskPlan and AgentLoop regression: `280 passed` in 214.70 seconds.
- Atomic transition, spawn admission and canonical event regression:
  `491 passed` in 136.93 seconds.
- Coordinator and audited recovery regression: `66 passed` in 38.04 seconds.
- Final spawn-admission negative/real-stage regression: `22 passed` in
  15.59 seconds.
- Required repository smoke: exit 0; compilation passed and the complete
  Harness/Research/API/service/composition/architecture suite reported
  `2697 passed, 23 deselected, 23 warnings` in 1493.92 seconds.
- Offline AgentLoop smoke passed with 3 fixture LLM calls, 1 tool call and
  0 network calls. Evidence manifest:
  `.newsroom/smoke/test-agent-loop-ea1e3cf470274cda9119827ff2b0df98/manifest.json`.
- Source validation: `is_valid=true`, 0 errors and 0 warnings.
- Strict OpenSpec validation and `git diff --check`: passed.

The 23 suite warnings are existing FastAPI/Starlette deprecations; the CLI
also reports the existing PyMuPDF import deprecation.

This is not completion of 1.7, G1-G5 or release approval. The four existing
TaskBudget dimensions are covered; time/cost allocation, complete cancellation,
indeterminate/lease/reclaim settlement, durable capacity-wait readiness, parent
continuations and production gates remain separately tracked. Pre-increment
non-atomic parallel histories are not promoted into verified atomic evidence.
No production defaults or Research feature flags are enabled by this increment.

### Immutable Group Admission Increment

Status: this implementation increment and its focused/full repository checks
are complete. Expanded PRD tasks and release qualification remain open below.

- Group v2 separates stable plan/correlation identity from the immutable
  checksum and pins complete accepted-plan membership, plan and policy
  checksums, parent Graph execution identity, static admission configuration,
  full-plan capacity demands and the total budget envelope.
- Pending admission does not publish an in-memory session before the durable
  event succeeds. Concurrent callers share the outcome, conflicts fail closed,
  and admission waits have an explicit bound.
- Both store adapters validate one group per accepted plan, immutable wave
  ownership, one active wave, contiguous bounded ordinal and closed-group
  rejection against the canonical prefix protected by the atomic commit.
  Direct append cannot bypass the single-active-wave constraint.
- Offline replay uses the same wave-slot validator and rejects multiple active
  waves. Ready budget-batch tracking resets at each admission; settled attempts
  do not prohibit a distinct retry attempt in a later wave.
- Stage restart restores the canonical group and complete wave history before
  audited spawn reconciliation. Recovered child results close the recorded wave
  before another wave or final join; no new child is spawned during restoration.
  Repeated restore with a different wave history fails closed instead of
  silently retaining a stale local projection. Invalid standalone completion
  events are rejected before projection artifacts or events are written.
- Canonical event schema and replay parsing require group v2 evidence; older v1
  groups are rejected rather than silently reinterpreted as pinned admissions.

Increment checks:

- Admission transactions, coordinator, audited recovery, lifecycle replay and
  real Stage regression: `123 passed` in 54.35 seconds.
- Canonical event suite: `463 passed` in 47.80 seconds.
- Strict OpenSpec validation and `git diff --check`: passed.
- An initial smoke run was deliberately stopped to fix the two review findings
  above; that interrupted run is not passing evidence.
- Final required repository smoke: exit 0; compilation passed and the complete
  Harness/Research/API/service/composition/architecture suite reported
  `2729 passed, 23 deselected, 23 warnings` in 1089.68 seconds.
- Offline AgentLoop smoke: succeeded with 3 fixture LLM calls, 1 tool call and
  0 network calls. Evidence manifest:
  `.newsroom/smoke/test-agent-loop-1059700bbb3e4bad99a4e16e7de27fff/manifest.json`.
- Source validation: `is_valid=true`, 0 errors and 0 warnings. The suite warnings
  are existing FastAPI/Starlette deprecations; the CLI also reports the existing
  PyMuPDF import deprecation.

Tasks 2.2 and 2.3 remain open. Absolute group/stage deadlines across restart,
shared RefAuthority, authoritative expiring capacity reservations, capacity-wait
READY without an attempt allocation, and full cancellation/reclaim settlement
remain outside this increment. Production defaults and feature flags are unchanged.

## Task 1.3: Versioned Lifecycle Contracts (2026-09-07)

Task 1.3 is complete; task 1.4 is the next unchecked item. This is contract
acceptance only, not completion of G1-G5 or permission to enable production
parallel orchestration. Earlier task 2.3 readiness/order edits remain deferred
in a separate Git stash and are not part of this increment.

- `TaskInstance`, `TaskProjection`, and their containing `TaskPlanProjection`
  use v3 live contracts. Old v2 payloads are rejected explicitly, including a
  checksummed old parent/child snapshot; no live compatibility reader or
  automatic migration is introduced. Graph identity remains Graph v2.
- Task-instance, idempotency, and fencing identifiers retain their established
  deterministic derivation, but both construction and readback now validate
  them. Recomputed envelope checksums do not authorize identity substitution.
  Existing instance identity has a retained v2 golden checksum proving the
  schema tag is the intended serialized change.
- Canonical task states now include `ADMITTED`, `CANCELLED`, `INDETERMINATE`,
  `QUARANTINED`, and existing `BLOCKED_DEPENDENCY`. Shared transition validation
  enforces state edges, active-attempt coherence, attempt sequencing, immutable
  task definition identity and terminal evidence. Scheduler, result commit,
  retry, dependency blocking, and replay use that validation.
- Real Stage wave admission persists selected tasks as `ADMITTED` in the same
  batch as the wave, budget and spawn intents. Dispatch/start advance the same
  attempt; replay and checkpoint reconstruct identical admission projections.
  Recovery retains wave-coordinator ownership instead of producing a new queue
  message or queue reclaim. Crash recovery regressions retain child counts.
- All non-success dependency terminal states share one classification, so
  cancelled, indeterminate and quarantined predecessors close unadmitted
  transitive descendants without creating attempts. Retryable `FAILED` below
  the pinned limit remains non-terminal for dependency propagation.
- Group and wave models share their transition validators with replay. Tests
  cover every `REPLAN_PENDING` successor, all seven typed wave terminal
  outcomes, stable admission identity, and rejection of terminal rewrites.

Final validation on the completed code:

- Lifecycle, dependency, admission, coordinator, spawn recovery, durable store
  and queue/checkpoint recovery focused suite: `383 passed` in 79.78 seconds.
- Independent canonical event suite: `463 passed` in 48.98 seconds.
- Required repository smoke: exit 0; compile passed;
  `2969 passed, 23 deselected, 23 warnings` in 1106.03 seconds.
- Offline AgentLoop smoke: succeeded, 3 fixture LLM calls, 1 tool call,
  0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-9ceb7a373dcc4c5a98c62eeaa14787f6/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec validation and `git diff --check`: passed.
- One earlier smoke was stopped to address review findings and does not count
  as passing evidence. Existing FastAPI/Starlette and PyMuPDF deprecations remain.
- Independent read-only re-review confirmed the dependency closure, malformed
  READY projection and schema-version findings are fixed, with no new confirmed
  regression in the bounded state/identity/admission/replay review surface.

Full cancellation/quarantine attempt history, expanded budget settlement,
shared RefAuthority, authoritative capacity, and parent continuation remain in
their later unchecked tasks. Static Research and feature defaults are unchanged.

## Candidate Redelivery Integrity Increment

Task 1.4 remains unchecked; overall progress is 13/46. This increment fixes confirmed admission and
redelivery defects in that task; it does not complete the pending parent receipt,
continuation or independent submission execution-scope contracts. Task 1.5 has
not started, and the separate task 2.3 stash is unchanged.

- `CandidateSubmission` v2 records an immutable `admission_id` for its first
  writer. The nonce is excluded from logical submission/plan/group identity,
  but included in the submission record checksum. Equal concurrent publish
  requests cannot both interpret the same durable record as newly created.
  Old v1 submissions fail closed on live readback; no automatic migration or
  compatibility reader is installed.
- `submit_candidate` returns a typed first-admission result. The memory store
  protects it under its lock and the durable store uses canonical event CAS.
  Generic runtime submissions also check exclusive stage ownership against
  that exact CAS prefix. Tests force same-key races and a competing parent-turn
  admission interleaved between artifact writing and canonical publication.
- Ordinary Agent dispatch never treats redelivery as online recovery. Only a
  newly admitted request starts execution; existing terminal results are read
  without workers, candidate materialization or validation. Active/incomplete
  redelivery fails closed with `task_plan_submission_resume_required`, leaving
  the original execution untouched. Explicit `recover_submission` is a separate
  Harness-only ingress, not part of the Agent dispatch port.
- A concurrency repro originally showed redelivery appending recovery/halt
  facts while the original workers were still running. Regression tests now
  prove no recovery events, no new attempts, no group mutation, original
  success and identical subsequent terminal reuse. Simultaneous first arrivals
  produce one candidate, one accepted plan, one group and exactly two workers.
- Store append/batch/commit and offline replay share submission ownership and
  terminal-integrity validation. A first group must bind its original dedup
  correlation; terminal outcomes cannot be rewritten at a new sequence.
  Rejections occur before projection artifact writes. Exact historical event
  redelivery and accepted-plan redelivery remain idempotent.
- Pre-plan deterministic rejection is a durable, reusable outcome. It cannot
  be converted into accepted execution, and success requires causal plan
  acceptance. Readback checks the recorded result checksum and accepted-plan
  outcomes still undergo full replay/aggregate validation.

Final validation:

- Submission/redelivery/store/schema suite: `107 passed` in 177.31 seconds.
- Coordinator, spawn, checkpoint/recovery and architecture suite:
  `342 passed, 4 warnings` in 168.30 seconds.
- Independent canonical event suite: `463 passed` in 47.58 seconds.
- Required repository `python -m scripts.dev smoke`: exit 0. Compile passed;
  `3000 passed, 23 deselected, 23 warnings` in 1153.43 seconds.
- Offline AgentLoop smoke: succeeded, 3 fixture LLM calls, 1 tool call,
  0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-4c87d64561514e62be0bb4f7f33fe731/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec validation and scoped `git diff --check`: passed.
- An earlier smoke was stopped after the active-redelivery defect was reproduced
  and is not passing evidence. The restarted smoke above validates the repair.
- Existing FastAPI/Starlette and PyMuPDF deprecations remain outside this change.

This guarded rejection is not a `PENDING` receipt or automatic same-parent-turn
continuation. Feature defaults and the static Research path remain unchanged;
no G1-G5 closure or production rollout approval is claimed.

### Candidate Receipt Integrity Increment

Task 1.4 is complete within the generic AgentLoop submission boundary. This increment does not claim task 3.2/3.3 `PENDING` wait or same-parent-turn continuation, and it does not wire the dynamic Research entrypoint to a second submission ingress.

- `AgentSubmissionReceipt` is a versioned immutable projection of the accepted `CandidateSubmission`. It carries the stable run/stage/parent-turn/action identity, candidate and record checksums, admission nonce, plan identity and optional admitted group identity.
- Receipt construction and deserialization fail closed when the canonical dedup key, submission id, plan id, candidate reference/checksum, or record checksum cannot be recomputed from the receipt projection. Receipt transport includes a self checksum and round-trip/tamper regressions.
- Terminal redelivery returns the same receipt and terminal observation without worker execution or new durable events. Active redelivery returns the original receipt with `task_plan_submission_resume_required` and no synthetic group identity; checksum conflicts return the original submission receipt and do not expose old result refs.
- Rejected observations with no group/plan identity use an explicit rejected observation schema. `parent-observation/v1` still requires non-null group and plan identity, so old readers fail closed on the new rejected payload rather than silently accepting changed v1 semantics.

Focused validation after this increment: submission/redelivery and AgentLoop observation regression passed (`91 passed` before the final schema/tamper additions; the focused additions passed `4 passed`), compile and `git diff --check` passed. Full repository smoke and strict OpenSpec validation remain required before commit.

### Broader Acceptance

- Route generic children through the real controlled Agent runtime and persist
  ToolExecutor/ToolBatchExecutor receipts under their child attempt identity.
- Bind group/wave identity into task-result verification and durable child
  evidence, not only coordinator events.
- Complete bounded planning retries, failure accounting, and crash handling.
- Complete durable candidate dedup and RefAuthority, per-task spawn intent/receipt
  reconciliation, multi-pool capacity and versioned budget reservations, and
  terminal dependency blocking under the revised PRD.
- Replace synchronous-only parent dispatch with durable submission and
  idempotent same-turn continuation; complete deterministic summary spill and
  full legacy result/cancellation/recovery golden fixtures.
- Complete negative production composition checks for all required durable
  transcript, artifact, tool, profile and capability dependencies.
- Expand recovery/replay coverage to failed, cancelled, indeterminate,
  lease-expired and serial-fallback groups, with no live execution during replay.
- Capture rollout telemetry and replay evidence before enabling any default or
  presenting the dynamic Research entrypoint as production-ready.
