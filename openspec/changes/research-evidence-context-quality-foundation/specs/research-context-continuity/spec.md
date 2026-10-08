## ADDED Requirements

### Requirement: Context continuity requires an audited trigger

Research SHALL NOT enable context continuity merely because a long-running task exists. Before implementation or rollout, the owner MUST record an auditable trigger showing actual context-boundary loss of accepted evidence or conditions, condition drift, or repeated-read cost. The trigger record MUST identify the affected run or representative fixed cases and the measurement method.

#### Scenario: No continuity trigger evidence exists
- **WHEN** no recorded trigger demonstrates context loss, condition drift, or repeated-read cost
- **THEN** the continuity feature remains disabled
- **AND** the system does not create a new context state store or automatic rollover path

### Requirement: Continuity reassembles one run's accepted Research state

When the continuity feature is enabled after trigger approval, it SHALL maintain only one run's accepted evidence references, applicable conditions, and source revisions. At an actual model-context boundary, it MUST deterministically reassemble that projection using existing Research context assembly and existing Harness transcript, artifact, or event evidence. It MUST NOT create cross-run memory, a parallel archive, CAS, claim-state service, or global cache-invalidation system.

#### Scenario: Accepted evidence is reassembled at a real window boundary
- **WHEN** an enabled analysis reaches a configured model-context boundary
- **THEN** the next context projection contains the accepted evidence references, conditions, and source revisions required by the current task
- **AND** the projection is traceable to existing run evidence

#### Scenario: Source revision changes before reassembly
- **WHEN** an accepted evidence reference has a different source revision at reassembly time
- **THEN** the continuity path records the revision mismatch and follows the configured deterministic verification or re-read policy
- **AND** it does not silently reuse the stale evidence as current

### Requirement: Continuity failure preserves bounded Harness control

Context reassembly failure SHALL be represented as deterministic Research evidence for the current Harness run. The failure MUST NOT authorize a worker to rewrite conditions, promote memory, bypass verification, or publish an incomplete report.

#### Scenario: Reassembly cannot reconstruct a protected condition
- **WHEN** the continuity path cannot reconstruct a required accepted condition
- **THEN** the Harness applies its existing bounded retry, replan, or halt policy
- **AND** no verified report artifact is published from the incomplete projection
