## ADDED Requirements

### Requirement: Opted-in Research quality paths are application-controlled and deterministically gated

An opted-in Research quality path SHALL be composed through a Research application service and the current Harness Graph boundary. Before report publication, the path MUST validate the Research input profile and resolve configured high-value claim eligibility through the exact registered Research deterministic gate version. A worker, parser, or reviewer result MUST remain candidate data and MUST NOT decide routing, memory writes, tool authorization, publication, or skill promotion.

#### Scenario: Quality profile and gate are complete
- **WHEN** an explicitly enabled single-paper request supplies a valid Research input profile and the configured eligibility gate is registered
- **THEN** the Harness executes the existing bounded Research Graph path and records the profile and gate identities in durable run evidence
- **AND** only the deterministic gate outcome can make a verified-report candidate eligible for terminal publication handling

#### Scenario: Quality profile or gate is incomplete
- **WHEN** an opted-in request has an invalid input profile or an unavailable eligibility gate version
- **THEN** the Research application service or Harness rejects the run before worker or external source invocation
- **AND** no report, memory write, tool side effect, or artifact publication is accepted
