# Task 2.3 implementation contract

Direction approved by the user on 2026-09-10. Task remains unchecked. This is an
implementation plan, not evidence that the following behavior already exists.

## State and version decisions

- PENDING: dependencies or retry eligibility are not yet satisfied; no active
  attempt. READY: logically eligible, no active attempt, no new reservation.
- ADMITTED: an exact attempt and all required reservations have committed.
  Admission provenance must identify QUEUE or GROUP_WAVE durably. DISPATCHED
  and RUNNING retain that same identity and owner.
- READY keeps cumulative completed-attempt counts but never increments them;
  the next successful admission alone allocates attempt n + 1.
- TaskProjection and TaskPlanProjection move from v3 to v4. TaskInstance stays
  v3 with unchanged deterministic identity. Replay semantics move to v4.
- Checkpoint, event and wave wire contracts must explicitly version any changed
  required fields. Exact existing event/wave versions must be checked before
  editing; do not infer them from prose. New readiness/wait evidence is v1.
- Old allocated-READY records are not silently read as new unallocated READY.
  Live readers fail closed on unsupported versions. Historical bytes remain
  unchanged; no implicit migration or compatibility layer is introduced.

## Logical readiness and packing

1. Compute the complete eligible set from accepted plan and committed task
   projection. Do not truncate it by physical capacity or tentatively charge
   candidate budget before resource eligibility is known.
2. Apply the single pinned priority/dependency-depth/task-id/checksum order.
   Coordinator must not replace it with its own task-id-only ordering.
3. Persist logical READY and its full order without creating TaskInstance,
   queue envelope, spawn key, lease or execution reservation.
4. First-fit considers all pool/resource/slot and budget dimensions for each
   candidate. Changes are provisional until every dimension fits. Failure in
   one dimension leaves all dimensions unchanged and permits checking later
   candidates. Budget exhaustion remains distinct from temporary capacity wait.
5. Publish the selected admission as one canonical CAS/batch: selected
   READY-to-ADMITTED transitions, exact attempts and owner provenance, budget
   and capacity reservations, wave admission and all spawn intents.
6. No child starts before that batch commits. Failure preserves prior logical
   READY but no authoritative partial attempt, wave, budget or pool allocation.

Packing evidence binds full ready order, selected and deferred orders, per-task
reasons, capacity owner/policy/snapshot revision/checksum/expiry, reservation
keys and quantities, and actual capacity/budget ledger before/after checksums.
It enters the immutable wave checksum and is verified against canonical
admission, not trusted solely because the submitted envelope is self-consistent.

The current pool algorithm has only local snapshot arithmetic. Implementation
must establish one authoritative admission transaction for the declared pool
scope, not claim shared-pool safety from a per-plan CAS alone. It must not claim
distributed exactly-once or atomicity between independent stores.

## Waiting and recovery

- A zero-selection pass records canonical TASK_GROUP_CAPACITY_WAITING with
  complete ordered READY and reasons, capacity revision and fixed deadline.
  It does not create an empty wave or enter JOINING.
- Rechecking the same committed inputs/revision reuses the same fact. Each
  scheduling/recovery invocation makes bounded progress or returns an internal
  resumable wait, rather than burning max_rounds on unchanged capacity.
- A durable admitted-at/absolute deadline is required for restart-safe waiting;
  it must not reset to another 900 seconds after reopening. If group snapshot
  fields change, its version changes while its stable identity algorithm stays.
- READY is never reconstructed as a queue task or child. QUEUE-owned ADMITTED
  can restore its original missing queue publication idempotently. GROUP_WAVE
  admission is restored only by its coordinator.
- Static queue admission uses canonical `TASK_QUEUE_ADMITTED`, carrying the
  exact allocated instance, QUEUE owner and budget admission evidence in the
  same projection/event commit. The persistence slice owns its event type,
  schema validator, history indexing and replay support; the admission slice
  emits it before ordinary queue dispatch/publication. This is necessary to
  preserve the static queue path under v4, not permission to disable it or
  limit task 2.3 to GROUP_WAVE.
