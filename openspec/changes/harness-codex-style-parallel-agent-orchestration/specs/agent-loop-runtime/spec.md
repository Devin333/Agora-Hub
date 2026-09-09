## ADDED Requirements

### Requirement: AgentLoop can submit a bounded multi-child delegation candidate

The parent `AgentLoop` SHALL parse a versioned `delegate_batch` action whose child proposals contain logical objectives, capability hints, input refs, output roles, dependency refs, and an action correlation id. The action MUST declare no more than the policy's `max_tasks_per_group`; a parallelism hint is advisory and MUST NOT widen policy. AgentLoop MUST pass the candidate to a production Harness-owned orchestration port and MUST NOT create threads, choose concrete worker implementations, grant tools, mutate workflow state, or decide result quality itself.

#### Scenario: Parent submits independent delegations

- **WHEN** the model returns a valid `delegate_batch` action with two policy-compatible independent child proposals
- **THEN** AgentLoop MUST submit one candidate with a correlation id to Harness
- **AND** it MUST wait for the Harness join outcome rather than invoking either child directly

#### Scenario: Parent submits an invalid delegation

- **WHEN** the model returns a child proposal with a callable, concrete worker reference, unauthorized tool, hidden context, or control field
- **THEN** AgentLoop MUST record a typed candidate rejection or retry diagnostic
- **AND** it MUST not start any child or alter the parent workflow

#### Scenario: Parent requests excessive fan-out

- **WHEN** the model proposes more child tasks or a higher parallelism hint than the AgentSpec/stage policy permits
- **THEN** AgentLoop MUST pass the untrusted candidate to Harness validation or return the typed rejection
- **AND** it MUST not silently bypass the policy or create an unbounded loop

### Requirement: Generic AgentLoop orchestration is production-composed

The production AgentLoop composition SHALL resolve a real `AgentOrchestrationPort`, parent observation policy, feature flag, and availability diagnostics for `delegate_batch`. Missing orchestration binding MUST produce a stable unavailable/deferred result and MUST NOT install an ad hoc child executor. The feature flag MUST be independently observable and MUST preserve the legacy single-child path when disabled.

#### Scenario: Generic multi-child delegation is enabled

- **WHEN** a production AgentLoop has the orchestration feature enabled and all required bindings are available
- **THEN** a valid `delegate_batch` MUST be submitted to Harness and its group outcome MUST be returned as one parent observation
- **AND** no interface entrypoint may bypass the orchestration port

#### Scenario: Generic multi-child delegation is unavailable

- **WHEN** the feature is requested but the orchestration port, policy, or required runtime binding is unavailable
- **THEN** AgentLoop MUST return the stable unavailable/deferred diagnostic
- **AND** it MUST not silently execute children serially unless the pinned policy explicitly selects `serial_fallback`

#### Scenario: Delegation uses the admitted parent Graph activity

- **WHEN** production AgentLoop delegation is configured
- **THEN** its Graph executable node MUST carry explicit leaf and TaskPlan delegation bindings with identical exact worker/activity refs
- **AND** Harness MUST persist a read-only input grant from the verified Graph task before the parent LLM runs
- **AND** the grant MUST include only the parent input names explicitly allowed by the pinned TaskPlan policy
- **AND** TaskPlan and DispatchGroup execution MUST retain the actual parent activity identity without synthesizing activity or node-instance ids from candidate correlation
- **AND** external candidate refs MUST be authorized against that committed grant before submission, including on restart or redelivery
- **AND** missing authority MUST reject rather than reconstruct a grant from candidate refs or current policy names

### Requirement: AgentLoop receives one security-projected joined observation

After Harness completes or fails a delegation group, AgentLoop SHALL receive one observation containing the group status, wave summaries, stable child task summaries, result refs/checksums, aggregate refs when valid, gate diagnostics, and retry/recovery information. The observation MUST obey `ParentObservationLimits` from the parent AgentSpec/stage policy: maximum task summaries, summary bytes, diagnostics, refs, and total observation bytes. Over-limit content MUST be represented only by checksum-bound artifact refs. The observation MUST exclude hidden prompts, sibling private transcripts, secrets, and unapproved raw tool payloads.

