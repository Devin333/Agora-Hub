## ADDED Requirements

### Requirement: Answer claims bind to exact visible evidence spans

Paper answer candidates using strict grounding policy SHALL represent every independently checkable factual claim with a stable claim identity and a binding to the exact quote and source offsets in the generation attempt's verified visible-context snapshot. The binding MUST identify the corresponding evidence passage and answer span; offsets MUST resolve against the snapshotted text and source version. Structural span validity establishes provenance only and MUST NOT be reported as proof that the evidence semantically entails the claim.

#### Scenario: Claim quote matches its visible source span
- **WHEN** a claim binding identifies a quote and offsets that resolve exactly within the cited visible passage
- **THEN** the deterministic gate SHALL pass span provenance for that binding
- **AND** semantic support SHALL remain a separate observation

#### Scenario: Claim cites evidence outside its visible snapshot
- **WHEN** a claim binding resolves only against full source text that was not sent in the same generation attempt
- **THEN** the deterministic gate MUST reject the binding as not visible to the generator

#### Scenario: Final text contains an unbound factual statement
- **WHEN** the rendered answer contains factual text that is not covered by a validated claim span
- **THEN** the deterministic gate MUST reject the answer candidate