- Checkpoint/replay separate logical ready order from active attempt order and
  preserve wait reasons, provenance and reservations. Offline replay performs
  no current-capacity read, wall-clock redecision, queue or child call.
- The parent receives no fabricated terminal observation for an internal wait.
  Adapt only the necessary active-submission/recover_submission boundary; full
  automatic parent continuation remains a separate G3 task.

## Worker ownership

### Agreed state interfaces for integration

- `TaskAdmissionOwner`: QUEUE or GROUP_WAVE. `TaskProjection.admission_owner`
  is required in v4 wire data and is null for unallocated READY/PENDING and
  terminal states. ADMITTED/DISPATCHED/RUNNING retain one exact owner and active
  instance. Only successful ADMITTED allocation increases the attempt counter.
- `TaskPlanProjection.logical_ready_order` is a required v4 wire array. It has
  no duplicates and exactly covers tasks currently in READY, preserving pinned
  scheduler order in its checksum. The task-definition collection itself stays
  canonically sorted by task id.
- ACTIVE_TASK_STATES contains only ADMITTED, DISPATCHED and RUNNING. Readiness
  and active-attempt checkpoint/recovery collections are separate.
- Readers/writers use task/plan projection v4; TaskInstance remains v3. Missing
  v4 fields or old projection schemas fail parsing, rather than defaulting old
  allocated READY into the new semantics.

A. State/wire contracts: models, schema registry, lifecycle validators and
their contract tests. Publish exact fields and method contracts before B/C.

B. Admission execution: scheduler, stage, coordinator, joint packing and runtime
tests. Own full ordering, durable readiness, selected admission and internal wait.
The B worker also owns necessary TaskPlan wrapper signature/pass-through changes
in `framework/harness/control_plane/scheduler.py`: explicit admission owner and
accepted-plan input for reclaim ordering must reach the TaskPlan scheduler.
Do not change ordinary Graph scheduling semantics. Coordinate recovery call-site
changes with C, which owns `recovery.py`.

C. Persistence/recovery: canonical store validation, checkpoint, replay,
recovery and their tests. Own joint reservation authority and queue/wave routing.

The lead coordinates cross-file interfaces and OpenSpec changes. Workers do not
edit each other's files or independently commit incomplete slices. Explorer
provides additional code evidence where needed; planner handles design changes.

## Required acceptance

### Capacity transaction integration contract

The persistence slice exposes `load_capacity_snapshot(owner_scope)` and one
`commit_wave_admission` entry point. The latter checks the expected projection
checksum and the expected shared capacity scope revision/before checksum in the
same transaction that publishes task admission, budget allocations, the wave,
spawn intents and the capacity after snapshot. A proposal is not a reservation.
The durable implementation must establish this boundary in the underlying
transaction, not through sequential calls or only an in-process coordinator lock.

The necessary backend transaction extension is explicitly in scope for this
task. The persistence owner may modify `framework/events/ports.py`, event runtime
transaction plumbing, `infrastructure/storage/events/sqlite.py` and `postgres.py`,
their schema DDL/migrations and scoped backend tests. Keep event-layer operations
backend-neutral: Harness owns capacity policy, packing and scope validation;
storage owns transactionally persisted revision/checksum compare-and-swap and
atomic event publication. Do not import Harness capacity models into the event
framework or expose backend connections to Harness as a shortcut. A small typed
transactional state primitive is appropriate if required by both backends.
Existing event append/outbox behavior and rollback semantics must be preserved.
No deployment migration or live production database mutation is authorized here.

An in-memory implementation plus a durable missing-capability rejection is only
an intermediate state. Task 2.3 cannot be accepted until the real durable
transaction path and rollback/concurrent-admission evidence are complete. The
persistence owner may delegate this bounded backend slice to a worker and must
publish its file ownership and exact interface before concurrent edits.

