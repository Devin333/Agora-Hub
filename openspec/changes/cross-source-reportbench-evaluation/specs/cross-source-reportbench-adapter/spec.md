## ADDED Requirements

### Requirement: Cross-source report result contract
The adapter SHALL represent each benchmark case with a stable case identifier, the original research question, a report body, zero or more structured sources, and explicit execution metadata.

#### Scenario: Valid multi-source result
- **WHEN** a runner returns one non-empty report and two distinct HTTPS sources for a case
- **THEN** the adapter accepts the result and preserves the report text, source metadata, run identity, and execution profile without contacting an external service

#### Scenario: Missing structured sources
- **WHEN** a runner returns a non-empty report without structured sources
- **THEN** validation rejects the result with a deterministic `sources.required` issue and does not infer sources from report text

### Requirement: Source and citation provenance
Each source record SHALL contain a stable source identifier, an HTTPS URL, a non-empty title, and an allowed source kind; optional citation locators SHALL remain attached to that source record.

#### Scenario: Duplicate source identity
- **WHEN** two source records in one case use the same source identifier or normalized URL
- **THEN** validation rejects the case with a deterministic duplicate-source issue

#### Scenario: Unsafe source URL
- **WHEN** a source uses a non-HTTP(S) URL, credentials, or control characters
- **THEN** validation rejects the case and does not export it

### Requirement: Execution provenance and qualification state
The adapter SHALL preserve execution profile, code revision, model identity, timing, cost fields, and qualification state, and SHALL default exported records to diagnostic/non-qualified status.

#### Scenario: Diagnostic export
- **WHEN** a valid result is exported without an approved qualification manifest
- **THEN** the output includes `execution_profile`, `qualification_status=not_qualified`, and explicit network/paid-call counters

#### Scenario: Failed run
- **WHEN** a runner fails before producing a report
- **THEN** the adapter emits a failed case record with a sanitized error code and no fabricated report or source
