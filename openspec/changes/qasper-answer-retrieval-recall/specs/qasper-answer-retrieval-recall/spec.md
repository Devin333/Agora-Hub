## ADDED Requirements

### Requirement: Explicit answer-evaluation retrieval profile

The QASPER answer evaluator MUST accept an explicit retrieval profile containing the `RetrievalPolicy` and optional field index, base reranker, and field reranker ports. The report identity MUST record the policy name, overfetch multiplier, enabled rerank intent allowlists, component names, and fixture execution profile.

#### Scenario: Baseline profile remains compatible
- **WHEN** the evaluator is constructed without optional retrieval components and with the existing default policy
- **THEN** it MUST preserve the chunk-only retrieval behavior and MUST report the components as unavailable or disabled

#### Scenario: Enabled components are observable
- **WHEN** a profile injects a field index or reranker
- **THEN** the report identity MUST identify each injected component and the policy settings that can activate it

### Requirement: Field cache preflight is fail-fast

When a profile selects field retrieval, the evaluator MUST validate that every non-empty indexed field has a finite vector with the base cache identity and dimension before processing a question. It MUST raise a deterministic offline cache error and publish no partial answer report when validation fails.

#### Scenario: Missing field vector
- **WHEN** a selected field profile references a field text absent from the base cache
- **THEN** the run MUST fail before the first answer request and MUST NOT silently fall back to chunk-only retrieval

#### Scenario: Complete field cache
- **WHEN** all indexed field texts have valid vectors
- **THEN** the evaluator MUST construct the field index with the same query vector resolver used by dense retrieval

### Requirement: Reranker activation follows policy gates

The evaluator MUST inject reranker ports only through composition, and the retrieval pipeline MUST call them only when the selected policy allows the current query intent. An empty intent allowlist MUST allow all intents, preserving the existing policy contract. Component availability and actual invocation MUST be represented separately in retrieval metadata.

#### Scenario: Policy enables reranking
- **WHEN** a query intent is included in the selected policy's rerank allowlist and a reranker is injected
- **THEN** the pipeline MUST score the candidate passages with that reranker and preserve the candidate provenance

#### Scenario: Policy disables reranking
- **WHEN** a reranker is injected but the query intent is not allowed by policy
- **THEN** the pipeline MUST NOT call the reranker and MUST retain the deterministic base ranking

#### Scenario: Empty allowlist permits all intents
- **WHEN** a reranker is injected and its policy intent allowlist is empty
- **THEN** the pipeline MUST allow reranking for every query intent and MUST NOT report the component as disabled merely because the allowlist is empty

### Requirement: Candidate expansion preserves final context budget

Increasing retrieval overfetch or candidate limits MUST affect only the candidate pool and retrieval diagnostics. The final accepted context MUST remain bounded by the configured Harness budget and answer generator passage limit, and candidate provenance MUST retain source and rank information after deduplication.

#### Scenario: Expanded candidates do not expand answer context
- **WHEN** an expanded profile retrieves more candidates than the baseline profile
- **THEN** the answer session MUST still accept no more than the configured final context item budget

### Requirement: Deterministic coverage replay

The evaluator MUST support a retrieval-only replay that uses the same sample, frozen mapping, dataset/corpus/cache identities, and fixed denominators for every profile. The replay MUST report strict and equivalent evidence/context coverage, candidate recall/provenance, unmappable cases, and system failures without calling the answer model or judge or changing gold evidence.

#### Scenario: Replay compares profiles on fixed cases
- **WHEN** baseline and expanded profiles are replayed against the same frozen sample and mapping
- **THEN** each profile MUST produce per-case results with matching case identities and denominators

#### Scenario: Missing identity or query vector
- **WHEN** dataset, mapping, corpus, cache, or required query-vector identity does not match the replay manifest
- **THEN** the replay MUST stop with a blocked preflight status and MUST NOT report a quality improvement

#### Scenario: A runtime query is absent from the offline cache
- **WHEN** the real retrieval path requests a query vector that the selected cache cannot resolve
- **THEN** the replay MUST publish a blocked diagnostic identifying the missing query text, digest, and affected profile/case when available, MUST NOT call an embedding provider, and MUST NOT publish coverage scores from the incomplete replay

### Requirement: Fixture boundary is explicit

Every answer-evaluation retrieval report MUST mark its execution profile as `fixture` and MUST state that in-memory cached-vector retrieval is not a production Qdrant quality or latency measurement. The evaluator MUST NOT write production indexes, business memory, active skills, or publication state.

#### Scenario: Fixture report
- **WHEN** a QASPER answer retrieval run completes
- **THEN** its identity MUST include the fixture boundary statement and the selected component/profile fingerprints
