## MODIFIED Requirements

### Requirement: Ready tasks are calculated deterministically and dispatched within bounds

The TaskPlan scheduler SHALL mark a task ready only when all required dependencies have durable successful results, all input references resolve, policy and binding checks remain valid, and budget reservation succeeds. When a plan version is accepted, the Harness group/wave coordinator MUST establish one immutable `DispatchGroup` for its complete logical execution scope before any physical dispatch. Ready tasks MUST be ordered deterministically and physical dispatch MUST honor stage `max_parallelism`, capability capacity, child supervisor capacity, and concurrency reservations. Every selected ready set MUST be submitted as a wave in that group; it MUST NOT silently execute a parallel candidate in a synchronous per-task loop.

#### Scenario: Independent tasks become ready together

- **WHEN** two tasks have no dependencies and their inputs and budgets are valid
- **THEN** the scheduler MUST return both in stable priority/depth/id order
- **AND** when the tasks pass concurrency checks, effective parallelism is at least two, and serial fallback is not selected, the coordinator MUST start them concurrently within the configured parallelism bound

#### Scenario: Dependent task waits for its predecessor

- **WHEN** task C depends on task A and A has not durably completed
- **THEN** C MUST NOT be ready or dispatched
- **AND** A's accepted result MUST be committed before C can become ready

#### Scenario: Group wave admission reserves capacity

- **WHEN** multiple ready tasks are selected for one dispatch wave
- **THEN** Harness MUST reserve their task identities, capacity, and normalized budgets before creating child attempts
- **AND** every child attempt MUST reference the same accepted plan version, group id, and wave id

#### Scenario: Admitted attempt is not a fresh ready task

- **WHEN** the durable wave admission and spawn intent batch has committed but dispatch is not yet confirmed
- **THEN** selected task projections MUST carry `ADMITTED` with their original active attempt and reservation
- **AND** checkpoint and offline replay MUST reconstruct that state without allocating another attempt
- **AND** recovery MUST retain coordinator ownership rather than enqueue or reclaim the admitted attempt as an ordinary queue task

#### Scenario: Versioned task state rejects identity substitution

- **WHEN** a task projection or attempt payload is deserialized or transitioned
- **THEN** Harness MUST reject unsupported schemas, inconsistent state/attempt fields, changed deterministic instance/idempotency/fencing identity, illegal state transitions and rewritten terminal evidence
- **AND** re-computing an envelope checksum MUST NOT authorize replacing the accepted attempt identity

#### Scenario: Budget reservation fails

- **WHEN** a ready task cannot reserve its normalized budget
- **THEN** Harness MUST not dispatch that task or any task whose reservation would exceed the remaining bound
- **AND** the stage MUST follow the explicit budget-exhaustion policy

#### Scenario: Group/wave adapter is unavailable

- **WHEN** policy-controlled parallel dispatch is requested but no valid group/wave adapter or child supervisor is available
- **THEN** Harness MUST fail closed unless an explicit `serial_fallback` policy is enabled
- **AND** an enabled fallback MUST record a `DEGRADED_SERIAL` reason before executing the wave

### Requirement: Task results have bounded attempts and deterministic verification

Every task attempt SHALL have a unique task-instance identity, plan version, group id and wave id when dispatched through group/wave orchestration, worker binding, budget snapshot, and result checksum. Harness MUST validate `HarnessWorkerResult`/`SubAgentResult`, output schema, declared deterministic gates, tool/memory usage, and attempt identity before committing success. A resolved subagent task MUST additionally carry exactly one typed durable attempt receipt; its transcript and candidate output MUST be read back, checksum-verified, and matched to the accepted plan/task/group/wave/attempt before a successful, failed, or halted result is committed. Non-subagent tasks MUST NOT be required to fabricate transcript evidence.

#### Scenario: Valid result completes a task

- **WHEN** a worker result matches the task identity, plan/group/wave identity, output contract, binding, budget, and required deterministic gates
- **THEN** Harness MUST commit the accepted result and task terminal event
- **AND** only then may downstream dependencies or the group join be reconsidered

