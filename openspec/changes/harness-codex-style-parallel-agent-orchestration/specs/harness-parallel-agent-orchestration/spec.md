## ADDED Requirements

### Requirement: Harness accepts bounded multi-child delegation candidates

Harness SHALL provide a versioned orchestration contract that accepts a parent Agent `PlanCandidate` or `delegate_batch` candidate containing multiple logical child task proposals. Each proposal MUST contain a stable task identity, objective, input references, logical capability hint, declared output roles, and dependency references. Harness MUST validate the candidate against the pinned stage policy before creating any child attempt or DispatchGroup.

#### Scenario: Parent proposes independent child tasks

- **WHEN** a parent candidate contains two independent tasks whose capabilities, inputs, outputs, and dependencies satisfy the pinned policy
- **THEN** Harness MUST accept an immutable plan version and create one DispatchGroup identity for the plan's complete logical execution scope
- **AND** no child may start before the plan and group/wave admission events are durable

#### Scenario: Candidate attempts to select a worker or route

- **WHEN** a candidate contains a callable, concrete worker id, queue name, `next_step`, quality verdict, publication command, memory promotion, or halt instruction
- **THEN** Harness MUST reject the candidate with a typed validation reason
- **AND** it MUST not create a group, queue projection, or child attempt

#### Scenario: Candidate widens concurrency or authorization

- **WHEN** a candidate requests more parallelism, tools, memory namespaces, capabilities, or budget than the pinned policy allows
- **THEN** Harness MUST clamp only values explicitly defined as safe hints or reject the candidate when clamping would change its declared contract
- **AND** the candidate MUST never grant itself additional authority

### Requirement: Group and wave admission SHALL be bounded and durable

Harness SHALL create a durable `DispatchGroup` only after the complete plan scope, dependency closure, required roles, capability bindings, policy checksum, input-reference authority, and total budget envelope validate. A group MAY contain tasks that are not ready yet. Harness SHALL create each `DispatchWave` only after its selected tasks are ready and their side-effect, budget, and capacity checks pass. A group covers the complete logical join scope; a wave covers one physical dispatch bounded by current capacity. Admission MUST use deterministic first-fit packing in stable plan order against all capability/resource pools, supervisor capacity, stage limits, and available concurrency reservations. Each selected task MUST reserve all resources atomically; unavailable tasks remain READY while later eligible tasks are considered. Pool reservations MUST enter the wave checksum; missing or stale capacity MUST fail closed. Group admission MUST pin but not consume the total budget envelope. Wave admission MUST atomically reserve each selected task's normalized budget and capacity exactly once using `RESERVED -> CONSUMED | RELEASED`; later waves MUST reuse the same group identity.

#### Scenario: Group and first wave are admitted within capacity

- **WHEN** three ready read-only tasks fit within `max_parallelism`, child capacity, and the remaining budget
- **THEN** Harness MUST persist one group admission and one wave admission with group id, wave id, plan version, task identities, join policy, reservations, and effective parallelism
- **AND** it MUST dispatch no task that is absent from the corresponding wave admission

#### Scenario: Eligible tasks require real concurrency

- **WHEN** at least two ready tasks are independent, concurrency-safe, reserved, and `effective_parallelism` is at least two without `serial_fallback`
- **THEN** Harness MUST start up to `effective_parallelism` child attempts concurrently
- **AND** an implementation that always invokes those tasks sequentially MUST NOT satisfy this requirement

#### Scenario: Ready set exceeds capacity

- **WHEN** five tasks are ready but the effective parallelism is two
- **THEN** Harness MUST dispatch at most two tasks in the current wave
- **AND** the remaining tasks MUST remain durable READY projections for a later wave in the same group without creating unbounded child attempts

#### Scenario: Side-effecting tasks lack a fence

- **WHEN** two ready tasks can write the same external resource and no policy-approved deterministic fence exists
- **THEN** Harness MUST reject concurrent admission for the conflicting tasks
- **AND** it MUST either schedule them serially under an explicit serial policy or return a typed conflict diagnostic

### Requirement: Child attempts SHALL reuse the controlled Agent runtime

Every admitted task MUST execute as an independent child attempt through the registered capability binding, `ChildAgentSupervisor`, and `SubAgentRuntime` or an equivalent Harness-owned worker adapter. The child envelope MUST contain only policy-approved input refs, tool allowlists, memory namespaces, context limits, and budget. A child tool call MUST be attributed to that child attempt and MUST pass the existing `ToolExecutor` authorization and receipt checks.

