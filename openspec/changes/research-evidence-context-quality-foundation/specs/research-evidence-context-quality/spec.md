## ADDED Requirements

### Requirement: Research quality evaluation extends the existing evaluator

The Research quality path SHALL extend the existing Research/RAG evaluation entry point with versioned fixed cases and comparable reports. The evaluator MUST record input-contract integrity, hard-constraint preservation, source and citation traceability, claim-eligibility outcome, deterministic fallback outcome, and worker-control-field rejection. It MUST NOT introduce a parallel evaluator, run store, event store, or promotion system.

#### Scenario: Fixed offline quality case runs
- **WHEN** a versioned fixed quality case is selected in the existing evaluator
- **THEN** the evaluator runs it without an external source or model service
- **AND** it records the case version, repository revision, configuration, metrics, and deterministic failure diagnostics

#### Scenario: Quality regression exceeds a configured threshold
- **WHEN** a fixed case or aggregate comparison violates its configured constraint, evidence, citation, or fallback threshold
- **THEN** the evaluation gate fails with the affected case and metric
- **AND** it does not approve a rollout based on text quality alone

### Requirement: Opted-in Research input profiles preserve protected task semantics

An explicitly enabled dynamic single-paper analysis path SHALL accept one validated `ResearchInputProfile` before worker invocation. The profile MUST contain exactly one `CURRENT_TASK`, preserve input category, source identity, source revision, status, and applicable conditions, and reject an invalid status transition. A `VERIFIED_CONCLUSION` MUST carry a valid qualification reference and a model candidate MUST NOT be represented as verified.

#### Scenario: Input profile contains more than one current task
- **WHEN** an opted-in analysis profile contains zero or multiple `CURRENT_TASK` items
- **THEN** the Research application service rejects the profile before worker invocation
- **AND** no prompt, tool invocation, memory write, or artifact publication is created from that profile

#### Scenario: Final logical request loses a protected constraint
- **WHEN** the prepared profile is projected for the dynamic AgentLoop and the final logical request omits a protected task constraint or required source association
- **THEN** the application boundary rejects the dispatch before the worker is called
- **AND** the failure identifies the missing protected input

### Requirement: High-value claim eligibility is deterministic and source-bound

For the configured V1 high-value claim type, Research SHALL bind a claim revision to exact source locators, applicable conditions, typed observations, and a deterministic `ClaimReviewResolver` result. Worker, parser, and reviewer outputs SHALL be candidate observations only. Only a registered Research deterministic gate using the resolver result MAY mark an output eligible for verified report publication.

#### Scenario: Supported observation satisfies the configured claim contract
- **WHEN** the claim revision has the required source locator, conditions, and typed observations and the resolver returns `supported`
- **THEN** the deterministic gate may create an eligible verified-report candidate
- **AND** the candidate records the resolver version and evidence bindings

#### Scenario: Claim is contradicted, insufficient, or unavailable
- **WHEN** the resolver returns `contradicted`, `insufficient`, or `unavailable`
- **THEN** the report path does not render the claim as verified
- **AND** the Harness follows its bounded deterministic failure, replan, or halt policy without granting worker publication authority

### Requirement: Research progress governance is observation-only

The Research quality path SHALL emit stable progress reason codes and metrics, including `insufficient_verified_progress`, for diagnostics and evaluation. These signals MUST NOT independently stop a run, change routing, authorize publication, or grant a worker authority.

#### Scenario: Progress signal reports insufficient verified progress
- **WHEN** a Research step records `insufficient_verified_progress`
- **THEN** the signal is retained with the current run and evidence context for diagnosis
- **AND** any retry, replan, halt, or publication decision remains owned by existing Harness deterministic policy
