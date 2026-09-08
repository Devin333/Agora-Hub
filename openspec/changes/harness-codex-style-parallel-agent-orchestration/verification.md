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

### RefAuthority Increment

Task 1.5 remains incomplete. The shared Harness reference contract and optional
entrypoint checks are under implementation. Production composition has not yet
bound the same authority across generic AgentLoop, dynamic Research, child
execution, and recovered results; existing passing suites do not prove that
requirement. No Research-specific authorization implementation is introduced.

- `framework/harness/ref_authority.py` defines versioned, checksummed
  `RefDescriptor` and `RefAccessPolicy` records. Authorization binds every
  input/result/planning/memory reference to run, stage, tenant, owner, access
  mode, artifact type, source checksum, pinned allowlist, and memory namespace.
- Cross-scope or cross-owner references are rejected by default. They can be
  used only when the pinned policy names the reference as
  `SHARED_READ_ONLY`, the descriptor has the same scope, and the request is
  read-only. Candidates and child contexts cannot grant that sharing policy.
- TaskPlan candidate/task admission, planning observation source validation,
  result verification, plan/stage request construction, and sub-agent context
  construction all delegate to the same `RefAuthority` when configured.
  Missing policies, resolvers, descriptors, type mismatches, stale checksums,
  and sibling-private references fail closed.
- Independent authority and focused regressions cover descriptor/policy
  tamper checks, pinned writes, cross-scope read-only sharing, memory namespace
  enforcement and resolver failures. Existing TaskPlan/planning/sub-agent
  regression suites passed, but most use no configured authority and are not
  proof of the new authorization boundary.
- Policy checksum mappings and descriptor snapshots are immutable. Ambiguous
  namespace aliases and partial authority configuration are rejected; an empty
  artifact-type allowlist grants no access. Consumer run/stage scope is checked
  independently from the descriptor-to-policy comparison.
- Configured child policies must identify the child run as their owner. Both
  memory URIs and namespace lookup pass through the authority, then intersect
  with the child's own namespace allowlist. Shared references remain read-only.
- StageRunner forwards the authority configuration to builder and validator.
  A new regression adds a sibling-private context reference that the ordinary
  TaskPlan string allowlist permits, and proves `REF_UNAUTHORIZED` with no
  accepted plan. This does not claim the configuration is produced by the
  generic or Research composition roots yet.
- Planning source authorization occurs before receipt lookup. The actual
  receipt checksum is then compared with authorized evidence. Result output,
  transcript and artifact authorization precedes transcript reads and gates
  for success, worker failure and gate failure. Denial tests assert zero
  subsequent transcript/gate calls and no extra planning receipt reads.

Validation after this increment:

- TaskPlan/sub-agent/authority and result-adapter regressions: `678 passed`
  in 223.94 seconds. Final focused authority and boundary checks: `64 passed`
  in 2.09 seconds; the final planning read-denial assertions: `9 passed`
  in 1.51 seconds.
- Required repository `python -m scripts.dev smoke`: exit 0. Compile passed;
  `3039 passed, 23 deselected, 23 warnings` in 1193.16 seconds. This run started
  after the final code and test edits and supersedes earlier smoke results for
  this increment.
- Offline AgentLoop smoke: succeeded, 3 fixture LLM calls, 1 tool call,
  0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-b29a80a6547244a8b733d2168f0a546c/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec validation and scoped/staged `git diff --check`: passed.
- Existing FastAPI/Starlette and PyMuPDF deprecations remain outside this change.

Remaining task 1.5 closure evidence includes production-owned descriptor
provenance, pinned input and newly-produced output grants, mandatory binding
through child execution and replacement plans, and authorization in recovered
result readback. Optional framework parameters are not a production security
boundary, and this checklist item must stay unchecked until those paths are
implemented and verified. No candidate-derived automatic grant is permitted.

The read-only production binding review identifies these next steps within
task 1.5, before proceeding to task 1.6:

- Create the admission snapshot from trusted Graph execution inputs. Research
  already computes checksums from the actual `document` and `evidence_pack`
  values in `ResearchAnalysisTaskPlanStageWorker.run()`. Generic AgentLoop's
  `_context_refs_for_candidate()` contains candidate-selected names only and
  must not be used as descriptor provenance.
- Forward a child-owned policy through
  `ResolvedSubAgentTaskAdapter.build_invocation()`. Its current context-builder
  call does not forward authority configuration. Parent inputs require explicit
  read-only sharing in the pinned child policy; a candidate cannot create that
  permission.
- After durable output persistence, issue a separate immutable result-grant
  snapshot from the trusted attempt receipt and verified artifact metadata.
  Bind it to the admission snapshot and exact plan/task/attempt identity. Do not
  mutate the admission policy or preauthorize unknown result checksums.
- Resolve the same persisted grant during verification and recovery, before
  payload reads. Artifact type/checksum and transcript receipt metadata need a
  trusted metadata-only resolution path. Allowed memory namespace names alone
  are not evidence of a materialized, versioned memory descriptor.

