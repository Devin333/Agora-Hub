## ADDED Requirements

### Requirement: Execution capability admission is Harness-owned
The runtime SHALL admit an external process, parser, tool, or child agent only when the requested execution profile is supported by the selected provider and is bound to the current Graph, activity, attempt, policy, and budget identities. Missing or unsupported capabilities SHALL fail closed with a typed diagnostic.

#### Scenario: Provider capability is unavailable
- **WHEN** a requested profile requires a provider capability that is unavailable or cannot be physically enforced
- **THEN** the Harness SHALL return `execution_environment_unavailable`, persist the denial identity, and SHALL NOT invoke a host-process fallback

### Requirement: Started attempts have durable terminal evidence
Every started activity or child attempt SHALL persist a terminal receipt containing status, termination confirmation, reason code, exact identity, output references, and capability profile. An unconfirmed termination SHALL be represented as `INDETERMINATE` and SHALL block automatic side-effect retry.

#### Scenario: Cancellation cannot confirm termination
- **WHEN** cancellation reaches the provider grace-period limit without confirmed process-tree termination
- **THEN** the runtime SHALL persist an indeterminate receipt, quarantine late output, and SHALL NOT redispatch the attempt automatically

### Requirement: Child lifecycle is controlled by the Harness supervisor
Child `spawn`, `status`, `wait`, `cancel`, `close`, heartbeat, lease, reclaim, and restart recovery SHALL pass through a Harness-owned supervisor using immutable parent/child Graph identities and idempotent operation identities.

#### Scenario: Parent restarts after a committed child result
- **WHEN** a parent process restarts after a child result and terminal receipt were durably committed
- **THEN** recovery SHALL reuse the committed result without invoking the child again

### Requirement: Shared child admission has durable resource ownership
The shared child capacity resource SHALL retain one stable admission scope across process restarts and runs. Its owner identity, resource-issued generation, and bounded lease SHALL be checked atomically with durable lifecycle mutations through the existing canonical persistence boundary. Task attempts, Graph sequence numbers, and unrelated side-effect leases SHALL NOT substitute for resource ownership. A control-plane takeover SHALL NOT imply that old workers have terminated or release their unconfirmed occupancy.

#### Scenario: Another process owns the admission scope
- **WHEN** a second process requests dynamic child execution while the first process holds a valid resource lease
- **THEN** the second process SHALL reject dynamic admission without invoking a worker, and constructing the default static Research composition SHALL NOT acquire that dynamic lease

#### Scenario: Resource ownership changes after a process stops responding
- **WHEN** a new owner legally takes over an expired resource lease
- **THEN** writes and new admissions from the previous owner SHALL be rejected, and every previously admitted child without confirmed termination SHALL remain accounted for before any new admission

### Requirement: Child recovery validates the complete admission scope
Production child recovery SHALL finish before opening admission and SHALL NOT clear live worker handles or futures. Lifecycle identity SHALL bind each child to a trusted registered run and tenant as well as its parent, task, attempt, and operation. Unparseable admission history, unknown occupancy, identity drift, or unavailable persistence SHALL block admission; missing or corrupt history SHALL NOT be treated as proof of an empty resource.

#### Scenario: A malformed spawn record prevents occupancy reconstruction
- **WHEN** restart recovery cannot determine the identity or occupancy of a persisted spawn
- **THEN** the shared admission scope SHALL fail closed with a diagnostic and SHALL NOT start replacement work

#### Scenario: A run is registered under another tenant
- **WHEN** a caller attempts to register an existing run identity with a different tenant
- **THEN** the runtime SHALL reject the registration before child admission and SHALL preserve the original binding

#### Scenario: A lifecycle commit acknowledgement is lost
- **WHEN** a spawn or terminal mutation may have committed but its acknowledgement is unavailable
- **THEN** the supervisor SHALL reject new admission and further lifecycle mutation until durable recovery reconstructs occupancy and the committed result, and SHALL NOT overwrite a committed terminal result from its stale memory view

#### Scenario: An external container survives the controller process
- **WHEN** a controller process is killed after its admitted Docker child has performed an external side effect and the container is still running
- **THEN** restart recovery SHALL retain the unconfirmed child occupancy, reject replacement execution, and SHALL NOT treat controller death as provider termination or repeat the side effect

