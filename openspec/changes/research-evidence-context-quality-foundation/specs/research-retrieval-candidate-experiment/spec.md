## ADDED Requirements

### Requirement: Candidate retrieval experimentation is explicit and default-off

Research SHALL expose retrieval-candidate generation only through an explicitly enabled experiment that is disabled by default. The original user query and deterministic retrieval plan MUST remain executable for every experiment run. A worker MAY propose only a bounded set of query candidates and MUST NOT decide route selection, quality pass/fail, memory writes, tool authorization, skill promotion, or publication.

#### Scenario: Candidate experiment is not enabled
- **WHEN** a Paper RAG request does not explicitly enable the candidate experiment
- **THEN** retrieval uses the existing deterministic planning path
- **AND** no worker candidate is requested or treated as a planner input

#### Scenario: Worker proposes bounded candidate queries
- **WHEN** an enabled experiment asks a worker for retrieval candidates
- **THEN** the worker output contains no more candidates than the configured experiment limit
- **AND** each candidate is retained as candidate data with its original-query lineage

### Requirement: Candidate admission preserves hard constraints and deterministic fallback

Before a candidate retrieval plan is used, a deterministic validator SHALL compare it with the original request's protected entity, time, source, language, version, metric, negation, and other configured hard constraints. The system MUST record accept/reject reason and fallback reason. Invalid candidates, worker failures, timeout, budget exhaustion, or validator errors MUST execute the original deterministic plan.

#### Scenario: Candidate drops a protected negation or source constraint
- **WHEN** a candidate changes or omits a protected negation, entity, time, source, language, version, or metric constraint
- **THEN** the validator rejects the candidate with a stable reason
- **AND** the original deterministic retrieval plan executes

#### Scenario: Candidate worker fails
- **WHEN** the candidate worker returns an invalid payload, times out, or exhausts its configured budget
- **THEN** retrieval completes through the original deterministic plan when that plan is available
- **AND** the transcript records that fallback without treating the failed candidate as an authoritative decision

### Requirement: Candidate rollout requires paired baseline evidence

The candidate experiment SHALL remain disabled for broader rollout until fixed-case paired evaluation shows a configured improvement and no regression in input integrity, protected constraint preservation, evidence/citation quality, or deterministic fallback behavior. The evaluation evidence MUST identify the baseline, candidate configuration, case set, and decision owner.

#### Scenario: Candidate metrics improve recall but regress citations
- **WHEN** paired evaluation shows an improvement in recall or task completion but a regression in citation, evidence, constraint, or fallback metrics
- **THEN** the rollout gate rejects broader enablement
- **AND** the experiment remains default-off