Capacity, ledger, parent continuation, and full retry/recovery remain separately
unchecked as well. Static Research and feature defaults are unchanged.

### Task 1.5 Durable Input Admission Increment

Task 1.5 is still unchecked. This increment implements trusted input admission
and child input grants; it does not claim complete result/planning/memory
authorization or generic AgentLoop production binding.

- `RefAuthoritySnapshot` freezes the full Graph execution identity, stage
  binding, TaskPlan policy checksum, actual input source checksum, exact
  descriptors and access policy. Child grants bind the accepted attempt and
  inherit an explicitly shared read-only subset of the original descriptors.
- `DurableRefAuthoritySnapshotStore` uses immutable artifacts and the existing
  canonical run event stream. Only `harness_ref_authority_committed` exposes a
  grant; a file without its event grants no access. Bounded CAS retries reuse
  equal admissions and reject conflicting source/policy/descriptor contents.
  Reopening the store preserves the original grant and its prior parent chain.
- Recovery checks event and artifact checksums, sizes, canonical serialization,
  business context, event identity, subject, correlation and exact producer.
  Missing/corrupt committed artifacts are not repaired from current policy.
  Independent review reproduced acceptance of a self-consistent but substituted
  event envelope; the production fix now rejects all four envelope mutations.
- `HarnessRefAdmissionService` derives descriptors from the actual Graph input
  document after verifying its activity input checksum and pinned binding.
  Research production composition supplies the same durable service to its
  stage worker and `ResolvedSubAgentTaskAdapter`. Candidate-selected names are
  not descriptor provenance, and missing/non-durable admission dependencies
  reject production stage construction.
- The real Research Graph fixture executes its three analysis children using
  persisted `document` and `evidence_pack` grants. A reopened store reuses the
  original snapshot; changed policy, extra private refs or tampered input
  values cannot add events or trigger another planner/child call.
- `ArtifactReferenceDescriptorPort` returns only integrity-protected manifest
  or catalog metadata. Payload readers are replaced with fail-fast sentinels in
  the metadata-only tests. Unknown tenant/owner fields remain unknown; staged
  artifacts cannot satisfy a requested tenant without trustworthy evidence.
  Actual artifact verification still checks bytes, size, checksum and JSON.

Focused validation for this increment:

- Input/snapshot/artifact/catalog tests: `83 passed, 3 skipped` in 44.64 seconds.
  The first direct pytest run exposed a subprocess import-environment failure;
  rerunning the complete group with the repository in `PYTHONPATH` passed.
- TaskPlan/sub-agent/Research/composition/architecture regressions:
  `925 passed, 4 warnings` in 346.90 seconds.
- Final production-dependency and input/snapshot regressions: `30 passed`
  in 54.25 seconds. The additional store envelope and full-parent-chain tests
  then passed independently: `14 passed`.
- Event contracts, runtime and SQLite regressions: `504 passed` in 58.08 seconds.
- Required final repository `.venv/Scripts/python.exe -m scripts.dev smoke`:
  exit 0. Compile passed; `3060 passed, 23 deselected, 23 warnings` in
  1240.63 seconds. This smoke started after the final code/test edits.
- Offline AgentLoop smoke: succeeded, 3 fixture LLM calls, 1 tool call,
  0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-67b8df9836944eb3b91d71e90a5c665d/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings. Strict OpenSpec and
  scoped/staged `git diff --check` passed. Existing FastAPI/Starlette/PyMuPDF
  deprecations are unchanged. Focused artifact tests retain three
  platform-dependent symlink skips; no assertions were weakened.

Next, still within task 1.5: issue result grants from the trusted bundle writer,
and authorize before `SubAgentRuntime.invoke()` / `recover()` read any existing
bundle. `find_by_identity()` currently reads the bundle to obtain a receipt, so
post-return adapter authorization alone cannot close that boundary. A separate
result-grant snapshot model and durable parent-chain tests are not evidence that
this production result path is wired. Generic input provenance, planning,
versioned memory descriptors, replacement/dependency grants and recovered
result consumers remain pending. `ResearchTaskPlanResultMaterializer` and the
TaskPlan replay reader also consume committed bundles and require the same
pre-read authorization. Common materialized artifact ownership can be checked
against `subagent_result_attempt_id(identity)`, which derives its value from the
full accepted child identity checksum, rather than inferred from a worker ref.
No task 1.6 work or feature-default change is included in this increment.
The passing checks qualify this increment, not the still-open G1-G5 release
contracts.

### Task 1.5 Execution-Bound Result Authorization Increment

Task 1.5 remains unchecked. This increment follows the input-admission work and
does not advance task 1.6 or qualify the entire G1-G5 release contract.

- Trusted SubAgent attempt descriptors bind the receipt, full attempt identity,
  artifact refs, bundle bytes checksum and size. Filesystem writes serialize
  equal/conflicting attempts, commit immutable `.meta` before the existing v3
  bundle, and reuse the original receipt on equal redelivery. Descriptor reads
  open only metadata; ordinary reads still verify actual bundle bytes. Either
  missing half is an explicit incomplete-attempt error, not a new worker run.
