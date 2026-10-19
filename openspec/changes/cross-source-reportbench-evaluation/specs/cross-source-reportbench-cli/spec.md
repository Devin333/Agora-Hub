## ADDED Requirements

### Requirement: Offline validation command
The CLI SHALL provide a ReportBench validation command that reads local task/output/manifest files, performs deterministic contract checks, and performs zero network or paid calls.

#### Scenario: Valid fixture validation
- **WHEN** the CLI receives a valid local fixture and matching task/output records
- **THEN** it exits successfully and prints machine-readable validation details including case count, schema version, `network_calls=0`, and `paid_calls=0`

#### Scenario: Invalid fixture validation
- **WHEN** a task is missing, duplicated, malformed, or has an invalid output contract
- **THEN** the CLI exits nonzero, lists stable issue codes and locations, and does not write an export file

### Requirement: Benchmark-compatible export
The CLI SHALL export validated project results as JSONL with one deterministic record per benchmark case and SHALL refuse to overwrite an existing file with different content.

#### Scenario: Export records
- **WHEN** validation passes and the destination is absent or byte-identical
- **THEN** the CLI writes one record per case containing the benchmark task ID, question, report body, citations, and execution metadata

#### Scenario: Conflicting destination
- **WHEN** the destination already contains different bytes
- **THEN** the CLI exits nonzero and preserves the existing file

### Requirement: Explicit run plan boundary
The CLI SHALL support generating a bounded run manifest for a selected subset without invoking a model, web scraper, judge, or production publication path.

#### Scenario: Generate three-case canary plan
- **WHEN** the operator selects three task IDs and a diagnostic output directory
- **THEN** the CLI writes a plan containing exactly those cases, a bounded budget, `qualification_status=not_qualified`, and an explicit `requires_approval` marker

#### Scenario: No implicit paid execution
- **WHEN** the operator invokes validation, export, or run-plan without an execution approval and runner
- **THEN** the CLI performs no external call and reports that execution was not started
