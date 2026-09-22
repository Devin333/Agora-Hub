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