- `HarnessResultRefAuthority` issues `RESULT_ACCEPTANCE` from original admitted
  child grants and trusted writer metadata. Online recovery can finish an
  interrupted grant publication before payload access; read-only consumers and
  replay cannot mint grants. Per-attempt read views are immutable and verify
  exact refs, types, source checksums and full accepted ownership.
- Runtime recovery, deterministic TaskPlan verification, SubAgent materializer
  reads and Research stage replay share this authority. Production Research
  construction rejects missing or non-durable result authority and requires
  the same snapshot store as input admission.
- Materialization appends `MATERIALIZED_RESULT` without changing original
  grants. Catalog ownership must match the real run, tenant, graph, node and
  full-identity-derived SubAgent attempt id. Replay validates the exact original
  and materialized ref union using a caller-supplied recorded Graph activity,
  with no event append or live worker call.
- New checks cover unauthorized activity/policy/receipt/tenant/sibling refs,
  payload-type mismatch and mutated metadata before any payload reader runs;
  bundle-before-grant crash recovery; rejected worker-selected sibling artifacts;
  reopened four-level grant chains; repeated materialization; and actual
  Research result-wrapper replay with materialized refs.

Validation: the initial final-focused group passed `63 passed` in
107.16 seconds; the broad TaskPlan/SubAgent/Research/composition/architecture
suite passed `841 passed, 4 warnings` in 360.70 seconds; event/SQLite regressions
passed `494 passed` in 52.19 seconds. After the independent-review fixes below,
the result-authority/metadata regression group passed `31 passed` in 46.21
seconds. Strict OpenSpec passed.
An early deep-path filesystem failure was fixed by storing the sidecar beside
the original bundle as `.meta`, avoiding an extra directory and long atomic
temporary path on Windows. A new test's incorrect configuration import was
corrected to the existing Harness result-policy module; no assertions were
weakened.

Independent review reproduced a self-consistent substituted result grant with
the correct metadata source checksum but a sibling descriptor. Existing grants
now must exactly equal the full snapshot reconstructed from trusted metadata,
not merely carry the same source checksum. A regression proves rejection
before any payload reader runs. Failed-result replay now explicitly authorizes
original artifact refs from metadata before reading the transcript; its test
asserts that ordering. A separate size-limit repro found descriptor overflow
was detected only after committing files; descriptor size is now checked before
either immutable file is created. The attempt-bound store also preserves the
canonical read keyword arguments and bounded parent-query signature. Three
earlier smoke runs were stopped for these production fixes and interface
alignment and are not counted as passing qualification.

Final required `.venv/Scripts/python.exe -m scripts.dev smoke` completed with
exit 0 after the final code/test edits:

- Compile passed; `3078 passed, 23 deselected, 24 warnings` in 1366.28 seconds.
- Offline AgentLoop succeeded with 3 fixture LLM calls, 1 tool call and 0
  network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-2b692cb6fd16411e8a70e6321cb742e3/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- The smoke includes existing FastAPI/Starlette deprecations and an Authlib
  deprecation from concurrent account-auth work. That account-auth/frontend
  implementation is outside this increment's commit scope.
- Strict OpenSpec and scoped/staged whitespace checks passed. The task checklist
  remains 14/46 with 1.5 as the earliest unchecked item.

Remaining task 1.5 work includes generic AgentLoop provenance and result
consumers, planning observations, versioned memory descriptors and namespaces,
replacement/dependency grants, and other result/recovery consumers. This
increment is not blanket authorization for those paths and does not enable any
new feature default.

## Task 1.5 Planning Reference Authorization Increment

The checklist remains 14/46. This increment implements planning receipt
authorization under an already admitted Graph input snapshot; it does not close
task 1.5 or advance capacity/budget tasks.

- `PLANNING_OBSERVATION` grants parent directly to `INPUT_ADMISSION`, retain the
  exact Graph execution/stage/policy/owner/tenant, and pin request checksum,
  receipt checksum and permitted artifact descriptors. They are read-only and
  carry no invented SubAgent attempt. Harness derives planner turn identity from
  the input grant and a policy-bounded turn ordinal.
- Trusted filesystem receipt metadata excludes arguments and summary text and
  pins input grant, request/turn/policy, receipt, artifacts and payload bytes
  checksum/size. Each input snapshot has an isolated directory. Metadata is
  created before payload under a verified cross-process lock; equal receipts
  are reusable, conflicts and incomplete/corrupt pairs fail closed. Metadata
  queries read sidecars and stat payloads; payload APIs verify actual bytes and
  canonical receipt identity after authorization.
- Online observation can finish interrupted grant publication from complete
  trusted metadata before reading the receipt, without another tool call.
  Offline receipt replay and candidate source validation cannot commit grants.
  A committed grant whose entire receipt pair disappeared prevents tool
  reexecution. Artifact metadata must match owner/tenant/type and the original
  pinned checksum before receipt payload access.