#### Scenario: Joined child results are successful

- **WHEN** all required child results pass their deterministic gates and Harness aggregation succeeds
- **THEN** AgentLoop MUST append one joined observation to the parent conversation/event stream
- **AND** the next parent model turn MUST be able to reference the aggregate and child identities without receiving private child context

#### Scenario: Multi-child delegation has a controlled failure

- **WHEN** Harness returns terminal partial failure, cancellation, indeterminate outcome, or halt
- **THEN** AgentLoop MUST expose the typed group outcome as one observation
- **AND** it MUST not convert the outcome into a successful final answer or infer a quality/publication decision

#### Scenario: AgentLoop retry budget is exhausted

- **WHEN** parent loop retry/turn budget is exhausted after a group outcome
- **THEN** AgentLoop MUST return its existing bounded terminal result
- **AND** it MUST not resubmit the same group or ask a child to bypass Harness gates

### Requirement: Legacy single-child delegation remains compatible

The existing single-child `delegate` action SHALL be converted by a compatibility adapter into a one-task group/wave with the same parent identity, policy, tool allowlist, memory boundary, budget, transcript, and result gates. A legacy concrete child reference MUST resolve through `legacy reference -> policy-pinned capability -> unique binding`; missing, ambiguous, or policy-less mappings MUST return stable typed diagnostics. Existing AgentRunner callers MUST continue to receive the current single-agent result shape unless they opt into the group result projection.

#### Scenario: Existing delegate action succeeds

- **WHEN** a legacy `delegate` action names one policy-compatible logical capability and the child returns a verified result
- **THEN** the adapter MUST execute it through the same Harness orchestration and receipt path
- **AND** AgentLoop MUST return a backward-compatible feedback/result projection

#### Scenario: Existing delegate action has no child executor

- **WHEN** a legacy caller has no configured Harness orchestration port
- **THEN** AgentLoop MUST preserve its typed deferred/unavailable diagnostic
- **AND** it MUST not instantiate an unbounded ad hoc child runtime

### Requirement: Planner tool observations are Harness-authorized

AgentLoop MAY request a planning observation through a Harness-controlled `ToolExecutor` only when the pinned AgentSpec/stage policy explicitly allows the read-only tool. The planning call MUST have its own receipt, planning budget, correlation id, and result checksum, and MUST be completed before the candidate is validated. The causal sequence MUST be `PlanningObservationRequest -> PlanningObservationReceipt -> PlanCandidate(source_observation_refs) -> candidate validation`; the receipt MUST first bind `run_id`, `stage_id`, `planner_turn_id`, and `policy_checksum`, and MUST NOT require a not-yet-created candidate checksum. Each planner turn MUST obey `max_planning_tool_calls` and `planning_timeout`. Planning tools MUST NOT grant authorization, mutate workflow routing, publish artifacts, or promote memory.

#### Scenario: Planner uses an allowed read-only tool

- **WHEN** the model requests a policy-allowlisted read-only retrieval tool during planning
- **THEN** Harness MUST execute and persist the tool receipt before candidate validation
- **AND** the receipt MAY be included as an input reference without becoming a quality verdict

#### Scenario: Planner requests a side-effect tool

- **WHEN** the model requests a tool that writes external state, publishes artifacts, changes policy, or promotes memory
- **THEN** Harness MUST deny the call before execution
- **AND** AgentLoop MUST receive a typed tool authorization diagnostic

#### Scenario: Planner tool receipt is not verifiable

- **WHEN** a planning observation has a missing, corrupt, stale, or cross-run receipt
- **THEN** candidate validation MUST fail closed
- **AND** AgentLoop MUST not dispatch child tasks based on that observation