#### Scenario: Result belongs to an old plan version

- **WHEN** a result arrives with a plan version lower than the current stage version and the task is not an accepted committed result
- **THEN** Harness MUST reject the result as stale
- **AND** the current task projection, group join, and downstream readiness MUST remain unchanged

#### Scenario: Task retry limit is reached

- **WHEN** a retryable task failure reaches the normalized `max_task_attempts`
- **THEN** the task MUST enter terminal failure; the coordinator MAY enter its non-terminal `REPLAN_PENDING` state only when the pinned policy permits a replan
- **AND** Harness MUST NOT silently create another attempt or add the failed task twice to a group

#### Scenario: Subagent result has verified durable evidence

- **WHEN** a resolved subagent result carries a receipt whose transcript, output, binding, group/wave identity, and attempt identity all verify
- **THEN** the versioned `TaskResultRecord` MUST include the transcript ref/checksum and candidate-output ref/checksum in its checksum projection
- **AND** a successful result MUST use the readable durable candidate-output ref as `result_ref`

#### Scenario: Subagent result has fabricated evidence

- **WHEN** a subagent result carries a missing, unreadable, stale, corrupt, mismatched, or fabricated receipt or output ref
- **THEN** deterministic result verification MUST reject the result before appending an accepted or rejected TaskResult
- **AND** Harness MUST record a controlled parent/group failure transition with the stable reason

#### Scenario: Failed subagent attempt is recorded

- **WHEN** a subagent worker fails or a deterministic subagent gate halts the attempt and its durable receipt verifies
- **THEN** the rejected result and terminal failure event MUST retain the same transcript/output evidence lineage and group/wave identity
- **AND** the candidate output MUST NOT become an accepted downstream or aggregate result

### Requirement: Stage aggregation is deterministic and role-complete

A dynamic stage SHALL expose outputs only through a registered deterministic aggregator. The aggregator MUST consume accepted task result references from a joined DispatchGroup in stable order across all waves, enforce output roles and schema, reject missing or conflicting roles, and produce a checksum-bound stage output projection. A group MUST NOT be considered successful merely because all children are terminal; required role and gate checks remain authoritative.

#### Scenario: Research roles are complete

- **WHEN** accepted task results from the joined group provide every required Research analysis role
- **THEN** the aggregator MUST produce the existing `analysis_branch_refs` contract
- **AND** the fixed `verify_claims` successor MAY consume that reference

#### Scenario: Aggregation has an incomplete role set

- **WHEN** one required output role has no accepted result after the group join condition
- **THEN** aggregation MUST fail with a typed missing-role outcome
- **AND** the stage MUST NOT enter downstream quality or publication steps

#### Scenario: Aggregation sees conflicting outputs

- **WHEN** two accepted tasks in one group claim one role without a declared merge contract
- **THEN** preflight or aggregation MUST fail closed
- **AND** it MUST not select a winner based on completion time, thread order, or queue order

#### Scenario: Parent receives aggregate result

- **WHEN** a group aggregator completes successfully
- **THEN** Harness MUST publish one aggregate ref/checksum and stable per-task summaries to the parent Agent or downstream stage
- **AND** raw sibling prompts and private transcripts MUST remain outside the aggregate observation

### Requirement: TaskPlan transitions are durable and replayable

The runtime SHALL record candidate, validation, acceptance, readiness, group/wave admission, wave dispatch, child start, retry, result, task terminal, group join waiting, group join completion, patch, aggregation, verification, cancel, reclaim, and halt transitions in the canonical run event stream. Checkpoints MUST include graph checksum, plan version/checksum, group/wave ids and join policy, task projection, output references, budget reservation/release/usage, replan/retry counters, stream sequence, and versioned TaskResult checksums. Result and terminal events for subagent attempts MUST carry the same transcript and candidate-output refs/checksums as their TaskResult record. Replay MUST use recorded candidate/plan/result/transcript/output/aggregate evidence and MUST NOT call live plan builders, LLMs, workers, tools, supervisors, or queues.