- Generic production planning composition requires a durable input grant,
  snapshot store and bounded planner turn. It resolves TaskPlan tool names to
  exact registered tool versions; missing bindings fail construction. This also
  fixes the previous incompatible direct conversion between name-based
  TaskPlan allowlists and exact-reference planning policy. Generic orchestration
  and enabled Research planning reject unbound planning ports. Stage observation,
  source validation and accepted-plan recovery compare full execution identity.
  Research planning remains disabled by its existing default policy.
- The pure standalone TaskPlan reducer still requires separate source-candidate
  evidence plumbing; generic parent delegation input admission, memory,
  replacement/dependency grants and other result consumers remain task 1.5 work.
  Bounded planning retry/concurrent budget accounting remains task 1.10.

The final focused planning authority/storage/composition/legacy observation
group passed `47 passed` in 44.38 seconds. Accepted-stage recovery additionally
proves both existing-plan reuse and stage replay validate the original planning
receipt, fail before payload reads when a grant is missing, and invoke no worker
or additional tool. Its fully authorized input fixture exposed a validator wiring
gap: candidate validation now composes a read-only view of the original input
descriptors plus already committed planning receipt descriptors, without changing
either grant or granting access to receipt artifacts as candidate inputs.

The broader TaskPlan/AgentLoop/Research/composition/architecture group passed
`993 passed, 4 warnings` in 781.32 seconds before that final validator fix; the
required full smoke was stopped to apply the fix and restarted afterward.
The earlier stopped smoke is not counted as qualification.

Independent metadata/store review confirmed
the authorize-before-payload order and scope/turn checks; filesystem tests cover
real two-process idempotent writers as well as threads, half commits, corruption,
size/query bounds and cross-snapshot isolation. Strict OpenSpec and whitespace
checks passed. The event schema and existing snapshot contract/store regression
group passed `23 passed` in 78.70 seconds.

The final required `.venv/Scripts/python.exe -m scripts.dev smoke` completed
successfully after the validator fix:

- Compile passed; `3089 passed, 23 deselected, 23 warnings` in 1935.68 seconds.
  The warnings are existing FastAPI/Starlette deprecations.
- Offline AgentLoop succeeded with 3 fixture LLM calls, 1 tool call and 0
  network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-316a105e12694cbe8782cc4a2faffd10/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec and scoped/staged whitespace checks passed. The checklist
  remains 14/46 with task 1.5 as the earliest unchecked item.

## Task 1.5 Memory Namespace Revision and Admission Increment

The checklist remains 14/46, with task 1.5 still the earliest unfinished item.
This increment supplies real immutable namespace persistence, publication policy,
Graph/child admission and authorized revision reads; it does not close every
existing memory recall or tool consumer.

- `MemoryNamespaceRevision` freezes canonical bytes of the complete scope and
  sorted `MemoryRecord` documents. Its full content checksum identifies the exact
  namespace revision. Record `version` remains data, not trusted version proof.
- The trusted publisher receives namespace/tenant/owner/sharing from composition,
  rejects mismatching record scope, applies the existing `MemoryPolicy` to the
  whole batch before commit, and neither promotes nor updates active memory.
- Filesystem storage bounds metadata, payload and records; serializes immutable
  metadata-then-payload commit; verifies canonical bytes and identity; and rejects
  links, half commits, changed files, conflicts and corruption. Description never
  opens the record payload or searches a live vector store.
- Input admission pins exact revision descriptors alongside actual Graph inputs.
  Existing executions restore original descriptors without a catalog lookup;
  changed revision configuration cannot replace the original binding. Namespace
  names alone grant no access.
- Actual TaskPlan child construction carries the exact requested, shareable
  read-only memory refs in its grant and envelope. A parent-private namespace
  fails before worker invocation. Revision reads first verify canonical grant,
  full caller execution/attempt and trusted metadata, then actual payload bytes.
- Research production composition now supplies the durable namespace store.
  Default bindings remain empty and normal Research outlines request no memory.
  The generic composition helper accepts explicit trusted exact refs; candidate
  hints cannot populate them.

Validation:

- Namespace contracts, reference reader, real Graph/child integration, existing
  input admission and production composition: `39 passed` in 110.13 seconds.
- Existing planning/snapshot/TaskPlan lineage, framework memory and architecture:
  `325 passed, 4 warnings` in 306.24 seconds. Warnings are existing FastAPI
  deprecations.
- Filesystem namespace persistence: `17 passed` in 5.09 seconds, including real
  concurrent processes, hardlink rejection and the Windows directory-junction
  boundary. A Windows junction substitutes for unavailable symlink creation,
  exercising an actual reparse point without skipping the test.
- Required `.venv/Scripts/python.exe -m scripts.dev smoke` exited 0: compile
  passed; `3105 passed, 23 deselected, 23 warnings` in 1331.86 seconds. Warnings
  are existing FastAPI/Starlette deprecations.