#### Scenario: Several planning calls share one turn deadline

- **WHEN** another permitted planning tool call is admitted in the same planner turn
- **THEN** Harness MUST reuse the durable deadline fixed by the first call and pass only its remaining time to execution
- **AND** reopening the store MUST NOT reset that deadline or forget an unresolved call's budget consumption

#### Scenario: Planning evidence is inspected through standalone offline recovery

- **WHEN** an accepted plan contains planning observation references
- **THEN** those references and the planner turn MUST be pinned by the plan checksum
- **AND** standalone replay and recovery MUST require matching recorded successful receipts and reject missing, corrupt or cross-scope evidence without invoking live tools

### Requirement: Parent submission and continuation SHALL be durable and distinct from terminal observation

`AgentOrchestrationPort.submit(candidate)` MUST return a durable `submission_id`, `group_id`, dedup status and bounded wait information. The port MAY wait for the terminal outcome within the bound. Capacity waits, online recovery and unfinished joins MUST return a `PENDING` submission receipt when that wait expires, without starting a new parent reasoning turn or appending a terminal observation. Harness MUST resume the same parent turn through durable continuation and append exactly one terminal observation using `observation_id + observation_version`. Progress is inspection-only; `REPLAN_PENDING` MUST NOT be exposed as a final parent outcome.

#### Scenario: Parent wait expires while children are still active

- **WHEN** the bounded submission wait expires before group terminal state
- **THEN** Harness MUST persist and return the same pending submission identity
- **AND** the parent model call count MUST not increase and no successful or terminal observation may be appended

#### Scenario: Process restarts before observation delivery is acknowledged

- **WHEN** a terminal group has a pending or already-delivered continuation after restart
- **THEN** Harness MUST resume the same parent turn and idempotently deliver its original observation id/version/checksum
- **AND** it MUST not duplicate the observation, candidate submission or child execution

### Requirement: Parent observation limits and summaries SHALL use one canonical contract

`ParentObservationLimits` MUST use `max_task_summaries=8`, `max_summary_bytes=2048`, `max_diagnostics=16`, `max_refs=16` and `max_observation_bytes=16384` unless explicitly overridden by trusted policy. `max_total_bytes` MUST NOT be an alternate schema field. Summary content MUST come only from durable gated structured results, typed status and deterministic diagnostics. Field selection, plan ordering, redaction, UTF-8 truncation, `summary_truncated` and projection version MUST enter the observation checksum. Projection/replay MUST NOT invoke an LLM to summarize. Oversize detail MUST use checksum-bound artifact refs while preserving identity, terminal outcome, checksums and continuation.

#### Scenario: Observation limits cross the Agent and Harness boundary

- **WHEN** the same policy limits are serialized by Agent and Harness
- **THEN** field names, default values and interpretation MUST match exactly
- **AND** legacy `max_total_bytes` or unknown fields MUST fail contract parsing instead of being silently ignored

#### Scenario: The same group is projected during offline replay

- **WHEN** recorded accepted results are projected after a different completion order or restart
- **THEN** summaries, truncation markers, ordering, redaction and observation checksum MUST match the original projection
- **AND** no live LLM or raw private payload may be accessed

### Requirement: Legacy compatibility SHALL be proven with caller golden fixtures

The one-logical-task compatibility adapter MUST preserve the existing `AgentLoopResult` success/error/stop_reason/diagnostics/trace projection, policy-pinned unique capability mapping, cancellation and recovery semantics. Tests MUST use legacy caller golden fixtures rather than only checking output strings. Runtime availability MUST distinguish `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`, `DEGRADED_SERIAL` and `ENABLED_PARALLEL`.

#### Scenario: Legacy delegate fails or is cancelled

- **WHEN** the adapted child fails or is cancelled under the pinned policy
- **THEN** all legacy result and diagnostic fields MUST match the corresponding caller fixture
- **AND** the adapter MUST preserve receipt lineage and MUST NOT turn failure into success