The persistence and backend workers agreed the generic interface:
`TransactionalStateSnapshot(namespace, key, revision, checksum, payload)`,
reader `load_transactional_state()`, UoW `cas_transactional_state()` and runtime
`publish_batch_with_state_cas()`. The runtime performs state CAS plus event
append and exactly one commit in the same UoW. SQLite uses `BEGIN IMMEDIATE`;
Postgres uses row locking plus an explicit safe initial-insert race path (locking
an absent row alone is not sufficient). Snapshot types remain generic and
policy validation remains in Harness. These interfaces are under implementation.

Exact redelivery validates the original committed capacity transition and
historical projection; it must not compare a previously accepted proposal with
the latest scope snapshot and falsely reject or reserve again. Conflicting new
admissions cannot overwrite either historical evidence or current reservations.
The read snapshot must have explicit owner scope, ordered pools, revision,
checksum and expiry. The same scope must be shared by competing plans.

`TASK_GROUP_CAPACITY_WAITING` uses versioned v1 evidence and a stable key derived
from group, logical-ready snapshot, capacity snapshot, budget snapshot and the
original absolute deadline. Identical inputs reuse the recorded fact. Missing
or stale required policy remains a rejection, not a capacity waiting outcome.

These are implementation contracts, not statements of completed acceptance.

### Integration review findings to close before testing

The read-only integration inventory also found required production consumers:

- `dependency.py` must stop treating logical READY as a budget-bearing active
  attempt, including the old `attempts == 1` readiness classification. Closing
  unallocated READY must not release a nonexistent reservation. Preserve the
  authorized settlement gate for actual admitted attempts.
- `attempt_history_index.py` must establish attempt identity from canonical
  admission evidence, not from unallocated TASK_READY. Preserve full history
  validation and fail-closed handling of missing or contradictory evidence.
- Patch-driven projection construction in store/replay must keep
  `logical_ready_order` consistent with the exact READY set and pinned order.
  `patches.py` itself returns a new `ValidatedTaskPlan`, not a projection;
  do not add projection writes there or expand into later replan eligibility
  work merely because the validator accepts READY as an unstarted state.
- Package exports must match the new reducer/checkpoint V4 constants. Old
  constants may identify historical formats but cannot enable old live readers.

Affected fixture groups include runtime/contract-matrix/budget/dependency and
Research graph tests; recovery/durable-store/subagent-lineage/spawn-admission
tests; and atomic-event/atomic-transition/parallel-admission/history fixtures.
Fixtures must exercise readiness followed by explicit admission, not emulate
old semantics through permissive fallbacks. Retain existing assertions' intent.

- The v4 `logical_ready_order` parser must explicitly require an ordered array
  (an allowed sequence, excluding strings and bytes). A string or mapping must
  not be accepted through generic iteration, even if its characters or keys
  happen to equal the complete READY task set. Null, numeric and unordered
  values must raise a typed contract error, not leak an incidental TypeError.
  Cover direct construction and wire parsing without weakening checksum tests.
- Event schema changes must include `stage_binding.py`'s expected schema and
  package exports, not only the event store reader/writer. Projection v4 does
  not by itself version the changed TASK_READY event interpretation.
  Verified production declarations also exist in
  `framework/events/schema/catalog.py` and
  `backend/research/graphs/contracts.py`; the persistence slice owns their
  necessary event-version alignment, without changing Research behavior or
  enabling its dynamic default.
- Review capacity-wait idempotence with two different observation timestamps:
  the same recorded ready/capacity/budget/deadline inputs must still reuse the
  same event rather than conflict merely because packing was checked later.
- Missing required demand policy must fail closed, not become an ordinary
  deferred task while other candidates execute. Capacity shortage and invalid
  policy are different outcomes.
- Reclaimed tasks must rejoin the pinned scheduler order, not acquire an order
  determined by the sequence in which reclaim operations were processed.

- Capacity one: earlier candidate missing a pool must not hide a later fully
  satisfiable candidate, including when both contend for the last budget.
- Earlier budget-ineligible candidate must not partially occupy pools before
  checking a smaller later candidate.
- Priority/depth/id, selected and overflow orders survive reopen and replay.
- Waiting creates no attempt, budget allocation, empty wave or retry count;
  repeated recovery preserves its fixed deadline and evidence identity.