#### Scenario: Crash occurs after plan acceptance

- **WHEN** the process crashes after `PLAN_ACCEPTED` but before all group/wave or queue projections are created
- **THEN** recovery MUST load the accepted plan and recompute missing ready/group projections deterministically
- **AND** it MUST not generate a new candidate, plan version, or duplicate group admission

#### Scenario: Replay verifies a completed stage

- **WHEN** a completed TaskPlan stage is replayed from its checkpoint and event stream
- **THEN** replay MUST rebuild the same plan/task/group/wave/output projection and decision checksums
- **AND** it MUST not call the live plan builder, subagent, tool, supervisor, or queue worker

#### Scenario: Plan evidence is corrupt

- **WHEN** a plan artifact, patch, result ref, transcript ref, candidate-output ref, aggregate ref, or checksum cannot be resolved or verified during recovery
- **THEN** recovery MUST fail closed with a typed history diagnostic
- **AND** it MUST not mark the stage complete, guess a replacement plan, or return partial success

#### Scenario: Crash occurs after subagent receipt commit

- **WHEN** the process crashes after a subagent receipt is committed but before its TaskResult or group join transition is appended
- **THEN** recovery MUST reuse that exact receipt and reconstruct the candidate outcome for deterministic TaskPlan verification
- **AND** it MUST not call the subagent worker, create a conflicting attempt body, or join the task twice

#### Scenario: Result event is missing after result document persistence

- **WHEN** recovery finds an immutable TaskResult document but its matching result, terminal, or join transition is absent
- **THEN** reconciliation MUST append only the missing transition with the same checksum and projection
- **AND** it MUST not run the worker or generate a new result document

### Requirement: Upstream terminal failure SHALL close unadmitted dependency descendants

When a predecessor reaches an unrecoverable terminal failure, the coordinator MUST traverse its dependency closure in stable DAG order and mark every unadmitted direct/transitive successor `BLOCKED_DEPENDENCY` with `TASK_BLOCKED_UPSTREAM_FAILURE`. Blocked tasks are terminal without entering a wave. Unconsumed reservations MUST be released and no child may be created. `wait_all` MUST include these states and return `DEPENDENCY_BLOCKED` or `REQUIRED_ROLE_MISSING` instead of waiting forever. Replacement replan MUST validate a new dependency closure in a new group without rewriting old blocked tasks.

#### Scenario: A predecessor exhausts retries

- **WHEN** A exhausts attempts while B depends on A and C depends on B, and B/C are not admitted
- **THEN** B and C MUST become terminal `BLOCKED_DEPENDENCY` in stable order, with no child calls or leaked reservations
- **AND** the group MUST reach typed failure or a policy-authorized new-plan replan without producing a success aggregate

#### Scenario: Wave budget is exhausted

- **WHEN** initial and retry admissions reach `max_waves` with tasks still unadmitted
- **THEN** Harness MUST record `WAVE_LIMIT_EXCEEDED` and close affected tasks in stable order, including dependency propagation
- **AND** no task may wait indefinitely or create a wave above the limit

### Requirement: Canonical result history SHALL retain every attempt outcome

The durable canonical `result_history_for()` MUST preserve accepted, rejected, failed, cancelled, indeterminate, reclaimed and quarantined attempt records, including plan/group/wave/task/attempt/binding identity and receipt evidence. `results_for()` MAY expose only accepted results, but retry, recovery, replay and checkpoint rebuilding MUST use complete history. Late old-group receipts MUST enter quarantine/audit without changing current-plan projections.

#### Scenario: A failed attempt is followed by success and a stale receipt

- **WHEN** one task succeeds on a later attempt and a receipt from a superseded group arrives afterward
- **THEN** accepted projection MUST contain only the valid accepted result and canonical history MUST retain failure, success and quarantined stale evidence
- **AND** offline replay MUST rebuild the same history without invoking live execution