### Requirement: Durable lifecycle bounds preserve terminal evidence
Lifecycle storage SHALL enforce bounded records and admission limits while reserving the storage needed to terminate already-admitted children. Exhaustion SHALL prevent new admissions before worker invocation. The runtime SHALL NOT truncate accepted history, change its stable scope, or discard outstanding occupancy to regain capacity. Persistence failures SHALL remain explicit and SHALL NOT be converted into successful termination receipts.

#### Scenario: Lifecycle history reaches its admission bound
- **WHEN** the remaining durable capacity is insufficient for a new child and its required terminal evidence
- **THEN** admission SHALL be rejected and already-admitted children SHALL retain their reserved terminal capacity

### Requirement: Runtime events are canonical and replayable
Turn, tool, approval, context, worker, and child lifecycle facts SHALL be appended to the canonical durable event stream with stable event identity, sequence, redaction, reason code, Graph/activity/attempt references, and bounded payload references. Projection rebuild and offline replay SHALL perform no live tool, worker, or scheduler call.

#### Scenario: Projection is rebuilt from history
- **WHEN** an operator rebuilds the runtime projection from a verified event prefix
- **THEN** the projection SHALL reproduce the same statuses and checksums without invoking any live dependency

### Requirement: Parallel admission uses one immutable group join scope
The coordinator SHALL create one immutable `DispatchGroup` per accepted plan version and SHALL create bounded `DispatchWave` records inside that group for readiness, capacity, retry, and dependency changes. Group membership, join policy, budget envelope, and policy checksum SHALL be durable before physical dispatch.

#### Scenario: Capacity admits a later wave
- **WHEN** only part of the accepted plan fits the current multi-pool capacity
- **THEN** the admitted tasks SHALL form a wave inside the existing group, remaining tasks SHALL stay durably `READY` or `PENDING`, and no second join group SHALL be created

### Requirement: Child results require deterministic verification
The runtime SHALL accept a child result only after verifying group, wave, plan, task, attempt, binding, schema, transcript, output, artifact, tool receipt, memory/budget, and registered gate references. Worker metadata SHALL NOT override a failed gate or grant routing, authorization, or publication authority.

#### Scenario: Result references the wrong attempt
- **WHEN** a child returns a result whose attempt or group identity does not match the admitted record
- **THEN** the verifier SHALL reject the result, persist a typed diagnostic, and SHALL NOT include it in aggregation or parent continuation

### Requirement: Retry and replacement are bounded and auditable
Retry SHALL be limited to policy-allowed reason codes and attempt budgets. Replacement replan SHALL create a new immutable plan version, group identity, and correlation identity; old-group late receipts SHALL be quarantined and SHALL NOT mutate the new projection.

#### Scenario: A retry budget is exhausted
- **WHEN** a task reaches its maximum attempts or receives a non-retryable gate failure
- **THEN** the coordinator SHALL close admission according to the failure policy and SHALL NOT start an unbounded retry loop

### Requirement: Parent continuation consumes only durable gated observations
Parallel submission SHALL return a durable submission identity and bounded `PENDING` receipt when work is not terminal. Parent reasoning SHALL not advance until a deterministic verifier accepts a terminal observation. Redelivery and restart SHALL reuse the same observation version and checksum.

#### Scenario: Continuation is redelivered
- **WHEN** the same terminal group observation is delivered after a timeout or process restart
- **THEN** the parent SHALL receive the previously accepted observation exactly once by identity and SHALL not re-run child work

### Requirement: Dynamic Research preserves existing quality and publication gates
Dynamic Research parallel branches SHALL use isolated branch references and existing evidence, claim verification, quality, reader, card, artifact, and publication gates. A missing role, partial failure, indeterminate child, or unverified artifact SHALL prevent downstream success publication.

#### Scenario: A required Research branch is incomplete
- **WHEN** one required branch is failed, cancelled, indeterminate, or missing its verified output
- **THEN** the group SHALL return a typed incomplete outcome and SHALL not publish a report, reader payload, paper card, or downstream success reference

### Requirement: Rollout states and rollback are explicit
The composition SHALL expose `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`, `DEGRADED_SERIAL`, and `ENABLED_PARALLEL` states. Parallel execution SHALL remain disabled by default; rollback SHALL stop new admissions while preserving pinned active-group policy, receipts, reservations, history, and replayability.

#### Scenario: Required rollout evidence is missing
- **WHEN** a deployment capability, golden-parity result, or rollback receipt required by the release gate is missing
- **THEN** the composition SHALL remain disabled or dependency-unavailable and SHALL not claim production readiness
