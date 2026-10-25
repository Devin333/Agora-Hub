## ADDED Requirements

### Requirement: Semantic claim failures use the existing bounded repair budget

Harness RAG SHALL route validated semantic failure observations through the existing bounded answer attempt and supplemental retrieval workflow. Repair action selection, evidence retrieval, retry count, final abstention, and halt behavior MUST be determined by Harness policy and share the configured generation, replan, and cost budgets. A repair query MUST NOT include gold answers or gold evidence mappings, and every phase transition and attempt SHALL be recorded in the durable transcript.

#### Scenario: Visible evidence exists but the answer claim is unsupported
- **WHEN** a calibrated verifier produces an eligible unsupported, contradicted, or insufficient observation for a structurally valid claim and repair budget remains
- **THEN** Harness policy SHALL select a bounded claim repair or supplemental retrieval action according to the frozen rule set
- **AND** the next candidate MUST undergo the same deterministic gates

#### Scenario: No repair budget remains or observation is unknown
- **WHEN** generation/replan/cost budget is exhausted or the observation is unknown, invalid, or ineligible under the frozen policy
- **THEN** Harness SHALL stop or abstain according to policy, preserve the reason, and MUST NOT launch an unbounded retry