- Offline AgentLoop succeeded with 3 fixture LLM calls, 1 tool call and 0
  network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-4b92e3c44d4c43139fe6ab954daba563/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec and scoped/staged whitespace checks passed. The checklist
  remains 14/46 with task 1.5 as the earliest unchecked item.

Existing RAG/AgentLoop recall, memory tools and their live vector lookup paths
still require authorized consumer integration. The immutable publisher is a
trusted composition port, not an automatic conversion or promotion of existing
vector records. Generic parent input/result provenance, replacement/dependency
grants and remaining replay consumers also remain in task 1.5. No default feature
is enabled by this increment.

## Task 1.5 Generic Parent Graph Input Admission Increment

The checklist remains 14/46 with task 1.5 still unchecked. This increment binds
generic delegation to the real parent Graph activity and persists its input
authority before parent reasoning. It does not complete the generic child/result
adapter composition or existing memory recall/tool consumers.

- A declared AgentLoop Graph activity can carry both its leaf binding and a
  TaskPlan delegation binding. Definition validation requires identical exact
  worker/activity refs and forbids side-effect ownership. The compiler alone
  adds delegation authority metadata; ordinary AgentLoop step metadata cannot
  forge it. Existing standalone TaskPlan bindings retain their checks.
- TaskPlan stage binding and preflight recognize that explicit declaration.
  Generic dispatch retains every field of the actual parent execution identity;
  candidate correlation no longer creates synthetic physical activity ids.
- The physical Graph worker invokes Harness input admission before AgentRunner.
  Admission verifies the complete original Graph task checksum, actual context,
  stage binding and AgentSpec policy. Only explicitly allowed business-input
  names enter the read-only grant; private parent inputs remain outside it.
- Dispatch authorizes external candidate input refs before candidate/submission
  writes. Planning and redelivery load the original grant by full execution
  identity. Missing grants are rejected rather than reconstructed from hints.
- Production runtime and Graph composition require durable input admission.
  A zero planning-tool quota permits no planning port; an enabled planning port
  retains its execution-bound authority and shares the canonical snapshot store.

Validation:

- Graph definition/compiler/preflight, TaskPlan stage binding, existing
  submission/recovery and composition regression: `243 passed` in 134.51 seconds.
- Updated composition negative checks: `13 passed` in 4.29 seconds.
- Candidate checksum conflict keeps `CANDIDATE_IDEMPOTENCY_CONFLICT`, including
  when the conflicting payload names unavailable inputs: `2 passed, 48
  deselected` in 7.97 seconds, covering both memory and durable TaskPlan stores.
  Input authorization still precedes every first submission write.
- New physical Graph coverage uses the real control plane, dispatcher, AgentRunner,
  SQLite event/grant/task stores and filesystem artifacts, with fixture LLM and
  child workers. It checks admission before the first LLM call, original
  TaskPlan/DispatchGroup/worker identity, private-input exclusion, restart
  redelivery without workers or events, and rejection of changed identities or
  input bytes. An allowlisted input absent from the original Graph task halts
  the parent with `REF_UNRESOLVED` after one LLM call, with no submission and no
  child calls. These cases are included in the required full smoke.
- The initial smoke process was deliberately stopped after the dedup diagnostic
  ordering fix because it had loaded the previous code. It is not passing
  evidence. Required `.venv/Scripts/python.exe -m scripts.dev smoke` on the final
  code exited 0: compile passed; `3116 passed, 23 deselected, 23 warnings` in
  1502.83 seconds. Warnings are existing FastAPI/Starlette deprecations.
