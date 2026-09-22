## ADDED Requirements

### Requirement: Evaluation cases are versioned and schema validated

The evaluation harness SHALL load a versioned case manifest in which every case has a stable `id`, `query`, `as_of_date`, `expected_requirements`, `required_source_types`, `forbidden_claims`, `minimum_sources`, and `risk_level`.

#### Scenario: Valid case manifest loads

- **WHEN** the runner loads a manifest with all required fields and a supported schema version
- **THEN** it SHALL return typed evaluation cases with the manifest version preserved
- **AND** it SHALL reject duplicate case IDs

#### Scenario: Invalid case manifest is rejected

- **WHEN** a manifest omits a required field or uses an unsupported schema version
- **THEN** the runner SHALL fail before executing a Research case
- **AND** the failure SHALL identify the case and validation reason

### Requirement: The first Golden Set covers product and workflow risks

The repository SHALL provide an initial versioned Golden Set containing 50 cases: 20 normal Research cases, 10 evidence and citation cases, 10 Harness failure cases, and 10 boundary or security cases.

#### Scenario: Golden Set composition is checked

- **WHEN** the dataset evaluator loads the initial Golden Set
- **THEN** it SHALL report counts for each required category
- **AND** it SHALL fail the dataset gate if any category is below its required count

### Requirement: Deterministic evaluation checks the complete Research chain

The evaluator SHALL check output schema validity, expected requirement coverage, factual support markers, citation validity, citation-to-evidence traceability, source constraints, artifact references, and critical failure rules for each case.

#### Scenario: Report is fully traceable

- **WHEN** a completed run contains source, evidence, report, and artifact records with valid references
- **THEN** the evaluator SHALL mark traceability as passing
- **AND** it SHALL emit coverage and citation metrics for the case

#### Scenario: Unsupported critical claim is present

- **WHEN** a report contains a critical claim without supporting evidence or a valid citation
- **THEN** the evaluator SHALL fail the case regardless of its aggregate score
- **AND** the failure SHALL identify the claim and missing support

### Requirement: Deterministic checks are separated from semantic review

The harness SHALL persist deterministic evaluator results and semantic review results as separate result types. An optional human or LLM judge MAY provide relevance, completeness, source quality, and clarity scores, but it MUST NOT override deterministic failures.

#### Scenario: Semantic review accompanies a passing deterministic result

- **WHEN** a case passes deterministic checks and a semantic reviewer returns a rubric-versioned score
- **THEN** the run SHALL persist both result sets with their evaluator identities
- **AND** the summary SHALL show them separately

#### Scenario: Judge attempts to override a deterministic failure

- **WHEN** semantic review scores a case as acceptable while deterministic evaluation reports a critical failure
- **THEN** the final case status SHALL remain failed
- **AND** the report SHALL retain both the judge result and the deterministic failure

### Requirement: Harness evaluation enforces bounded PLAN, EXECUTE, VERIFY behavior

The evaluator SHALL verify that Harness runs use legal phase transitions, deterministic verification, durable transition records, and enforced `max_replans`, `max_turns`, and retry budgets.

#### Scenario: Verification fails and retry budget remains

- **WHEN** VERIFY fails and the configured retry budget is positive
- **THEN** the Harness SHALL record the failed transition and perform only an allowed retry or replan
- **AND** the evaluator SHALL pass the scenario when the next bounded attempt succeeds

#### Scenario: Verification budget is exhausted

- **WHEN** VERIFY continues to fail after the retry, turn, or replan budget is exhausted
- **THEN** the Harness SHALL halt with a terminal failure status
- **AND** the evaluator SHALL fail any run that continues execution or publishes output after the halt

### Requirement: Failure scenarios are reproducible without external services

The evaluation suite SHALL provide explicit fake adapters for source collection, worker output, tools, verification, and artifact storage so Harness failure scenarios can run deterministically offline.

#### Scenario: Invalid worker output is injected