- Static queue recovery retains original admitted attempt; logical READY and
  wave-owned admission trigger zero queue/reclaim calls.
- Admission publication failure or CAS conflict leaves no authoritative partial
  reservation. Exact redelivery does not charge twice.
- Missing/stale required policy fails closed. Recomputed-checksum tampering of
  pool scope/revision/quantity, packing order or budget evidence is rejected.
- Existing complete attempt history and deterministic gates remain enforced.
- Collect all implementation and test changes before running checks. Then run
  concentrated regression, canonical event/replay coverage, strict OpenSpec,
  diff checks and required full smoke. Fix roots of failures without weakening
  assertions. Commit and check task 2.3 only after final acceptance.

### Batch integration checkpoint

Integration decision: the declared shared-capacity authority must also commit
normal verified wave settlement with its canonical event. Recording release
before/after snapshots without updating that same authority is insufficient:
the next wave would still read the admitted reservation. The persistence slice
owns this bounded settlement CAS, coordinated with the admission slice's
TASK_WAVE_COMPLETED emitter. Release must derive from recorded reservations
and verified terminal/settlement evidence, preserve unrelated allocations,
reject over-release or stale revisions, and be idempotent. Unknown or unconfirmed
child outcomes must not release capacity merely because a local wave ended.
Do not implement the later full cancellation/fencing recovery workflow here.

Settlement completeness is part of that transaction: the supplied settled
reservation identities must equal the admitted wave members reported as
CONSUMED or RELEASED in the completion event. A caller must not report two
settled tasks while releasing only one task's capacity; once the wave is
terminal, that discrepancy cannot be repaired by replaying its completion.
Members still reported RESERVED remain allocated and are excluded from the
release set. Validate this equality before making any authoritative write.

Queue admission validation must preserve the existing atomic transition-batch
contract. A valid READY -> QUEUE_ADMITTED -> DISPATCHED batch is checked against
each immediately preceding canonical projection, not rejected solely because
QUEUE_ADMITTED is not the only event in the batch. No supplied intermediate
projection may become its own authority: validate readiness and each lifecycle
transition against the accepted plan and the previous validated projection.

Exact admission redelivery also binds the full reserved TaskInstance, including
worker_ref and budget_snapshot, not just its deterministic task_instance_id.
Those fields are not part of attempt_identity(), so matching that ID alone
cannot establish an identical request. The ADMITTED fast path must verify the
existing RESERVED ledger record and unchanged ledger before returning the
original projection; changed payloads and missing/terminal reservations fail
closed instead of being accepted as idempotent retries.

The admission slice may edit framework/harness/agent_loop/orchestration.py and
tests/framework/harness/agent_loop/test_orchestration_submission.py for the
previously approved narrow active-submission/recover_submission waiting bridge.
Verify the actual test location before editing. Internal capacity wait must
preserve the active submission and fixed deadline, must not publish a terminal
parent observation, and must not advance parent reasoning. Full automatic
parent continuation remains G3 work.

The dependency-blocking implementation and its focused fixtures have been
delivered and reviewed statically. Unallocated PENDING/READY tasks preserve
historical attempt counts and their complete budget snapshot when blocked;
only the blocked task is removed from logical READY order. Active admitted
attempts remain protected by their cancellation/settlement owner. This is
source-review evidence only: no task 2.3 tests or compile checks have run.

Before opening the concentrated test batch, finish the remaining old allocated
READY fixtures, including budget recovery, attempt-history runtime, Research
graph recovery, durable store/replay and atomic transition fixtures. Assertions
must continue to prove the original budget, history, recovery and publication
properties with explicit logical readiness followed by canonical admission.

The storage test batch must cover real SQLite transactions, not only fake UoW
calls: independent writers competing for one capacity revision, rollback after
state CAS and a later append failure, exact redelivery, and rejection of new
events attached to an already-consumed state transition. PostgreSQL SQL-shape
unit tests are not evidence of a live PostgreSQL concurrency run.

