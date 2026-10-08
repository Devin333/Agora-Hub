## ADDED Requirements

### Requirement: Opt-in candidate sources produce validated serializable retrieval plans

When the Research retrieval-candidate experiment is explicitly enabled, Paper RAG retrieval SHALL preserve the original request and produce a serializable `RetrievalPlan` for a validated candidate or for the deterministic original query. The candidate plan MUST retain the same plan fields required by the deterministic planner and MUST include original-query lineage, validation decision, and fallback reason when applicable.

#### Scenario: Validated candidate is admitted
- **WHEN** an explicitly enabled candidate satisfies deterministic protected-constraint validation
- **THEN** Paper RAG produces a serializable `RetrievalPlan` for that candidate with original-query lineage and validation evidence
- **AND** downstream recall receives the plan rather than an unstructured worker response

#### Scenario: Candidate is rejected
- **WHEN** candidate validation rejects the worker proposal
- **THEN** Paper RAG produces the serializable plan for the original deterministic query
- **AND** the plan records the candidate rejection and fallback reason
