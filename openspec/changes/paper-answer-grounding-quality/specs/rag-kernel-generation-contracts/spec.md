## ADDED Requirements

### Requirement: Generation records the final model-visible context snapshot

RAG generation SHALL bind each answer attempt to the exact context passages serialized into the final provider request. The immutable snapshot MUST include passage identifiers, rendered text, source content identity, source offsets, prompt/render version, and the applied budget and truncation decision. Citation validation and actual-visible faithfulness evaluation MUST use that same snapshot; unselected or post-context source text MUST NOT be treated as model-visible evidence.

#### Scenario: Evidence exceeds the actual request budget
- **WHEN** fully serialized messages exceed the configured request budget
- **THEN** the context builder SHALL deterministically trim passages or reject the request before dispatch
- **AND** the persisted snapshot SHALL record the resulting exact passage text, offsets, and trim reasons

#### Scenario: Evidence is visible only in the full source
- **WHEN** a supporting statement exists in the source body but falls outside the final serialized passage view
- **THEN** citation support for that generation attempt MUST NOT count that statement as visible evidence
- **AND** a full-source diagnostic MAY report it separately