- Offline AgentLoop succeeded with 3 fixture LLM calls, 1 tool call and 0 network
  calls. Manifest:
  `.newsroom/smoke/test-agent-loop-94b7ed18da21424bb926b09e640e3e4e/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings.
- Strict OpenSpec and scoped/staged whitespace checks passed. No task checkbox
  was advanced: task 1.5 remains the earliest unfinished item, 14/46 complete.

The generic factory still accepts a worker executor and result verifier. Their
shared child/result authority dependencies and real generic child runtime wiring
remain unclosed; fixture workers/verifiers in these tests do not prove that
production composition. Existing memory recall/tool integration, dependency and
replacement grants, remaining replay consumers and G1-G5 acceptance also remain.
No default feature was enabled.

## Task 1.5 Generic Child and Result Authority Increment

The checklist remains 14/46 with task 1.5 as the earliest unfinished item.
This increment replaces the generic production factory's callback child path
with one `HarnessSubAgentTaskExecutor` for invocation and online recovery.

- The executor loads the durable accepted plan and exact task instance, checks
  the pinned capability boundary and the original group/spawn intent against
  the caller's complete physical Graph identity, then constructs the bounded
  context and invokes the existing `ResolvedSubAgentTaskAdapter`/`SubAgentRuntime`.
  A deterministic instance checksum alone cannot grant an unadmitted attempt.
- The factory and physical Graph composition require the same TaskPlan store,
  input admission service, canonical snapshot store, result authority and
  durable transcript owner across execution/recovery/verification. The result
  descriptor and artifact verifier must share their canonical artifact owner.
  Every profile must resolve to its registered SUBAGENT implementation and
  registered deterministic gates. The production factory no longer accepts
  a separate recovery callback; manually composed runtimes cannot bypass checks.
- Real Graph tests now use the SubAgent runtime, immutable transcript sidecars,
  canonical input/child/result grants, and real TaskPlanResultVerifier. Fixture
  LLM and business workers remain test data, with no production fake fallback.
- Restart redelivery and child recovery reuse exact evidence with no extra
  workers or grants. Interrupted result-grant publication is recovered through
  the original metadata and child authority; ordinary read-only access rejects
  a missing grant, and no worker is repeated.

Validation:

- Physical Graph, child/result grant lineage, restart recovery, sibling receipt
  rejection, changed physical execution and unadmitted attempt rejection:
  `18 passed` in 190.50 seconds.
- Production composition and dependency mismatch matrix: `24 passed` in 78.14
  seconds. Covers callback executors, different TaskPlan/input/result/transcript/
  artifact owners, missing gates, replaced workers and permissive gate suites.
- Additional AgentLoop application service and composition regression:
  `36 passed` in 108.19 seconds. Independent child-boundary review found no
  blocking issue in this increment; this is not a whole-change release review.
- Strict OpenSpec validation passed after the contract/design update.
- Required `.venv/Scripts/python.exe -m scripts.dev smoke` exited 0 on the final
  code: compile passed; `3125 passed, 23 deselected, 23 warnings` in 1690.57
  seconds. Warnings are existing FastAPI/Starlette deprecations.
- Offline AgentLoop succeeded with 3 fixture LLM calls, 1 tool call and 0 network
  calls. Manifest:
  `.newsroom/smoke/test-agent-loop-a9e1f9ad7624494d860c6fc22796f9a1/manifest.json`.
- Source validation: `is_valid=true`, 0 errors, 0 warnings. Strict OpenSpec and
  scoped/staged whitespace checks passed; no task checkbox was advanced.

Existing memory recall/tool consumers, dependency/replacement input grants and
remaining recovery/replay consumers still require task 1.5 work. Concrete child
AgentRunner/ToolRuntime receipt wiring, complete parent continuation and G1-G5
acceptance remain under their original later tasks. No default feature or live
traffic was enabled, and no task checkbox was advanced.

## Task 1.5 Execution-Bound Memory Recall Increment

Status: increment implemented and verified; task 1.5 remains unchecked.
This increment does not advance G1-G5 acceptance or enable a feature.

Implemented:

- Physical parent Graph input admission now supplies one per-invocation,
  Harness-issued read-only memory capability to AgentRunner. Automatic recall
  and the execution-local memory tool overlay share the same committed grant.
  Shared Runner and registry state cannot retain a previous caller's grant.
- Graph mutable-runtime/vector-only recall fails closed; standalone remains
  explicitly isolated. Empty admitted memory produces an empty result, not a
  fallback. Authorization and integrity failures propagate before the next LLM
  call. Query selectors cannot issue namespace, revision, tenant or owner access.
- Recall reads verified immutable records after all selected metadata checks,
  reuses the existing keyword retrieval and policy/context assembly, and records
  snapshot/ref/checksum lineage. Default bounds are 5 results and 1500 context
  tokens; per-call policies cannot increase them. No memory writes or promotion
  APIs are exposed through the capability.
- Framework, infrastructure and business tool catalog ingress can pass the same
  neutral recall port. Agent and Tool do not import Harness authorization code.
- `memory_enabled` disables automatic recall, explicit tools and candidate
  recording; disabled tools are omitted from Graph and standalone schemas.
  `memory_recall_enabled` controls automatic recall only. Explicit tools inherit
  the same caller MemoryPolicy, intersected with Harness limits. Unrestricted
  caller scope/kind lists do not remove the Harness ceiling.
- The offline AgentLoop smoke now publishes fixture records into the real
  immutable namespace store, commits an execution-bound grant, and uses the
  actual memory tool. Its deterministic gate verifies successful recall and
  identity/revision lineage, in addition to the existing event, metric and
  artifact checks. This smoke-specific grant is not production admission proof.
- Two tool attempt tests now use the real Supervisor with an injected clock
  rather than 10ms OS scheduling assumptions. The unconfirmed worker is released
  explicitly in cleanup; timeout/retry/termination/idempotency assertions remain,
  with terminal-event assertions added to the confirmed retry case.

Final integrated validation (2026-09-08):

- Agent, Memory, Tool, immutable recall, business catalog, AgentLoop services,
  production composition and smoke command regression: `336 passed, 2 skipped`
  in 169.71 seconds. Skips are existing unavailable Windows symlink cases in
  artifact tools. Includes disabled Graph/standalone schemas, narrower and empty
  policy allowlists, direct ToolExecutor rejection, same-policy tool recall,
  grant reopen, mutable fallback denial, and corrupt smoke memory rejection.
- `openspec validate harness-codex-style-parallel-agent-orchestration --strict`
  and `git diff --check`: passed.
- `.venv/Scripts/python.exe -m scripts.dev smoke`: exit 0. Compile passed;
  Harness/Research/API/service/recorded transport/architecture coverage returned
  `3152 passed, 23 deselected, 23 warnings` in 1570.53 seconds. Deselection follows
  the existing repository marker policy; warnings are existing FastAPI/Starlette
  deprecations. This includes physical parent automatic/tool recall, rejected
  corrupt payloads before LLM/child invocation, and Research child grants.
- Offline AgentLoop run `test-agent-loop-0dee69c014154a4388cecf55eed9133d`:
  succeeded, 3 fixture LLM calls, 1 successful real memory tool call, 1 judge
  retry, 60 fixture tokens and 0 network calls. Terminal manifest:
  `.newsroom/smoke/test-agent-loop-0dee69c014154a4388cecf55eed9133d/manifest.json`;
  manifest hash `sha256:2ef3b842e009006ca8bbbfc25a60de471bd7ab5090b01abae8bdb77f83a7a158`.
- Source validation: `is_valid=true`, `error_count=0`, `warning_count=0`.

Earlier failed runs exposed an empty-grant constructor check, a missing memory
tool allowlist in the new Graph fixture, and a legacy business test passing an
unbound vector placeholder. These were corrected with a legitimate empty grant,
an explicit fixture allowlist and a committed namespace capability respectively,
without weakening gate behavior or reducing the original tool assertions.
Additional regressions exposed default-policy expansion order, unbound direct
Graph memory tools, inconsistent memory flags and missing policy propagation.
Review also found standalone schema exposure and empty-allowlist handling.
These boundaries were repaired together with the obsolete smoke fixture, without
weakening authorization or changing production deadline semantics. The final
negative smoke test also uses a short semantic run ID to stay within Windows
path limits; its corrupt-recall and no-terminal-publication assertions remain.
Three smoke runs were stopped after further code changes were identified; none
is counted as passing evidence. The final integrated results above supersede
all earlier partial validation.

Remaining task 1.5 scope includes RAG memory consumers, dependency/replacement
input grants and remaining recovery/replay consumers. Concrete child AgentRunner
receipt wiring and complete parent continuation retain their later task owners.
Online recall retains normal record expiry rules; offline observation replay
must consume recorded evidence rather than rerun recall against the current
clock. Namespace reopen tests are not full offline AgentLoop replay evidence.

### Task 1.5 RAG memory consumer boundary (2026-09-08)

`ResearchRAGMemoryPort` now consumes the execution-bound immutable recall port.
Physical controllers validate their full Graph activity before planning or
retrieval and again before memory recall. Namespace, tenant and owner selectors
narrow admitted descriptors; `user_id` supplies the owner selector. Each hit
retains the exact revision/record ref, namespace checksum, input snapshot and
execution identity through context-pack trimming. Empty summaries use verified
record content. Process-scoped Paper RAG resources no longer cache a memory
adapter or query mutable vector memory; enabled memory requires a committed
capability supplied to that invocation, and collection selectors fail closed.

The focused matrix covered framework memory/RAG, reference memory readers,
Research RAG adapters, PaperRAGSession, and Paper RAG factory/service:
`656 passed in 125.81s`. Initial failures were fixture issues: the existing
physical generation fixture supplied an unbound memory fake, and a new test
attempted an already-forbidden ambiguous namespace alias. The former now uses
an explicitly identity-bound fake; the latter tests owner narrowing across
distinct admitted private/shared namespaces. Production authorization was not
relaxed. Real durable grant tests prove missing/foreign authority fails before
payload IO, newer publications do not redirect old grants, and persisted RAG
context replay performs zero live namespace/recall/planner/retrieval calls or
grant writes. RAG context replay does not prove complete TaskPlan grant replay.

Final integrated validation after all code/fixture edits:

- `openspec validate harness-codex-style-parallel-agent-orchestration --strict`
  and scoped `git diff --check`: passed.
- `.venv/Scripts/python.exe -m scripts.dev smoke`: exit 0; compile passed,
  `3171 passed, 23 deselected, 23 warnings in 1647.16s` (27m27s). Deselection
  follows the existing marker policy; warnings are FastAPI/Starlette deprecations.
- Offline AgentLoop `test-agent-loop-c108e8619278449d8f9ff88fa617f042`: succeeded,
  3 fixture LLM calls, 1 successful real memory tool call, 1 judge retry,
  60 fixture tokens, 0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-c108e8619278449d8f9ff88fa617f042/manifest.json`,
  hash `sha256:d2c3813d82b19a6e9b39948db74b3d40f7a861d8d100b0ea39c0a18e6ffc0609`.