#### Scenario: Child uses an allowlisted tool

- **WHEN** a resolved child capability calls a tool listed in its `SubAgentSpec` and stage policy
- **THEN** the ToolRuntime MUST execute it under the child attempt identity and persist its receipt
- **AND** the tool observation MAY be used as child evidence but MUST NOT alter parent routing or policy

#### Scenario: Child requests a disallowed tool

- **WHEN** a child requests a tool outside the resolved allowlist or beyond the child budget
- **THEN** the tool call MUST be denied deterministically
- **AND** the child result MUST enter the existing rejected or failed gate path without widening permissions

#### Scenario: Child returns a control field

- **WHEN** a child result contains workflow routing, quality, publication, memory promotion, or arbitrary executable fields
- **THEN** the result gate MUST reject or strip those fields according to the result contract
- **AND** the fields MUST NOT influence group join or parent Agent continuation

### Requirement: Fan-in SHALL produce a deterministic parent result package

Harness SHALL wait for the policy-selected group join condition, verify every child result independently, and aggregate only accepted result references across all waves in the group. The aggregate MUST use stable plan order, enforce required roles and output conflict rules, include every task result checksum, and expose a versioned `ParallelDispatchResult` to the parent Agent as one security-projected observation.

#### Scenario: All required children succeed

- **WHEN** every required child reaches a verified success state and output roles are complete
- **THEN** Harness MUST persist a joined result package with stable task summaries, result refs/checksums, aggregate ref/checksum, budget usage, and gate evidence
- **AND** the parent Agent MUST receive one joined observation that it can use in its next candidate turn

#### Scenario: One required child fails

- **WHEN** the join policy is `wait_all` and a required child reaches terminal failure after bounded retry
- **THEN** Harness MUST withhold the aggregate and return a typed partial-failure result
- **AND** no incomplete output may be published as a successful parent observation

#### Scenario: Completion order differs from plan order

- **WHEN** child B finishes before child A even though A precedes B in the accepted plan
- **THEN** the joined task list and aggregate checksum MUST follow the stable plan order
- **AND** completion timing MUST not change the parent result or downstream decision checksum

#### Scenario: Parent observation is inspected

- **WHEN** an authorized caller reads a joined result package
- **THEN** it MUST expose group/wave/task identities, statuses, refs, checksums, diagnostics, and budget/recovery summaries
- **AND** it MUST redact hidden prompts, secrets, sibling private transcripts, and unauthorized payloads

### Requirement: Group failure handling SHALL be explicit and bounded

Harness SHALL apply the group's pinned `join_policy`, retry budget, replan budget, and cancellation policy. A retry MUST use a new attempt identity; a replan MUST create an immutable next plan version and a new `DispatchGroup`. `ADD_REPLACEMENT_TASK` MAY target only a terminal failed logical task; `SKIP_PENDING_TASK` and `UPDATE_PENDING_DEPENDENCY` MAY target only tasks not admitted to any wave. Running tasks, completed results, required roles, policy, gates, and outer Graph data MUST remain immutable.

#### Scenario: Retryable child failure is retried

- **WHEN** a child fails a retryable deterministic gate and its task has remaining attempts
- **THEN** Harness MUST persist the failed attempt and create at most one next attempt according to the retry budget
- **AND** it MUST not reuse the failed attempt identity or silently increase the budget

#### Scenario: Retry budget is exhausted

- **WHEN** a required task reaches `max_task_attempts` and the policy allows replan
- **THEN** Harness MAY create a versioned replacement-task patch against the current plan version
- **AND** it MUST preserve all verified sibling evidence, reuse a sibling result only when policy/schema/input identity allow it, and re-run role/aggregate validation

#### Scenario: Fail-fast cancellation is selected

- **WHEN** a non-retryable child fails under `fail_fast` join policy while siblings are pending or running
- **THEN** Harness MUST persist cancellation requests and terminal cancellation outcomes for affected siblings
- **AND** it MUST close the group and return a failed group without treating cancelled or partial outputs as success

#### Scenario: Replan budget is exhausted

- **WHEN** a group cannot satisfy required roles and its retry/replan budgets are exhausted
- **THEN** Harness MUST enter a typed controlled failure or halt state
- **AND** it MUST not ask the LLM to bypass the gate or create an unbounded task loop