Waiting verification boundary: the current submission test exercises
HarnessAgentOrchestrationRuntime dispatch/recover_submission, not an end-to-end
parent AgentLoop. Source inspection of framework/agent/loop/loop.py shows its
delegate-batch caller still routes non-succeeded orchestration results through
_orchestration_group_failed_result. Thus this test cannot prove resumable parent
AgentLoop waiting or absence of a terminal parent failure. That caller boundary
remains explicit G3 work; do not use the task 2.3 submission test to claim G3
acceptance or enable generic production orchestration.

### First concentrated validation batch and repair boundary

The earlier source-only checkpoints above describe intermediate implementation
states. The first concentrated validation batch has now completed; task 2.3
remains unchecked and no code commit has been made for it.

- Required smoke: 152 failed, 3308 passed, 23 deselected and 45 setup errors;
  exit 1 after 45 minutes 28 seconds. Compilation, the subsequent offline
  AgentLoop smoke (zero network calls), and source validation passed. These
  individual passes do not override the failed smoke gate.
- Additional event/storage coverage: 12 failed, 740 passed and 81 skipped;
  exit 1. The 12 failures reject ordinary readers after transactional-state
  reading was added to their structural protocol.
- Strict OpenSpec validation and `git diff --check` passed for that batch.
- Live PostgreSQL integration was not configured. SQLite transaction tests and
  PostgreSQL unit tests do not establish live PostgreSQL concurrency behavior.

Evidence is in the untracked local logs under
`outputs/task-2-3-validation-20260910-0113-{smoke,events,openspec}.log`.
Logs are diagnostic artifacts, not source files to stage.

The second repair batch separates ordinary event read/publish contracts from
transactional-state capabilities, corrects canonical frozen-array handling in
replay, and migrates old fixtures to explicit admission and packing evidence.
The reader capability split has been applied but not retested. Runtime and
replay production repairs and the fixture migrations are still in progress.
Keep budget, lineage, recovery, quarantine and zero-live-replay assertions;
do not relax guards to preserve obsolete allocated-READY fixtures.

Collect this entire repair batch before rerunning the required smoke,
event/storage coverage, strict validation and diff checks. No per-edit test
runs, duplicate pre-smoke Harness suites, task checkmark or commit are authorized
by these intermediate results.

Second-batch integration review still requires these closures before testing:

- Preserve independent checksum regression oracles. Replacing fixed golden
  checks solely with the model's own `checksum_projection()` and canonical
  hash function cannot detect accidental changes to that projection. Update
  the expected versioned fixture values with an explained identity delta, or
  pin an independently enumerated expected projection as well.
- Check the ordinary public `commit_events()` boundary for capacity-bearing
  admissions and settlements. Source review currently shows it can publish
  events without the specialized capacity CAS even though the stage caller
  normally selects the specialized method. A fresh capacity mutation must not
  become authoritative through that alternate entry point. Preserve valid
  capacity-less events and exact historical redelivery.
- Check mixed zero-selection waiting reasons: dispatch currently records a
  wait when some tasks are budget-ineligible and others are temporarily
  capacity-blocked, but replay accepts only capacity/resource reasons. The
  recorded complete reason set and deterministic replay must agree without
  turning a missing/stale policy into ordinary waiting.

These are source-review findings, not claims that the additional closures have
already been implemented or tested.

The state-fixture reviewer restored the fixed instance/continuation/readback
oracles and complete eligible READY ordering after integration feedback. The
queue projection oracle still needs an independent version-aware expected
payload/value: leaving a known obsolete literal until a 45-minute smoke run
reports its replacement is not a completed repair. Resolve that fixture before
opening the next validation batch. The old-version identity oracle must also
be checked for transitive identity changes rather than assumed unchanged.

Integration closures now applied, but not yet tested:

- The independent fixture payloads now pin the complete TaskInstance and
  queue-wire field sets and aliases. The obsolete queue projection and old-v2
  identity golden values have been replaced after independent data/hash
  derivation; no test invocation was used to harvest a replacement value.
- Replay now accepts BUDGET_EXCEEDED alongside a temporary capacity/resource
  reason in the complete waiting set. At least one temporary blocker remains
  mandatory, so an all-budget-failed decision cannot become a capacity wait.
  Missing/stale policy and unknown reasons remain invalid. Added focused
  positive/idempotence/offline-clock and negative contract cases in
  `test_capacity_wait_reason_replay.py`; these cases have not been run.

