## ADDED Requirements

### Requirement: Evidence body remains lossless and distinct from its summary

Harness RAG evidence candidates SHALL preserve the original evidence body separately from its display/selection summary. Conversion from kernel evidence MUST NOT replace, prefix-truncate, or reconstruct the body from `summary`; source version, content digest, and locator MUST remain associated with the body. Implementations MAY reference an immutable body artifact when inline storage is unsuitable, provided the generation boundary can resolve and verify it before prompt construction.

#### Scenario: Long evidence crosses the adapter boundary
- **WHEN** kernel evidence contains a body longer than the adapter's summary limit
- **THEN** the Harness candidate SHALL preserve a distinct summary and a verifiable reference to the complete body
- **AND** the candidate MUST preserve the evidence identity and source locator

#### Scenario: Evidence body cannot be resolved
- **WHEN** a candidate references an immutable body artifact that is missing or fails its content digest check
- **THEN** answer generation MUST fail closed for that evidence and MUST NOT silently use the summary as the body