### Requirement: Group and wave lifecycle SHALL be replayable and recoverable

Harness SHALL persist group admission, wave admission/dispatch, child lifecycle, join waiting, join completion, retry, replan, cancel, reclaim, and recovery transitions in the canonical event stream. Checkpoints MUST include group/wave identity, plan version/checksum, task projections, spawn intents/receipts, complete attempt-history indices, reservation ledger, join policy, aggregate/observation checksums, and stream sequence. Offline replay MUST use only recorded evidence and MUST NOT call live LLMs, sources, RAG, tools, workers, supervisors, queues, or publication adapters. Online recovery MAY perform audited supervisor status/termination/reconcile calls and policy-authorized new attempts only when side-effect safety, idempotency and deadlines permit. Recovery MUST NOT replan through a live LLM or repeat confirmed or uncertain non-idempotent effects.

#### Scenario: Crash occurs after admission

- **WHEN** the process crashes after a group admission event but before all wave projections are materialized
- **THEN** recovery MUST reload the accepted plan and group, recreate only missing bounded dispatch projections, and preserve task/attempt identities
- **AND** it MUST not generate a new candidate or duplicate the group

#### Scenario: Crash occurs after child receipt commit

- **WHEN** a child receipt and output artifact are durable but the matching task result transition is missing
- **THEN** reconciliation MUST verify the receipt and append only the missing result/join transition
- **AND** it MUST not invoke the child or tool again

#### Scenario: Lease expires with uncertain outcome

- **WHEN** a child has no verifiable terminal receipt after its lease expires
- **THEN** Harness MUST follow the pinned reclaim policy and record an indeterminate or controlled retry outcome
- **AND** it MUST not assume success or replay an unconfirmed external side effect

#### Scenario: Side-effect outcome is uncertain during recovery

- **WHEN** recovery cannot verify whether a non-idempotent child or tool side effect committed
- **THEN** Harness MUST fail closed or require deterministic reconciliation under the pinned policy
- **AND** at-least-once recovery MUST NOT be represented as cross-process exactly-once execution

#### Scenario: Completed group is replayed

- **WHEN** an operator replays a completed group from checkpoint and durable events
- **THEN** replay MUST reproduce the same task order, result checksums, aggregate checksum, and parent observation
- **AND** replay MUST perform no live child, tool, queue, or LLM call

### Requirement: Parallel orchestration SHALL expose bounded operations telemetry

The orchestration projection SHALL expose group id, wave ids, plan/task/attempt identities, effective and requested parallelism, admission/queue/wait/run/join durations, child statuses, join policy, budget reservation/release and usage, retry/replan counts, recovery outcome, and any `DEGRADED_SERIAL` reason. Telemetry MUST be security-projected and correlated to the parent run.

#### Scenario: Parallel group completes

- **WHEN** a group joins successfully
- **THEN** authorized inspection and metrics MUST expose the effective parallelism, child terminal counts, join duration, budget usage, and aggregate checksum
- **AND** all fields MUST point to the same parent run and plan version

#### Scenario: Serial fallback is used

- **WHEN** an explicitly configured serial adapter executes a group because parallel capacity is unavailable
- **THEN** the event and inspection projection MUST state `DEGRADED_SERIAL` and the stable reason
- **AND** the runtime MUST not present the execution as parallel success

### Requirement: Group and wave state transitions SHALL be explicit

