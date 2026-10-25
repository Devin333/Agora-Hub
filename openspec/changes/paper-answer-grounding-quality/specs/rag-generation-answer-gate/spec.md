## ADDED Requirements

### Requirement: Strict Paper answer policy gates an atomic candidate before rendering

Research Paper answer generation using strict grounding policy SHALL submit a versioned claim candidate to Harness verification before exposing answer text. Each factual claim MUST retain its required qualifiers, polarity, and numeric roles when present; final public answer text and citation markers SHALL be rendered deterministically from claims whose structural requirements pass. The answer worker MUST NOT replace missing claim structure with a single whole-answer claim bound to every retrieved evidence item. Semantic verifier output is an observation, not a pass decision; routing and final outcome remain owned by deterministic Harness policy.

#### Scenario: Candidate contains separately supported facts
- **WHEN** a candidate has distinct facts supported by different visible passages
- **THEN** the renderer SHALL preserve each fact's validated citation binding adjacent to its public answer text
- **AND** no citation SHALL be attached to a fact solely because it appeared elsewhere in the retrieved context

#### Scenario: Candidate omits a required qualifier or claim structure
- **WHEN** strict grounding policy is active and a factual answer is missing required claim bindings or a material qualifier recorded for that claim
- **THEN** the deterministic gate MUST reject it and record the reason before public rendering

#### Scenario: Verifier reports support but a deterministic policy check fails
- **WHEN** a semantic verifier reports a claim as supported while a required structural, typed-value, or snapshot-integrity check fails
- **THEN** Harness policy MUST reject or route the candidate through a bounded repair action
- **AND** the verifier observation MUST NOT override the failed check