- **WHEN** the fake worker returns output that violates the application schema
- **THEN** the Harness SHALL route the case through its controlled failure path
- **AND** the evaluator SHALL assert the expected terminal status and transcript entries

#### Scenario: External source failure is injected

- **WHEN** the fake source adapter returns a timeout or partial collection failure
- **THEN** the run SHALL record the source failure taxonomy
- **AND** it SHALL not claim successful evidence collection for the missing source

### Requirement: Every evaluation run produces replayable artifacts

The runner SHALL write a unique run directory containing `manifest.json`, `cases.jsonl`, `outputs.jsonl`, `metrics.json`, `failures.jsonl`, and `summary.md`. The records SHALL include source, evidence, plan, phase transitions, worker candidate, verification result, final report, timing, call counts, and cost signals when available.

#### Scenario: Successful run writes the complete artifact set

- **WHEN** an evaluation run completes
- **THEN** all required files SHALL exist under `evaluations/runs/<run_id>/`
- **AND** the manifest SHALL record dataset version, evaluator version, repository revision, and run configuration

#### Scenario: Case failure is persisted for diagnosis

- **WHEN** an individual case fails
- **THEN** `failures.jsonl` SHALL contain the case ID, phase, failure category, evidence references, and deterministic assertion details
- **AND** the Markdown summary SHALL link the failure to the run artifacts

### Requirement: Metrics and thresholds are explicit and comparable

The evaluator SHALL calculate requirement coverage, factual precision, unsupported claim rate, citation validity, citation correctness, citation coverage, source quality, traceability, invalid transition rate, budget violation rate, transcript completeness, latency percentiles, call counts, and estimated cost when available.

#### Scenario: Baseline report is generated

- **WHEN** the runner processes one or more completed cases
- **THEN** `metrics.json` SHALL contain per-case and aggregate values for all applicable metrics
- **AND** `summary.md` SHALL show configured thresholds and pass/fail status

#### Scenario: Critical threshold is violated

- **WHEN** an aggregate or critical-case threshold is below the configured target
- **THEN** the runner SHALL return a non-zero CI exit code
- **AND** the report SHALL identify the violated metric, observed value, target, and affected cases

### Requirement: The runner supports local, CI, and baseline workflows

The evaluation entry point SHALL support dataset selection, case selection, explicit run IDs, JSON/Markdown output, baseline comparison, and CI-compatible exit codes. The fast tier SHALL be runnable without external services.

#### Scenario: Selected cases run locally

- **WHEN** an operator provides a dataset version and a subset of case IDs
- **THEN** the runner SHALL execute only the selected cases
- **AND** it SHALL preserve the selection in the run manifest

#### Scenario: Regression is compared with a baseline

- **WHEN** an operator provides a prior baseline run
- **THEN** the runner SHALL report metric deltas and newly failing cases
- **AND** it SHALL fail the comparison when a configured regression threshold is exceeded

### Requirement: Evaluation code respects architecture and authority boundaries

The evaluation implementation SHALL use application services and structured APIs, SHALL NOT make LLM output responsible for workflow routing, quality pass/fail, memory writes, tool authorization, publication, or skill promotion, and SHALL NOT add a forbidden dependency from `backend/research` to legacy `backend/boards/paper_radar`, `interfaces`, or `infrastructure` during the Research rebuild.

#### Scenario: Architecture checks inspect the evaluation path

- **WHEN** the architecture evaluator scans the new evaluation modules and their imports
- **THEN** it SHALL pass only when the allowed ownership boundaries are respected
- **AND** it SHALL identify the importing file and forbidden target for each violation

#### Scenario: Worker proposes a side effect

- **WHEN** a fake or real worker candidate requests publication, memory write, skill promotion, or gate bypass
- **THEN** the Harness SHALL treat it as candidate data
- **AND** deterministic runtime authority SHALL reject or route the request without granting the side effect