The alternate public event-commit path still needs its capacity mutation
boundary closed. Its integration must distinguish real settled capacity changes
from a completion that retains every reservation as RESERVED and leaves the
snapshot unchanged; the latter must not be forced through an advancing CAS.

### Second concentrated validation result

The capacity route guard and its regression cases were completed before this
batch. All four workers had finished. SHA-256 snapshots of all 62 changed/new
Python and SQL files were identical before and after validation.

- Required smoke terminated with exit 1: 33 failed, 3499 passed, 23 deselected,
  31 warnings, no setup errors, in 3440.71 seconds (57 minutes 20 seconds).
- Compilation, the subsequent offline AgentLoop smoke (zero network calls),
  and source validation passed. The latter reported zero errors and warnings.
- Event/storage coverage terminated with exit 0: 756 passed, 81 skipped in
  173.38 seconds. The first batch's twelve ordinary-reader failures are gone.
- Strict OpenSpec validation and diff whitespace checks passed.
- Local logs use the prefix `outputs/task-2-3-validation-20260910-104234-`.
  Both test processes are terminal; there is no validation process to restart
  or resume. Task 2.3 remains unchecked and uncommitted.

The next concentrated repair batch must address the remaining evidence:

- Seven spawn-replay tests still pass a logical READY-only projection to the
  direct parallel reducer instead of the admitted projection/reserved ledger.
- Three durable completion fixtures lack `child_states`, which the canonical
  v3 event schema requires. Preserve that contract rather than relaxing it.
- `restore_group()` compares a newly observed request clock with the original
  admission clock, unlike `create_group()`'s existing pinned-clock handling.
- One exclusive candidate-submission interleaving test did not reach its
  artifact rendezvous within five seconds. Determine whether the submitter
  failed before the rendezvous or the scheduling assumption is brittle; do not
  merely increase its timeout or weaken the exclusivity assertions.
- Nine dependency-authority and ten Research integration failures still stop
  before their intended downstream assertions. Inspect their retained
  `pytest-3204` event/artifact/transcript evidence to identify the actual
  runtime failure; do not assume the now-fixed frozen-array bug explains them.
- Capacity waiting still returns partial failure in one orchestration test,
  and same-group retry returns blocked in one runtime test. Inspect the
  recorded diagnostics and the relevant current paths before editing.

These are failure classifications, not completed fixes. Collect all changes
for this repair batch before executing tests again, preserve independent
oracles and guards, and require a successful complete pre-commit smoke.

### Third repair delivery and validation provenance

All three implementation workers have now reported completion. Source review
confirmed the delivered fixes for completion `child_states`, semantic candidate
artifact matching, restore admission-clock reuse, explicit GROUP_WAVE replay
fixtures, immutable cross-wave attempt-history identity, and canonical ordered
capacity-wait evidence. These reports are not passing-test evidence.

The task-plan/Research integration run associated with `pytest-3205` ended with
28 failures, 957 passes and 9 deselections in 1604.10 seconds. It was started
before all workers had reported completion, without a pinned source snapshot.
It must not be treated as validation of the final third-batch files. A subsequent
single dependency-authority case passed (43.80 seconds); that isolated rerun
neither closes the batch nor follows the requested concentrated-test workflow.
Do not repeat individual cases as edits arrive.

After confirming every implementation worker was terminal and reviewing the
delivered source, a new frozen validation batch was opened with logs prefixed
`outputs/task-2-3-validation-20260910-130207-`. The main agent captured SHA-256
values for changed/new Python and SQL files before starting. Do not edit those
files during this batch; compare their hashes again at completion.

- Strict OpenSpec validation and `git diff --check` passed.
- Additional event/storage tests passed: 756 passed, 81 skipped, exit 0 in
  156.89 seconds. Skipped live integrations are not production-backend proof.
- Required smoke is still running; its final result remains unproven.