- Source validation: `is_valid=true`, `error_count=0`, `warning_count=0`.

The first focused run exposed the two fixture failures described above; both
were repaired together, then the complete focused matrix passed. The full smoke
was run once after the final batch edits. Concurrent frontend edits are outside
this commit and outside this validation claim.

This is consumer integration and trusted in-process dependency injection.
`ExecutionMemoryRecallPort` is an interface, not a sandbox for arbitrary Python
objects; the real Harness implementation verifies committed grants. Multiple
bounded queries within one admitted execution are permitted. No HTTP/LLM
request can serialize this capability. Production Research Graph/HTTP/CLI
callers do not yet automatically supply it, so production memory remains off
by default and task 1.5 is still unchecked.

Remaining verified 1.5 gaps after the ingress batch below:

- Bind `task://producer/output` to accepted predecessor result/grant identity
  before downstream dispatch. Current plan validation accepts the DAG reference,
  but child admission only inherits root input descriptors and rejects it.
  Replacement rewrites require the same exact-result binding; do not weaken the
  single-parent CHILD_INPUT grant invariant to add an unproven descriptor.
- Supply the RAG memory capability from the real Harness admission path before
  claiming production consumption. Child AgentRunner/tool receipts remain
  tasks 2.6/2.7.

Manual physical SubAgent composition and generic replay/recovery ingress are
covered by the following batch; its integrated validation is recorded there.