Each `DispatchGroup` MUST use the states `PLANNED`, `ADMITTED`, `DISPATCHING`, `RUNNING`, `JOINING`, `REPLAN_PENDING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `INDETERMINATE`, `HALTED`, and `SUPERSEDED`. `SUCCEEDED`, `FAILED`, `CANCELLED`, `INDETERMINATE`, `HALTED`, and `SUPERSEDED` are terminal. Each `DispatchWave` MUST use `PLANNED`, `ADMITTED`, `DISPATCHING`, `RUNNING`, and `TERMINAL`, and every terminal wave MUST carry exactly one typed terminal outcome: `SUCCEEDED`, `PARTIAL_FAILED`, `FAILED`, `CANCELLED`, `INDETERMINATE`, `RECLAIMED`, or `DEADLINE_EXCEEDED`. Replay MUST read back and checksum-validate that outcome together with child states and reservation settlement. Every transition MUST define one owner, one canonical event name, one idempotency key, allowed successor states, terminality, and recovery action. `REPLAN_PENDING` is coordinator-internal and non-terminal; it MUST become `SUPERSEDED` only after the replacement plan/group is accepted, or `FAILED`/`HALTED` when replan cannot complete. It MUST NOT be exposed as a final parent outcome.

#### Scenario: Group spans multiple waves

- **WHEN** the ready set exceeds effective parallelism
- **THEN** every wave MUST reference the same group id and plan version
- **AND** the group MUST not join or aggregate until all required tasks across its waves satisfy the pinned join policy

#### Scenario: Fail-fast group closes

- **WHEN** a non-retryable failure occurs under `fail_fast`
- **THEN** the coordinator MUST record failure, close group admission, request sibling cancellation, and wait for cancel receipt or lease expiry
- **AND** late success receipts MUST be quarantined to the closed group and MUST NOT change its aggregate

### Requirement: Candidate admission SHALL be durably idempotent

Harness MUST persist `candidate_dedup_key = run_id + stage_id + parent_turn_id + action_correlation_id` with `candidate_checksum` before accepting a new execution scope. Equal key and checksum MUST reuse the original plan, group, submission, or terminal observation. A conflicting checksum MUST return `CANDIDATE_IDEMPOTENCY_CONFLICT` without executing the new payload. Group identity MUST be a stable hash of accepted plan identity and dedup key. Replan MUST use a new action correlation id and dedup key.

#### Scenario: Candidate is resubmitted after restart

- **WHEN** the same parent turn and action correlation resubmit the same candidate after process restart
- **THEN** Harness MUST return the original group/submission identity and existing outcome when terminal
- **AND** it MUST create no additional group, child attempt, or publication

#### Scenario: Candidate payload conflicts with an admitted key

- **WHEN** a submitted candidate has an existing dedup key but different checksum
- **THEN** admission MUST return `CANDIDATE_IDEMPOTENCY_CONFLICT`
- **AND** the original plan, group and observation MUST remain unchanged

### Requirement: Reference authority SHALL be uniform and fail closed

Harness MUST apply one `RefAuthority` boundary to input refs, result refs, planning observation refs and memory namespaces. Validation MUST cover run, stage, tenant/owner, access mode, artifact type, source checksum and pinned allowlist. Cross-scope sharing MUST be explicitly policy-approved and read-only; candidates cannot authorize sharing and children cannot access sibling private refs.

#### Scenario: Candidate references a sibling private artifact

- **WHEN** a candidate references an artifact outside its permitted owner/stage/tenant scope
- **THEN** Harness MUST reject it with `REF_UNAUTHORIZED` before admission
- **AND** no worker or tool may read the referenced payload

### Requirement: Spawn SHALL use a durable intent receipt and reconciliation protocol

Each attempt MUST have `spawn_operation_key = group_id + wave_id + task_instance_id + attempt`. Wave admission, reservation ledger and `TASK_ATTEMPT_SPAWN_INTENT` MUST be committed in one transaction or equivalent durable batch before spawn. `ChildAgentSupervisor` MUST handle the operation key idempotently and persist `SPAWN_CONFIRMED` or `SPAWN_UNKNOWN` receipts. `TASK_WAVE_DISPATCHED` requires known spawn status and trackable children for all selected tasks. Partially successful batches MUST reconcile each task independently. Identical verified receipt redelivery MUST be reused; conflicting identity/checksum evidence MUST halt rather than overwrite history.

#### Scenario: Process crashes after intent but before receipt

- **WHEN** recovery finds a spawn intent without a receipt
- **THEN** it MUST query the supervisor operation status and record the audited recovery decision
- **AND** unknown status MUST become `SPAWN_UNKNOWN`, not permission to blindly repeat spawn

#### Scenario: Admission publication fails after preparing readiness

- **WHEN** any selected-task readiness, ledger projection, wave or intent write fails before the atomic batch commits
- **THEN** the canonical history, visible task states and ledger MUST remain at the prior committed projection
- **AND** no child may start and no unselected task may acquire an attempt reservation

#### Scenario: A child budget envelope is not backed by its attempt ledger

- **WHEN** an intent carries a valid self-checksum but its allocation or reservation revision differs from the committed attempt record
- **THEN** live admission, recovery and offline replay MUST reject that intent
- **AND** wave `budget_before_checksum` and `budget_after_checksum` MUST bind the actual ledger transition, not an independently constructed counter snapshot

#### Scenario: An old committed batch is delivered again

- **WHEN** an identical event/projection batch is redelivered after later transitions
- **THEN** the store MUST verify each original event's historical projection reference and return without appending events or charging budget
- **AND** partial batches, different projections and missing historical artifacts MUST fail closed without recreating or overwriting committed evidence

#### Scenario: Child started before dispatch event was saved

- **WHEN** the supervisor confirms an existing child but the dispatch event is absent
- **THEN** recovery MUST reuse the receipt and append only the missing transition
- **AND** it MUST not start another child or charge another reservation

#### Scenario: Reservation exists without admission projection

- **WHEN** recovery finds a ledger entry but no corresponding admission event
- **THEN** it MUST repair admission with the same idempotency key or halt on ledger conflict
- **AND** it MUST not reserve the same resources twice

### Requirement: Capacity packing and mutation fences SHALL be resource scoped

Task demand MUST identify every required capability pool, quantity, resource conflict key and policy-resolved side-effect class. Multi-pool reservations MUST be all-or-nothing. `READ_ONLY` tasks may share keys; same-key `EXTERNAL_IDEMPOTENT` tasks serialize unless policy explicitly permits concurrency with receipts. `MUTATING_SERIAL` serializes by key unless policy declares global serialization. `FENCED_MUTATION` requires a per-key fence with owner, generation, TTL, renewal, release and recovery history; loss or uncertain ownership MUST halt or become indeterminate without automatic retry.

#### Scenario: An earlier task cannot reserve all pools

- **WHEN** task A lacks one required pool while later task B can reserve every required resource
- **THEN** A MUST remain READY with `CAPACITY_NOT_AVAILABLE`, without partial reservation, and B MAY enter the wave
- **AND** selection and pool ledger evidence MUST be stable and included in the wave checksum

#### Scenario: Independent mutations have different resource keys

- **WHEN** two mutation tasks have different keys, valid reservations and any required independent fences, without a global serial policy
- **THEN** they MUST remain eligible for concurrent execution within capacity
- **AND** same-key conflicts MUST still obey the pinned serialization or fencing rule

### Requirement: Budget settlement SHALL be bounded versioned and idempotent

`BudgetReservation` MUST carry token, time, tool-call and optional cost limits, owner scope, reservation key, parent/attempt allocations and ledger version. For each dimension `consumed + released + outstanding_reserved <= group_envelope` MUST hold. Attempt hard-limit violations MUST stop execution with `BUDGET_EXCEEDED`; consumed and unused portions MUST be settled separately. Retry requires a new attempt reservation. Cancel, reclaim and crash reconciliation MUST settle by key idempotently. Replan MUST NOT treat unsettled old-group allocations as available new-group budget.

#### Scenario: Attempt cancellation is reconciled twice

- **WHEN** cancellation and recovery both settle the same attempt reservation
- **THEN** the ledger MUST retain one settlement per reservation key and preserve every budget invariant
- **AND** neither consumed budget nor released allocation may be counted twice

### Requirement: Recovery calls and release gates SHALL have explicit evidence

Every recovery live call MUST record `RECOVERY_STATUS_READ`, `RECOVERY_RECONCILED`, `RECOVERY_RETRY_ADMITTED` or `RECOVERY_HALTED` with run/stage/plan/group/wave/attempt correlation. G1 Contract, G2 Coordinator, G3 AgentLoop, G4 Research and G5 Release MUST be evaluated independently. Feature enablement belongs to G5 and MUST NOT substitute for implementation, telemetry, alert, recovery or rollback evidence. Running groups MUST retain pinned policy through rollback and preserve inspection/replay history.

#### Scenario: Feature flag is disabled during an active group

- **WHEN** an operator disables the feature for new submissions
- **THEN** active groups MUST complete recovery, cancellation or halt under their original pinned policy
- **AND** their join policy, budgets, bindings, receipts and event history MUST not be rewritten

#### Scenario: Offline replay encounters a live dependency

- **WHEN** accepted, failed, cancelled, indeterminate, quarantined, serial or crash-recovered history is replayed
- **THEN** failing live adapters MUST remain uncalled and complete state/history/ledger and aggregate/observation checksums MUST match
- **AND** any attempted live call MUST fail with `REPLAY_LIVE_DEPENDENCY`