The read-only acceptance review identified additional direct-oracle gaps for
budget-ineligible-first packing and stable overflow/recovery ordering. Prepare
any supplemental tests outside the tested source tree until this frozen batch
is terminal. Passing the existing smoke alone must not close task 2.3 while a
required acceptance case lacks evidence. Keep task 2.3 unchecked and uncommitted.

Supplemental acceptance patches are staged only as reviewable text under
`outputs/`, not applied to the frozen test tree:

- `task-2-3-packing-acceptance.patch`: last-budget preservation after capacity
  rejection and preservation of a shared slot after budget rejection.
- `task-2-3-shared-plan-acceptance.patch`: two distinct accepted plans with
  independent SQLite runtimes contend for one capacity scope. Both propose
  the same aggregate after-state, so the losing writer must hit the exact
  `EventContractError` for unseen events attached to state redelivery, with
  transaction rollback. This is different from unequal-state revision conflict.
- `task-2-3-ready-order-acceptance.patch`: priority/depth/id ordering through
  durable reopen/replay. Review requires persisting the admitted wave and its
  packing evidence, not just checking a provisional in-memory packing result.

Collect and review all supplemental tests before applying and running them.
If they change tests only and the production source hashes remain unchanged,
combine the completed full smoke with one focused supplemental batch. A
production repair requires a fresh full smoke. Do not repeat the same broad
regression without a changed production surface or another concrete concern.

### Frozen validation result and supplemental integration

The `20260910-130207` smoke is terminal, exit 1: 3531 passed, one failed,
23 deselected, 31 warnings in 3424.70 seconds. The remaining failure is
`test_capacity_wait_remains_pending_until_explicit_recovery_without_parent_progress`:
the first dispatch returns `partial_failure` instead of `waiting`. Compilation,
offline AgentLoop smoke (zero network calls), and source validation passed;
the latter reported zero errors and warnings. All 63 pinned Python/SQL hashes
were unchanged at the end. This is valid evidence for that frozen source,
but the failed smoke is not acceptance.

After termination, all three reviewed supplemental patches were applied to
the test tree together. No supplemental tests have run yet. Investigate and
repair the sole remaining waiting failure before opening the next test batch.
Source evidence shows the no-pool coordinator can emit a null capacity snapshot
while replay requires a mapping; preserve fail-closed required-policy handling
when resolving this producer/consumer mismatch. A production repair requires
fresh required smoke, in addition to the concentrated supplemental validation.

The waiting mismatch repair is now applied: pool-free concurrency-slot waits
retain canonical `capacity_snapshot: null`; non-null snapshots must still be
typed capacity objects. Live admission with required pools continues to reject
missing policy/snapshot before event generation. The existing submission test
now asserts null snapshot preservation on initial wait and explicit recovery,
alongside unchanged deadline, no child/attempt/budget allocation and pending
receipt assertions. This narrow submission bridge does not close G3 parent-loop
continuation.

All repair and supplemental changes were collected before starting the focused
five-file batch logged at `outputs/task-2-3-supplemental-final.log`. Strict
OpenSpec validation and diff checks passed; the test batch is still running.

The concentrated five-file batch has now terminated successfully: 147 passed
in 560.31 seconds, exit 0. This includes the repaired slot-only waiting case
and all applied supplemental acceptance tests. No code changed during the
batch. Required final smoke is running with the same source at
`outputs/task-2-3-final-smoke-20260910-1415.log`; its result is not yet known.
Task 2.3 remains unchecked and uncommitted until that gate completes.

### Final acceptance

The final required smoke completed with exit 0: 3536 passed, 23 deselected,
31 warnings in 3444.05 seconds. Compile passed, the offline AgentLoop succeeded
with zero network calls, and source validation reported zero errors/warnings.
All 64 frozen Python/SQL file hashes matched after completion. Combined with
the 147-test supplemental batch and 756-pass event/storage batch, this closes
the task 2.3 acceptance cases recorded in `task-2-3-verification.md`.
Task 2.3 is now checked. Task 2.5 is the next unchecked task; subsequent gates
and live PostgreSQL integration remain outside this acceptance.