### Task 1.5 physical SubAgent and replay ingress (2026-09-08)

The physical SubAgent activity constructor, execute/recover entrypoints and
adapter Graph-binding entrypoint now require durable result authority. Runtime
and acceptance share the same authority/transcript owner; materialization and
authority share the exact artifact catalog. Dependency changes after construction
are checked before worker or evidence payload access. Existing bundle,
materialization, scope and restart tests retain their assertions and now use
real durable input/result grants and transcript storage. Negative tests cover
missing/non-durable/mixed authority and metadata owners with zero worker/payload,
artifact-write and Graph-commit access.

Generic SubAgent replay now has no raw-store fallback: its accepted Graph v2
plan/task and caller-supplied execution select the committed attempt grant before
payload access. Recovery receives explicit dependencies and constructs its own
reducer. Pure metadata replay/recovery can carry a Graph identity without an
unused payload capability. Replays reject missing authority/execution, changed
durability/transcript owner, missing grants and conflicting grants before payload
reads; authorized recovery must preserve the replay checksum and grant event
count. Research integration fixtures now supply the same durable input/result
authority, and crash recovery reuses the exact original Graph inputs.

First integrated smoke: `3193 passed, 1 failed, 23 deselected, 23 warnings in
1831.20s`. The failure was an incorrect new test expectation: the explicit
test-store fixture intentionally isolates input admission and can execute online
without result authority. Its original input-grant assertions were restored;
the same ungranted history now additionally proves offline replay rejects before
any result payload read. No production check was relaxed. Compile, offline
AgentLoop smoke and source validation passed in that run; the command returned
1 because of the failed assertion.

The second integrated run returned `3193 passed, 1 failed, 23 deselected,
23 warnings in 1844.75s`. The new negative replay assertions passed, but two
original positive result-grant assertions had been placed under the wrong
parameter branch and raised `UnboundLocalError`. Both assertions were restored
to the authorized branch without changing their predicates. Compile, offline
AgentLoop smoke and source validation also passed in that run. No production
code changed between these runs.

Final integrated validation after the complete edit batch:

- `openspec validate harness-codex-style-parallel-agent-orchestration --strict`:
  passed. All 11 changed source/test files retained their validation-start
  SHA-256 values through smoke completion.
- `.venv/Scripts/python.exe -m scripts.dev smoke --keep-going`: exit 0. Compile
  passed; the fixed smoke test selection returned `3194 passed, 23 deselected,
  23 warnings in 1850.29s` (30m50s). Deselection follows the existing repository
  marker policy; warnings are existing FastAPI/Starlette deprecations. The only
  additional pytest option was `--durations=10`, which reports timing without
  changing collection or assertions.
- The slowest cases were fifty same-paper actor-isolated runs (218.31s), complete
  SubAgent result materialization and lineage projection (145.03s), and the full
  recorded production Research transport (132.88s). These are integrated test
  timings, not production latency measurements.
- Offline AgentLoop `test-agent-loop-3a96769e3bd34682a98f31de5dd26a49`: succeeded,
  3 fixture LLM calls, 1 successful real memory tool call, 1 judge retry,
  60 fixture tokens and 0 network calls. Manifest:
  `.newsroom/smoke/test-agent-loop-3a96769e3bd34682a98f31de5dd26a49/manifest.json`,
  hash `sha256:832e31d2c94da0ada90bb4296507f2f22b1c23983f350481b468b8f0d5b91202`.
- Source validation: `is_valid=true`, `error_count=0`, `warning_count=0`.

Validation used full smoke runs after completed edit batches; no separate
focused pytest runs were added. Task 1.5 remains unchecked;
dependency/replacement input binding and production Harness-to-RAG capability
issuance remain open.

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
